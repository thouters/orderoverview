# AliExpress order overview

`aliexpress_orders.py` fetches all your AliExpress orders and builds `overview/index.html`:
the products of every order, a screenshot of each product page, the product images and,
for products still for sale, the specifications, PDF documents and description images.

## With Nix

```bash
nix run . -- login     # first time: log in in the browser window
nix run . -- update    # new orders + missing products + html
nix develop            # shell with python, playwright, pillow and Chromium
```

`nix run` writes `overview/` and `.browser-profile/` to the current directory
(or to `$ALIEXPRESS_ORDERS_DIR`). New source files must be `git add`-ed before the
flake can see them.

## Without Nix (one-time setup)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

```bash
.venv/bin/python aliexpress_orders.py login
.venv/bin/python aliexpress_orders.py update
```

## Commands

| Command | What it does |
|---|---|
| `login` | Open the browser so you can log in; the session is kept in `.browser-profile/` |
| `update [--full]` | `orders`, then `products`, `details` and `html` |
| `orders [--full]` | Load the order list; stops at already known orders unless `--full` |
| `products [--force] [--only ID ...] [--limit N]` | Screenshot + images for products not yet processed |
| `details [--force] [--all] [--only ID ...] [--limit N]` | Specifications, documents (PDF) and description images for products still for sale |
| `html` | Regenerate `overview/index.html` from the saved data |

- On a captcha or expired login the script pauses; solve it in the window and it continues.
- It is resumable: products that already have a `meta.json` are skipped.
- `--cdp http://127.0.0.1:9333` attaches to an already running Chromium instead of launching one.

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
