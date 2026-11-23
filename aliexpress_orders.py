#!/usr/bin/env python3
"""
AliExpress order overview: fetches your orders, takes a screenshot of each
product page, downloads the product images and builds overview/index.html.

Usage:
  python aliexpress_orders.py login       # open the browser to log in (again)
  python aliexpress_orders.py update      # new orders + missing products + details + html
  python aliexpress_orders.py orders [--full]
  python aliexpress_orders.py products [--force] [--only ID ...] [--limit N]
  python aliexpress_orders.py details [--force] [--all] [--only ID ...] [--limit N]
  python aliexpress_orders.py html

Option for all commands that use the browser:
  --cdp http://127.0.0.1:9333   attach to an already running Chromium (started with
                                --remote-debugging-port) instead of launching one.

The browser runs visibly with its own profile in ./.browser-profile, so you stay
logged in between runs. On a captcha the script pauses until you solve it.
"""
import argparse, asyncio, html, io, json, os, random, re, sys, time
from datetime import datetime

from playwright.async_api import async_playwright

# Working dir for overview/ and .browser-profile/; with `nix run` this is the current dir.
ROOT = os.environ.get("ALIEXPRESS_ORDERS_DIR") or os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "overview")
PROD = os.path.join(OUT, "products")
ORDERS_JSON = os.path.join(OUT, "orders.json")
PROFILE = os.path.join(ROOT, ".browser-profile")
ORDER_URL = "https://www.aliexpress.com/p/order/index.html"
VIEWPORT = {"width": 1400, "height": 1000}
META_VERSION = 2

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

# ---------------------------------------------------------------- browser

class Browser:
    """Persistent profile (default) or attach to an existing browser via CDP."""

    def __init__(self, cdp=None):
        self.cdp = cdp

    async def __aenter__(self):
        self.pw = await async_playwright().start()
        if self.cdp:
            self.browser = await self.pw.chromium.connect_over_cdp(self.cdp)
            self.ctx = self.browser.contexts[0]
        else:
            self.browser = None
            self.ctx = await self.pw.chromium.launch_persistent_context(
                PROFILE, headless=False, viewport=VIEWPORT,
                args=["--no-first-run", "--no-default-browser-check"])
        return self

    async def new_page(self):
        pg = await self.ctx.new_page()
        await pg.set_viewport_size(VIEWPORT)
        return pg

    async def __aexit__(self, *exc):
        try:
            if self.browser is None:
                await self.ctx.close()
        finally:
            await self.pw.stop()

def is_login(url):
    return "login" in url or "passport" in url

async def wait_for_login(pg):
    if not is_login(pg.url):
        return
    log("Not logged in: log in in the browser window (waiting up to 10 minutes)...")
    for _ in range(600):
        await pg.wait_for_timeout(1000)
        if not is_login(pg.url):
            log("Logged in.")
            await pg.wait_for_timeout(3000)
            return
    sys.exit("No login within 10 minutes; stopped.")

CAPTCHA_SEL = "#nc_1_n1z, .baxia-dialog, iframe[src*='punish']"

async def has_captcha(pg):
    if re.search(r"punish|captcha|_____tmd_____", pg.url):
        return True
    try:
        return await pg.locator(CAPTCHA_SEL).count() > 0
    except Exception:
        return False

async def wait_for_captcha(pg):
    if not await has_captcha(pg):
        return
    await pg.bring_to_front()
    log("Captcha! Solve it in the browser window (waiting up to 15 minutes)...")
    for _ in range(900):
        await pg.wait_for_timeout(1000)
        if not await has_captcha(pg):
            log("Captcha solved, continuing.")
            await pg.wait_for_timeout(3000)
            return
    sys.exit("Captcha not solved within 15 minutes; stopped. Run again to resume.")

# ---------------------------------------------------------------- orders

def parse_order_response(body):
    j = json.loads(body[body.find("{"):body.rfind("}") + 1])
    out = []
    for k, v in (j.get("data", {}).get("data") or {}).items():
        if not k.startswith("pc_om_list_order_"):
            continue
        f = v["fields"]
        absu = lambda u: ("https:" + u) if u and u.startswith("//") else u
        out.append({
            "orderId": f["orderId"], "date": f.get("orderDateText"),
            "status": (f.get("statusText") or "").strip(), "store": f.get("storeName"),
            "storeUrl": absu(f.get("storePageUrl")), "total": f.get("totalPriceText"),
            "detailUrl": f.get("orderDetailUrl"),
            "lines": [{
                "productId": l.get("productId"), "title": l.get("itemTitle"),
                "price": l.get("itemPriceText"), "qty": l.get("quantity"),
                "sku": ", ".join(a["text"] for a in l.get("skuAttrs", [])),
                "img": l.get("itemImgUrl"), "url": absu(l.get("itemDetailUrl")),
            } for l in f.get("orderLines", [])],
        })
    return out

def load_orders():
    return json.load(open(ORDERS_JSON, encoding="utf-8")) if os.path.exists(ORDERS_JSON) else []

def save_orders(orders):
    os.makedirs(OUT, exist_ok=True)
    orders.sort(key=lambda o: pdate(o["date"]), reverse=True)
    json.dump(orders, open(ORDERS_JSON, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

async def cmd_orders(b, full=False):
    known = {o["orderId"]: o for o in load_orders()}
    pages = []  # list of order lists, one per API response
    pending = set()

    pg = await b.new_page()

    async def on_resp(r):
        if "mtop.aliexpress.trade.buyer.order.list" in r.url:
            try:
                pages.append(parse_order_response(await r.text()))
            except Exception as e:
                log("could not parse order response:", e)
    pg.on("response", lambda r: pending.add(asyncio.ensure_future(on_resp(r))))

    await pg.goto(ORDER_URL, wait_until="domcontentloaded")
    await pg.wait_for_timeout(4000)
    await wait_for_login(pg)
    if "order/index" not in pg.url:
        await pg.goto(ORDER_URL, wait_until="domcontentloaded")
    await wait_for_captcha(pg)
    await pg.wait_for_timeout(5000)

    for i in range(500):
        await asyncio.gather(*pending)
        if not full and pages and known and all(o["orderId"] in known for o in pages[-1]):
            log("Only known orders left; stopping (use --full to load everything).")
            break
        btn = pg.locator("button:has-text('View orders'), .order-more button")
        if await btn.count() == 0 or not await btn.first.is_visible():
            break
        await btn.first.scroll_into_view_if_needed()
        await btn.first.click()
        await pg.wait_for_timeout(3000)
        await wait_for_captcha(pg)
        log(f"loaded more ({sum(len(p) for p in pages)} orders fetched)")
    await asyncio.gather(*pending)
    await pg.close()

    new = 0
    for p in pages:
        for o in p:
            new += o["orderId"] not in known
            known[o["orderId"]] = o
    save_orders(list(known.values()))
    log(f"{len(known)} orders in orders.json ({new} new).")

# ---------------------------------------------------------------- products

PRODUCT_JS = """()=>{
 const o={};
 try{o.gallery=window._d_c_.DCData.imagePathList}catch(e){}
 if(!o.gallery){try{o.gallery=window.runParams.data.imageModule.imagePathList}catch(e){}}
 const h=document.querySelector('[data-pl="product-title"]')||document.querySelector('h1[class*=title]');
 o.title=h?h.innerText:null; o.doctitle=document.title;
 return o}"""

def products_from_orders(orders):
    prods = {}
    for o in orders:
        for l in o["lines"]:
            if l.get("productId"):
                prods.setdefault(l["productId"], []).append(l)
    return prods

def is_done(pid):
    f = os.path.join(PROD, pid, "meta.json")
    try:
        return json.load(open(f, encoding="utf-8")).get("v") == META_VERSION
    except Exception:
        return False

async def download(ctx, url, path):
    if os.path.exists(path):
        return True
    try:
        r = await ctx.request.get(url, timeout=30000)
        if r.ok:
            with open(path, "wb") as fh:
                fh.write(await r.body())
            return True
    except Exception as e:
        log("  download failed:", url, e)
    return False

async def scrape_product(b, pg, pid, lines):
    from PIL import Image
    d = os.path.join(PROD, pid)
    os.makedirs(d, exist_ok=True)
    try:
        await pg.goto(f"https://www.aliexpress.com/item/{pid}.html", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("  load:", str(e)[:100])
    await wait_for_captcha(pg)
    await pg.bring_to_front()
    try:
        await pg.wait_for_selector('[data-pl="product-title"]', timeout=12000)
    except Exception:
        pass
    await pg.wait_for_timeout(3000)
    for t in ("Don't allow", "Niet toestaan"):
        try:
            btn = pg.get_by_text(t, exact=True)
            if await btn.count():
                await btn.first.click(timeout=1500)
        except Exception:
            pass
    info = await pg.evaluate(PRODUCT_JS)
    await pg.evaluate("window.scrollTo(0,0)")
    await pg.wait_for_timeout(800)
    # Note: Chromium's JPEG screenshots came out garbled, so take PNG and convert with Pillow.
    png = await pg.screenshot(type="png", animations="disabled")
    Image.open(io.BytesIO(png)).convert("RGB").save(os.path.join(d, "screenshot.jpg"), quality=85)

    files = []
    for k, g in enumerate(info.get("gallery") or [], 1):
        fn = f"img_{k:02d}{os.path.splitext(g.split('?')[0])[1] or '.jpg'}"
        if await download(b.ctx, g, os.path.join(d, fn)):
            files.append(fn)
    for l in lines:  # image of the ordered variant (also available for removed products)
        if l.get("img"):
            full = re.sub(r"_\d+x\d+\.jpg$", "", l["img"])
            await download(b.ctx, full, os.path.join(d, "order_" + os.path.basename(full.split("?")[0])))

    meta = {"v": META_VERSION, "productId": pid, "finalUrl": pg.url,
            "pageTitle": info.get("title"), "docTitle": info.get("doctitle"),
            "live": bool(info.get("gallery")), "images": files}
    json.dump(meta, open(os.path.join(d, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return meta

async def cmd_products(b, force=False, only=None, limit=None):
    prods = products_from_orders(load_orders())
    todo = [p for p in (only or prods) if p in prods and (force or not is_done(p))]
    if limit:
        todo = todo[:limit]
    log(f"{len(todo)} of {len(prods)} products to do.")
    pg = await b.new_page()
    for i, pid in enumerate(todo):
        if i and i % 25 == 0:  # fresh tab to avoid memory growth / crashes
            await pg.close()
            pg = await b.new_page()
        for attempt in range(2):
            try:
                m = await scrape_product(b, pg, pid, prods[pid])
                state = "for sale" if m["pageTitle"] else ("offline" if m["live"] else "removed")
                log(f"[{i+1}/{len(todo)}] {pid} {state}, {len(m['images'])} images")
                break
            except Exception as e:
                log(f"[{i+1}/{len(todo)}] {pid} ERROR: {str(e)[:120]}")
                try:
                    await pg.close()
                except Exception:
                    pass
                pg = await b.new_page()
        await pg.wait_for_timeout(random.randint(1500, 4000))
    await pg.close()

# ---------------------------------------------------------------- details

DETAILS_VERSION = 1

DETAILS_JS = """()=>{
 const o={specs:[],docs:[],descImgs:[],descText:null};
 const nav=document.querySelector('#nav-specification');
 if(nav) for(const p of nav.querySelectorAll('[class*="specification--prop"]')){
   const k=p.querySelector('[class*="specification--title"]'), v=p.querySelector('[class*="specification--desc"]');
   if(k&&v) o.specs.push([k.innerText.trim(), (v.getAttribute('title')||v.innerText).trim()]);
 }
 for(const a of document.querySelectorAll('a[href*=".pdf"]'))
   o.docs.push({url:a.href, name:a.innerText.replace(/\\s*View\\s*$/,'').trim()});
 for(const h of document.querySelectorAll('#nav-description *')){
   if(!h.shadowRoot) continue;
   const d=h.shadowRoot.querySelector('.product-description')||h.shadowRoot;
   o.descImgs=[...d.querySelectorAll('img')].map(i=>i.src||i.dataset.src).filter(Boolean);
   o.descText=(d.innerText||'').trim()||null;
   break;
 }
 return o}"""

def details_done(pid):
    try:
        return json.load(open(os.path.join(PROD, pid, "details.json"), encoding="utf-8")).get("v") == DETAILS_VERSION
    except Exception:
        return False

async def scrape_details(b, pg, pid):
    d = os.path.join(PROD, pid)
    try:
        await pg.goto(f"https://www.aliexpress.com/item/{pid}.html", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("  load:", str(e)[:100])
    await wait_for_captcha(pg)
    try:
        await pg.wait_for_selector('[data-pl="product-title"]', timeout=12000)
    except Exception:
        pass
    # Specs and description are lazy-loaded: scroll down the page in steps.
    for y in range(0, 16000, 800):
        await pg.evaluate(f"window.scrollTo(0,{y})")
        await pg.wait_for_timeout(250)
    try:  # expand the specification list if it is collapsed
        more = pg.locator("#nav-specification button")
        if await more.count():
            await more.first.click(timeout=2000)
            await pg.wait_for_timeout(500)
    except Exception:
        pass
    try:
        await pg.wait_for_function(
            "()=>[...document.querySelectorAll('#nav-description *')].some(h=>h.shadowRoot&&h.shadowRoot.querySelector('img'))",
            timeout=8000)
    except Exception:
        pass
    info = await pg.evaluate(DETAILS_JS)

    docs = []
    seen = set()
    for k, doc in enumerate(info["docs"], 1):
        if doc["url"] in seen:
            continue
        seen.add(doc["url"])
        os.makedirs(os.path.join(d, "docs"), exist_ok=True)
        name = re.sub(r"[^\w.-]+", "_", doc["name"] or "document").strip("_")[:60] or "document"
        fn = f"docs/{k:02d}_{name}.pdf"
        if await download(b.ctx, doc["url"], os.path.join(d, fn)):
            docs.append({"name": doc["name"], "file": fn, "url": doc["url"]})

    desc = []
    if info["descImgs"]:
        os.makedirs(os.path.join(d, "description"), exist_ok=True)
    for k, u in enumerate(dict.fromkeys(info["descImgs"]), 1):
        ext = os.path.splitext(u.split("?")[0])[1].lower()
        fn = f"description/desc_{k:02d}{ext if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp') else '.jpg'}"
        if await download(b.ctx, u, os.path.join(d, fn)):
            desc.append(fn)

    out = {"v": DETAILS_VERSION, "specs": info["specs"], "docs": docs,
           "descriptionImages": desc,
           "descriptionText": re.sub(r"\n{3,}", "\n\n", info["descText"]) if info["descText"] else None}
    json.dump(out, open(os.path.join(d, "details.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return out

async def cmd_details(b, force=False, only=None, limit=None, all_products=False):
    prods = products_from_orders(load_orders())
    def eligible(pid):  # offline/removed product pages have no specs or description
        m = read_meta(pid)
        return m is not None and (all_products or m.get("pageTitle"))
    todo = [p for p in (only or prods) if p in prods and eligible(p) and (force or not details_done(p))]
    if limit:
        todo = todo[:limit]
    log(f"{len(todo)} products to fetch details for.")
    pg = await b.new_page()
    for i, pid in enumerate(todo):
        if i and i % 25 == 0:
            await pg.close()
            pg = await b.new_page()
        for attempt in range(2):
            try:
                r = await scrape_details(b, pg, pid)
                log(f"[{i+1}/{len(todo)}] {pid}: {len(r['specs'])} specs, {len(r['docs'])} docs, "
                    f"{len(r['descriptionImages'])} description images")
                break
            except Exception as e:
                log(f"[{i+1}/{len(todo)}] {pid} ERROR: {str(e)[:120]}")
                try:
                    await pg.close()
                except Exception:
                    pass
                pg = await b.new_page()
        await pg.wait_for_timeout(random.randint(1500, 4000))
    await pg.close()

# ---------------------------------------------------------------- html

def pdate(s):
    try:
        return datetime.strptime(s, "%b %d, %Y")
    except Exception:
        return datetime(1970, 1, 1)

def eur(s):
    try:
        return float((s or "0").replace("€", "").replace(",", ""))
    except Exception:
        return 0.0

def read_meta(pid):
    try:
        return json.load(open(os.path.join(PROD, pid, "meta.json"), encoding="utf-8"))
    except Exception:
        return None

def details_html(folder, absf):
    e = html.escape
    try:
        dt = json.load(open(os.path.join(absf, "details.json"), encoding="utf-8"))
    except Exception:
        return ""
    parts, label = [], []
    if dt.get("specs"):
        rows = "".join(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in dt["specs"])
        parts.append(f'<table class="specs">{rows}</table>')
        label.append(f"{len(dt['specs'])} specs")
    if dt.get("docs"):
        # No backslashes inside f-string expressions: that needs Python 3.12+ (PEP 701).
        def doc_label(x):
            return re.sub(r"\s*\(PDF\)", "", x["name"] or "") or "Document"
        links = "".join(f'<a href="{e(folder)}/{e(x["file"])}" target="_blank"><b>PDF</b> {e(doc_label(x))}</a>' for x in dt["docs"])
        parts.append(f'<div class="docs">{links}</div>')
        label.append(f"{len(dt['docs'])} document{'s' if len(dt['docs']) > 1 else ''}")
    if dt.get("descriptionText"):
        parts.append(f'<div class="desctext">{e(dt["descriptionText"])}</div>')
    if dt.get("descriptionImages"):
        imgs = "".join(f'<img loading="lazy" class="g" src="{e(folder)}/{e(x)}" data-full="{e(folder)}/{e(x)}" alt="">' for x in dt["descriptionImages"])
        parts.append(f'<div class="descimgs">{imgs}</div>')
        label.append(f"{len(dt['descriptionImages'])} description images")
    if not parts:
        return ""
    return f'<details class="det"><summary>Details: {", ".join(label) or "description"}</summary>{"".join(parts)}</details>'

def cmd_html():
    orders = sorted(load_orders(), key=lambda o: pdate(o["date"]), reverse=True)
    e = html.escape
    cards, years, seen = [], set(), set()
    tot, nlive, ngone = 0.0, 0, 0
    for o in orders:
        d = pdate(o["date"])
        years.add(d.year)
        status = (o["status"] or "").strip()
        if status == "Completed":
            tot += eur(o["total"])
        lines = []
        for l in o["lines"]:
            pid = l["productId"]
            m = read_meta(pid)
            folder = f"products/{pid}"
            absf = os.path.join(OUT, folder)
            if m and pid not in seen:
                seen.add(pid)
                nlive += bool(m.get("pageTitle"))
                ngone += not m.get("pageTitle")
            shot = f"{folder}/screenshot.jpg" if os.path.exists(os.path.join(absf, "screenshot.jpg")) else ""
            imgs = [f"{folder}/{x}" for x in (m or {}).get("images", [])]
            order_imgs = sorted(x for x in os.listdir(absf) if x.startswith("order_")) if os.path.isdir(absf) else []
            thumb = f"{folder}/{order_imgs[0]}" if order_imgs else (imgs[0] if imgs else l.get("img") or "")
            gone = m is not None and not m.get("pageTitle")
            gal = "".join(f'<img loading="lazy" src="{e(i)}" data-full="{e(i)}" class="g" alt="">' for i in imgs)
            sub = (e(l["sku"]) + " · " if l.get("sku") else "") + f'{e(l["price"] or "")} × {l["qty"]}'
            if gone:
                sub += ' · <span class="gone">no longer for sale</span>'
            det_html = details_html(folder, absf)
            shot_html = (f'<img loading="lazy" class="shot g" src="{e(shot)}" data-full="{e(shot)}" alt="Product page screenshot">'
                         if shot else '<div class="shot none">no screenshot</div>')
            lines.append(f"""
      <div class="line">
        <img loading="lazy" class="thumb g" src="{e(thumb)}" data-full="{e(thumb)}" alt="">
        <div class="info">
          <a class="title" href="{e(l['url'] or '')}" target="_blank" rel="noopener">{e(l['title'] or '(no title)')}</a>
          <div class="sub">{sub}</div>
          <div class="gallery">{gal}</div>
          {det_html}
        </div>
        {shot_html}
      </div>""")
        search = " ".join([o["store"] or "", o["orderId"]] + [l["title"] or "" for l in o["lines"]]).lower()
        cards.append(f"""
    <article class="order" data-year="{d.year}" data-status="{e(status)}" data-q="{e(search)}">
      <header>
        <span class="date">{d.strftime('%Y-%m-%d')}</span>
        <span class="status s-{e(status.lower())}">{e(status)}</span>
        <a class="store" href="{e(o['storeUrl'] or '')}" target="_blank" rel="noopener">{e(o['store'] or '')}</a>
        <span class="total">{e(o['total'] or '')}</span>
        <a class="oid" href="{e(o['detailUrl'] or '')}" target="_blank" rel="noopener">#{e(o['orderId'])}</a>
      </header>{''.join(lines)}
    </article>""")

    year_opts = "".join(f'<option value="{y}">{y}</option>' for y in sorted(years, reverse=True))
    nlines = sum(len(o["lines"]) for o in orders)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AliExpress orders</title>
<style>
:root{{--bg:#f6f6f4;--card:#fff;--fg:#1d1d1b;--mut:#6b6b66;--line:#e4e3de;--acc:#c2410c;--ok:#15803d;--bad:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--card:#1f1f1d;--fg:#ecebe6;--mut:#9a9992;--line:#33322f;--acc:#fb923c;--ok:#4ade80;--bad:#f87171}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}}
.top{{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:14px 16px}}
.wrap{{max-width:1200px;margin:0 auto}}
h1{{margin:0 0 4px;font-size:20px}}
.stats{{color:var(--mut);margin-bottom:10px}}
.filters{{display:flex;gap:8px;flex-wrap:wrap}}
input,select{{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}}
input{{flex:1;min-width:200px}}
main{{padding:16px}}
.order{{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:0 auto 14px;max-width:1200px;overflow:hidden}}
.order header{{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline;padding:10px 14px;border-bottom:1px solid var(--line)}}
.date{{font-weight:600;font-variant-numeric:tabular-nums}}
.status{{font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid currentColor}}
.s-completed{{color:var(--ok)}} .s-expired,.s-canceled{{color:var(--bad)}}
.store{{color:var(--mut);text-decoration:none}}
.total{{margin-left:auto;font-weight:600}}
.oid{{color:var(--mut);font-size:12px;text-decoration:none}}
.line{{display:grid;grid-template-columns:72px 1fr 280px;gap:14px;padding:12px 14px;border-top:1px solid var(--line)}}
.line:first-of-type{{border-top:0}}
.thumb{{width:72px;height:72px;object-fit:cover;border-radius:8px;background:var(--line)}}
.title{{color:var(--fg);text-decoration:none;font-weight:500}}
.title:hover{{color:var(--acc)}}
.sub{{color:var(--mut);margin:3px 0 8px}}
.gone{{color:var(--bad)}}
.gallery{{display:flex;flex-wrap:wrap;gap:6px}}
.gallery img{{width:64px;height:64px;object-fit:cover;border-radius:6px;border:1px solid var(--line);background:#fff}}
.shot{{width:280px;aspect-ratio:1.4;object-fit:cover;object-position:top left;border-radius:8px;border:1px solid var(--line)}}
.shot.none{{display:grid;place-items:center;color:var(--mut);font-size:12px}}
.g{{cursor:zoom-in}}
details.det{{margin-top:10px}}
details.det summary{{cursor:pointer;color:var(--acc);font-size:13px}}
.specs{{border-collapse:collapse;margin:8px 0;font-size:13px}}
.specs td{{border-top:1px solid var(--line);padding:3px 12px 3px 0;vertical-align:top}}
.specs td:first-child{{color:var(--mut);white-space:nowrap}}
.docs a{{display:inline-block;margin:4px 8px 4px 0;padding:3px 10px;border:1px solid var(--line);border-radius:99px;color:var(--fg);text-decoration:none;font-size:13px}}
.desctext{{white-space:pre-wrap;color:var(--mut);font-size:13px;max-height:12em;overflow:auto;margin:8px 0}}
.info{{min-width:0}}
.descimgs{{display:flex;flex-direction:column;gap:0;margin-top:8px;max-width:640px}}
.descimgs img{{width:100%;height:auto;background:#fff}}
#lb{{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;place-items:center;z-index:10;cursor:zoom-out}}
#lb img{{max-width:95vw;max-height:95vh;background:#fff}}
@media (max-width:760px){{.line{{grid-template-columns:56px 1fr}}.thumb{{width:56px;height:56px}}.shot{{grid-column:1/-1;width:100%}}.total{{margin-left:0}}}}
</style></head><body>
<div class="top"><div class="wrap">
<h1>AliExpress orders</h1>
<div class="stats">{len(orders)} orders · {nlines} order lines · {len(seen)} unique products ({nlive} still for sale, {ngone} offline) · completed total €{tot:,.2f} · generated {datetime.now():%Y-%m-%d}</div>
<div class="filters">
<input id="q" type="search" placeholder="Search product, store or order number…">
<select id="y"><option value="">All years</option>{year_opts}</select>
<select id="s"><option value="">All statuses</option><option>Completed</option><option>Expired</option><option>Canceled</option></select>
<span id="n" class="stats" style="align-self:center;margin:0"></span>
</div></div></div>
<main>{''.join(cards)}</main>
<div id="lb"><img alt=""></div>
<script>
const q=document.getElementById('q'),y=document.getElementById('y'),s=document.getElementById('s'),n=document.getElementById('n');
const os=[...document.querySelectorAll('.order')];
function f(){{const t=q.value.toLowerCase().trim();let c=0;
 os.forEach(o=>{{const v=(!t||o.dataset.q.includes(t))&&(!y.value||o.dataset.year==y.value)&&(!s.value||o.dataset.status==s.value);o.style.display=v?'':'none';c+=v}});
 n.textContent=c+' shown';}}
[q,y,s].forEach(x=>x.addEventListener('input',f));f();
const lb=document.getElementById('lb');
document.addEventListener('click',ev=>{{const g=ev.target.closest('.g');
 if(g){{lb.firstElementChild.src=g.dataset.full;lb.style.display='grid'}} else if(ev.target.closest('#lb')) lb.style.display='none'}});
document.addEventListener('keydown',ev=>{{if(ev.key==='Escape')lb.style.display='none'}});
</script></body></html>"""
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)
    log(f"overview/index.html written: {len(orders)} orders, {len(seen)} products with data.")

# ---------------------------------------------------------------- main

async def cmd_login(b):
    pg = await b.new_page()
    await pg.goto(ORDER_URL, wait_until="domcontentloaded")
    await pg.wait_for_timeout(3000)
    await wait_for_login(pg)
    log("You are logged in; the profile is saved in .browser-profile.")
    await pg.close()

async def run(args):
    if args.cmd == "html":
        return cmd_html()
    async with Browser(args.cdp) as b:
        if args.cmd == "login":
            await cmd_login(b)
        elif args.cmd == "orders":
            await cmd_orders(b, full=args.full)
        elif args.cmd == "products":
            await cmd_products(b, force=args.force, only=args.only, limit=args.limit)
        elif args.cmd == "details":
            await cmd_details(b, force=args.force, only=args.only, limit=args.limit, all_products=args.all)
        elif args.cmd == "update":
            await cmd_orders(b, full=args.full)
            await cmd_products(b)
            await cmd_details(b)
    if args.cmd in ("products", "details", "update"):
        cmd_html()

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cdp", help="attach to an existing Chromium, e.g. http://127.0.0.1:9333")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("html")
    o = sub.add_parser("orders"); o.add_argument("--full", action="store_true", help="reload all orders")
    u = sub.add_parser("update"); u.add_argument("--full", action="store_true")
    p = sub.add_parser("products")
    p.add_argument("--force", action="store_true", help="redo already processed products")
    p.add_argument("--only", nargs="+", metavar="ID")
    p.add_argument("--limit", type=int)
    dt = sub.add_parser("details", help="specifications, documents (PDF) and description images")
    dt.add_argument("--force", action="store_true", help="redo products that already have details")
    dt.add_argument("--all", action="store_true", help="also try products whose page is offline")
    dt.add_argument("--only", nargs="+", metavar="ID")
    dt.add_argument("--limit", type=int)
    asyncio.run(run(ap.parse_args()))

if __name__ == "__main__":
    main()
