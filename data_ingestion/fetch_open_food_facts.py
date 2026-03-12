"""Open Food Facts API integration for real UK product data.

Fetches real UK supermarket products from the Open Food Facts API and
applies realistic UK price ranges + discount logic to produce offer rows
compatible with the existing Supabase schema.

API Docs: https://world.openfoodfacts.org/data
Free, legal and no API key required.
"""

import logging
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

import requests

# Persistent session — reuses TCP connections across all category requests
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "InflationBustingRecipeApp/1.0 (contact@example.com)"})

logger = logging.getLogger(__name__)

# ── API config ────────────────────────────────────────────────────────────────
OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
OFF_HEADERS = {
    "User-Agent": "InflationBustingRecipeApp/1.0 (contact@example.com)"
}

REQUEST_TIMEOUT = 30  # seconds per API call (OFF can be slow — 20-25s is typical)

# ── UK store names to assign products to ─────────────────────────────────────
UK_STORES = ["Tesco", "Sainsbury's", "Asda", "Morrisons", "Lidl", "Aldi"]

# ── Category search tags & realistic UK base prices (£) ──────────────────────
# base_price is the typical "was_price" for a standard-size unit in the UK.
CATEGORY_CONFIG: List[Dict[str, Any]] = [
    {
        "label": "meat",
        "off_category": "en:meats",
        "base_price_range": (2.50, 5.50),
    },
    {
        "label": "fish",
        "off_category": "en:fishes",           # en:fish-and-seafood returns 0 — en:fishes works
        "base_price_range": (2.00, 5.00),
    },
    {
        "label": "fresh",
        "off_category": "en:fresh-vegetables",
        "base_price_range": (0.40, 1.50),
    },
    {
        "label": "dairy",
        "off_category": "en:dairy-products",
        "base_price_range": (0.80, 4.00),
    },
    {
        "label": "tins",
        "off_category": "en:canned-foods",
        "base_price_range": (0.60, 3.00),
    },
    {
        "label": "cupboard",
        "off_category": "en:pasta",
        "base_price_range": (0.30, 2.00),
    },
    {
        "label": "bakery",
        "off_category": "en:breads",
        "base_price_range": (0.75, 2.50),
    },
    {
        "label": "frozen",
        "off_category": "en:frozen-foods",
        "base_price_range": (0.90, 3.50),
    },
]

# ── Discount band (% off original price) ─────────────────────────────────────
MIN_DISCOUNT_PCT = 15
MAX_DISCOUNT_PCT = 40


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_off_products(category_tag: str, page_size: int = 20) -> List[Dict[str, Any]]:
    """Call Open Food Facts search API and return a list of product dicts.

    Strategy:
      1. Try with UK country filter (best quality for UK app)
      2. If 0 results, retry without country filter (some tags have no UK entries)
      3. On any network error, retry once after 2s
      4. If all attempts fail, return []
    """
    base_params = {
        "action": "process",
        "tagtype_0": "categories",
        "tag_contains_0": "contains",
        "tag_0": category_tag,
        "page_size": page_size,
        "page": 1,
        "json": 1,
        "fields": "product_name,quantity,brands,categories_tags",
        "sort_by": "popularity",
    }

    def _do_request(params: dict, attempt: int = 1) -> List[Dict[str, Any]]:
        try:
            resp = _SESSION.get(OFF_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json().get("products", [])
        except requests.RequestException as exc:
            if attempt == 1:
                logger.warning("OFF request failed for '%s' (attempt 1), retrying: %s", category_tag, exc)
                return _do_request(params, attempt=2)  # immediate retry, no sleep
            logger.warning("OFF request failed for '%s' (attempt 2): %s", category_tag, exc)
            return []

    # Attempt 1: with UK country filter
    uk_params = {
        **base_params,
        "tagtype_1": "countries",
        "tag_contains_1": "contains",
        "tag_1": "en:united-kingdom",
    }
    products = _do_request(uk_params)

    # Attempt 2: without UK filter (some categories have no UK-tagged entries)
    if not products:
        logger.info("No UK results for '%s', retrying without country filter.", category_tag)
        products = _do_request(base_params)

    return products


def _build_offer(product: Dict[str, Any], category_label: str, base_price_range: tuple) -> Dict[str, Any] | None:
    """Convert a raw OFF product into an offer row."""
    name = (product.get("product_name") or "").strip()
    if not name or len(name) < 3:
        return None

    quantity = (product.get("quantity") or "").strip() or "1 unit"

    # Assign to a random UK store
    store = random.choice(UK_STORES)

    # Generate realistic UK prices
    original_price = round(random.uniform(*base_price_range), 2)
    discount_pct = random.randint(MIN_DISCOUNT_PCT, MAX_DISCOUNT_PCT)
    discounted_price = round(original_price * (1 - discount_pct / 100), 2)

    return {
        "name": name,
        "store": store,
        "category": category_label,
        "unit": quantity,
        "original_price": original_price,
        "discounted_price": discounted_price,
        "is_discount": True,
        "source": "open_food_facts",
    }


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_off_discounts(products_per_category: int = 15) -> List[Dict[str, Any]]:
    """Fetch real UK products from Open Food Facts and return offer rows.

    Each product has:
      - name           : real product name from Open Food Facts
      - store          : assigned UK store (Tesco / Asda / etc.)
      - category       : meat / fish / fresh / dairy / tins / cupboard / bakery / frozen
      - unit           : real quantity string from OFF (e.g. "400g", "1.5kg")
      - original_price : realistic UK base price (£)
      - discounted_price: original_price minus 15-40% discount
      - is_discount    : True

    Falls back silently to an empty list on any network error so the app
    continues working with the static dataset.
    """
    all_offers: List[Dict[str, Any]] = []
    seen_names: set = set()

    def _fetch_category(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        products = _fetch_off_products(cfg["off_category"], page_size=products_per_category)
        results = []
        for product in products:
            offer = _build_offer(product, cfg["label"], cfg["base_price_range"])
            if offer:
                results.append(offer)
        if not results:
            logger.warning("No API results for category '%s' — skipping (no static fallback).", cfg["label"])
        return results

    with ThreadPoolExecutor(max_workers=len(CATEGORY_CONFIG)) as executor:
        futures = {executor.submit(_fetch_category, cfg): cfg["label"] for cfg in CATEGORY_CONFIG}
        for future in as_completed(futures):
            label = futures[future]
            try:
                offers = future.result()
                added = 0
                for offer in offers:
                    key = offer["name"].lower()
                    if key not in seen_names:
                        seen_names.add(key)
                        all_offers.append(offer)
                        added += 1
                logger.info("  → %d offers added for '%s'", added, label)
            except Exception as exc:
                logger.warning("Category '%s' failed: %s", label, exc)

    logger.info("Open Food Facts: fetched %d total offers.", len(all_offers))
    return all_offers


def fetch_off_discounts_summary() -> str:
    """Return a human-readable summary of what was fetched from OFF API."""
    offers = fetch_off_discounts()
    if not offers:
        return "⚠️ Open Food Facts API returned no data. Check your network connection and try again."

    by_store: Dict[str, int] = {}
    by_cat: Dict[str, int] = {}
    for o in offers:
        by_store[o["store"]] = by_store.get(o["store"], 0) + 1
        by_cat[o["category"]] = by_cat.get(o["category"], 0) + 1

    lines = [f"✅ Fetched **{len(offers)} real UK products** from Open Food Facts API:\n"]
    lines.append("**By store:**")
    for store, count in sorted(by_store.items()):
        lines.append(f"  - {store}: {count} products")
    lines.append("\n**By category:**")
    for cat, count in sorted(by_cat.items()):
        lines.append(f"  - {cat}: {count} products")
    return "\n".join(lines)
