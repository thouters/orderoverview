#!/usr/bin/env python3
"""Export the order overviews of all shops to a folder of Markdown notes.

The data folders (overview/, overview-tinytronics/, ...) are copied to DEST/<shop>/ and a
Markdown note is written next to the data for every shop, order and product, with wiki links
and image embeds that work in Obsidian (and other tools that read an Obsidian vault).

    python export_notes.py ~/notes/aankopen

All links use full paths from the vault root, so DEST must be inside the vault; the vault root
is found by looking for a `.obsidian` folder above DEST, or given with --vault.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent

# slug, name, default data folder, env var that overrides the folder, shop URL
SHOPS = [
    ("aliexpress", "AliExpress", "overview", "ALIEXPRESS_ORDERS_DIR", "https://www.aliexpress.com"),
    ("tinytronics", "TinyTronics", "overview-tinytronics", "TINYTRONICS_ORDERS_DIR", "https://www.tinytronics.nl"),
    ("conrad", "Conrad", "overview-conrad", "CONRAD_ORDERS_DIR", "https://www.conrad.be"),
    ("automation24", "Automation24", "overview-automation24", "AUTOMATION24_ORDERS_DIR", "https://www.automation24.be"),
    ("reichelt", "reichelt", "overview-reichelt", "REICHELT_ORDERS_DIR", "https://www.reichelt.de"),
    ("solyd", "Solyd", "overview-solyd", "SOLYD_ORDERS_DIR", "https://www.solyd.be"),
]

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}
THUMB = 80


# ---------------------------------------------------------------- helpers

def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def money(v):
    """Amount as float from a number or a text like '€1,234.56', '€ 51,65' or '388.1'"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.\-]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def eur(v):
    return "" if v is None else f"€{v:,.2f}"


# orders with such a status were never paid (or were refunded) and are left out of the totals
CANCELLED = re.compile(r"cancel|expired|closed|refund|annul|storn", re.I)


def currency(v):
    """'$' for a US dollar amount (old AliExpress orders), else '€'"""
    return "$" if "$" in str(v or "") else "€"


def fmt(v, cur="€"):
    return "" if v is None else f"{cur}{v:,.2f}"


def fmt_sums(sums, decimals=2):
    """{'€': 12.5, '$': 3} as '€12.50 + $3.00'"""
    parts = [f"{c}{v:,.{decimals}f}" for c, v in sorted(sums.items(), key=lambda cv: cv[0] != "€") if v]
    return " + ".join(parts) or f"€{0:.{decimals}f}"


def line_amount(line):
    """(amount, currency) of an order line: its total, or else unit price × quantity"""
    if line.get("total") not in (None, ""):
        return money(line["total"]), currency(line["total"])
    price = line.get("unitPrice") if line.get("unitPrice") not in (None, "") else line.get("price")
    v = money(price)
    return (None if v is None else v * float(line.get("qty") or 1)), currency(price)


def unit_price(line):
    price = line.get("unitPrice") if line.get("unitPrice") not in (None, "") else line.get("price")
    return money(price), currency(price)


DATE_FORMATS = ["%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%b %d, %Y", "%d %b %Y", "%d/%m/%Y"]


def iso_date(s):
    s = str(s or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    return m.group(0) if m else ""


def cell(s):
    """Text that is safe inside a Markdown table cell"""
    return re.sub(r"\s+", " ", str(s or "")).replace("|", "\\|").strip()


def text_line(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def yaml_str(s):
    return json.dumps(text_line(s), ensure_ascii=False)


class Links:
    """Wiki links with paths from the vault root"""

    def __init__(self, vault, dest):
        self.base = dest.resolve().relative_to(vault.resolve()).as_posix()

    def path(self, *parts):
        return "/".join(p for p in (self.base, *parts) if p)

    def link(self, target, alias=None, table=False):
        sep = "\\|" if table else "|"
        return f"[[{target}{sep}{cell(alias) if table else text_line(alias)}]]" if alias else f"[[{target}]]"

    def embed(self, target, width=None, table=False):
        sep = "\\|" if table else "|"
        return f"![[{target}{sep}{width}]]" if width else f"![[{target}]]"


# ---------------------------------------------------------------- normalised data

def line_key(line):
    for k in ("key", "productId", "sku", "number", "articleId"):
        if line.get(k):
            return str(line[k])
    return ""


def order_documents(order):
    docs = []
    if order.get("invoice"):
        docs.append(("Invoice", order["invoice"]))
    for f in order.get("invoices") or []:
        docs.append(("Invoice", f))
    for d in order.get("documents") or []:
        if d.get("file"):
            label = d.get("label") or "Document"
            docs.append((f"{label} {d['number']}" if d.get("number") else label, d["file"]))
    return docs


def order_facts(order):
    facts = []
    for key, label in (("status", "Status"), ("store", "Store"), ("seller", "Seller"), ("payment", "Payment"),
                       ("shipping", "Shipping"), ("reference", "Reference"), ("invoiceNo", "Invoice")):
        if order.get(key):
            facts.append((label, text_line(order[key])))
    if order.get("shippingCost") not in (None, ""):
        facts.append(("Shipping cost", eur(money(order["shippingCost"]))))
    codes = []
    for t in order.get("tracking") or []:
        if isinstance(t, dict):
            code = t.get("code") or t.get("Tracking number") or ""
            carrier = t.get("carrier") or t.get("Carrier") or ""
            codes.append(f"{carrier} {code}".strip())
    if codes:
        facts.append(("Tracking", ", ".join(codes)))
    for s in order.get("shipments") or []:
        if isinstance(s, dict):
            facts.append(("Shipment", " ".join(text_line(v) for v in s.values() if isinstance(v, str) and v)))
    return facts


class Product:
    def __init__(self, folder, key):
        self.key = key
        self.dir = folder / "products" / key
        self.meta = load(self.dir / "meta.json", {}) or {}
        details = load(self.dir / "details.json", {}) or {}
        self.specs = self.meta.get("specs") or details.get("specs") or []
        self.docs = [d for d in (self.meta.get("docs") or details.get("docs") or []) if d.get("file")]
        self.tabs = list(self.meta.get("tabs") or [])
        if details.get("descriptionText"):
            self.tabs.append({"name": "Description", "text": details["descriptionText"]})
        self.title = (self.meta.get("pageTitle") or self.meta.get("apiTitle") or "").strip()
        self.url = self.meta.get("finalUrl") or self.meta.get("url") or ""
        if "/error/" in self.url or self.meta.get("httpStatus") == 404:
            self.url = self.meta.get("url") or ""

    def exists(self):
        return self.dir.is_dir()

    def files(self, pattern):
        return sorted(p for p in self.dir.glob(pattern) if p.suffix.lower() in IMAGE_EXT)

    def images(self):
        named = [self.dir / f for f in self.meta.get("images") or [] if (self.dir / f).is_file()]
        return named or self.files("img_*")

    def thumbnail(self):
        imgs = self.files("order_*") + self.images()
        if imgs:
            return imgs[0]
        shot = self.dir / "screenshot.jpg"
        return shot if shot.is_file() else None


# ---------------------------------------------------------------- export

class ShopExport:
    def __init__(self, slug, name, src, url, dest, links):
        self.slug, self.name, self.src, self.url = slug, name, src, url
        self.dest = dest / slug
        self.links = links
        raw = load(src / "orders.json", [])
        orders = raw.get("orders", []) if isinstance(raw, dict) else raw
        if isinstance(orders, dict):
            orders = list(orders.values())
        self.orders = []
        for o in orders:
            o = dict(o)
            o["_date"] = iso_date(o.get("date"))
            o["_total"], o["_cur"], o["_estimate"] = money(o.get("total")), currency(o.get("total")), False
            if o["_total"] is None:
                # old AliExpress orders have no order total: the item prices, without shipping
                amounts = [line_amount(l) for l in o.get("lines") or []]
                o["_total"] = sum(a or 0 for a, _ in amounts)
                o["_cur"] = amounts[0][1] if amounts else "€"
                o["_estimate"] = True
            o["_counts"] = not CANCELLED.search(str(o.get("status") or ""))
            self.orders.append(o)
        self.orders.sort(key=lambda o: (o["_date"], str(o.get("orderId"))), reverse=True)
        self.products = {}

    def p(self, *parts):
        return self.links.path(self.slug, *parts)

    def sums(self, orders=None):
        """Totals per currency of the orders that count (not cancelled or expired)"""
        out = defaultdict(float)
        for o in self.orders if orders is None else orders:
            if o["_counts"]:
                out[o["_cur"]] += o["_total"] or 0
        return out

    @staticmethod
    def order_total(o):
        s = fmt(o["_total"], o["_cur"]) + ("*" if o["_estimate"] else "")
        return s if o["_counts"] else f"~~{s}~~"

    @staticmethod
    def spent(uses):
        out = defaultdict(float)
        for o, line in uses:
            if o["_counts"]:
                v, cur = line_amount(line)
                out[cur] += v or 0
        return out

    def order_note(self, order):
        return f"orders/{order['_date'] or 'undated'} {order.get('orderId')}"

    def product(self, key):
        if key not in self.products:
            self.products[key] = Product(self.dest, key)
        return self.products[key]

    def product_link(self, key, title, table=False):
        return self.links.link(self.p("products", key, key), title, table)

    def thumb(self, prod, width=THUMB):
        t = prod.thumbnail()
        return self.links.embed(self.p(t.relative_to(self.dest).as_posix()), width, table=True) if t else ""

    def copy_data(self):
        self.dest.mkdir(parents=True, exist_ok=True)
        if shutil.which("rsync"):
            subprocess.run(["rsync", "-a", "--exclude", "index.html", f"{self.src}/", f"{self.dest}/"], check=True)
        else:
            shutil.copytree(self.src, self.dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("index.html"))

    def write(self, rel, text):
        text = "\n".join(line.rstrip() for line in text.split("\n"))
        path = self.dest / f"{rel}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old != text:
            path.write_text(text, encoding="utf-8")

    def export(self):
        self.copy_data()
        product_orders = defaultdict(list)
        for o in self.orders:
            for line in o.get("lines") or []:
                key = line_key(line)
                if key:
                    product_orders[key].append((o, line))
        for o in self.orders:
            self.write(self.order_note(o), self.order_md(o))
        for key, uses in product_orders.items():
            self.write(f"products/{key}/{key}", self.product_md(key, uses))
        self.write(self.slug, self.shop_md(product_orders))
        return product_orders

    # -- notes

    def order_md(self, o):
        lines = o.get("lines") or []
        out = ["---", f"shop: {self.name}", f"order: {yaml_str(o.get('orderId'))}", f"date: {o['_date']}",
               f"total: {o['_total']:.2f}", f"currency: {'USD' if o['_cur'] == '$' else 'EUR'}",
               f"counted: {'true' if o['_counts'] else 'false'}", "tags: [aankoop]", "---", "",
               f"# {self.name} order {o.get('orderId')}", "",
               f"{self.links.link(self.p(self.slug), self.name)} · {o['_date']} · **{self.order_total(o)}**", ""]
        if o["_estimate"]:
            out += ["\\* No order total known: the sum of the item prices, without shipping.", ""]
        if not o["_counts"]:
            out += ["Not counted in the totals: this order was cancelled or expired.", ""]
        for label, value in order_facts(o):
            out.append(f"- {label}: {value}")
        if o.get("detailUrl"):
            out.append(f"- [Order on the website]({o['detailUrl']})")
        docs = order_documents(o)
        pictures = [(label, f) for label, f in docs if Path(f).suffix.lower() in IMAGE_EXT]
        for label, f in docs:
            if (label, f) not in pictures:
                out.append(f"- {label}: {self.links.link(self.p(f), Path(f).name)}")
        out += ["", "| | Product | Qty | Price | Total |", "|---|---|---:|---:|---:|"]
        for line in lines:
            key = line_key(line)
            prod = self.product(key) if key else None
            title = line.get("title") or (prod.title if prod else "") or key
            if line.get("sku") and self.slug == "aliexpress":
                title = f"{title} ({line['sku']})"
            name = self.product_link(key, title, table=True) if key else cell(title)
            unit, ucur = unit_price(line)
            total, tcur = line_amount(line)
            qty = line.get("qty")
            qty = int(qty) if isinstance(qty, float) and qty.is_integer() else qty
            out.append(f"| {self.thumb(prod) if prod else ''} | {name} | {qty if qty is not None else ''} | "
                       f"{fmt(unit, ucur)} | {fmt(total, tcur)} |")
        for label, f in pictures:
            out += ["", f"## {label}", "", self.links.embed(self.p(f), 700)]
        return "\n".join(out) + "\n"

    def product_md(self, key, uses):
        prod = self.product(key)
        m = prod.meta
        first_line = uses[0][1]
        title = prod.title or first_line.get("title") or key
        spent = self.spent(uses)
        out = ["---", f"title: {yaml_str(title)}", f"shop: {self.name}", f"key: {yaml_str(key)}"]
        for k in ("brand", "sku", "articleNo", "ean", "manufacturerNumber"):
            if m.get(k):
                out.append(f"{k}: {yaml_str(m[k])}")
        out += [f"spent: {yaml_str(fmt_sums(spent))}", "tags: [aankoop]", "---", "", f"# {text_line(title)}", ""]
        facts = [self.links.link(self.p(self.slug), self.name)]
        if prod.url:
            facts.append(f"[product page]({prod.url})")
        out.append(" · ".join(facts))
        out.append("")
        if m.get("availability"):
            out.append(f"- Availability: {text_line(m['availability'])}")
        if m.get("discontinued"):
            out.append("- Discontinued")
        if m.get("price") not in (None, ""):
            out.append(f"- Current price: {eur(money(m['price']))}")
        if not prod.title and not prod.images():
            out.append("- No longer found in the shop")
        out += ["", "## Orders", "", "| Date | Order | Qty | Total |", "|---|---|---:|---:|"]
        for o, line in uses:
            qty = line.get("qty")
            qty = int(qty) if isinstance(qty, float) and qty.is_integer() else qty
            variant = f" ({cell(line['sku'])})" if self.slug == "aliexpress" and line.get("sku") else ""
            out.append(f"| {o['_date']} | {self.links.link(self.p(self.order_note(o)), str(o.get('orderId')), True)}"
                       f"{variant} | {qty if qty is not None else ''} | "
                       f"{fmt(*line_amount(line)) if o['_counts'] else '~~' + fmt(*line_amount(line)) + '~~'} |")
        imgs = prod.files("order_*") + prod.images()
        if imgs:
            out += ["", "## Images", ""]
            out += [self.links.embed(self.p(i.relative_to(self.dest).as_posix()), 300) for i in imgs]
        if prod.specs:
            out += ["", "## Specifications", "", "| | |", "|---|---|"]
            for row in prod.specs:
                if not row:
                    continue
                k, v = (row + [None])[:2] if isinstance(row, list) else (row, None)
                out.append(f"| **{cell(k)}** | |" if v is None else f"| {cell(k)} | {cell(v)} |")
        if prod.docs:
            out += ["", "## Documents", ""]
            for d in prod.docs:
                if (prod.dir / d["file"]).is_file():
                    out.append(f"- {self.links.link(self.p('products', key, d['file']), d.get('name') or Path(d['file']).name)}")
        for tab in prod.tabs:
            text = (tab.get("text") or "").strip()
            if text:
                out += ["", f"## {text_line(tab.get('name') or 'Description')}", "", re.sub(r"\n{3,}", "\n\n", text)]
        desc = prod.files("description/*")
        if desc:
            out += ["", "## Description images", ""]
            out += [self.links.embed(self.p(i.relative_to(self.dest).as_posix()), 500) for i in desc]
        if (prod.dir / "screenshot.jpg").is_file():
            out += ["", "## Product page", "", self.links.embed(self.p("products", key, "screenshot.jpg"), 700)]
        return "\n".join(out) + "\n"

    def shop_md(self, product_orders):
        total = self.sums()
        years = defaultdict(list)
        for o in self.orders:
            years[o["_date"][:4] or "?"].append(o)
        out = ["---", f"shop: {self.name}", f"orders: {len(self.orders)}", f"total: {yaml_str(fmt_sums(total))}",
               "tags: [aankoop]", "---", "", f"# {self.name}", "", f"[{self.url}]({self.url}) · {len(self.orders)} orders · "
               f"{len(product_orders)} products · **{fmt_sums(total)}**", ""]
        out += totals_note([self])
        out += ["| Year | Orders | Total |", "|---|---:|---:|"]
        for y in sorted(years, reverse=True):
            out.append(f"| {y} | {len(years[y])} | {fmt_sums(self.sums(years[y]))} |")
        out += ["", "## Orders", "", "| Date | Order | Items | Status | Total |", "|---|---|---:|---|---:|"]
        for o in self.orders:
            out.append(f"| {o['_date']} | {self.links.link(self.p(self.order_note(o)), str(o.get('orderId')), True)} | "
                       f"{len(o.get('lines') or [])} | {cell(o.get('status'))} | {self.order_total(o)} |")
        out += ["", "## Products", "", "| | Product | Orders | Spent |", "|---|---|---:|---:|"]

        for key, uses in sorted(product_orders.items(), key=lambda kv: -sum(self.spent(kv[1]).values())):
            prod = self.product(key)
            title = prod.title or uses[0][1].get("title") or key
            out.append(f"| {self.thumb(prod)} | {self.product_link(key, title, True)} | {len(uses)} | "
                       f"{fmt_sums(self.spent(uses))} |")
        return "\n".join(out) + "\n"


def totals_note(exports):
    """Explains the totals, when some orders are left out or have no order total"""
    left_out = sum(1 for e in exports for o in e.orders if not o["_counts"])
    estimated = sum(1 for e in exports for o in e.orders if o["_estimate"] and o["_counts"])
    out = []
    if left_out:
        out.append(f"Totals leave out the {left_out} cancelled or expired orders (struck through).")
    if estimated:
        out.append(f"{estimated} old orders have no order total (marked *): they count with the sum of their "
                   "item prices, without shipping, in the currency shown ($ for US dollars).")
    return [" ".join(out), ""] if out else []


def index_md(exports, links, title):
    out = ["---", "tags: [aankoop]", "---", "", f"# {title}", ""]
    out += totals_note([e for e, _ in exports])
    out += ["| Shop | Orders | Products | First | Last | Total |", "|---|---:|---:|---|---|---:|"]
    years = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    grand = defaultdict(float)
    for e, products in exports:
        total = e.sums()
        for c, v in total.items():
            grand[c] += v
        dates = sorted(o["_date"] for o in e.orders if o["_date"])
        out.append(f"| {links.link(e.p(e.slug), e.name, True)} | {len(e.orders)} | {len(products)} | "
                   f"{dates[0] if dates else ''} | {dates[-1] if dates else ''} | {fmt_sums(total)} |")
        for o in e.orders:
            if o["_counts"]:
                years[o["_date"][:4] or "?"][e.name][o["_cur"]] += o["_total"] or 0
    out.append(f"| **Total** | | | | | **{fmt_sums(grand)}** |")
    names = [e.name for e, _ in exports]
    out += ["", "## Per year", "", "| Year | " + " | ".join(names) + " | Total |",
            "|---|" + "---:|" * (len(names) + 1)]
    for y in sorted(years, reverse=True):
        row = years[y]
        year_total = defaultdict(float)
        for sums in row.values():
            for c, v in sums.items():
                year_total[c] += v
        out.append(f"| {y} | " + " | ".join(fmt_sums(row[n], 0) if n in row else "" for n in names) +
                   f" | {fmt_sums(year_total, 0)} |")
    return "\n".join(out) + "\n"


def find_vault(dest):
    for d in [dest, *dest.parents]:
        if (d / ".obsidian").is_dir():
            return d
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dest", type=Path, help="folder inside the vault to export to, e.g. ~/notes/aankopen")
    ap.add_argument("--vault", type=Path, help="vault root (default: the folder above DEST with .obsidian)")
    ap.add_argument("--shops", nargs="+", choices=[s[0] for s in SHOPS], help="only these shops")
    ap.add_argument("--title", default="Purchases", help="title of the index note")
    args = ap.parse_args()

    dest = args.dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    vault = (args.vault.expanduser().resolve() if args.vault else None) or find_vault(dest) or dest.parent
    links = Links(vault, dest)
    exports = []
    for slug, name, folder, env, url in SHOPS:
        if args.shops and slug not in args.shops:
            continue
        src = Path(os.environ.get(env) or HERE / folder)
        if not (src / "orders.json").is_file():
            print(f"{name}: no data in {src}, skipped")
            continue
        e = ShopExport(slug, name, src, url, dest, links)
        products = e.export()
        exports.append((e, products))
        print(f"{name}: {len(e.orders)} orders, {len(products)} products")
    if not exports:
        sys.exit("nothing exported")
    index = dest / f"{dest.name}.md"
    text = index_md(exports, links, args.title)
    if not index.exists() or index.read_text(encoding="utf-8") != text:
        index.write_text(text, encoding="utf-8")
    print(f"Written to {dest} (vault {vault}); open {index.relative_to(vault)}")


if __name__ == "__main__":
    main()
