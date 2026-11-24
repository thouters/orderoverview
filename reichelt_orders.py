#!/usr/bin/env python3
"""
reichelt order overview: fetches your orders from www.reichelt.de, takes a screenshot
of each product page, downloads the product images, technical data and datasheets, and
builds overview-reichelt/index.html.

Usage:
  python reichelt_orders.py login       # open the browser to log in (again)
  python reichelt_orders.py update      # new orders + missing products + html
  python reichelt_orders.py orders [--full]
  python reichelt_orders.py products [--force] [--only ID ...] [--limit N]
  python reichelt_orders.py html

Options for all commands that use the browser (put them before the command):
  --channel chrome|msedge       use the installed Google Chrome or Microsoft Edge instead of
                                Playwright's own Chromium (no `playwright install` needed).
  --cdp http://127.0.0.1:9333   attach to an already running Chromium (started with
                                --remote-debugging-port) instead of launching one.

The browser runs visibly with its own profile in ./.browser-profile-reichelt (or
./.browser-profile-reichelt-<channel>), so you stay logged in between runs.
"""
import argparse, asyncio, html, io, json, os, random, re, sys, time
from datetime import datetime

from playwright.async_api import async_playwright

# Working dir for the output and the browser profile; with `nix run` this is the current dir.
ROOT = os.environ.get("REICHELT_ORDERS_DIR") or os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "overview-reichelt")
PROD = os.path.join(OUT, "products")
ORDERS_JSON = os.path.join(OUT, "orders.json")
CHANNELS = ("chrome", "msedge")
ORDERS_URL = "https://secure.reichelt.de/index.html?ACTION=10020&PAGE=0"
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
        name = ".browser-profile-reichelt" + (f"-{self.channel}" if self.channel else "")
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
    return "ACTION=10000" in url

COOKIES_SEEN = False

async def accept_cookies(pg):
    """Click the cookie banner away, otherwise it ends up in the screenshots. It appears with a
    delay, so the first time wait a few seconds for it; once accepted it does not come back."""
    global COOKIES_SEEN
    btn = pg.locator("#cookieok")
    try:
        if not COOKIES_SEEN:
            await btn.first.wait_for(state="visible", timeout=4000)
        if await btn.count() and await btn.first.is_visible():
            await btn.first.click(timeout=3000)
            await pg.wait_for_timeout(800)
            COOKIES_SEEN = True
    except Exception:
        COOKIES_SEEN = True  # no banner: cookies were accepted earlier

async def goto(pg, url):
    resp = None
    try:
        resp = await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("  load:", str(e)[:100])
    await pg.wait_for_timeout(1500)
    await accept_cookies(pg)
    return resp

async def open_account(pg, url):
    """Open an account page; if you are not logged in, wait until you have logged in."""
    await goto(pg, url)
    if not is_login(pg.url):
        return
    await pg.bring_to_front()
    log("Not logged in: log in in the browser window (waiting up to 10 minutes)...")
    for _ in range(600):
        await pg.wait_for_timeout(1000)
        if not is_login(pg.url):
            log("Logged in.")
            break
    else:
        sys.exit("No login within 10 minutes; stopped.")
    await goto(pg, url)

# ---------------------------------------------------------------- orders

LIST_JS = """()=>[...document.querySelectorAll('a[href*="BESTID="]')].map(a=>{
  const v=(c)=>{const e=a.querySelector('.'+c+' .ovalue');return e?e.innerText.trim():null};
  const info=a.querySelector('.orderinfo');
  return {orderId:(a.href.match(/BESTID=(\\d+)/)||[])[1], date:v('orderdate'), totalText:v('ordervalue'),
          payment:v('orderpayment'),
          internetNo:info?((info.innerText.match(/Internet-Nr:\\s*(\\S+)/)||[])[1]||null):null,
          status:info?((info.querySelector('.odata')||{}).innerText||'').trim():'', detailUrl:a.href};
}).filter(o=>o.orderId)"""

DETAIL_JS = """()=>{
  const num=(s)=>{s=(s||'').replace(/[^\\d,.-]/g,'').replace(/\\./g,'').replace(',','.');return s?parseFloat(s):null};
  const shipments=[...document.querySelectorAll('#packages > li.pli')].map(p=>{
    const st=p.querySelector('.pstatus');
    return {status:st?(st.querySelector('p')||{}).innerText:null,
            delivery:st?((st.querySelector('.status')||{}).innerText||null):null,
            date:((p.querySelector('.left > span:not(.pstatus)')||{}).innerText||'').trim(),
            invoiceNo:((p.innerText.match(/Rechnungs-Nr\\.:\\s*(\\S+)/)||[])[1])||null,
            tracking:[...p.querySelectorAll('a[href]')].map(a=>a.href).filter(h=>/track|sendung|dhl|dpd|ups|gls|postnl/i.test(h))};
  });
  const lines=[...document.querySelectorAll('li.itemdata')].map(li=>{
    const row=li.parentElement, img=row.querySelector('li.picture img'), a=li.querySelector('a[href]');
    const artid=((img&&img.getAttribute('src')||'').match(/ARTID=(\\d+)/)||[])[1]||((a&&a.href||'').match(/-p?(\\d+)\\.html/)||[])[1]||null;
    return {articleNo:((li.querySelector('.dvartnr')||{}).innerText||'').trim(),
            title:((li.querySelector('.al_artinfo_link')||{}).innerText||'').trim(),
            url:a?a.href.replace(/[?&]+nbc=1$/,'').replace(/\\?&$/,''):null, artid,
            qty:parseInt(((li.querySelector('.additional')||{}).innerText||'').replace(/\\D/g,''))||null,
            unitPrice:num((li.querySelector('.unit_cost')||{}).innerText),
            total:num((li.querySelector('.total_price')||{}).innerText)};
  });
  const tot=document.querySelector('.summary .complete .value');
  return {shipments, lines, invoiceTotal:tot?num(tot.innerText):null};
}"""

def num(s):
    s = re.sub(r"[^\d,.-]", "", s or "").replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None

def load_orders():
    return json.load(open(ORDERS_JSON, encoding="utf-8")) if os.path.exists(ORDERS_JSON) else []

def pdate(s):
    try:
        return datetime.strptime(s, "%d.%m.%Y")
    except Exception:
        return datetime(1970, 1, 1)

def save_orders(orders):
    os.makedirs(OUT, exist_ok=True)
    orders.sort(key=lambda o: pdate(o["date"]), reverse=True)
    json.dump(orders, open(ORDERS_JSON, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

def line_key(l):
    return l.get("artid") or re.sub(r"[^\w.-]+", "_", l.get("articleNo") or l.get("title") or "unknown")[:80]

async def cmd_orders(b, full=False):
    known = {o["orderId"]: o for o in load_orders()}
    pg = await b.new_page()
    listed, start = [], 0
    for _ in range(100):
        await open_account(pg, ORDERS_URL + f"&START={start}")
        rows = [r for r in await pg.evaluate(LIST_JS) if r["orderId"] not in {x["orderId"] for x in listed}]
        if not rows:
            break
        listed += rows
        log(f"order list from {start}: {len(rows)} orders")
        if not full and known and all(r["orderId"] in known and known[r["orderId"]]["status"] == r["status"] for r in rows):
            log("Only known orders left; stopping (use --full to load everything).")
            break
        start += len(rows)

    new = 0
    for i, r in enumerate(listed):
        old = known.get(r["orderId"])
        if old and old["status"] == r["status"] and not full:
            continue
        await open_account(pg, r["detailUrl"])
        det = await pg.evaluate(DETAIL_JS)
        for l in det["lines"]:
            l["key"] = line_key(l)
        r.update(total=num(r.pop("totalText")), **det)
        new += old is None
        known[r["orderId"]] = r
        log(f"[{i+1}/{len(listed)}] order {r['orderId']} {r['date']}: {len(det['lines'])} lines, €{r['total']}")
        await pg.wait_for_timeout(random.randint(800, 2000))
    await pg.close()
    save_orders(list(known.values()))
    log(f"{len(known)} orders in orders.json ({new} new).")

# ---------------------------------------------------------------- products

PRODUCT_JS = """()=>{
  const o={};
  const h=document.querySelector('h1'); o.title=h?h.innerText.trim():null; o.docTitle=document.title;
  const sku=document.querySelector('[itemprop=sku]'); o.articleNo=sku?sku.innerText.trim():null;
  const br=document.querySelector('[itemprop=brand] [itemprop=name], meta[itemprop=brand]'); o.brand=br?(br.content||br.innerText).trim():null;
  const pr=document.querySelector('[itemprop=price]'); o.price=pr?parseFloat(pr.getAttribute('content')||pr.innerText.replace(',','.')):null;
  const gone=[...document.querySelectorAll('body *')].find(e=>e.children.length==0&&/nicht mehr lieferbar/i.test(e.textContent));
  o.discontinued=!!gone;
  const av=[...document.querySelectorAll('[class*=availability] *, .availability, [class*=delivery] *')].map(e=>e.children.length?'':e.textContent.trim()).find(t=>t&&t.length<80&&/lager|lieferbar|liefer|verfügbar|stock/i.test(t));
  o.availability=gone?gone.textContent.trim():(av||null);
  o.gallery=[...new Set([...document.querySelectorAll('img, a[href], [data-src]')].flatMap(e=>[e.src,e.href,e.dataset&&e.dataset.src])
     .filter(x=>x&&/\\/bilder\\/web\\/xxl_ws\\//.test(x)))];
  o.specs=[];
  for(const ul of document.querySelectorAll('.tabbed .tab2 ul.articleTechnicalData')){
    const g=ul.querySelector('.articleTechnicalHeadline'); if(g) o.specs.push([g.innerText.trim(), null]);
    const cells=[...ul.querySelectorAll('ul.articleAttribute > li')].map(li=>li.innerText.trim());
    for(let i=0;i+1<cells.length;i+=2) o.specs.push([cells[i], cells[i+1].replace(/\\n+/g,' · ')]);
  }
  o.docs=[...document.querySelectorAll('.tabbed .tab3 a[href]')].map(a=>({name:a.innerText.trim()||a.href.split('/').pop(), url:a.href}));
  const d=document.querySelector('.tabbed .tab1'); o.description=d?d.innerText.trim():null;
  o.descImages=d?[...d.querySelectorAll('img')].map(i=>i.src).filter(s=>s&&!/pdf_64|Fileextension/.test(s)):[];
  return o}"""

def products_from_orders(orders):
    prods = {}
    for o in orders:
        for l in o["lines"]:
            prods.setdefault(l["key"], []).append(l)
    return prods

def read_meta(key):
    try:
        return json.load(open(os.path.join(PROD, key, "meta.json"), encoding="utf-8"))
    except Exception:
        return None

def is_done(key):
    m = read_meta(key)
    return bool(m) and m.get("v") == META_VERSION

def available(m):
    return bool(m and m.get("pageTitle")) and not m.get("discontinued")

async def download(ctx, url, path):
    if os.path.exists(path):
        return True
    try:
        r = await ctx.request.get(url, timeout=60000)
        if r.ok:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(await r.body())
            return True
        log("  download failed:", url, r.status)
    except Exception as e:
        log("  download failed:", url, e)
    return False

async def scrape_product(b, pg, key, lines):
    from PIL import Image
    d = os.path.join(PROD, key)
    os.makedirs(d, exist_ok=True)
    url = next((l["url"] for l in lines if l.get("url")), None)
    info = {"gallery": [], "specs": [], "docs": [], "descImages": []}
    status = None
    if url:
        resp = await goto(pg, url)
        status = resp.status if resp else None
        info = await pg.evaluate(PRODUCT_JS)
        await pg.evaluate("window.scrollTo(0,0)")
        await pg.wait_for_timeout(500)
        png = await pg.screenshot(type="png", animations="disabled")
        Image.open(io.BytesIO(png)).convert("RGB").save(os.path.join(d, "screenshot.jpg"), quality=85)
    # Discontinued products answer with HTTP 410 but still show the full product page.
    found = bool(url) and status in (200, 410) and bool(info.get("title"))

    images = []
    for k, u in enumerate(info["gallery"], 1):
        ext = os.path.splitext(u.split("?")[0])[1].lower() or ".jpg"
        fn = f"img_{k:02d}{ext}"
        if await download(b.ctx, u, os.path.join(d, fn)):
            images.append(fn)

    docs = []
    for doc in {x["url"]: x for x in info["docs"]}.values():
        stem, ext = os.path.splitext(os.path.basename(doc["url"].split("?")[0]))
        fn = "docs/" + re.sub(r"[^\w.-]+", "_", stem)[:80] + (ext or ".pdf")
        if await download(b.ctx, doc["url"], os.path.join(d, fn)):
            docs.append({"name": doc["name"], "file": fn, "url": doc["url"]})

    desc_imgs = []
    for k, u in enumerate(dict.fromkeys(info["descImages"]), 1):
        ext = os.path.splitext(u.split("?")[0])[1].lower() or ".jpg"
        fn = f"description/desc_{k:02d}{ext}"
        if await download(b.ctx, u, os.path.join(d, fn)):
            desc_imgs.append(fn)

    meta = {"v": META_VERSION, "key": key, "url": url, "finalUrl": pg.url if found else None,
            "httpStatus": status, "pageTitle": info.get("title") if found else None,
            "docTitle": info.get("docTitle"), "articleNo": info.get("articleNo"), "brand": info.get("brand"),
            "discontinued": info.get("discontinued"), "availability": info.get("availability"),
            "price": info.get("price"), "images": images, "specs": info["specs"], "docs": docs,
            "tabs": ([{"name": "Description", "text": info.get("description") or "", "images": desc_imgs}]
                     if info.get("description") or desc_imgs else [])}
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
                state = "for sale" if available(m) else ("discontinued" if m.get("discontinued") else "page not found")
                log(f"[{i+1}/{len(todo)}] {key} {state}: {len(m['images'])} images, "
                    f"{sum(1 for s in m['specs'] if s[1] is not None)} specs, {len(m['docs'])} documents")
                break
            except Exception as e:
                log(f"[{i+1}/{len(todo)}] {key} ERROR: {str(e)[:120]}")
                try:
                    await pg.close()
                except Exception:
                    pass
                pg = await b.new_page()
        await pg.wait_for_timeout(random.randint(1000, 2500))
    await pg.close()

# ---------------------------------------------------------------- html

def money(x):
    return f"€{x:,.2f}" if isinstance(x, (int, float)) else ""

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
        links = "".join(f'<a href="{e(folder)}/{e(x["file"])}" target="_blank"><b>{e(os.path.splitext(x["file"])[1][1:].upper() or "FILE")}</b> {e(x["name"])}</a>'
                        for x in m["docs"])
        parts.append(f'<div class="docs">{links}</div>')
        label.append(f"{len(m['docs'])} document{'s' if len(m['docs']) > 1 else ''}")
    nimg = 0
    for t in m.get("tabs") or []:
        if t.get("text"):
            parts.append(f'<div class="desctext">{e(t["text"])}</div>')
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
        status = re.sub(r"\s+am\s.*$", "", o.get("status") or "")  # "Zugestellt am 01.04.2026 13:04" -> "Zugestellt"
        statuses.add(status)
        if not re.search(r"annul|storn|cancel", status, re.I):
            tot += o.get("total") or 0
        lines = []
        for l in o["lines"]:
            key = l["key"]
            m = read_meta(key)
            folder = f"products/{key}"
            absf = os.path.join(OUT, folder)
            if m and key not in seen:
                seen.add(key)
                nlive += available(m)
                ngone += not available(m)
            shot = f"{folder}/screenshot.jpg" if os.path.exists(os.path.join(absf, "screenshot.jpg")) else ""
            imgs = [f"{folder}/{x}" for x in (m or {}).get("images", [])]
            thumb = imgs[0] if imgs else (l.get("img") or "")
            gone = m is not None and not available(m)
            gal = "".join(f'<img loading="lazy" src="{e(i)}" data-full="{e(i)}" class="g" alt="">' for i in imgs)
            sub = f"Art. {e(l.get('articleNo') or '')} · " + (f"{e(m['brand'])} · " if m and m.get("brand") else "") + f'{money(l.get("unitPrice"))} × {l.get("qty")}'
            if m and m.get("availability") and not gone:
                sub += f' · now: {e(m["availability"])}'
            if gone:
                sub += ' · <span class="gone">no longer available</span>'
            det = details_html(folder, m) if m else ""
            thumb_html = (f'<img loading="lazy" class="thumb g" src="{e(thumb)}" data-full="{e(thumb)}" alt="">'
                          if thumb else '<div class="thumb"></div>')
            shot_html = (f'<img loading="lazy" class="shot g" src="{e(shot)}" data-full="{e(shot)}" alt="Product page screenshot">'
                         if shot else '<div class="shot none">no screenshot</div>')
            link = (m or {}).get("finalUrl") or l.get("url") or ""
            lines.append(f"""
      <div class="line">
        {thumb_html}
        <div class="info">
          <a class="title" href="{e(link)}" target="_blank" rel="noopener">{e(l.get('title') or l.get('articleNo') or '(no title)')}</a>
          <div class="sub">{sub}</div>
          <div class="gallery">{gal}</div>
          {det}
        </div>
        {shot_html}
      </div>""")
        links = "".join(f'<span class="oid">invoice {e(sh["invoiceNo"])}</span>' for sh in o.get("shipments") or [] if sh.get("invoiceNo"))
        links += "".join(f'<a class="oid" href="{e(u)}" target="_blank" rel="noopener">track</a>' for sh in o.get("shipments") or [] for u in sh.get("tracking") or [])
        search = " ".join([o["orderId"], o.get("internetNo") or ""] + [(l.get("title") or "") + " " + (l.get("articleNo") or "") for l in o["lines"]]).lower()
        cards.append(f"""
    <article class="order" data-year="{d.year}" data-status="{e(status)}" data-q="{e(search)}">
      <header>
        <span class="date">{d.strftime('%Y-%m-%d')}</span>
        <span class="status">{e(status)}</span>
        <span class="total">{money(o.get('total'))}</span>
        {links}
        <a class="oid" href="{e(o.get('detailUrl') or '')}" target="_blank" rel="noopener">#{e(o['orderId'])}</a>
      </header>{''.join(lines)}
    </article>""")

    year_opts = "".join(f'<option value="{y}">{y}</option>' for y in sorted(years, reverse=True))
    status_opts = "".join(f"<option>{e(s)}</option>" for s in sorted(statuses))
    nlines = sum(len(o["lines"]) for o in orders)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>reichelt orders</title>
<style>
:root{{--bg:#f5f6f8;--card:#fff;--fg:#16191d;--mut:#666e78;--line:#e1e4e8;--acc:#d9480f;--ok:#15803d;--bad:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#131518;--card:#1c1f23;--fg:#e9ebee;--mut:#99a1ab;--line:#30343a;--acc:#ff8a4c;--ok:#4ade80;--bad:#f87171}}}}
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
.desctext{{white-space:pre-wrap;color:var(--mut);font-size:13px;max-height:12em;overflow:auto;margin:4px 0 8px}}
.info{{min-width:0}}
.descimgs{{display:flex;flex-direction:column;gap:0;margin-top:8px;max-width:640px}}
.descimgs img{{width:100%;height:auto;background:#fff}}
#lb{{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;place-items:center;z-index:10;cursor:zoom-out}}
#lb img{{max-width:95vw;max-height:95vh;background:#fff}}
@media (max-width:760px){{.line{{grid-template-columns:56px 1fr}}.thumb{{width:56px;height:56px}}.shot{{grid-column:1/-1;width:100%}}.total{{margin-left:0}}}}
</style></head><body>
<div class="top"><div class="wrap">
<h1>reichelt orders</h1>
<div class="stats">{len(orders)} orders · {nlines} order lines · {len(seen)} unique products ({nlive} still available, {ngone} no longer available) · total €{tot:,.2f} · generated {datetime.now():%Y-%m-%d}</div>
<div class="filters">
<input id="q" type="search" placeholder="Search product, article number or order number…">
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
    log(f"overview-reichelt/index.html written: {len(orders)} orders, {len(seen)} products with data.")

# ---------------------------------------------------------------- main

async def cmd_login(b):
    pg = await b.new_page()
    await open_account(pg, ORDERS_URL)
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
    p = sub.add_parser("products", help="screenshot, images, specifications and documents")
    p.add_argument("--force", action="store_true", help="redo already processed products")
    p.add_argument("--only", nargs="+", metavar="ID")
    p.add_argument("--limit", type=int)
    asyncio.run(run(ap.parse_args()))

if __name__ == "__main__":
    main()
