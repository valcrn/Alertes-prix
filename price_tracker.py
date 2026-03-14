"""
Amazon.fr Price Tracker
Monitors PS5 games, whisky, and football jersey categories.
Sends Telegram alerts when prices drop below tracked levels.
"""

import json
import os
import re
import time
import logging
from datetime import datetime
from pathlib import Path

import requests
import schedule
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = "8346118269:AAGxoRW18oEVNAEhCnzEBo6poymjMu3dHUA"
TELEGRAM_CHAT_ID = "8604666788"

# Amazon.fr category search URLs
CATEGORIES = {
    "PS5 Games": "https://www.amazon.fr/s?k=jeux+ps5&s=price-asc-rank",
    "Whisky": "https://www.amazon.fr/s?k=whisky&s=price-asc-rank",
    "Football Jerseys": "https://www.amazon.fr/s?k=maillot+de+football&s=price-asc-rank",
}

# Number of top products to track per category
PRODUCTS_PER_CATEGORY = 10

# How often to check prices (in minutes)
CHECK_INTERVAL_MINUTES = 60

# File where price history is persisted between runs
PRICE_DB_FILE = "price_history.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("price_tracker.log"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def fetch_page(url: str) -> BeautifulSoup | None:
    """Fetch a URL and return a BeautifulSoup object, or None on failure."""
    try:
        resp = SESSION.get(url, timeout=20)
        resp.raise_for_status()
        # Detect CAPTCHA / bot detection page
        if "robot" in resp.url or "captcha" in resp.text.lower():
            log.warning("Bot detection triggered for %s", url)
            return None
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as exc:
        log.error("Request failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------

def parse_price(price_str: str) -> float | None:
    """Convert a French-formatted price string like '12,99\xa0€' to a float."""
    if not price_str:
        return None
    # Remove currency symbols and whitespace, swap comma for dot
    cleaned = re.sub(r"[^\d,.]", "", price_str).replace(",", ".")
    # If multiple dots remain (e.g. "1.299.99"), keep only last dot
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Amazon.fr scraper
# ---------------------------------------------------------------------------

def scrape_category(category_name: str, url: str) -> list[dict]:
    """
    Scrape the first page of an Amazon.fr search result and return
    a list of product dicts: {title, price, url, asin}.
    """
    soup = fetch_page(url)
    if soup is None:
        return []

    products = []
    # Amazon search result items share the data-component-type attribute
    items = soup.select('[data-component-type="s-search-result"]')

    for item in items:
        if len(products) >= PRODUCTS_PER_CATEGORY:
            break

        # ASIN
        asin = item.get("data-asin", "").strip()
        if not asin:
            continue

        # Title
        title_tag = item.select_one("h2 span")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown product"

        # Price — try several selectors Amazon uses
        price_tag = (
            item.select_one(".a-price .a-offscreen")
            or item.select_one(".a-price-whole")
        )
        if price_tag:
            raw_price = price_tag.get_text(strip=True)
            price = parse_price(raw_price)
        else:
            price = None

        # Skip items without a price
        if price is None:
            continue

        # Product URL
        link_tag = item.select_one("h2 a")
        product_url = (
            "https://www.amazon.fr" + link_tag["href"]
            if link_tag and link_tag.get("href")
            else f"https://www.amazon.fr/dp/{asin}"
        )

        products.append(
            {
                "asin": asin,
                "title": title,
                "price": price,
                "url": product_url,
                "category": category_name,
            }
        )

    log.info("Category '%s': found %d products.", category_name, len(products))
    return products


# ---------------------------------------------------------------------------
# Price history persistence
# ---------------------------------------------------------------------------

def load_price_history() -> dict:
    """Load persisted price history from disk."""
    if Path(PRICE_DB_FILE).exists():
        try:
            with open(PRICE_DB_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not load price history: %s", exc)
    return {}


def save_price_history(history: dict) -> None:
    """Save price history to disk."""
    try:
        with open(PRICE_DB_FILE, "w", encoding="utf-8") as fh:
            json.dump(history, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.error("Could not save price history: %s", exc)


# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> bool:
    """Send a message via the Telegram Bot API."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(api_url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Telegram send failed: %s", exc)
        return False


def build_alert_message(product: dict, old_price: float, new_price: float) -> str:
    """Format a Telegram alert message for a price drop."""
    drop_pct = (old_price - new_price) / old_price * 100
    return (
        f"📉 <b>Price Drop Alert!</b>\n\n"
        f"🏷️ <b>Category:</b> {product['category']}\n"
        f"📦 <b>Product:</b> {product['title'][:120]}\n"
        f"💸 <b>Old price:</b> {old_price:.2f} €\n"
        f"✅ <b>New price:</b> {new_price:.2f} €\n"
        f"📊 <b>Saving:</b> -{drop_pct:.1f}%\n"
        f"🔗 <a href=\"{product['url']}\">View on Amazon.fr</a>"
    )


# ---------------------------------------------------------------------------
# Core check logic
# ---------------------------------------------------------------------------

def check_prices() -> None:
    """Main routine: scrape all categories, compare to history, alert on drops."""
    log.info("=== Price check started at %s ===", datetime.now().isoformat())
    history = load_price_history()
    alerts_sent = 0

    for category_name, url in CATEGORIES.items():
        log.info("Checking category: %s", category_name)
        products = scrape_category(category_name, url)

        # Polite delay between category requests
        time.sleep(3)

        for product in products:
            asin = product["asin"]
            new_price = product["price"]

            if asin not in history:
                # First time seeing this product — store baseline
                history[asin] = {
                    "title": product["title"],
                    "category": product["category"],
                    "url": product["url"],
                    "lowest_price": new_price,
                    "last_price": new_price,
                    "last_checked": datetime.now().isoformat(),
                }
                log.info(
                    "  [NEW] %s — %.2f €  (%s)",
                    product["title"][:60],
                    new_price,
                    asin,
                )
            else:
                old_price = history[asin]["last_price"]

                if new_price < old_price:
                    log.info(
                        "  [DROP] %s: %.2f → %.2f €  (%s)",
                        product["title"][:60],
                        old_price,
                        new_price,
                        asin,
                    )
                    message = build_alert_message(product, old_price, new_price)
                    if send_telegram(message):
                        alerts_sent += 1
                        log.info("  Telegram alert sent.")
                    else:
                        log.warning("  Failed to send Telegram alert.")

                    # Update lowest price if applicable
                    if new_price < history[asin].get("lowest_price", old_price):
                        history[asin]["lowest_price"] = new_price
                else:
                    log.info(
                        "  [OK]   %s — %.2f €  (was %.2f €)",
                        product["title"][:60],
                        new_price,
                        old_price,
                    )

                history[asin]["last_price"] = new_price
                history[asin]["last_checked"] = datetime.now().isoformat()
                history[asin]["title"] = product["title"]
                history[asin]["url"] = product["url"]

    save_price_history(history)
    log.info(
        "=== Check complete. %d alert(s) sent. Tracking %d products. ===",
        alerts_sent,
        len(history),
    )


# ---------------------------------------------------------------------------
# Scheduler / entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Amazon.fr Price Tracker starting up.")
    log.info("Categories: %s", ", ".join(CATEGORIES.keys()))
    log.info("Check interval: every %d minutes.", CHECK_INTERVAL_MINUTES)
    log.info("Telegram chat ID: %s", TELEGRAM_CHAT_ID)

    # Run immediately on start
    check_prices()

    # Then run on the configured schedule
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(check_prices)

    log.info("Scheduler running. Press Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
