from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException


AWAITING_URL = "https://www.ebay.com/sh/ord/?filter=status:AWAITING_SHIPMENT"
ALL_ORDERS_URL = "https://www.ebay.com/sh/ord/?filter=status:ALL_ORDERS"

RE_ORDER_FULL = re.compile(r"^\d{2}-\d{5}-\d{5}$")   # e.g. 27-13984-70927
RE_AVAILABLE = re.compile(r"\((\d+)\s+available\)", re.IGNORECASE)
RE_PRICE = re.compile(r"\$?\s*([0-9]+(?:\.[0-9]{2})?)")


def ensure_logged_in_or_pause(driver):
    cur = (driver.current_url or "").lower()
    if "signin" in cur or "login" in cur:
        print("Redirected to sign-in. Please log in in the Chrome window, then press Enter here.")
        input()


def scroll_to_bottom(driver, steps=6, pause_s=0.5):
    import time
    for _ in range(steps):
        driver.execute_script("window.scrollBy(0, document.body.scrollHeight);")
        time.sleep(pause_s)


def extract_item_id_from_url(href: str) -> str | None:
    try:
        path = urlparse(href).path
    except Exception:
        path = href
    m = re.search(r"/itm/(\d+)", path)
    return m.group(1) if m else None


def extract_short_order(full_text: str) -> str | None:
    t = (full_text or "").strip()
    if not RE_ORDER_FULL.match(t):
        return None
    parts = t.split("-")
    return f"{parts[1]}-{parts[2]}" if len(parts) == 3 else None


def parse_qty_available(text: str) -> int | None:
    m = RE_AVAILABLE.search(text or "")
    return int(m.group(1)) if m else None


def parse_price(text: str) -> float | None:
    if not text:
        return None
    m = RE_PRICE.search(text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def find_row_container(el, max_hops=12):
    """
    Walk up the DOM until we reach something row-ish.
    eBay changes markup; this heuristic keeps it robust.
    """
    cur = el
    for _ in range(max_hops):
        try:
            tag = cur.tag_name.lower()
            cls = (cur.get_attribute("class") or "").lower()
            role = (cur.get_attribute("role") or "").lower()

            if tag == "tr":
                return cur
            if role in ("row", "rowgroup"):
                return cur
            if "row" in cls or "card" in cls:
                return cur

            cur = cur.find_element(By.XPATH, "..")
        except Exception:
            break
    return el


def safe_find_text(root, by, sel) -> str:
    try:
        return (root.find_element(by, sel).text or "").strip()
    except Exception:
        return ""


def scrape_orders(driver, timeout=30, max_items=500, debug=False):
    wait = WebDriverWait(driver, timeout)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    # helps with lazy-rendered rows
    scroll_to_bottom(driver, steps=6, pause_s=0.5)

    item_links = driver.find_elements(By.XPATH, "//a[contains(@href,'/itm/')]")
    if debug:
        print(f"Found /itm/ anchors: {len(item_links)}")

    rows = []
    seen = set()

    for a in item_links:
        try:
            href = (a.get_attribute("href") or "").strip()
            title = (a.text or "").strip()

            item_id = extract_item_id_from_url(href)
            if not item_id:
                continue

            key = (item_id, title)
            if key in seen:
                continue
            seen.add(key)

            row = find_row_container(a)

            # order number anchor
            order_full = ""
            try:
                order_el = row.find_element(By.XPATH, ".//a[contains(@href,'/mesh/ord/details') and contains(normalize-space(.),'-')]")
                cand = (order_el.text or "").strip()
                if RE_ORDER_FULL.match(cand):
                    order_full = cand
            except Exception:
                # fallback: any anchor matching the pattern
                try:
                    order_el = row.find_element(By.XPATH, ".//a[normalize-space(.)]")
                    cand = (order_el.text or "").strip()
                    if RE_ORDER_FULL.match(cand):
                        order_full = cand
                except Exception:
                    order_full = ""

            order_short = extract_short_order(order_full) if order_full else None

            # quantity sold & available
            qty_sold = None
            qty_avail = None
            price_text = ""

            try:
                avail_span = row.find_element(By.XPATH, ".//span[contains(@class,'available-quantity')]")
                avail_text = (avail_span.text or "").strip()
                qty_avail = parse_qty_available(avail_text)

                # prefer immediate preceding sibling <strong>, else nearest preceding <strong>
                try:
                    strong_el = avail_span.find_element(By.XPATH, "./preceding-sibling::strong[1]")
                except Exception:
                    strong_el = row.find_element(By.XPATH, ".//span[contains(@class,'available-quantity')]/preceding::strong[1]")

                s = (strong_el.text or "").strip()
                qty_sold = int(s) if s.isdigit() else None
            except Exception:
                pass

            # price
            price_text = safe_find_text(row, By.CSS_SELECTOR, "div.price-column-item")
            price = parse_price(price_text)

            rows.append({
                "order_number": order_short or "",
                "order_full": order_full or "",
                "item_id": item_id or "",
                "title": title or "",
                "item_url": href or "",
                "qty_sold": "" if qty_sold is None else str(qty_sold),
                "qty_available": "" if qty_avail is None else str(qty_avail),
                "price": "" if price is None else f"{price:.2f}",
                "price_text": price_text or "",
            })

            if len(rows) >= max_items:
                break

        except StaleElementReferenceException:
            continue

    return rows


def print_table(rows, headers=None, max_widths=None):
    """
    Simple aligned table printer.
    """
    if not rows:
        print("(no rows)")
        return

    if headers is None:
        headers = list(rows[0].keys())

    widths = {h: len(h) for h in headers}
    for r in rows:
        for h in headers:
            widths[h] = max(widths[h], len(str(r.get(h, ""))))

    if max_widths:
        for h, cap in max_widths.items():
            if h in widths:
                widths[h] = min(widths[h], cap)

    def fmt_cell(h, v):
        s = str(v)
        cap = widths[h]
        if len(s) > cap:
            s = s[: max(0, cap - 1)] + "…"
        return s.ljust(widths[h])

    sep = " | "
    line = "-+-".join("-" * widths[h] for h in headers)

    print(sep.join(h.ljust(widths[h]) for h in headers))
    print(line)
    for r in rows:
        print(sep.join(fmt_cell(h, r.get(h, "")) for h in headers))


def write_csv(rows, path: Path):
    if not rows:
        return
    headers = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-orders", action="store_true",
                    help="Scrape ALL_ORDERS instead of AWAITING_SHIPMENT (default).")
    ap.add_argument("--headless", action="store_true",
                    help="Run without showing Chrome. Use only after you have a valid logged-in profile.")
    ap.add_argument("--stdout-short", action="store_true",
                    help="Print only item_id,title,item_url to stdout (CSV remains full).")
    ap.add_argument("--max-items", type=int, default=500)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--out-dir", default=".", help="Output folder for CSV.")
    args = ap.parse_args()

    url = ALL_ORDERS_URL if args.all_orders else AWAITING_URL
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Change CSV filename based on mode
    csv_name = "all_orders_items.csv" if args.all_orders else "awaiting_shipment_items.csv"
    out_csv = out_dir / csv_name

    options = webdriver.ChromeOptions()

    # Dedicated profile folder so you remain logged in between runs (KEEP THIS)
    profile_dir = Path(__file__).with_name("chrome_profile_selenium").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={str(profile_dir)}")

    # headless (optional)
    if args.headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1400,900")

    driver = webdriver.Chrome(options=options)

    try:
        driver.get(url)
        ensure_logged_in_or_pause(driver)

        # Reload after login (same pattern as your working script)
        driver.get(url)

        rows = scrape_orders(driver, timeout=args.timeout, max_items=args.max_items, debug=args.debug)

        print()
        if args.stdout_short:
            short_headers = ["item_id", "title", "item_url"]
            print_table(rows, headers=short_headers, max_widths={"title": 80, "item_url": 80})
        else:
            print_table(rows, max_widths={"title": 60, "item_url": 60})

        write_csv(rows, out_csv)
        print(f"\nSaved CSV: {out_csv}")

        if not args.headless:
            input("\nDone. Press Enter to quit...")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
