"""Load discount data into Supabase using the normalized schema.

Schema:
  stores   (id, name)
  products (id, store_id, name, category, unit, store_name)
  offers   (id, product_id, current_price, was_price, start_date, end_date, is_discount)
"""

import logging
from config import supabase

logger = logging.getLogger(__name__)

# Cache store_name → store_id to avoid repeated lookups per batch
_store_id_cache: dict[str, str] = {}


def _get_or_create_store(store_name: str) -> str | None:
    """Return the UUID of the store, creating it if it doesn't exist."""
    if store_name in _store_id_cache:
        return _store_id_cache[store_name]
    try:
        result = (
            supabase.table("stores")
            .upsert({"name": store_name}, on_conflict="name")
            .execute()
        )
        store_id = result.data[0]["id"] if result.data else None
        if not store_id:
            # Fetch if upsert returned nothing
            r = supabase.table("stores").select("id").eq("name", store_name).single().execute()
            store_id = r.data["id"]
        _store_id_cache[store_name] = store_id
        return store_id
    except Exception as exc:
        logger.error("Store upsert failed for '%s': %s", store_name, exc)
        return None


def _get_or_create_product(store_id: str, store_name: str, name: str, category: str, unit: str) -> str | None:
    """Return the UUID of the product, creating it if it doesn't exist."""
    try:
        result = (
            supabase.table("products")
            .upsert(
                {
                    "store_id":   store_id,
                    "store_name": store_name,
                    "name":       name,
                    "category":   category,
                    "unit":       unit,
                },
                on_conflict="store_id,name",
            )
            .execute()
        )
        product_id = result.data[0]["id"] if result.data else None
        if not product_id:
            r = (
                supabase.table("products")
                .select("id")
                .eq("store_id", store_id)
                .eq("name", name)
                .single()
                .execute()
            )
            product_id = r.data["id"]
        return product_id
    except Exception as exc:
        logger.error("Product upsert failed for '%s': %s", name, exc)
        return None


def upsert_offers(offers: list[dict]) -> int:
    """Upsert offers into Supabase using the normalized stores → products → offers schema.

    Accepts dicts with these field names (supports both old and new naming):
      name / product_name      — product name
      store / store_name       — store name
      category                 — product category
      unit                     — e.g. "500g", "1kg"
      original_price / was_price      — original price (£)
      discounted_price / current_price — discounted price (£)
      is_discount              — True
      end_date                 — optional ISO date string

    Strategy: clear all existing offers, then insert fresh ones.
    This avoids duplicate/conflict issues and always reflects latest data.
    """
    if not offers:
        return 0
    if supabase is None:
        logger.warning("Supabase not available — skipping upsert.")
        return 0

    # Step 1: clear all existing offers for a clean slate
    try:
        supabase.table("offers").delete().eq("is_discount", True).execute()
        logger.info("Cleared existing offers.")
    except Exception as exc:
        logger.warning("Could not clear offers: %s", exc)

    # Step 2: build stores + products, then insert fresh offers
    count = 0
    for item in offers:
        store_name      = item.get("store") or item.get("store_name", "Unknown")
        product_name    = item.get("name") or item.get("product_name", "Unknown")
        category        = item.get("category", "")
        unit            = item.get("unit", "")
        was_price       = item.get("original_price") or item.get("was_price") or 0
        current_price   = item.get("discounted_price") or item.get("current_price") or 0
        is_discount     = item.get("is_discount", True)
        end_date        = item.get("end_date") or None

        store_id = _get_or_create_store(store_name)
        if not store_id:
            continue

        product_id = _get_or_create_product(store_id, store_name, product_name, category, unit)
        if not product_id:
            continue

        try:
            supabase.table("offers").insert({
                "product_id":    product_id,
                "current_price": current_price,
                "was_price":     was_price,
                "is_discount":   is_discount,
                "end_date":      end_date,
            }).execute()
            count += 1
        except Exception as exc:
            logger.error("Offer insert failed for '%s': %s", product_name, exc)

    logger.info("Inserted %d fresh offers into Supabase.", count)
    return count

