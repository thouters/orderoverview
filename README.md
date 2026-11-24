# Order overview

Two scripts that build a local HTML overview of your web shop orders:

- `aliexpress_orders.py` fetches all your AliExpress orders and builds `overview/index.html`:
  the products of every order, a screenshot of each product page, the product images and,
  for products still for sale, the specifications, PDF documents and description images.
- `tinytronics_orders.py` does the same for [TinyTronics](https://www.tinytronics.nl) and
  builds `overview-tinytronics/index.html`; see [TinyTronics](#tinytronics) below.
- `conrad_orders.py` does the same for [Conrad](https://www.conrad.be) and builds
  `overview-conrad/index.html`; see [Conrad](#conrad) below.
- `automation24_orders.py` does the same for [Automation24](https://www.automation24.be) and
  builds `overview-automation24/index.html`; see [Automation24](#automation24) below.
- `reichelt_orders.py` does the same for [reichelt elektronik](https://www.reichelt.de) and
  builds `overview-reichelt/index.html`; see [reichelt](#reichelt) below.
- `solyd_orders.py` does the same for [Solyd](https://www.solyd.be) and builds
  `overview-solyd/index.html`; see [Solyd](#solyd) below.
- `export_notes.py` copies all of that into an Obsidian vault as Markdown notes; see
  [Markdown notes](#markdown-notes) below.

## With Nix

```bash
nix run . -- login     # first time: log in in the browser window
nix run . -- update    # new orders + missing products + html
nix develop            # shell with python, playwright, pillow and Chromium
```

`nix run` writes `overview/` and `.browser-profile/` to the current directory
(or to `$ALIEXPRESS_ORDERS_DIR`). New source files must be `git add`-ed before the
flake can see them.

## Linux / macOS without Nix

Requires Python 3.9 or newer. One-time setup:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Then:

```bash
.venv/bin/python aliexpress_orders.py login
.venv/bin/python aliexpress_orders.py update
```

## Windows

Requires Python 3.9 or newer from [python.org](https://www.python.org/downloads/)
(tick "Add python.exe to PATH" during installation). One-time setup, in a Command Prompt
(`cmd`) opened in the project folder:

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Then run it with Microsoft Edge, which is already installed on Windows:

```bat
.venv\Scripts\python aliexpress_orders.py --channel msedge login
.venv\Scripts\python aliexpress_orders.py --channel msedge update
```

Prefer Google Chrome? Use `--channel chrome`. To use Playwright's own Chromium instead,
run `.venv\Scripts\playwright install chromium` once and leave out `--channel`.

The script takes a command (`login`, `update`, ...), not your e-mail address or password:
`login` opens a browser window where you log in to AliExpress yourself. In PowerShell
the commands are the same; if activating or running scripts is blocked, use `cmd` instead.
Open `overview\index.html` in your browser when it is done.

## Choosing the browser

By default the script uses Playwright's own Chromium. With `--channel` it uses a browser
that is already installed on your computer instead:

| Option | Browser | Profile folder |
|---|---|---|
| *(none)* | Playwright's Chromium (`playwright install chromium`) | `.browser-profile/` |
| `--channel chrome` | Google Chrome | `.browser-profile-chrome/` |
| `--channel msedge` | Microsoft Edge | `.browser-profile-msedge/` |

`--channel` goes before the command, e.g. `aliexpress_orders.py --channel msedge update`.
Each browser has its own profile, so you log in once per browser. The script always uses
that separate profile, never your everyday browser profile, so your normal browser
logins are not used or touched. Use the same `--channel` every time to stay logged in.

## Commands

| Command | What it does |
|---|---|
| `login` | Open the browser so you can log in; the session is kept in the profile folder (see above) |
| `update [--full] [--backoff MIN,...]` | `orders`, then `products`, `details` and `html` |
| `orders [--full] [--backoff MIN,...]` | Load the order list; stops at already known orders unless `--full` or the last run was interrupted |
| `products [--force] [--only ID ...] [--limit N]` | Screenshot + images for products not yet processed |
| `details [--force] [--all] [--only ID ...] [--limit N]` | Specifications, documents (PDF) and description images for products still for sale |
| `html` | Regenerate `overview/index.html` from the saved data |

- On a captcha or expired login the script pauses; solve it in the window and it continues.
- When AliExpress shows its bot check while loading the order list and nobody solves it within
  2 minutes, `orders` pauses and tries again (after 15, 30, 60 and 120 minutes; change with
  `--backoff 10,20` or stop at once with `--backoff ""`).
- Orders are saved after every page. When a run stops before the oldest order (bot check,
  Ctrl+C, crash), the next `orders` or `update` keeps loading until it reaches the oldest one.
- It is resumable: products that already have a `meta.json` are skipped.
- `--cdp http://127.0.0.1:9333` attaches to an already running Chromium instead of launching one
  (`--channel` is ignored then).

## Output

```
overview/
  index.html                  the overview (open in a browser)
  orders.json                 all orders and order lines
  products/<productId>/
    screenshot.jpg            product page
    img_01.jpg ...            gallery images
    order_*.jpg               image of the variant you ordered
    details.json              specifications, document list, description text
    docs/*.pdf                item guides & documents (e.g. user manuals)
    description/desc_*.jpg    images from the product description
    meta.json
```

## TinyTronics

`tinytronics_orders.py` works like the AliExpress script, with the same setup, browser options
(`--channel`, `--cdp`) and commands, minus `details`: TinyTronics product pages are fetched
in one go.

```bash
nix run .#tinytronics -- login     # first time: log in in the browser window
nix run .#tinytronics -- update    # new orders + missing products + html
```

Without Nix: `python tinytronics_orders.py login`, then `python tinytronics_orders.py update`
(on Windows `.venv\Scripts\python tinytronics_orders.py --channel msedge update`).

| Command | What it does |
|---|---|
| `login` | Open the browser so you can log in; the session is kept in `.browser-profile-tinytronics/` |
| `update [--full]` | `orders`, then `products` and `html` |
| `orders [--full]` | Load the order list, order details and PDF invoices; stops at already known orders unless `--full` |
| `products [--force] [--only SKU ...] [--limit N]` | Screenshot, images, specifications, description and downloads for products not yet processed |
| `html` | Regenerate `overview-tinytronics/index.html` from the saved data |

Products are stored by their TinyTronics SKU. Output (set `$TINYTRONICS_ORDERS_DIR` to put it
elsewhere):

```
overview-tinytronics/
  index.html                  the overview (open in a browser)
  orders.json                 orders, order lines, totals, tracking and status history
  invoices/<orderId>.pdf      PDF invoice of each order
  products/<sku>/
    screenshot.jpg            product page
    img_01.jpg ...            gallery images (original size)
    docs/*                    files from the "Downloads" tab (datasheets etc.)
    description/desc_*.jpg    images from the description
    meta.json                 title, availability, price, specifications, description text
```

## Conrad

`conrad_orders.py` works like the TinyTronics script (same options and commands; products are
stored by their Conrad article number) for orders placed on www.conrad.be.

```bash
nix run .#conrad -- login     # first time: log in in the browser window
nix run .#conrad -- update    # new orders + missing products + html
```

Without Nix: `python conrad_orders.py login`, then `python conrad_orders.py update`.

- `orders` opens your account, selects "All orders" (Conrad shows only the last 2 years by
  default), and reads each order, the product data and the PDF invoices.
- `products` takes a screenshot of each product page and downloads the product images,
  specifications, description and documents (datasheets, manuals). Of documents that come in
  many languages only the Dutch and English versions are kept.
- Products Conrad no longer sells have no name, images or page left; they are shown by
  article number with the price you paid.

Output (set `$CONRAD_ORDERS_DIR` to put it elsewhere):

```
overview-conrad/
  index.html                  the overview (open in a browser)
  orders.json                 orders, order lines, tracking
  invoices/<order>_<invoice>.pdf
  products/<article number>/
    screenshot.jpg            product page
    img_01.jpg ...            product images
    docs/<lang>_*.pdf         datasheets and manuals
    product.json              product data from Conrad
    meta.json                 summary: title, brand, EAN, specifications, description
```

## Automation24

`automation24_orders.py` works like the other scripts (same options and commands) for orders
placed on www.automation24.be.

```bash
nix run .#automation24 -- login     # first time: log in in the browser window
nix run .#automation24 -- update    # new orders + missing products + html
```

Without Nix: `python automation24_orders.py login`, then `python automation24_orders.py update`.

- The site is behind a Cloudflare check; it usually passes by itself after a few seconds. If it
  asks you to click a checkbox, do so in the browser window and the script continues.
- `orders` reads the order list and saves each order's confirmation, delivery note and invoice.
- The order history has no product links, so `products` finds each product through the shop's
  search (by part number) and checks the article id. It saves a screenshot, the product images,
  the technical data, the description and the downloads (datasheets, manuals, certificates).
  The cookie banner is accepted automatically.

Output (set `$AUTOMATION24_ORDERS_DIR` to put it elsewhere):

```
overview-automation24/
  index.html                  the overview (open in a browser)
  orders.json                 orders, order lines, tracking
  documents/<order>_<type>.pdf   order confirmation, delivery note, invoice
  products/<article id>/
    screenshot.jpg            product page
    img_01.webp ...           product images
    docs/*                    datasheets, manuals, certificates, CAD files
    meta.json                 title, brand, availability, specifications, description
```

## reichelt

`reichelt_orders.py` works like the other scripts (same options and commands) for orders
placed on www.reichelt.de. Products are stored by reichelt's internal article id.

```bash
nix run .#reichelt -- login     # first time: log in in the browser window
nix run .#reichelt -- update    # new orders + missing products + html
```

Without Nix: `python reichelt_orders.py login`, then `python reichelt_orders.py update`.

- `orders` reads the order list and each order's shipments (status, delivery date, invoice
  number) and lines. reichelt offers no invoice PDF in the account.
- `products` saves a screenshot, the product images, the technical data, the description and
  the datasheets. The cookie banner is accepted automatically.
- For discontinued products reichelt keeps the page and the technical data, but removes the
  images.

Output (set `$REICHELT_ORDERS_DIR` to put it elsewhere):

```
overview-reichelt/
  index.html                  the overview (open in a browser)
  orders.json                 orders, shipments, order lines
  products/<article id>/
    screenshot.jpg            product page
    img_01.jpg ...            product images
    docs/*.pdf                datasheets
    meta.json                 title, availability, price, technical data, description
```

## Solyd

`solyd_orders.py` works like the other scripts (same options and commands) for orders placed
on www.solyd.be. Products are stored by their article number (SKU).

```bash
nix run .#solyd -- login     # first time: log in in the browser window
nix run .#solyd -- update    # new orders + missing products + html
```

Without Nix: `python solyd_orders.py login`, then `python solyd_orders.py update`.

- `orders` reads the order history, each order's lines, totals, shipping and payment method.
  Solyd has no invoice PDF, only a print page; the script saves that page as an image.
- The order history has no product links, so `products` opens the page whose address is the
  product name (that is how Solyd names its pages), or else uses the shop's search, and checks
  the article number. It saves a screenshot, the product images (original size), the
  specifications and the description. The cookie banner is accepted automatically.

Output (set `$SOLYD_ORDERS_DIR` to put it elsewhere):

```
overview-solyd/
  index.html                  the overview (open in a browser)
  orders.json                 orders, order lines, totals
  invoices/<order>_<invoice>.jpg   the invoice print page
  products/<sku>/
    screenshot.jpg            product page
    img_01.jpg ...            product images
    meta.json                 title, price, availability, specifications, description
```

## Markdown notes

`export_notes.py` copies the data of every shop into a folder of an Obsidian vault and writes a
Markdown note for each shop, order and product, so the purchase history can be browsed in
Obsidian (or any app that reads the vault):

```bash
python export_notes.py ~/notes/purchases            # all shops that have data
python export_notes.py ~/notes/purchases --shops solyd reichelt
```

- `<folder>/<folder>.md`: totals per shop and per year.
- `<shop>/<shop>.md`: orders and products of that shop, with totals.
- `<shop>/orders/<date> <order>.md`: the order lines with thumbnails, documents and invoice.
- `<shop>/products/<key>/<key>.md`: images, specifications, documents, description and the
  screenshot of the product page, plus the orders it was in.

Links and image embeds use paths from the vault root (the first folder above the target with a
`.obsidian` folder, or `--vault`). Run it again after an `update`; unchanged notes are left alone.
The data is a few hundred MB of images and PDFs: in a vault kept in git, put those in git-annex or
Git LFS (e.g. `<folder>/** annex.largefiles=anything` and `<folder>/**/*.md annex.largefiles=nothing`
in `.gitattributes`).
