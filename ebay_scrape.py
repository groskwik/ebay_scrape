#!/usr/bin/env python

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException


AWAITING_URL = "https://www.ebay.com/sh/ord/?filter=status:AWAITING_SHIPMENT"
AWAITING_URL = "https://www.ebay.com/sh/ord/?filter=status:ALL_ORDERS" 


RE_ORDER_FULL = re.compile(r"^\d{2}-\d{5}-\d{5}$")   # e.g. 27-13984-70927
RE_AVAILABLE = re.compile(r"\((\d+)\s+available\)", re.IGNORECASE)
RE_PRICE = re.compile(r"[-+]?\$?\s*([0-9]+(?:\.[0-9]{2})?)")


def ensure_logged_in_or_pause(driver):
    cur = driver.current_url.lower()
    if "signin" in cur or "login" in cur:
        print("Redirected to sign-in. Please log in in the Chrome window, then press Enter here.")
        input()


def scroll_to_bottom(driver, steps=8, pause=0.6):
    # Helps if the table lazy-loads additional rows
    for _ in range(steps):
        driver.execute_script("window.scrollBy(0, document.body.scrollHeight);")
        WebDriverWait(driver, 10).until(lambda d: True)
        driver.implicitly_wait(0)
        # small pause
        driver.execute_script("return 0;")
        import time
        time.sleep(pause)


def extract_item_id_from_url(href: str) -> str | None:
    """
    https://www.ebay.com/itm/356885714929 -> 356885714929
    Also handles /itm/<id>?... variants.
    """
    try:
        path = urlparse(href).path  # /itm/356885714929
    except Exception:
        path = href
    m = re.search(r"/itm/(\d+)", path)
    return m.group(1) if m else None


def extract_short_order(full_text: str) -> str | None:
    """
    "27-13984-70927" -> "13984-70927"
    """
    t = full_text.strip()
    if not RE_ORDER_FULL.match(t):
        return None
    parts = t.split("-")
    if len(parts) == 3:
        return f"{parts[1]}-{parts[2]}"
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
            if "order" in cls and ("row" in cls or "card" in cls):
                return cur
            if "shui" in cls and "card" in cls:
                return cur

            cur = cur.find_element(By.XPATH, "..")
        except Exception:
            break
    return el


def safe_find_text(root, by, sel) -> str:
    try:
        return root.find_element(by, sel).text.strip()
    except Exception:
        return ""


def safe_find_attr(root, by, sel, attr) -> str:
    try:
        return (root.find_element(by, sel).get_attribute(attr) or "").strip()
    except Exception:
        return ""


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


def scrape_awaiting_shipment_table(driver, timeout=30, max_items=500, debug=False):
    wait = WebDriverWait(driver, timeout)

    # Wait for body
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    # Let page settle
    try:
        wait.until(lambda d: "Awaiting shipment" in (d.title or "") or "Orders" in (d.title or ""))
    except TimeoutException:
        pass

    # If the table is lazy-loaded, scrolling helps
    scroll_to_bottom(driver, steps=6, pause=0.5)

    # Identify all /itm/ anchors
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

            # Deduplicate by item_id + title (same item may appear multiple times in DOM)
            key = (item_id, title)
            if key in seen:
                continue
            seen.add(key)

            row = find_row_container(a)

            # Order number anchor: looks like /mesh/ord/details... and visible text like 27-13984-70927
            order_full = ""
            try:
                order_el = row.find_element(By.XPATH, ".//a[contains(@href,'/mesh/ord/details') and contains(normalize-space(.),'-')]")
                order_full = (order_el.text or "").strip()
            except Exception:
                # fallback: find any anchor with pattern NN-NNNNN-NNNNN
                try:
                    order_el = row.find_element(By.XPATH, ".//a[normalize-space(.) and contains(normalize-space(.),'-')]")
                    cand = (order_el.text or "").strip()
                    if RE_ORDER_FULL.match(cand):
                        order_full = cand
                except Exception:
                    order_full = ""

            order_short = extract_short_order(order_full) if order_full else None

            # Quantity sold + available:
            # Provided pattern:
            #   <strong>1</strong>
            #   <span class="available-quantity">(1 available)</span>
            qty_sold = None
            qty_avail = None

            avail_text = ""
            try:
                avail_span = row.find_element(By.XPATH, ".//span[contains(@class,'available-quantity')]")
                avail_text = (avail_span.text or "").strip()
                qty_avail = parse_qty_available(avail_text)

                # Prefer sibling strong, else nearest preceding strong
                try:
                    strong_el = avail_span.find_element(By.XPATH, "./preceding-sibling::strong[1]")
                except Exception:
                    strong_el = row.find_element(By.XPATH, ".//span[contains(@class,'available-quantity')]/preceding::strong[1]")
                s = (strong_el.text or "").strip()
                qty_sold = int(s) if s.isdigit() else None

            except Exception:
                pass

            # Price
            price_text = safe_find_text(row, By.CSS_SELECTOR, "div.price-column-item")
            price = parse_price(price_text)

            rows.append({
                "order_full": order_full or None,
                "order_number": order_short,
                "item_id": item_id,
                "title": title,
                "item_url": href,
                "qty_sold": qty_sold,
                "qty_available": qty_avail,
                "price_text": price_text or None,
                "price": price,
            })

            if len(rows) >= max_items:
                break

        except StaleElementReferenceException:
            continue

    return rows


def main():
    options = webdriver.ChromeOptions()

    # Dedicated profile so you stay logged in between runs
    profile_dir = Path(__file__).with_name("chrome_profile_selenium").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={str(profile_dir)}")

    driver = webdriver.Chrome(options=options)

    try:
        driver.get(AWAITING_URL)
        ensure_logged_in_or_pause(driver)
        driver.get(AWAITING_URL)

        data = scrape_awaiting_shipment_table(driver, timeout=30, max_items=500, debug=True)

        df = pd.DataFrame(data)

        # Nice ordering
        cols = [
            "order_number", "order_full",
            "item_id", "title", "item_url",
            "qty_sold", "qty_available",
            "price", "price_text",
        ]
        df = df[[c for c in cols if c in df.columns]]

        # Optional: sort by order_number then item_id for readability
        # df = df.sort_values(["order_number", "item_id"], na_position="last")

        # Display
        pd.set_option("display.max_colwidth", 120)
        print(df.to_string(index=False))

        # Optional: save
        out = Path("awaiting_shipment_items.csv").resolve()
        df.to_csv(out, index=False, encoding="utf-8")
        print(f"\nSaved CSV: {out}")

        input("\nDone. Press Enter to quit...")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()

