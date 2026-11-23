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
