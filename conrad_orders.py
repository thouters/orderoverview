#!/usr/bin/env python3
"""
Conrad order overview: fetches your orders from www.conrad.be, takes a screenshot
of each product page, downloads the product images, specifications, datasheets and
invoices, and builds overview-conrad/index.html.

Usage:
  python conrad_orders.py login       # open the browser to log in (again)
  python conrad_orders.py update      # new orders + missing products + html
  python conrad_orders.py orders [--full]
  python conrad_orders.py products [--force] [--only NUMBER ...] [--limit N]
  python conrad_orders.py html

Options for all commands that use the browser (put them before the command):
  --channel chrome|msedge       use the installed Google Chrome or Microsoft Edge instead of
                                Playwright's own Chromium (no `playwright install` needed).
  --cdp http://127.0.0.1:9333   attach to an already running Chromium (started with
                                --remote-debugging-port) instead of launching one.

The browser runs visibly with its own profile in ./.browser-profile-conrad (or
./.browser-profile-conrad-<channel>), so you stay logged in between runs.
"""
import argparse, asyncio, html, io, json, os, random, re, sys, time
from datetime import datetime

from playwright.async_api import async_playwright

# Working dir for the output and the browser profile; with `nix run` this is the current dir.
ROOT = os.environ.get("CONRAD_ORDERS_DIR") or os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "overview-conrad")
PROD = os.path.join(OUT, "products")
INVOICES = os.path.join(OUT, "invoices")
ORDERS_JSON = os.path.join(OUT, "orders.json")
CHANNELS = ("chrome", "msedge")
SITE = "https://www.conrad.be"
ACCOUNT_URL = SITE + "/nl/account.html"
ORDERS_URL = ACCOUNT_URL + "#/orders"
ORDER_URL = ACCOUNT_URL + "#/orders/{}"
PRODUCT_URL = SITE + "/nl/p/{}.html"  # redirects to the full product URL
VIEWPORT = {"width": 1400, "height": 1000}
META_VERSION = 1
DOC_LANGUAGES = ("nl", "en")  # datasheets and manuals often come in 20+ languages

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
        name = ".browser-profile-conrad" + (f"-{self.channel}" if self.channel else "")
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

async def needs_login(pg):
    if "accounts.conrad.com" in pg.url:
        return True
    try:
        return await pg.get_by_text("Inloggen vereist").count() > 0
    except Exception:
        return False

async def open_account(pg, url):
    """Open an account page; if you are not logged in, wait until you have logged in."""
    await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    await pg.wait_for_timeout(4000)
    if not await needs_login(pg):
        return
    await pg.bring_to_front()
    try:
        await pg.get_by_text("Nu inloggen").first.click(timeout=5000)
    except Exception:
        pass
    log("Not logged in: log in in the browser window (waiting up to 10 minutes)...")
    for _ in range(600):
        await pg.wait_for_timeout(1000)
        if "conrad.be" in pg.url and "accounts.conrad.com" not in pg.url and not await needs_login(pg):
            log("Logged in.")
            break
    else:
        sys.exit("No login within 10 minutes; stopped.")
    await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    await pg.wait_for_timeout(4000)

# ---------------------------------------------------------------- orders

def load_orders():
    return json.load(open(ORDERS_JSON, encoding="utf-8")) if os.path.exists(ORDERS_JSON) else []

def save_orders(orders):
    os.makedirs(OUT, exist_ok=True)
    orders.sort(key=lambda o: o["date"] or "", reverse=True)
    json.dump(orders, open(ORDERS_JSON, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

def short_number(item_number):
    return (item_number or "").lstrip("0") or item_number

class Catcher:
    """Collects JSON (and invoice PDF) responses of the Conrad API for one page."""

    def __init__(self, pg):
        self.pending, self.order_list, self.order, self.products, self.pdfs = set(), [], None, [], {}
        pg.on("response", lambda r: self.pending.add(asyncio.ensure_future(self.on(r))))

    async def on(self, r):
        u = r.url.split("?")[0]
        try:
            if "/order/" in u and u.endswith("/list"):
                self.order_list.append(json.loads(await r.text()))
            elif re.search(r"/order/\d+/[^/]+/\d+$", u):
                self.order = json.loads(await r.text())
            elif u.endswith("/productdetails"):
                self.products.append(json.loads(await r.text()))
            elif r.request.method == "GET" and re.search(r"/invoices/\d+/pdf$", u):
                self.pdfs[u.split("/")[-2]] = await r.body()
        except Exception as e:
            log("  could not read", u, str(e)[:80])

    async def settle(self):
        await asyncio.gather(*self.pending)
        self.pending.clear()

def extract_pdf(body):
    """Old invoices come wrapped in a multipart/form-data envelope; cut out the PDF."""
    a, z = body.find(b"%PDF"), body.rfind(b"%%EOF")
    return body[a:z + 5] if a >= 0 and z > a else None

def positions(order_json):
    """All order lines of an order (from its invoices and still open items)."""
    out = []
    for sub in order_json.get("orders") or []:
        for inv in sub.get("invoice") or []:
            for p in inv.get("position") or []:
                out.append((inv.get("invoiceNumber"), p))
        for p in (sub.get("openOrderItems") or {}).get("position") or []:
            out.append((None, p))
    return out

async def cmd_orders(b, full=False):
    known = {o["orderId"]: o for o in load_orders()}
    pg = await b.new_page()
    c = Catcher(pg)
    await open_account(pg, ORDERS_URL)
    await pg.select_option("#timePeriodProperty", "0")  # "Alle bestellingen"
    await pg.wait_for_timeout(5000)
    await c.settle()
    listed = []
    for page in range(2, 100):
        if not c.order_list:
            break
        last = c.order_list[-1]
        listed = [o for l in c.order_list for o in l.get("orderInformation") or []]
        pag = last.get("pagination") or {}
        if page > (pag.get("pageCount") or 1):
            break
        nxt = pg.get_by_role("button", name=str(page), exact=True)
        if not await nxt.count():
            break
        await nxt.first.click()
        await pg.wait_for_timeout(4000)
        await c.settle()
    listed = list({o["orderNumber"]: o for o in listed}.values())
    log(f"{len(listed)} orders in the account.")

    new = 0
    for i, lo in enumerate(listed):
        oid = lo["orderNumber"]
        old = known.get(oid)
        if old and old["status"] == lo["orderStatus"] and not full:
            continue
        opg = await b.new_page()
        oc = Catcher(opg)
        await open_account(opg, ORDER_URL.format(oid))
        await opg.wait_for_timeout(2000)
        await oc.settle()
        if not oc.order:
            log(f"[{i+1}/{len(listed)}] order {oid}: no order details received, skipped")
            await opg.close()
            continue
        # Product data the order page itself requests (titles, images, specs, documents).
        pd = {p["shortProductNumber"]: p for r in oc.products for p in r.get("productDetailPages") or []}
        names = await opg.evaluate("""()=>Object.fromEntries([...document.querySelectorAll('[data-e2e=ccpUserPosition]')].map(e=>{
            const m=(e.innerText.match(/Artikelnummer\\.?:\\s*(\\d+)/)||[])[1];
            const t=(e.querySelector('[data-e2e=ccpUserPosition-title]')||{}).innerText;
            return [m, (t||'').trim()]}).filter(x=>x[0]))""")
        track = await opg.evaluate("""()=>[...new Set([...document.querySelectorAll('a[href]')].map(a=>a.href).filter(h=>/track|trace|sendungsverfolgung/i.test(h)))]""")
        lines = []
        for inv_no, p in positions(oc.order):
            num = short_number(p.get("itemNumber"))
            prod = pd.get(num)
            title = (prod or {}).get("productShortInformation", {}).get("title") or p.get("productName") or names.get(num) or None
            lines.append({"number": num, "title": title, "qty": p.get("quantity"), "unitPrice": p.get("unitPrice"),
                          "total": p.get("totalPrice"), "invoice": inv_no, "url": PRODUCT_URL.format(num)})
            if prod:
                d = os.path.join(PROD, num)
                os.makedirs(d, exist_ok=True)
                json.dump(prod, open(os.path.join(d, "product.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

        # Invoices: clicking a download button makes the page fetch the PDF; keep that response.
        btns = opg.locator("button").filter(has_text=re.compile(r"^\s*Factuur( \d+)? downloaden\s*$"))
        nbtn = await btns.count()
        for attempt in range(3):
            for k in range(nbtn):
                try:
                    await btns.nth(k).click(timeout=5000)
                    await opg.wait_for_timeout(2500)
                except Exception as e:
                    log("  invoice button:", str(e)[:80])
            await oc.settle()
            good = {no: extract_pdf(body) for no, body in oc.pdfs.items()}
            if len([x for x in good.values() if x]) >= nbtn:
                break
        for pp in b.ctx.pages:  # the PDF opens in a blob: tab
            if pp.url.startswith("blob:"):
                await pp.close()
        invoices = []
        for no, body in oc.pdfs.items():
            body = extract_pdf(body)
            if not body:
                log(f"  invoice {no}: no PDF received")
                continue
            os.makedirs(INVOICES, exist_ok=True)
            fn = f"invoices/{oid}_{no}.pdf"
            with open(os.path.join(OUT, fn), "wb") as fh:
                fh.write(body)
            invoices.append(fn)

        subs = oc.order.get("orders") or []
        tracking = [{"carrier": (i.get("tracking") or {}).get("supplier"), "code": (i.get("tracking") or {}).get("identCode")}
                    for s in subs for i in s.get("invoice") or [] if (i.get("tracking") or {}).get("identCode")]
        new += old is None
        known[oid] = {
            "orderId": oid, "date": lo["orderDate"][:10], "status": lo["orderStatus"],
            "total": lo.get("orderValue"), "currency": lo.get("currency"),
            "seller": ", ".join(s["sellerInformation"]["name"] for s in lo.get("subOrder") or [] if s.get("sellerInformation")),
            "reference": ", ".join(filter(None, (s.get("customersOwnReference") for s in subs))),
            "shippingCost": oc.order.get("deliveryTypeCosts"), "detailUrl": ORDER_URL.format(oid),
            "tracking": tracking, "trackingUrls": track, "invoices": sorted(invoices), "lines": lines,
        }
        log(f"[{i+1}/{len(listed)}] order {oid} {lo['orderDate'][:10]}: {len(lines)} lines, "
            f"€{lo.get('orderValue')}, {len(invoices)} invoice(s)")
        await opg.close()
        await pg.wait_for_timeout(random.randint(500, 1500))
    await pg.close()
    save_orders(list(known.values()))
    log(f"{len(known)} orders in orders.json ({new} new).")

# ---------------------------------------------------------------- products

PAGE_JS = """()=>{
  const h=document.querySelector('h1');
  const av=[...document.querySelectorAll('[class*=vailab], [data-e2e*=vailab], [class*=delivery]')].map(e=>e.innerText.trim()).find(t=>t&&t.length<80);
  return {title:h?h.innerText.trim():null, docTitle:document.title, availability:av||null}}"""

def products_from_orders(orders):
    prods = {}
    for o in orders:
        for l in o["lines"]:
            prods.setdefault(l["number"], []).append(l)
    return prods

def read_json(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None

def read_meta(num):
    return read_json(os.path.join(PROD, num, "meta.json"))

def is_done(num):
    m = read_meta(num)
    return bool(m) and m.get("v") == META_VERSION

def available(m):
    return bool(m and m.get("pageTitle"))

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

def pick_docs(media):
    """Datasheets/manuals; of documents available in many languages keep only Dutch and English."""
    docs = [m for group in (media or {}).values() if isinstance(group, list) for m in group
            if isinstance(m, dict) and m.get("fullUrl")]
    by_type = {}
    for m in docs:
        by_type.setdefault(m.get("code") or m.get("typeName"), []).append(m)
    out = []
    for group in by_type.values():
        keep = [m for m in group if (m.get("language") or "").lower() in DOC_LANGUAGES] or group[:1]
        out += keep
    return list({m["fullUrl"]: m for m in out}.values())

async def scrape_product(b, pg, num):
    from PIL import Image
    d = os.path.join(PROD, num)
    os.makedirs(d, exist_ok=True)
    prod = read_json(os.path.join(d, "product.json")) or {}
    status = None
    try:
        resp = await pg.goto(PRODUCT_URL.format(num), wait_until="domcontentloaded", timeout=60000)
        status = resp.status if resp else None
    except Exception as e:
        log("  load:", str(e)[:100])
    await pg.wait_for_timeout(3000)
    page = await pg.evaluate(PAGE_JS)
    live = status == 200 and f"-{num}.html" in pg.url
    if live:
        await pg.evaluate("window.scrollTo(0,0)")
        png = await pg.screenshot(type="png", animations="disabled")
        Image.open(io.BytesIO(png)).convert("RGB").save(os.path.join(d, "screenshot.jpg"), quality=85)

    images = []
    for k, im in enumerate(dict.fromkeys(i["imageUrl"] for i in prod.get("productImages") or [] if i.get("imageUrl")), 1):
        ext = os.path.splitext(im.split("?")[0])[1].lower() or ".jpg"
        fn = f"img_{k:02d}{ext}"
        if await download(b.ctx, im, os.path.join(d, fn)):
            images.append(fn)

    docs = []
    for m in pick_docs(prod.get("productMedia")):
        stem, ext = os.path.splitext(os.path.basename(m["fullUrl"].split("?")[0]))
        base = re.sub(r"[^\w.-]+", "_", stem)[:80] + (ext or ".pdf")
        lang = (m.get("language") or "xx").lower()
        fn = f"docs/{lang}_{base}"
        if await download(b.ctx, m["fullUrl"], os.path.join(d, fn)):
            docs.append({"name": f"{m.get('typeName') or 'Document'} ({(m.get('language') or '?').upper()})",
                         "file": fn, "url": m["fullUrl"]})

    full = prod.get("productFullInformation") or {}
    def spec_value(v):
        unit = v.get("unit")
        unit = unit.get("name") if isinstance(unit, dict) else unit
        return (v.get("value") or "") + (" " + unit if unit else "")
    specs = [[a.get("attributeName"), " · ".join(spec_value(v) for v in a.get("values") or [])]
             for a in full.get("technicalAttributes") or []]
    text = "\n\n".join(filter(None, [full.get("headline"), full.get("description"),
                                     "\n".join("• " + f for f in full.get("features") or []),
                                     "\n".join(full.get("specialAdd") or [])]))
    meta = {"v": META_VERSION, "number": num, "url": PRODUCT_URL.format(num), "finalUrl": pg.url,
            "httpStatus": status, "pageTitle": page["title"] if live else None, "docTitle": page["docTitle"],
            "apiTitle": (prod.get("productShortInformation") or {}).get("title"),
            "brand": (prod.get("brand") or {}).get("displayName"),
            "manufacturerNumber": (full.get("manufacturer") or {}).get("id"), "ean": full.get("eanCode"),
            "availability": page["availability"] if live else None,
            "images": images, "specs": specs, "docs": docs,
            "tabs": [{"name": "Description", "text": text, "images": []}] if text else []}
    json.dump(meta, open(os.path.join(d, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return meta

async def cmd_products(b, force=False, only=None, limit=None):
    prods = products_from_orders(load_orders())
    todo = [p for p in (only or prods) if p in prods and (force or not is_done(p))]
    if limit:
        todo = todo[:limit]
    log(f"{len(todo)} of {len(prods)} products to do.")
    pg = await b.new_page()
    for i, num in enumerate(todo):
        if i and i % 25 == 0:  # fresh tab to avoid memory growth / crashes
            await pg.close()
            pg = await b.new_page()
        for attempt in range(2):
            try:
                m = await scrape_product(b, pg, num)
                state = "for sale" if available(m) else "no longer available"
                log(f"[{i+1}/{len(todo)}] {num} {state}: {len(m['images'])} images, "
                    f"{len(m['specs'])} specs, {len(m['docs'])} documents")
                break
            except Exception as e:
                log(f"[{i+1}/{len(todo)}] {num} ERROR: {str(e)[:120]}")
                try:
                    await pg.close()
                except Exception:
                    pass
                pg = await b.new_page()
        await pg.wait_for_timeout(random.randint(1000, 2500))
    await pg.close()

# ---------------------------------------------------------------- html

STATUS = {"SENT": "Shipped", "DELIVERED": "Delivered", "OPEN": "Open", "IN_PROCESS": "In progress",
          "CANCELLED": "Cancelled", "CANCELED": "Cancelled", "RETURNED": "Returned"}

def money(x):
    return f"€{x:,.2f}" if isinstance(x, (int, float)) else ""

def details_html(folder, m):
    e = html.escape
    parts, label = [], []
    if m.get("specs"):
        rows = "".join(f"<tr><td>{e(k or '')}</td><td>{e(v or '')}</td></tr>" for k, v in m["specs"])
        parts.append(f'<table class="specs">{rows}</table>')
        label.append(f"{len(m['specs'])} specs")
    if m.get("docs"):
        links = "".join(f'<a href="{e(folder)}/{e(x["file"])}" target="_blank"><b>PDF</b> {e(x["name"])}</a>' for x in m["docs"])
        parts.append(f'<div class="docs">{links}</div>')
        label.append(f"{len(m['docs'])} document{'s' if len(m['docs']) > 1 else ''}")
    for t in m.get("tabs") or []:
        if t.get("text"):
            parts.append(f'<div class="desctext">{e(t["text"])}</div>')
    if not parts:
        return ""
    return f'<details class="det"><summary>Details: {", ".join(label) or "description"}</summary>{"".join(parts)}</details>'

def cmd_html():
    orders = sorted(load_orders(), key=lambda o: o["date"] or "", reverse=True)
    e = html.escape
    cards, years, statuses, seen = [], set(), set(), set()
    tot, nlive, ngone = 0.0, 0, 0
    for o in orders:
        year = (o["date"] or "")[:4]
        years.add(year)
        status = STATUS.get(o["status"], (o["status"] or "").title())
        statuses.add(status)
        if status != "Cancelled":
            tot += o.get("total") or 0
        lines = []
        for l in o["lines"]:
            num = l["number"]
            m = read_meta(num)
            folder = f"products/{num}"
            absf = os.path.join(OUT, folder)
            if m and num not in seen:
                seen.add(num)
                nlive += available(m)
                ngone += not available(m)
            shot = f"{folder}/screenshot.jpg" if os.path.exists(os.path.join(absf, "screenshot.jpg")) else ""
            imgs = [f"{folder}/{x}" for x in (m or {}).get("images", [])]
            thumb = imgs[0] if imgs else ""
            gone = m is not None and not available(m)
            title = l.get("title") or (m or {}).get("apiTitle") or f"Conrad article {num}"
            gal = "".join(f'<img loading="lazy" src="{e(i)}" data-full="{e(i)}" class="g" alt="">' for i in imgs)
            sub = f"Art. {e(num)} · " + (f"{e(m['brand'])} · " if m and m.get("brand") else "")
            qty = l.get("qty")
            # Unit prices of old orders are unreliable in Conrad's data; the line total is correct.
            sub += f'{int(qty) if isinstance(qty, float) and qty.is_integer() else qty} pcs · {money(l.get("total"))}'
            if m and m.get("availability") and not gone:
                sub += f' · now: {e(m["availability"])}'
            if gone:
                sub += ' · <span class="gone">no longer available</span>'
            det = details_html(folder, m) if m else ""
            thumb_html = (f'<img loading="lazy" class="thumb g" src="{e(thumb)}" data-full="{e(thumb)}" alt="">'
                          if thumb else '<div class="thumb"></div>')
            shot_html = (f'<img loading="lazy" class="shot g" src="{e(shot)}" data-full="{e(shot)}" alt="Product page screenshot">'
                         if shot else '<div class="shot none">no screenshot</div>')
            link = (m or {}).get("finalUrl") if m and available(m) else l.get("url")
            lines.append(f"""
      <div class="line">
        {thumb_html}
        <div class="info">
          <a class="title" href="{e(link or '')}" target="_blank" rel="noopener">{e(title)}</a>
          <div class="sub">{sub}</div>
          <div class="gallery">{gal}</div>
          {det}
        </div>
        {shot_html}
      </div>""")
        links = "".join(f'<a class="oid" href="{e(f)}" target="_blank">invoice{" " + str(k + 1) if len(o["invoices"]) > 1 else ""}</a>'
                        for k, f in enumerate(o.get("invoices") or []))
        links += "".join(f'<a class="oid" href="{e(u)}" target="_blank" rel="noopener">track</a>' for u in o.get("trackingUrls") or [])
        search = " ".join([o["orderId"]] + [(l.get("title") or "") + " " + l["number"] for l in o["lines"]]).lower()
        cards.append(f"""
    <article class="order" data-year="{e(year)}" data-status="{e(status)}" data-q="{e(search)}">
      <header>
        <span class="date">{e(o['date'] or '')}</span>
        <span class="status">{e(status)}</span>
        <span class="store">{e(o.get('seller') or '')}</span>
        <span class="total">{money(o.get('total'))}</span>
        {links}
        <a class="oid" href="{e(o['detailUrl'])}" target="_blank" rel="noopener">#{e(o['orderId'])}</a>
      </header>{''.join(lines)}
    </article>""")

    year_opts = "".join(f'<option value="{e(y)}">{e(y)}</option>' for y in sorted(years, reverse=True))
    status_opts = "".join(f"<option>{e(s)}</option>" for s in sorted(statuses))
    nlines = sum(len(o["lines"]) for o in orders)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Conrad orders</title>
<style>
:root{{--bg:#f4f6f9;--card:#fff;--fg:#14181f;--mut:#646d7a;--line:#e0e4ea;--acc:#2c5fd9;--ok:#15803d;--bad:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151a;--card:#1b1f26;--fg:#e8ebf0;--mut:#97a0ad;--line:#2e3440;--acc:#7aa2ff;--ok:#4ade80;--bad:#f87171}}}}
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
.docs a{{display:inline-block;margin:4px 8px 4px 0;padding:3px 10px;border:1px solid var(--line);border-radius:99px;color:var(--fg);text-decoration:none;font-size:13px}}
.desctext{{white-space:pre-wrap;color:var(--mut);font-size:13px;max-height:12em;overflow:auto;margin:4px 0 8px}}
.info{{min-width:0}}
#lb{{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;place-items:center;z-index:10;cursor:zoom-out}}
#lb img{{max-width:95vw;max-height:95vh;background:#fff}}
@media (max-width:760px){{.line{{grid-template-columns:56px 1fr}}.thumb{{width:56px;height:56px}}.shot{{grid-column:1/-1;width:100%}}.total{{margin-left:0}}}}
</style></head><body>
<div class="top"><div class="wrap">
<h1>Conrad orders</h1>
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
    log(f"overview-conrad/index.html written: {len(orders)} orders, {len(seen)} products with data.")

# ---------------------------------------------------------------- main

async def cmd_login(b):
    pg = await b.new_page()
    await open_account(pg, ACCOUNT_URL)
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
    p.add_argument("--only", nargs="+", metavar="NUMBER")
    p.add_argument("--limit", type=int)
    asyncio.run(run(ap.parse_args()))

if __name__ == "__main__":
    main()
