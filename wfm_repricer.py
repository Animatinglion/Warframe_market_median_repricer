#!/usr/bin/env python3
"""
WFM Median Repricer
───────────────────
Fetches your active sell orders from warframe.market,
computes the 48-hour median sell price for each item,
and updates prices automatically — asking you first
whenever the new price would be LOWER than the current one.

Items with fewer than MIN_TRADES_48H sales in the past 48 h
are never touched and shown in a summary list at the end.

Setup
─────
1.  pip install curl_cffi keyring   (tkinter ships with Python on Windows)
2.  Get your JWT token:
      - Log into warframe.market in your browser
      - Open DevTools (F12) → Application/Storage → Cookies → warframe.market
      - Copy the value of the "JWT" cookie
3.  Run:  python wfm_repricer.py
    You'll be prompted for the token on first run; it's saved to wfm_token.txt.
"""

import sys
import time
import webbrowser
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen

# ── optional GUI deps ──────────────────────────────────────────────────────────
try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    HAS_TK = True
except ImportError:
    HAS_TK = False

try:
    from curl_cffi import requests
except ImportError:
    print("ERROR: 'curl_cffi' not installed.  Run:  pip install curl_cffi")
    sys.exit(1)

try:
    import keyring
except ImportError:
    print("ERROR: 'keyring' not installed.  Run:  pip install keyring")
    sys.exit(1)

# ── config ─────────────────────────────────────────────────────────────────────
SERVICE_ID      = "wfm_repricer" # service ID for credentials manager
BASE_URL        = "https://api.warframe.market/v2"
BASE_URL_V1     = "https://api.warframe.market/v1"   # stats endpoint still v1
ASSETS_ROOT     = "https://warframe.market"
MIN_TRADES_48H  = 5          # minimum closed sales in 48 h to act on
RATE_SLEEP      = 0.5         # seconds between API calls (be polite)
# ──────────────────────────────────────────────────────────────────────────────

def print_token_instructions() -> None:
    print("To get your JWT token:")
    print("  1. Log into warframe.market in your browser")
    print("  2. Open DevTools (F12) → Application → Cookies → warframe.market")
    print("  3. Copy the value of the cookie named 'JWT'\n")

def get_token() -> str:
    tok = retrieve_token()
    if tok:
        return tok
    print("\nNo token found.")
    print_token_instructions()
    tok = input("Paste your JWT token here: ").strip()
    save_token(tok)
    print(f"Token saved to OS Credential manager\n")
    return tok

def on_auth_error() -> None:
    print("\nAuthentication failed. Possibly your token is invalid? You should update it.\n")
    print_token_instructions()
    tok = input("Paste your JWT token here: ").strip()
    save_token(tok)

def retrieve_token() -> str | None:
    return keyring.get_password(SERVICE_ID, SERVICE_ID)

def save_token(token: str) -> None:
    keyring.set_password(SERVICE_ID, SERVICE_ID, token)

def make_session(token: str) -> requests.Session:
    # impersonate="firefox" makes curl_cffi mimic Firefox's TLS fingerprint,
    # which is what bypasses Cloudflare — regular requests looks like a bot
    s = requests.Session(impersonate="firefox")
    # Cookie header exactly as the browser sends it — just JWT, nothing else
    s.headers.update({
        "Cookie":          f"JWT={token}",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin":          "https://warframe.market",
        "Referer":         "https://warframe.market/",
        "Content-Type":    "application/json",
        "platform":        "pc",
        "language":        "en",
        "crossplay":       "true",
        # mimic the Firefox User-Agent seen in your DevTools
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
    })
    return s


def api_get(session: requests.Session, path: str, v1: bool = False) -> dict:
    base = BASE_URL_V1 if v1 else BASE_URL
    url = f"{base}{path}"
    r = session.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def api_patch(session: requests.Session, path: str, payload: dict) -> dict:
    # v2 uses PATCH for partial updates; note singular /order/ not /orders/
    url = f"{BASE_URL}{path}"
    r = session.patch(url, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


# ── market data ────────────────────────────────────────────────────────────────

def build_item_lookup(session: requests.Session) -> dict[str, dict]:
    """
    Fetch all tradable items once and return a dict keyed by item id.
    Each value has: slug, name
    This avoids N individual item fetches — one bulk call instead.
    """
    data  = api_get(session, "/items")
    items = data.get("data", [])
    lookup = {}
    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue
        slug    = item.get("slug", "")
        # i18n is a dict keyed by language code; we asked for "en" via header
        i18n    = item.get("i18n", {})
        en      = i18n.get("en", {})
        name    = en.get("name") or slug
        lookup[item_id] = {"slug": slug, "name": name}
    return lookup


def get_my_sell_orders(session: requests.Session) -> list[dict]:
    # v2: GET /v2/orders/my — returns YOUR orders, no username needed
    data   = api_get(session, "/orders/my")
    orders = data.get("data", [])
    # keep only visible sell orders
    return [o for o in orders if o.get("type") == "sell" and o.get("visible", True)]


def get_48h_stats(session: requests.Session, slug: str) -> tuple[float | None, int]:
    """
    Returns (median_price, trade_count) for the past 48 hours.
    Statistics endpoint is still on v1.
    """
    try:
        data = api_get(session, f"/items/{slug}/statistics", v1=True)
    except Exception:
        return None, 0

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=48)

    closed = data.get("payload", {}).get("statistics_closed", {}).get("48hours", [])

    # collect all individual sale prices within the window
    prices = []
    for entry in closed:
        try:
            dt = datetime.fromisoformat(entry["datetime"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if dt >= cutoff:
            # each entry represents one closed order; use avg_price as the price
            avg = entry.get("avg_price") or entry.get("median")
            vol = entry.get("volume", 1)
            if avg is not None:
                prices.extend([avg] * int(vol))

    if not prices:
        return None, 0

    prices.sort()
    n = len(prices)
    if n % 2 == 1:
        median = prices[n // 2]
    else:
        median = (prices[n // 2 - 1] + prices[n // 2]) / 2.0

    return round(median), n



# ── GUI confirm dialog ──────────────────────────────────────────────────────────

class ConfirmDialog:
    """
    Modal dialog shown when a price decrease is proposed.
    Returns True (apply), False (skip), or raises SystemExit (quit all).
    """

    def __init__(self, root, item_name: str, old_price: int, new_price: int,
                remaining: int):
        self.result = None

        win = tk.Toplevel(root)
        win.title("WFM Repricer — Confirm Price Decrease")
        win.configure(bg="#1a1a2e")
        win.resizable(False, False)
        win.grab_set()

        pad    = dict(padx=14)   # pady set per-call to avoid keyword conflict

        # ── header ──
        tk.Label(win, text="⬇  Price decrease detected",
                 font=("Segoe UI", 13, "bold"),
                 fg="#e94560", bg="#1a1a2e").pack(**pad, pady=(14, 2))

        tk.Label(win, text=item_name,
                 font=("Segoe UI", 15, "bold"),
                 fg="#eaeaea", bg="#1a1a2e").pack(**pad, pady=6)

        # ── price table ──
        frame = tk.Frame(win, bg="#16213e", bd=1, relief="solid")
        frame.pack(padx=16, pady=8, fill="x")

        def row(label, value, color="#eaeaea"):
            r = tk.Frame(frame, bg="#16213e")
            r.pack(fill="x", padx=10, pady=3)
            tk.Label(r, text=label, font=("Segoe UI", 10),
                     fg="#a0a0c0", bg="#16213e", anchor="w").pack(side="left")
            tk.Label(r, text=value, font=("Segoe UI", 11, "bold"),
                     fg=color, bg="#16213e", anchor="e").pack(side="right")

        diff = new_price - old_price  # negative
        row("Current price", f"{old_price} platinum")
        row("New median price", f"{new_price} platinum", "#4fc3f7")
        row("Difference", f"{diff:+d} platinum", "#e94560")

        if remaining > 1:
            tk.Label(win, text=f"{remaining - 1} more item(s) to review after this",
                     font=("Segoe UI", 9), fg="#707090", bg="#1a1a2e").pack()

        # ── buttons ──
        btn_frame = tk.Frame(win, bg="#1a1a2e")
        btn_frame.pack(pady=(8, 14))

        def do_apply():
            self.result = "apply"
            win.destroy()

        def do_skip():
            self.result = "skip"
            win.destroy()

        def do_quit():
            self.result = "quit"
            win.destroy()

        tk.Button(btn_frame, text="✓  Apply", command=do_apply,
                  bg="#1b5e20", fg="white", font=("Segoe UI", 11, "bold"),
                  relief="flat", padx=18, pady=8, cursor="hand2").pack(side="left", padx=6)

        tk.Button(btn_frame, text="—  Skip", command=do_skip,
                  bg="#37474f", fg="white", font=("Segoe UI", 11),
                  relief="flat", padx=18, pady=8, cursor="hand2").pack(side="left", padx=6)

        tk.Button(btn_frame, text="✕  Quit", command=do_quit,
                  bg="#b71c1c", fg="white", font=("Segoe UI", 11),
                  relief="flat", padx=18, pady=8, cursor="hand2").pack(side="left", padx=6)

        # centre on screen
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

        root.wait_window(win)


def ask_confirm_tk(root, item_name, old_price, new_price, remaining) -> str:
    dlg = ConfirmDialog(root, item_name, old_price, new_price, remaining)
    return dlg.result or "skip"


def ask_confirm_cli(item_name, old_price, new_price) -> str:
    diff = new_price - old_price
    print(f"\n  Item   : {item_name}")
    print(f"  Old    : {old_price} plat")
    print(f"  New    : {new_price} plat  ({diff:+d})")
    while True:
        ans = input("  Apply? [y/n/q(uit all)] ").strip().lower()
        if ans in ("y", "yes"):
            return "apply"
        if ans in ("n", "no", "s", "skip"):
            return "skip"
        if ans in ("q", "quit"):
            return "quit"


# ── summary window ─────────────────────────────────────────────────────────────

def show_summary_tk(low_volume: list[dict], applied: int, skipped: int, no_data: int, root):
    root.title("WFM Repricer — Done")
    root.configure(bg="#1a1a2e")

    tk.Label(root, text="Repricing Complete",
             font=("Segoe UI", 14, "bold"), fg="#eaeaea", bg="#1a1a2e").pack(padx=20, pady=(16, 4))

    stats_text = f"✓ Applied: {applied}   —  Skipped: {skipped}   —  No data: {no_data}"
    tk.Label(root, text=stats_text,
             font=("Segoe UI", 10), fg="#a0a0c0", bg="#1a1a2e").pack(padx=20, pady=(0, 10))

    if low_volume:
        tk.Label(root, text=f"⚠  {len(low_volume)} item(s) with <{MIN_TRADES_48H} sales in 48 h — review manually:",
                 font=("Segoe UI", 10, "bold"), fg="#ffb74d", bg="#1a1a2e").pack(padx=20, anchor="w")

        frame = tk.Frame(root, bg="#16213e")
        frame.pack(padx=16, pady=6, fill="both", expand=True)

        canvas = tk.Canvas(frame, bg="#16213e", highlightthickness=0, width=480, height=300)
        sb = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<MouseWheel>", _on_mousewheel)

        inner = tk.Frame(canvas, bg="#16213e")
        canvas.create_window((0, 0), window=inner, anchor="nw")

        for item in low_volume:
            name     = item["item_name"]
            price    = item["platinum"]
            trades   = item["trades_48h"]
            url      = item["url"]
            slug     = item["slug"]

            row = tk.Frame(inner, bg="#1e2a3a", pady=4)
            row.pack(fill="x", padx=6, pady=2)

            left = tk.Frame(row, bg="#1e2a3a")
            left.pack(side="left", fill="x", expand=True, padx=8)

            tk.Label(left, text=name, font=("Segoe UI", 10, "bold"),
                     fg="#eaeaea", bg="#1e2a3a", anchor="w").pack(anchor="w")
            tk.Label(left, text=f"Current: {price} plat  ·  Only {trades} sale(s) in 48 h",
                     font=("Segoe UI", 9), fg="#808090", bg="#1e2a3a", anchor="w").pack(anchor="w")

            def open_url(u=url):
                webbrowser.open(u)

            tk.Button(row, text="Open →", command=open_url,
                      bg="#0d47a1", fg="white", font=("Segoe UI", 9),
                      relief="flat", padx=10, pady=4, cursor="hand2").pack(side="right", padx=8)

        inner.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))

    tk.Button(root, text="Close", command=root.destroy,
              bg="#37474f", fg="white", font=("Segoe UI", 11),
              relief="flat", padx=24, pady=8).pack(pady=14)

    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    root.deiconify()  # make root visible so mainloop has a window to manage
    root.mainloop()


def show_summary_cli(low_volume, applied, skipped, no_data):
    print(f"\n{'─'*50}")
    print(f"  Done.  Applied: {applied}  |  Skipped: {skipped}  |  No data: {no_data}")
    if low_volume:
        print(f"\n  ⚠  Items with <{MIN_TRADES_48H} trades in 48 h (not touched):\n")
        for item in low_volume:
            print(f"    {item['item_name']:40s}  {item['platinum']:>4d} plat  "
                  f"({item['trades_48h']} trades)  → {item['url']}")
    print()


# ── main logic ─────────────────────────────────────────────────────────────────

def main():
    global _tk_root
    _tk_root = None
    print("═" * 55)
    print("  WFM Median Repricer")
    print("═" * 55)

    token   = get_token()
    session = make_session(token)

    # verify auth — v2: GET /v2/me
    try:
        me = api_get(session, "/me")
        profile  = me.get("data", {})
        username = profile.get("ingameName")   # Go struct: IngameName → json:"ingameName"
        if not username:
            raise ValueError(f"Could not find ingameName in response: {me}")
        print(f"  Logged in as: {username}\n")
    except Exception as e:
        print(f"ERROR: Could not authenticate. Check your token.\n  {e}")
        on_auth_error()
        print("Script wil now exit. Restart to try again.")
        sys.exit(1)

    print("  Fetching item catalogue…")
    item_lookup = build_item_lookup(session)
    print(f"  Loaded {len(item_lookup)} items.\n")

    print("  Fetching your sell orders…")
    orders = get_my_sell_orders(session)
    print(f"  Found {len(orders)} active sell order(s).\n")

    if not orders:
        print("  Nothing to do.")
        return

    # categorise orders
    to_apply_up   = []   # price goes up   → auto-apply
    to_confirm    = []   # price goes down  → ask user
    low_volume    = []   # not enough data  → skip + list
    no_data       = []   # API returned nothing

    print(f"  Fetching 48-hour statistics for each item…")
    for i, order in enumerate(orders, 1):
        # v2 Order has no embedded item — just itemId, look it up in our catalogue
        item_id   = order.get("itemId", "")
        item_info = item_lookup.get(item_id, {})
        slug      = item_info.get("slug", item_id)   # fallback to id if unknown
        item_name = item_info.get("name", slug)
        cur_price = order.get("platinum", 0)
        order_id  = order["id"]

        sys.stdout.write(f"\r  [{i}/{len(orders)}] {item_name[:40]:<40s}")
        sys.stdout.flush()

        median, trades = get_48h_stats(session, slug)
        time.sleep(RATE_SLEEP)

        wfm_url = f"https://warframe.market/items/{slug}/statistics"

        if median is None:
            no_data.append({"item_name": item_name, "platinum": cur_price,
                            "trades_48h": 0, "slug": slug, "url": wfm_url})
            continue

        if trades < MIN_TRADES_48H:
            low_volume.append({"item_name": item_name, "platinum": cur_price,
                               "trades_48h": trades, "slug": slug, "url": wfm_url})
            continue

        if median == cur_price:
            continue  # already correct
        elif median > cur_price:
            to_apply_up.append((order_id, item_name, cur_price, median))
        else:
            to_confirm.append((order_id, item_name, cur_price, median))

    print()  # newline after progress

    applied = 0
    skipped = 0

    # ── auto-apply price increases ──
    if to_apply_up:
        print(f"\n  Auto-applying {len(to_apply_up)} price increase(s)…")
        for order_id, name, old, new in to_apply_up:
            try:
                api_patch(session, f"/order/{order_id}", {"platinum": new})
                print(f"    ↑  {name}: {old} → {new} plat")
                applied += 1
                time.sleep(RATE_SLEEP)
            except Exception as e:
                print(f"    ✗  {name}: failed ({e})")
                skipped += 1

    if HAS_TK:
        _tk_root = tk.Tk()
        _tk_root.withdraw()

    # ── confirm price decreases ──
    if to_confirm:
        print(f"\n  {len(to_confirm)} price decrease(s) need your confirmation.")
        if HAS_TK:
            for idx, (order_id, name, old, new) in enumerate(to_confirm):
                remaining = len(to_confirm) - idx
                decision = ask_confirm_tk(_tk_root, name, old, new, remaining)
                if decision == "apply":
                    try:
                        api_patch(session, f"/order/{order_id}", {"platinum": new})
                        applied += 1
                        time.sleep(RATE_SLEEP)
                    except Exception as e:
                        print(f"  ✗  {name}: failed ({e})")
                        skipped += 1
                elif decision == "skip":
                    skipped += 1
                else:  # quit
                    skipped += len(to_confirm) - idx
                    break
        else:
            for order_id, name, old, new in to_confirm:
                decision = ask_confirm_cli(name, old, new)
                if decision == "apply":
                    try:
                        api_patch(session, f"/order/{order_id}", {"platinum": new})
                        applied += 1
                        time.sleep(RATE_SLEEP)
                    except Exception as e:
                        print(f"  ✗  {name}: failed ({e})")
                        skipped += 1
                elif decision == "skip":
                    skipped += 1
                else:
                    skipped += 1
                    break

    # merge low-volume + no-data for summary
    all_low = low_volume + no_data

    if HAS_TK:
        show_summary_tk(all_low, applied, skipped, len(no_data), _tk_root)
    else:
        show_summary_cli(all_low, applied, skipped, len(no_data))


if __name__ == "__main__":
    main()
