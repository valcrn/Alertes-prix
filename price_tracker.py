"""
Amazon.fr Price Tracker - Version 4.1
Catégories : PS5 Games, Whisky, Salon de jardin.
Comparaison bon plan : Dealabs, Idealo, LeGuide, PriceSpy.
"""

import json
import re
import subprocess
import time
import logging
from datetime import datetime
from pathlib import Path
from threading import Thread

import requests
import schedule
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = "8346118269:AAGxoRW18oEVNAEhCnzEBo6poymjMu3dHUA"
TELEGRAM_CHAT_ID = "8604666788"

CATEGORIES = {
    "PS5 Games": [
        "https://www.amazon.fr/s?k=jeux+ps5&s=price-asc-rank",
        "https://www.auchan.fr/recherche?query=jeux+ps5",
        "https://www.e.leclerc/cat/jeux-video-ps5",
    ],
    "Whisky": [
        "https://www.amazon.fr/s?k=whisky&s=price-asc-rank",
        "https://www.whiskysite.nl/whisky",
        "https://www.drankdozijn.nl/whisky-en-bourbon",
        "https://www.auchan.fr/recherche?query=whisky",
        "https://www.e.leclerc/cat/whisky",
    ],
    "Salon de jardin": [
        "https://www.amazon.fr/s?k=salon+de+jardin+table+6+chaises",
        "https://www.leroymerlin.fr/recherche?q=salon+de+jardin",
        "https://www.but.fr/recherche/?q=salon+de+jardin",
        "https://www.conforama.fr/search?q=salon+de+jardin",
        "https://www.carrefour.fr/s?q=salon+de+jardin",
        "https://www.e.leclerc/cat/salon-de-jardin",
    ],
}

PRODUCTS_PER_CATEGORY = 200
CHECK_INTERVAL_MINUTES = 60
PRICE_DB_FILE = "price_history.json"
ALERTS_FILE = "custom_alerts.json"
TRACKER_STATUS = {
    "running": False,
    "last_check": None,
    "total_products": 0,
    "paused": False,
}

# Dealabs search keywords per tracked category (used for category-level /bonplan scan)
DEALABS_KEYWORDS = {
    "PS5 Games": "jeux ps5",
    "Whisky": "whisky",
    "Salon de jardin": "salon de jardin",
}

BON_PLAN_THRESHOLD = 0.20  # alert when tracked price is ≥20% below comparison reference

# Regex to exclude small sample / miniature bottles from tracking.
# Uses word boundaries (\b) so "mini" won't match inside "aluminium", etc.
_EXCLUDE_RE = re.compile(
    r"\b(?:sample|échantillon|miniature|5\s*cl|6\s*cl|10\s*cl|20\s*cl|mini|tasting\s+set)\b",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("price_tracker.log")],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        resp = SESSION.get(url, timeout=20)
        resp.raise_for_status()
        if "robot" in resp.url or "captcha" in resp.text.lower():
            log.warning("Bot detection pour %s", url)
            return None
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as exc:
        log.error("Erreur requête %s: %s", url, exc)
        return None


def fetch_page_curl(
    url: str, extra_headers: list | None = None
) -> BeautifulSoup | None:
    """Fetch via curl subprocess to bypass TLS-fingerprint bot detection."""
    cmd = [
        "curl",
        "-s",
        "-L",
        url,
        "-H",
        "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H",
        "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
        "-H",
        "Sec-Fetch-Dest: document",
        "-H",
        "Sec-Fetch-Mode: navigate",
        "--compressed",
        "--max-time",
        "25",
    ]
    if extra_headers:
        for h in extra_headers:
            cmd += ["-H", h]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 or not result.stdout:
            log.error("curl échoué pour %s", url)
            return None
        if "Access Denied" in result.stdout or "captcha" in result.stdout.lower():
            log.warning("Accès refusé pour %s", url)
            return None
        return BeautifulSoup(result.stdout, "lxml")
    except subprocess.TimeoutExpired:
        log.error("curl timeout pour %s", url)
        return None
    except Exception as exc:
        log.error("Erreur curl %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Price parsing helper
# ---------------------------------------------------------------------------


def parse_price(price_str: str) -> float | None:
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d,.]", "", price_str).replace(",", ".")
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def is_excluded_title(title: str) -> bool:
    """Return True if the product title matches a sample/miniature exclusion pattern."""
    return bool(_EXCLUDE_RE.search(title))


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


# ---------------------------------------------------------------------------
# Site scrapers
# ---------------------------------------------------------------------------


def scrape_amazon(url: str, category_name: str) -> list[dict]:
    soup = fetch_page(url)
    if not soup:
        return []
    products = []
    page = 1
    while len(products) < PRODUCTS_PER_CATEGORY:
        if page > 1:
            soup = fetch_page(url + f"&page={page}")
            if not soup:
                break
        items = soup.select('[data-component-type="s-search-result"]')
        if not items:
            break
        for item in items:
            if len(products) >= PRODUCTS_PER_CATEGORY:
                break
            asin = item.get("data-asin", "").strip()
            if not asin:
                continue
            title_tag = item.select_one("h2 span")
            title = title_tag.get_text(strip=True) if title_tag else "Produit inconnu"
            price_tag = item.select_one(".a-price .a-offscreen") or item.select_one(
                ".a-price-whole"
            )
            price = parse_price(price_tag.get_text(strip=True)) if price_tag else None
            if not price:
                continue
            if is_excluded_title(title):
                log.debug("Exclu (échantillon): %s", title[:80])
                continue
            link_tag = item.select_one("h2 a")
            product_url = (
                "https://www.amazon.fr" + link_tag["href"]
                if link_tag
                else f"https://www.amazon.fr/dp/{asin}"
            )
            products.append(
                {
                    "asin": asin,
                    "title": title,
                    "price": price,
                    "url": product_url,
                    "category": category_name,
                    "source": "Amazon.fr",
                }
            )
        page += 1
        time.sleep(2)
    return products


def scrape_generic(url: str, category_name: str, source_name: str) -> list[dict]:
    soup = fetch_page(url)
    if not soup:
        return []
    products = []
    price_tags = soup.select("[class*='price'], [class*='prix'], [class*='Price']")
    for i, tag in enumerate(price_tags[:PRODUCTS_PER_CATEGORY]):
        price = parse_price(tag.get_text(strip=True))
        if not price:
            continue
        parent = tag.find_parent(["div", "li", "article"])
        title_tag = (
            parent.select_one(
                "h2, h3, [class*='title'], [class*='name'], [class*='nom']"
            )
            if parent
            else None
        )
        title = (
            title_tag.get_text(strip=True)
            if title_tag
            else f"Produit {source_name} #{i + 1}"
        )
        link_tag = parent.find("a") if parent else None
        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url
        if not product_url.startswith("http"):
            product_url = "/".join(url.split("/")[:3]) + product_url
        if is_excluded_title(title):
            log.debug("Exclu (échantillon): %s", title[:80])
            continue
        asin = f"{source_name}_{i}_{hash(title) % 100000}"
        products.append(
            {
                "asin": asin,
                "title": title[:120],
                "price": price,
                "url": product_url,
                "category": category_name,
                "source": source_name,
            }
        )
    return products


def scrape_category(category_name: str, urls: list[str]) -> list[dict]:
    all_products: list[dict] = []
    SOURCE_MAP = {
        "amazon.fr": ("scrape_amazon", "Amazon.fr"),
        "auchan": (None, "Auchan"),
        "leclerc": (None, "Leclerc"),
        "leroymerlin": (None, "Leroy Merlin"),
        "but.fr": (None, "But"),
        "conforama": (None, "Conforama"),
        "carrefour": (None, "Carrefour"),
        "drankdozijn": (None, "Drankdozijn"),
        "whiskysite": (None, "Whiskysite.nl"),
    }
    for url in urls:
        matched_source = None
        for key, (fn, src) in SOURCE_MAP.items():
            if key in url:
                matched_source = (fn, src)
                break
        if matched_source and matched_source[0] == "scrape_amazon":
            products = scrape_amazon(url, category_name)
        elif matched_source:
            products = scrape_generic(url, category_name, matched_source[1])
        else:
            products = scrape_generic(url, category_name, url.split("/")[2])
        all_products.extend(products)
        time.sleep(3)
    log.info("Catégorie '%s': %d produits trouvés.", category_name, len(all_products))
    return all_products


# ---------------------------------------------------------------------------
# Price comparison sources — bon plan detection
# ---------------------------------------------------------------------------


def get_dealabs_prices(query: str) -> list[float]:
    """
    Search Dealabs.com for a product query and return reference market prices
    (nextBestPrice from each active deal's embedded data-vue3 JSON).
    Uses curl with full browser headers + Referer to bypass bot detection.
    """
    url = f"https://www.dealabs.com/search?q={requests.utils.quote(query)}"
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "-L",
                url,
                "-H",
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "-H",
                "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "-H",
                "Accept-Language: fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                "-H",
                "Accept-Encoding: gzip, deflate, br",
                "-H",
                "Referer: https://www.google.fr/",
                "-H",
                'sec-ch-ua: "Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
                "-H",
                "sec-ch-ua-mobile: ?0",
                "-H",
                'sec-ch-ua-platform: "Windows"',
                "-H",
                "Sec-Fetch-Dest: document",
                "-H",
                "Sec-Fetch-Mode: navigate",
                "-H",
                "Sec-Fetch-Site: cross-site",
                "-H",
                "Upgrade-Insecure-Requests: 1",
                "-b",
                "cookiefile=/dev/null",
                "-c",
                "/dev/null",
                "--compressed",
                "--max-time",
                "30",
            ],
            capture_output=True,
            text=True,
            timeout=35,
        )
    except Exception as exc:
        log.error("[Dealabs] Erreur curl '%s': %s", query, exc)
        return []

    if result.returncode != 0 or not result.stdout:
        log.warning("[Dealabs] curl échoué pour '%s'", query)
        return []

    soup = BeautifulSoup(result.stdout, "lxml")
    prices: list[float] = []
    for art in soup.select("article.cept-thread-item"):
        vue3_div = art.select_one("[data-vue3]")
        if not vue3_div:
            continue
        try:
            data = json.loads(vue3_div.get("data-vue3", "{}"))
            thread = data.get("props", {}).get("thread", {})
        except (json.JSONDecodeError, AttributeError):
            continue
        if thread.get("isExpired") or thread.get("status") == "Archived":
            continue
        if thread.get("type") not in ("Deal", "Coupon"):
            continue
        next_best = thread.get("nextBestPrice")
        price = thread.get("price")
        ref = next_best if next_best is not None else price
        if ref is not None:
            try:
                prices.append(float(ref))
            except (ValueError, TypeError):
                pass

    log.info("[Dealabs] '%s' -> %d prix de référence.", query, len(prices))
    return prices


def get_idealo_prices(query: str) -> list[float]:
    """
    Search Idealo.fr for a product query and return listed prices.
    Currently returns [] because Idealo blocks all automated access (HTTP 503).
    The function is structured to be activated if access is restored.
    """
    url = (
        f"https://www.idealo.fr/comparer/recherche.html?q={requests.utils.quote(query)}"
    )
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "-L",
                url,
                "-H",
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "-H",
                "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "-H",
                "Accept-Language: fr-FR,fr;q=0.9",
                "-H",
                "Referer: https://www.google.fr/",
                "--compressed",
                "--max-time",
                "20",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception as exc:
        log.error("[Idealo] Erreur curl '%s': %s", query, exc)
        return []

    if not result.stdout or len(result.stdout) < 10000:
        log.warning("[Idealo] Accès bloqué pour '%s' (réponse trop courte).", query)
        return []

    soup = BeautifulSoup(result.stdout, "lxml")
    prices: list[float] = []
    for el in soup.select("[class*='price'], [class*='Price'], [class*='prix']"):
        p = parse_price(el.get_text(strip=True))
        if p and p > 1:
            prices.append(p)

    # Deduplicate and cap
    prices = sorted(set(prices))[:20]
    log.info("[Idealo] '%s' -> %d prix.", query, len(prices))
    return prices


def get_leguide_prices(query: str) -> list[float]:
    """
    Search LeGuide.com for a product query and return listed prices.
    Currently returns [] because LeGuide blocks automated access (HTTP 403).
    The function is structured to be activated if access is restored.
    """
    url = f"https://www.leguide.com/av/recherche.cgi?req={requests.utils.quote(query)}"
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "-L",
                url,
                "-H",
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "-H",
                "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "-H",
                "Accept-Language: fr-FR,fr;q=0.9",
                "-H",
                "Referer: https://www.google.fr/",
                "--compressed",
                "--max-time",
                "20",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception as exc:
        log.error("[LeGuide] Erreur curl '%s': %s", query, exc)
        return []

    if not result.stdout or len(result.stdout) < 5000:
        log.warning("[LeGuide] Accès bloqué pour '%s'.", query)
        return []

    soup = BeautifulSoup(result.stdout, "lxml")
    prices: list[float] = []
    for el in soup.select(
        "[class*='price'], [class*='Price'], [class*='prix'], .price"
    ):
        p = parse_price(el.get_text(strip=True))
        if p and p > 1:
            prices.append(p)

    prices = sorted(set(prices))[:20]
    log.info("[LeGuide] '%s' -> %d prix.", query, len(prices))
    return prices


def get_pricespy_prices(query: str) -> list[float]:
    """
    Search PriceSpy (fr.pricespy.com) for a product query and return listed prices.
    Currently returns [] because PriceSpy blocks automated access (HTTP 403 / timeout).
    The function is structured to be activated if access is restored.
    """
    url = f"https://fr.pricespy.com/search?search={requests.utils.quote(query)}"
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "-L",
                url,
                "-H",
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "-H",
                "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "-H",
                "Accept-Language: fr-FR,fr;q=0.9",
                "-H",
                "Referer: https://www.google.fr/",
                "--compressed",
                "--max-time",
                "20",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception as exc:
        log.error("[PriceSpy] Erreur curl '%s': %s", query, exc)
        return []

    if not result.stdout or len(result.stdout) < 5000:
        log.warning("[PriceSpy] Accès bloqué pour '%s'.", query)
        return []

    soup = BeautifulSoup(result.stdout, "lxml")
    prices: list[float] = []

    # PriceSpy uses JSON-LD for product listings
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                items = data
            elif data.get("@type") == "ItemList":
                items = [el.get("item", el) for el in data.get("itemListElement", [])]
            else:
                items = [data]
            for item in items:
                offer = item.get("offers", {})
                p = parse_price(str(offer.get("price", "")))
                if p and p > 1:
                    prices.append(p)
        except (json.JSONDecodeError, AttributeError):
            continue

    # Fallback: raw price selectors
    if not prices:
        for el in soup.select("[class*='price'], [class*='Price']"):
            p = parse_price(el.get_text(strip=True))
            if p and p > 1:
                prices.append(p)

    prices = sorted(set(prices))[:20]
log.info("[PriceSpy] '%s' -> %d prix.", query, len(prices))
