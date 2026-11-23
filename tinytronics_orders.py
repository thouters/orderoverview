#!/usr/bin/env python3
"""
TinyTronics order overview: fetches your orders from www.tinytronics.nl, takes a
screenshot of each product page, downloads the product images, specifications,
downloads (datasheets) and invoices, and builds overview-tinytronics/index.html.

Usage:
  python tinytronics_orders.py login       # open the browser to log in (again)
  python tinytronics_orders.py update      # new orders + missing products + html
  python tinytronics_orders.py orders [--full]
  python tinytronics_orders.py products [--force] [--only SKU ...] [--limit N]
  python tinytronics_orders.py html

Options for all commands that use the browser (put them before the command):
  --channel chrome|msedge       use the installed Google Chrome or Microsoft Edge instead of
                                Playwright's own Chromium (no `playwright install` needed).
  --cdp http://127.0.0.1:9333   attach to an already running Chromium (started with
                                --remote-debugging-port) instead of launching one.

The browser runs visibly with its own profile in ./.browser-profile-tinytronics (or
./.browser-profile-tinytronics-<channel>), so you stay logged in between runs.
"""
import argparse, asyncio, html, io, json, os, random, re, sys, time
from datetime import datetime

from playwright.async_api import async_playwright

# Working dir for the output and the browser profile; with `nix run` this is the current dir.
ROOT = os.environ.get("TINYTRONICS_ORDERS_DIR") or os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "overview-tinytronics")
PROD = os.path.join(OUT, "products")
INVOICES = os.path.join(OUT, "invoices")
ORDERS_JSON = os.path.join(OUT, "orders.json")
CHANNELS = ("chrome", "msedge")
SITE = "https://www.tinytronics.nl"
ORDER_LIST_URL = SITE + "/index.php?route=account/order"
ORDER_INFO_URL = SITE + "/index.php?route=account/order/info&order_id={}"
VIEWPORT = {"width": 1400, "height": 1000}
META_VERSION = 1

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

# ---------------------------------------------------------------- browser

class Browser:
    """Persistent profile (default) or attach to an existing browser via CDP."""

    def __init__(self, cdp=None, channel=None):
        self.cdp = cdp
        self.channel = channel

    @property
    def profile(self):
        name = ".browser-profile-tinytronics" + (f"-{self.channel}" if self.channel else "")
        return os.path.join(ROOT, name)

    async def __aenter__(self):
        self.pw = await async_playwright().start()
        if self.cdp:
            if self.channel:
                log(f"--channel {self.channel} is ignored with --cdp.")
            self.browser = await self.pw.chromium.connect_over_cdp(self.cdp)
            self.ctx = self.browser.contexts[0]
            return self
        self.browser = None
        try:
            self.ctx = await self.pw.chromium.launch_persistent_context(
                self.profile, channel=self.channel, headless=False, viewport=VIEWPORT,
                args=["--no-first-run", "--no-default-browser-check"])
        except Exception as e:
            await self.pw.stop()
            if self.channel and ("is not found" in str(e) or "Executable doesn't exist" in str(e)):
                sys.exit(f"Browser '{self.channel}' is not installed on this computer. Install it, "
                         f"or leave out --channel to use Playwright's Chromium.")
            raise
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
    return "account/login" in url

async def goto(pg, url):
    try:
        await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("  load:", str(e)[:100])
    await wait_for_login(pg)
    if pg.url.split("#")[0] != url and "account/" in url and not is_login(pg.url):
        # OpenCart sends you to the account page after logging in; go back to where we were.
        await pg.goto(url, wait_until="domcontentloaded", timeout=60000)

async def wait_for_login(pg):
    if not is_login(pg.url):
        return
    await pg.bring_to_front()
    log("Not logged in: log in in the browser window (waiting up to 10 minutes)...")
    for _ in range(600):
        await pg.wait_for_timeout(1000)
        if not is_login(pg.url):
            log("Logged in.")
            await pg.wait_for_timeout(2000)
            return
    sys.exit("No login within 10 minutes; stopped.")

# ---------------------------------------------------------------- orders

LIST_JS = """()=>[...document.querySelectorAll('#content table tbody tr')].map(r=>{
  const td=[...r.querySelectorAll('td')].map(t=>t.innerText.trim());
  const a=r.querySelector('a[href*="order/info"]:not([href*="pdf="])');
  return {id:(td[0]||'').replace('#',''), count:td[2], status:td[3], total:td[4], date:td[5], url:a&&a.href};
}).filter(o=>/^\\d+$/.test(o.id))"""

INFO_JS = """()=>{
  const o={lines:[], history:[], tracking:[], totals:[]};
  const head=document.querySelector('#content table td[style*="50%"]');
  const txt=(document.querySelector('#content table')||{}).innerText||'';
  const m=(re)=>{const x=txt.match(re);return x?x[1].trim():null};
  o.invoiceNo=m(/Invoice No\\.:\\s*(\\S+)/); o.payment=m(/Payment Method:\\s*([^\\n]+)/);
  o.shipping=m(/Shipping Method:\\s*([^\\n]+)/);
  const t=document.querySelector('table.table-order');
  if(t){
    for(const r of t.querySelectorAll('tbody tr')){
      const td=[...r.querySelectorAll('td')];
      if(td.length<5) continue;
      const a=td[0].querySelector('a');
      o.lines.push({title:td[0].innerText.trim(), url:a?a.href:null, sku:td[1].innerText.trim(),
                    qty:parseInt(td[2].innerText)||1, price:td[3].innerText.trim(), total:td[4].innerText.trim()});
    }
    for(const r of t.querySelectorAll('tfoot tr')){
      const td=[...r.querySelectorAll('td')].map(x=>x.innerText.trim()).filter(Boolean);
      if(td.length>=2) o.totals.push([td[0].split('\\n')[0], td[1].split('\\n')[0]]);
    }
  }
  for(const tb of document.querySelectorAll('#content table.list')){
    const r={};
    for(const tr of tb.querySelectorAll('tr')){
      const td=tr.querySelectorAll('td');
      if(td.length==2) r[td[0].innerText.replace(':','').trim()]=td[1].innerText.trim();
      const a=tr.querySelector('a[href*="track"],a[href*="trace"]'); if(a) r.url=a.href;
    }
    if(Object.keys(r).length) o.tracking.push(r);
  }
  const hist=[...document.querySelectorAll('#content table')].find(x=>/Date Added\\s+Status/i.test(x.innerText.split('\\n')[0]+' '+(x.innerText.split('\\n')[1]||'')) || /^DATE ADDED\\tSTATUS/i.test(x.innerText));
  if(hist) for(const r of hist.querySelectorAll('tbody tr')){
    const td=[...r.querySelectorAll('td')].map(x=>x.innerText.trim());
    if(td.length>=2 && /\\d{2}-\\d{2}-\\d{4}/.test(td[0])) o.history.push({date:td[0], status:td[1], comment:td[2]||''});
  }
  return o}"""

def load_orders():
    return json.load(open(ORDERS_JSON, encoding="utf-8")) if os.path.exists(ORDERS_JSON) else []

def save_orders(orders):
    os.makedirs(OUT, exist_ok=True)
    orders.sort(key=lambda o: pdate(o["date"]), reverse=True)
    json.dump(orders, open(ORDERS_JSON, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

async def download(ctx, url, path, want=None):
    if os.path.exists(path):
        return True
    try:
        r = await ctx.request.get(url, timeout=60000)
        if r.ok and (want is None or want in r.headers.get("content-type", "")):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(await r.body())
            return True
        log("  download failed:", url, r.status, r.headers.get("content-type"))
    except Exception as e:
        log("  download failed:", url, e)
    return False

async def cmd_orders(b, full=False):
    known = {o["orderId"]: o for o in load_orders()}
    pg = await b.new_page()
    listed = []
    for page in range(1, 200):
        await goto(pg, ORDER_LIST_URL + (f"&page={page}" if page > 1 else ""))
        rows = await pg.evaluate(LIST_JS)
        if not rows:
            break
        listed += rows
        log(f"order list page {page}: {len(rows)} orders")
        # Stop at a page of known orders whose status did not change, unless --full.
        if not full and known and all(r["id"] in known and known[r["id"]]["status"] == r["status"] for r in rows):
            log("Only known orders left; stopping (use --full to load everything).")
            break
        if not await pg.locator(f'.pagination a[href*="page={page + 1}"]').count():
            break

    new = 0
    for i, r in enumerate(listed):
        old = known.get(r["id"])
        if old and old["status"] == r["status"] and old.get("lines") and not full:
            continue
        await goto(pg, ORDER_INFO_URL.format(r["id"]))
        info = await pg.evaluate(INFO_JS)
        new += old is None
        known[r["id"]] = {
            "orderId": r["id"], "date": r["date"], "status": r["status"], "total": r["total"],
            "count": r["count"], "detailUrl": ORDER_INFO_URL.format(r["id"]),
            "invoiceNo": info["invoiceNo"], "payment": info["payment"], "shipping": info["shipping"],
            "totals": info["totals"], "tracking": info["tracking"], "history": info["history"],
            "lines": info["lines"],
        }
        inv = os.path.join(INVOICES, f"{r['id']}.pdf")
        if await download(b.ctx, ORDER_INFO_URL.format(r["id"]) + "&pdf=1", inv, want="pdf"):
            known[r["id"]]["invoice"] = f"invoices/{r['id']}.pdf"
        log(f"[{i+1}/{len(listed)}] order {r['id']} {r['date']}: {len(info['lines'])} lines, {r['total']}")
        await pg.wait_for_timeout(random.randint(500, 1500))
    await pg.close()
    save_orders(list(known.values()))
    log(f"{len(known)} orders in orders.json ({new} new).")

# ---------------------------------------------------------------- products

PRODUCT_JS = """()=>{
  const o={};
  const h=document.querySelector('h1.page-title'); o.title=h?h.innerText.trim():null;
  o.docTitle=document.title;
  o.live=!!document.querySelector('.product-info, #product, .product-details') && !!h;
  o.gallery=[...document.querySelectorAll('.main-image .swiper-slide img')].map(i=>i.dataset.largeimg||i.src);
  if(!o.gallery.length) o.gallery=[...document.querySelectorAll('img[src*="catalog/products"]')].map(i=>i.dataset.largeimg||i.src);
  const sku=[...document.querySelectorAll('li, span, div')].find(e=>/^SKU:\\s*\\d+/.test(e.innerText||'')&&e.children.length<3);
  o.sku=sku?sku.innerText.replace(/^SKU:\\s*/,'').trim():null;
  const av=document.querySelector('li.product-stock'); o.availability=av?av.innerText.replace(/^\\s*Availability:\\s*/i,'').trim():null;
  const pr=document.querySelector('.product-price'); o.price=pr?pr.innerText.trim():null;
  o.breadcrumb=[...document.querySelectorAll('.breadcrumb li')].map(l=>l.innerText.trim()).filter(Boolean).slice(1,-1);
  o.specs=[];
  for(const tr of document.querySelectorAll('#tab-specification tr')){
    const td=[...tr.querySelectorAll('td')];
    if(td.length==1) o.specs.push([td[0].innerText.trim(), null]);
    else if(td.length>=2) o.specs.push([td[0].innerText.trim(), td[1].innerText.trim().replace(/\\n+/g,' · ')]);
  }
  o.tabs=[]; o.docs=[];
  for(const a of document.querySelectorAll('.product_tabs .nav-tabs a')){
    const name=a.innerText.trim(), pane=document.querySelector(a.getAttribute('href'));
    if(!pane||/^Reviews/i.test(name)) continue;
    const c=pane.querySelector('.block-content')||pane;
    for(const l of c.querySelectorAll('a[href*="product_files"], a[href$=".pdf"], a[href$=".zip"]'))
      o.docs.push({name:l.innerText.trim()||l.href.split('/').pop(), url:l.href});
    if(/^Downloads$/i.test(name)) continue;
    o.tabs.push({name, text:c.innerText.trim(),
      images:[...c.querySelectorAll('img')].map(i=>i.dataset.src||i.src).filter(s=>s&&!s.startsWith('data:'))});
  }
  return o}"""

def full_image(u):
    # Strip Cloudflare's resize options to get the original upload.
    return re.sub(r"/cdn-cgi/image/[^/]+/", "/", u)

def product_key(line):
    if line.get("sku"):
        return line["sku"]
    slug = (line.get("url") or line.get("title") or "unknown").rstrip("/").split("/")[-1]
    return re.sub(r"[^\w.-]+", "_", slug)[:80]

def products_from_orders(orders):
    prods = {}
    for o in orders:
        for l in o["lines"]:
            prods.setdefault(product_key(l), []).append(l)
    return prods

def available(m):
    """Product page still exists and the product is not discontinued."""
    return bool(m and m.get("pageTitle")) and not re.match(r"discontinued", m.get("availability") or "", re.I)

def read_meta(key):
    try:
        return json.load(open(os.path.join(PROD, key, "meta.json"), encoding="utf-8"))
    except Exception:
        return None

def is_done(key):
    m = read_meta(key)
    return bool(m) and m.get("v") == META_VERSION

async def scrape_product(b, pg, key, lines):
    from PIL import Image
    d = os.path.join(PROD, key)
    os.makedirs(d, exist_ok=True)
    url = next((l["url"] for l in lines if l.get("url")), None)
    info = {"title": None, "live": False, "gallery": [], "specs": [], "tabs": [], "docs": []}
    if url:
        try:
            resp = await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            status = resp.status if resp else None
        except Exception as e:
            log("  load:", str(e)[:100])
            status = None
        await pg.wait_for_timeout(1500)
        info = await pg.evaluate(PRODUCT_JS)
        info["status"] = status
        await pg.evaluate("window.scrollTo(0,0)")
        # Chromium's JPEG screenshots can come out garbled, so take PNG and convert with Pillow.
        png = await pg.screenshot(type="png", animations="disabled")
        Image.open(io.BytesIO(png)).convert("RGB").save(os.path.join(d, "screenshot.jpg"), quality=85)

    images = []
    for k, g in enumerate(dict.fromkeys(full_image(u) for u in info["gallery"]), 1):
        ext = os.path.splitext(g.split("?")[0])[1].lower() or ".jpg"
        fn = f"img_{k:02d}{ext}"
        if await download(b.ctx, g, os.path.join(d, fn)):
            images.append(fn)

    docs, seen = [], set()
    for k, doc in enumerate(info["docs"], 1):
        if doc["url"] in seen:
            continue
        seen.add(doc["url"])
        base = os.path.basename(doc["url"].split("?")[0])
        fn = "docs/" + re.sub(r"[^\w.()-]+", "_", re.sub(r"%28", "(", re.sub(r"%29", ")", base)))[:100]
        if await download(b.ctx, doc["url"], os.path.join(d, fn)):
            docs.append({"name": doc["name"], "file": fn, "url": doc["url"]})

    tabs = []
    n = 0
    for t in info["tabs"]:
        files = []
        for u in dict.fromkeys(t["images"]):
            n += 1
            ext = os.path.splitext(u.split("?")[0])[1].lower()
            fn = f"description/desc_{n:02d}{ext if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp') else '.jpg'}"
            if await download(b.ctx, full_image(u), os.path.join(d, fn)):
                files.append(fn)
        tabs.append({"name": t["name"], "text": re.sub(r"\n{3,}", "\n\n", t["text"]), "images": files})

    meta = {"v": META_VERSION, "key": key, "url": url, "finalUrl": pg.url if url else None,
            "httpStatus": info.get("status"), "pageTitle": info["title"] if info["live"] else None,
            "docTitle": info.get("docTitle"), "sku": info.get("sku"),
            "availability": info.get("availability"), "price": info.get("price"),
            "breadcrumb": info.get("breadcrumb"), "images": images, "specs": info["specs"],
            "docs": docs, "tabs": tabs}
    json.dump(meta, open(os.path.join(d, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return meta

async def cmd_products(b, force=False, only=None, limit=None):
    prods = products_from_orders(load_orders())
    todo = [p for p in (only or prods) if p in prods and (force or not is_done(p))]
    if limit:
        todo = todo[:limit]
    log(f"{len(todo)} of {len(prods)} products to do.")
    pg = await b.new_page()
    for i, key in enumerate(todo):
        if i and i % 25 == 0:  # fresh tab to avoid memory growth / crashes
            await pg.close()
            pg = await b.new_page()
        for attempt in range(2):
            try:
                m = await scrape_product(b, pg, key, prods[key])
                state = "for sale" if available(m) else "no longer available"
                log(f"[{i+1}/{len(todo)}] {key} {state}: {len(m['images'])} images, "
                    f"{sum(1 for s in m['specs'] if s[1] is not None)} specs, {len(m['docs'])} downloads")
                break
            except Exception as e:
                log(f"[{i+1}/{len(todo)}] {key} ERROR: {str(e)[:120]}")
                try:
                    await pg.close()
                except Exception:
                    pass
                pg = await b.new_page()
        await pg.wait_for_timeout(random.randint(800, 2000))
    await pg.close()

# ---------------------------------------------------------------- html

def pdate(s):
    try:
        return datetime.strptime(s, "%d-%m-%Y")
    except Exception:
        return datetime(1970, 1, 1)

def eur(s):
    try:
        return float((s or "0").replace("€", "").replace(",", ""))
    except Exception:
        return 0.0

def details_html(folder, m):
    e = html.escape
    parts, label = [], []
    specs = m.get("specs") or []
    if specs:
        rows = "".join(f'<tr class="grp"><td colspan="2">{e(k)}</td></tr>' if v is None
                       else f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in specs)
        parts.append(f'<table class="specs">{rows}</table>')
        label.append(f"{sum(1 for s in specs if s[1] is not None)} specs")
    if m.get("docs"):
        def doc_label(x):
            return x["name"] or os.path.basename(x["file"])
        links = "".join(f'<a href="{e(folder)}/{e(x["file"])}" target="_blank"><b>{e(os.path.splitext(x["file"])[1][1:].upper() or "FILE")}</b> {e(doc_label(x))}</a>'
                        for x in m["docs"])
        parts.append(f'<div class="docs">{links}</div>')
        label.append(f"{len(m['docs'])} download{'s' if len(m['docs']) > 1 else ''}")
    nimg = 0
    for t in m.get("tabs") or []:
        if t.get("text"):
            parts.append(f'<div class="tabname">{e(t["name"])}</div><div class="desctext">{e(t["text"])}</div>')
        if t.get("images"):
            nimg += len(t["images"])
            imgs = "".join(f'<img loading="lazy" class="g" src="{e(folder)}/{e(x)}" data-full="{e(folder)}/{e(x)}" alt="">' for x in t["images"])
            parts.append(f'<div class="descimgs">{imgs}</div>')
    if nimg:
        label.append(f"{nimg} description images")
    if not parts:
        return ""
    return f'<details class="det"><summary>Details: {", ".join(label) or "description"}</summary>{"".join(parts)}</details>'

def cmd_html():
    orders = sorted(load_orders(), key=lambda o: pdate(o["date"]), reverse=True)
    e = html.escape
    cards, years, statuses, seen = [], set(), set(), set()
    tot, nlive, ngone = 0.0, 0, 0
    for o in orders:
        d = pdate(o["date"])
        years.add(d.year)
        status = (o["status"] or "").strip()
        statuses.add(status)
        if not re.search(r"cancel|refund|void|denied|failed", status, re.I):
            tot += eur(o["total"])
        lines = []
        for l in o["lines"]:
            key = product_key(l)
            m = read_meta(key)
            folder = f"products/{key}"
            absf = os.path.join(OUT, folder)
            if m and key not in seen:
                seen.add(key)
                nlive += available(m)
                ngone += not available(m)
            shot = f"{folder}/screenshot.jpg" if os.path.exists(os.path.join(absf, "screenshot.jpg")) else ""
            imgs = [f"{folder}/{x}" for x in (m or {}).get("images", [])]
            thumb = imgs[0] if imgs else ""
            gone = m is not None and not available(m)
            gal = "".join(f'<img loading="lazy" src="{e(i)}" data-full="{e(i)}" class="g" alt="">' for i in imgs)
            sub = f'SKU {e(l["sku"])} · ' if l.get("sku") else ""
            sub += f'{e(l["price"] or "")} × {l["qty"]}'
            if m and m.get("availability") and not gone:
                sub += f' · now: {e(m["availability"])}'
            if gone:
                sub += f' · <span class="gone">{e(m.get("availability") or "no longer available")}</span>' if m.get("pageTitle") \
                    else ' · <span class="gone">no longer available</span>'
            det = details_html(folder, m) if m else ""
            thumb_html = (f'<img loading="lazy" class="thumb g" src="{e(thumb)}" data-full="{e(thumb)}" alt="">'
                          if thumb else '<div class="thumb"></div>')
            shot_html = (f'<img loading="lazy" class="shot g" src="{e(shot)}" data-full="{e(shot)}" alt="Product page screenshot">'
                         if shot else '<div class="shot none">no screenshot</div>')
            lines.append(f"""
      <div class="line">
        {thumb_html}
        <div class="info">
          <a class="title" href="{e(l.get('url') or '')}" target="_blank" rel="noopener">{e(l['title'] or '(no title)')}</a>
          <div class="sub">{sub}</div>
          <div class="gallery">{gal}</div>
          {det}
        </div>
        {shot_html}
      </div>""")
        track = "".join(f'<a class="oid" href="{e(t["url"])}" target="_blank" rel="noopener">track {e(t.get("Tracking number", ""))}</a>'
                        for t in o.get("tracking") or [] if t.get("url"))
        inv = (f'<a class="oid" href="{e(o["invoice"])}" target="_blank">invoice</a>' if o.get("invoice") else "")
        search = " ".join([o["orderId"]] + [(l["title"] or "") + " " + (l.get("sku") or "") for l in o["lines"]]).lower()
        cards.append(f"""
    <article class="order" data-year="{d.year}" data-status="{e(status)}" data-q="{e(search)}">
      <header>
        <span class="date">{d.strftime('%Y-%m-%d')}</span>
        <span class="status">{e(status)}</span>
        <span class="store">{e(o.get('shipping') or '')}</span>
        <span class="total">{e(o['total'] or '')}</span>
        {inv}{track}
        <a class="oid" href="{e(o['detailUrl'])}" target="_blank" rel="noopener">#{e(o['orderId'])}</a>
      </header>{''.join(lines)}
    </article>""")

    year_opts = "".join(f'<option value="{y}">{y}</option>' for y in sorted(years, reverse=True))
    status_opts = "".join(f"<option>{e(s)}</option>" for s in sorted(statuses))
    nlines = sum(len(o["lines"]) for o in orders)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TinyTronics orders</title>
<style>
:root{{--bg:#f5f7f9;--card:#fff;--fg:#1b1d1f;--mut:#677079;--line:#e1e5e9;--acc:#2f7fc1;--ok:#15803d;--bad:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141618;--card:#1d2023;--fg:#e9ecef;--mut:#98a1aa;--line:#30353a;--acc:#6cb2ea;--ok:#4ade80;--bad:#f87171}}}}
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
.status{{font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid currentColor;color:var(--ok)}}
.store{{color:var(--mut)}}
.total{{margin-left:auto;font-weight:600}}
.oid{{color:var(--mut);font-size:12px;text-decoration:none}}
.oid:hover{{color:var(--acc)}}
.line{{display:grid;grid-template-columns:72px 1fr 280px;gap:14px;padding:12px 14px;border-top:1px solid var(--line)}}
.line:first-of-type{{border-top:0}}
.thumb{{width:72px;height:72px;object-fit:contain;border-radius:8px;background:#fff;border:1px solid var(--line)}}
.title{{color:var(--fg);text-decoration:none;font-weight:500}}
.title:hover{{color:var(--acc)}}
.sub{{color:var(--mut);margin:3px 0 8px}}
.gone{{color:var(--bad)}}
.gallery{{display:flex;flex-wrap:wrap;gap:6px}}
.gallery img{{width:64px;height:64px;object-fit:contain;border-radius:6px;border:1px solid var(--line);background:#fff}}
.shot{{width:280px;aspect-ratio:1.4;object-fit:cover;object-position:top left;border-radius:8px;border:1px solid var(--line)}}
.shot.none{{display:grid;place-items:center;color:var(--mut);font-size:12px}}
.g{{cursor:zoom-in}}
details.det{{margin-top:10px}}
details.det summary{{cursor:pointer;color:var(--acc);font-size:13px}}
.specs{{border-collapse:collapse;margin:8px 0;font-size:13px}}
.specs td{{border-top:1px solid var(--line);padding:3px 12px 3px 0;vertical-align:top}}
.specs td:first-child{{color:var(--mut)}}
.specs tr.grp td{{color:var(--fg);font-weight:600;padding-top:10px}}
.docs a{{display:inline-block;margin:4px 8px 4px 0;padding:3px 10px;border:1px solid var(--line);border-radius:99px;color:var(--fg);text-decoration:none;font-size:13px}}
.tabname{{font-weight:600;font-size:13px;margin-top:8px}}
.desctext{{white-space:pre-wrap;color:var(--mut);font-size:13px;max-height:12em;overflow:auto;margin:4px 0 8px}}
.info{{min-width:0}}
.descimgs{{display:flex;flex-direction:column;gap:0;margin-top:8px;max-width:640px}}
.descimgs img{{width:100%;height:auto;background:#fff}}
#lb{{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;place-items:center;z-index:10;cursor:zoom-out}}
#lb img{{max-width:95vw;max-height:95vh;background:#fff}}
@media (max-width:760px){{.line{{grid-template-columns:56px 1fr}}.thumb{{width:56px;height:56px}}.shot{{grid-column:1/-1;width:100%}}.total{{margin-left:0}}}}
</style></head><body>
<div class="top"><div class="wrap">
<h1>TinyTronics orders</h1>
<div class="stats">{len(orders)} orders · {nlines} order lines · {len(seen)} unique products ({nlive} still available, {ngone} no longer available) · total €{tot:,.2f} · generated {datetime.now():%Y-%m-%d}</div>
<div class="filters">
<input id="q" type="search" placeholder="Search product, SKU or order number…">
<select id="y"><option value="">All years</option>{year_opts}</select>
<select id="s"><option value="">All statuses</option>{status_opts}</select>
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
    log(f"overview-tinytronics/index.html written: {len(orders)} orders, {len(seen)} products with data.")

# ---------------------------------------------------------------- main

async def cmd_login(b):
    pg = await b.new_page()
    await goto(pg, ORDER_LIST_URL)
    log(f"You are logged in; the profile is saved in {os.path.basename(b.profile)}.")
    await pg.close()

async def run(args):
    if args.cmd == "html":
        return cmd_html()
    async with Browser(args.cdp, args.channel) as b:
        if args.cmd == "login":
            await cmd_login(b)
        elif args.cmd == "orders":
            await cmd_orders(b, full=args.full)
        elif args.cmd == "products":
            await cmd_products(b, force=args.force, only=args.only, limit=args.limit)
        elif args.cmd == "update":
            await cmd_orders(b, full=args.full)
            await cmd_products(b)
    if args.cmd in ("orders", "products", "update"):
        cmd_html()

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", choices=CHANNELS,
                    help="use the installed Google Chrome or Microsoft Edge instead of Playwright's Chromium")
    ap.add_argument("--cdp", help="attach to an existing Chromium, e.g. http://127.0.0.1:9333")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("html")
    o = sub.add_parser("orders"); o.add_argument("--full", action="store_true", help="reload all orders")
    u = sub.add_parser("update"); u.add_argument("--full", action="store_true")
    p = sub.add_parser("products", help="screenshot, images, specifications and downloads")
    p.add_argument("--force", action="store_true", help="redo already processed products")
    p.add_argument("--only", nargs="+", metavar="SKU")
    p.add_argument("--limit", type=int)
    asyncio.run(run(ap.parse_args()))

if __name__ == "__main__":
    main()
