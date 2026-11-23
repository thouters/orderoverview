# Order overview

Two scripts that build a local HTML overview of your web shop orders:

- `aliexpress_orders.py` fetches all your AliExpress orders and builds `overview/index.html`:
  the products of every order, a screenshot of each product page, the product images and,
  for products still for sale, the specifications, PDF documents and description images.
- `tinytronics_orders.py` does the same for [TinyTronics](https://www.tinytronics.nl) and
  builds `overview-tinytronics/index.html`; see [TinyTronics](#tinytronics) below.
- `conrad_orders.py` does the same for [Conrad](https://www.conrad.be) and builds
  `overview-conrad/index.html`; see [Conrad](#conrad) below.

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
| `update [--full]` | `orders`, then `products`, `details` and `html` |
| `orders [--full]` | Load the order list; stops at already known orders unless `--full` |
| `products [--force] [--only ID ...] [--limit N]` | Screenshot + images for products not yet processed |
| `details [--force] [--all] [--only ID ...] [--limit N]` | Specifications, documents (PDF) and description images for products still for sale |
| `html` | Regenerate `overview/index.html` from the saved data |

- On a captcha or expired login the script pauses; solve it in the window and it continues.
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
