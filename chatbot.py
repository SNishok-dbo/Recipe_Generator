# -*- coding: utf-8 -*-
"""Guided conversational chatbot for the Inflation-Busting Recipe Generator.

State machine flow:
  INIT
    → user sends first message (budget + meal type extracted)
  COMPARE_PRICES
    → show cheapest store comparison for the budget
    → ask: vegetarian or non-vegetarian?
  ASK_DIET  (waiting for answer)
    → ask: how many people?
  ASK_SERVINGS  (waiting for answer)
    → ask: cooking time preference? (quick / medium / any)
  ASK_TIME  (waiting for answer)
    → ask: any ingredients to avoid?
  ASK_RESTRICTIONS  (waiting for answer)
    → generate 3 recipe suggestions
  SUGGESTIONS_SHOWN  (waiting for selection)
    → user types recipe name/number
    → show full recipe detail
  RECIPE_SHOWN  (offer refinements)
"""

import logging
import random
import re
import time

from config import get_llm, supabase

logger = logging.getLogger(__name__)

KNOWN_STORES = ["Tesco", "Sainsbury's", "Asda", "Morrisons", "Lidl", "Aldi"]

# Categories considered non-vegetarian
MEAT_FISH_CATEGORIES = {"meat", "fish", "poultry", "seafood"}


def filter_by_diet(ingredients: list[dict], diet: str) -> list[dict]:
    """Return ingredients filtered by diet. Vegetarian excludes meat/fish."""
    if not diet or diet.lower() in ("none", "non-vegetarian"):
        return ingredients
    return [
        i for i in ingredients
        if (i.get("category") or "").lower() not in MEAT_FISH_CATEGORIES
    ]


# ── LLM ───────────────────────────────────────────────────────────────────────

def _llm():
    return get_llm()


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _fallback_ingredients() -> list[dict]:
    """Called when Supabase has no data — tries Open Food Facts API live."""
    try:
        from data_ingestion.fetch_open_food_facts import fetch_off_discounts
        logger.info("Supabase empty — fetching live from Open Food Facts API.")
        result = fetch_off_discounts(products_per_category=15)
        if result:
            return result
    except Exception as exc:
        logger.warning("Could not fetch from Open Food Facts API: %s", exc)
    logger.warning("No live ingredient data available from any source.")
    return []


# ── Ingredient cache (5-minute TTL) ─────────────────────────────────────────────
_INGREDIENTS_CACHE: list[dict] = []
_CACHE_EXPIRES_AT: float = 0.0
_CACHE_TTL = 300  # seconds


def _normalise_offer_row(row: dict) -> dict:
    """Flatten a Supabase joined offer row into the flat format the chatbot expects.

    Supabase JOIN returns nested dicts:
      row["products"] = {"name": ..., "category": ..., "unit": ..., "store_name": ...}
    Also remaps was_price→original_price and current_price→discounted_price.
    """
    product = row.get("products") or {}
    return {
        "name":             product.get("name")       or row.get("name", "Unknown"),
        "store":            product.get("store_name") or row.get("store", "Unknown"),
        "category":         product.get("category")  or row.get("category", ""),
        "unit":             product.get("unit")       or row.get("unit", ""),
        "original_price":   row.get("was_price")      or row.get("original_price", 0),
        "discounted_price": row.get("current_price")  or row.get("discounted_price", 0),
        "is_discount":      row.get("is_discount", True),
    }


def get_all_ingredients() -> list[dict]:
    """Fetch discounted offers from Supabase (cached 5 min), falling back to Open Food Facts API.

    Uses a JOIN so we get name/category/unit from products and store name from stores.
    """
    global _INGREDIENTS_CACHE, _CACHE_EXPIRES_AT
    now = time.monotonic()
    if _INGREDIENTS_CACHE and now < _CACHE_EXPIRES_AT:
        return _INGREDIENTS_CACHE
    try:
        if supabase is None:
            return _fallback_ingredients()
        result = (
            supabase.table("offers")
            .select("*, products(name, category, unit, store_name)")
            .eq("is_discount", True)
            .order("current_price")
            .execute()
        )
        if result.data:
            data = [_normalise_offer_row(r) for r in result.data]
            # Filter out rows where name is still unknown (bad FK data)
            data = [d for d in data if d["name"] != "Unknown"]
        else:
            data = []
        if not data:
            logger.warning("Supabase returned no usable offers — fetching live from API.")
            data = _fallback_ingredients()
        _INGREDIENTS_CACHE = data
        _CACHE_EXPIRES_AT = now + _CACHE_TTL
        return data
    except Exception as exc:
        logger.warning("Supabase fetch failed: %s — using fallback.", exc)
        _INGREDIENTS_CACHE = _fallback_ingredients()
        _CACHE_EXPIRES_AT = now + _CACHE_TTL
        return _INGREDIENTS_CACHE


# ── Parse initial user message ────────────────────────────────────────────────

def parse_initial_message(user_input: str) -> dict:
    """Extract all intent fields from the user's single message.

    Returns dict with keys:
        budget (float|None), meal_type (str|None), diet (str|None),
        servings (int|None), cook_time (str|None), restrictions (str|None)
    """
    import json as _json
    prompt = (
        f'Extract cooking preferences from this message. Return ONLY valid JSON.\n\n'
        f'User message: "{user_input}"\n\n'
        f'Return exactly this JSON structure (use null for anything not mentioned):\n'
        f'{{\n'
        f'  "budget": <number in GBP or null>,\n'
        f'  "meal_type": <"breakfast"|"lunch"|"dinner"|"snack"|"any" or null>,\n'
        f'  "diet": <"Vegetarian"|"Non-Vegetarian" or null>,\n'
        f'  "servings": <integer or null>,\n'
        f'  "cook_time": <"quick"|"medium"|"any" or null>,\n'
        f'  "restrictions": <string describing allergies/dislikes or null>\n'
        f'}}\n\n'
        f'Examples: "quick" or "fast" → cook_time "quick". '
        f'"no nuts" → restrictions "no nuts". '
        f'"for 4" or "4 people" → servings 4. '
        f'Vegetarian/veggie → diet "Vegetarian". '
        f'Non-veg/meat → diet "Non-Vegetarian".'
    )
    try:
        response = _llm().invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if match:
            parsed = _json.loads(match.group())
            # Normalise servings to int
            if parsed.get("servings") is not None:
                try:
                    parsed["servings"] = int(parsed["servings"])
                except (ValueError, TypeError):
                    parsed["servings"] = None
            return parsed
    except Exception as exc:
        logger.warning("LLM parse failed: %s", exc)
    # Regex fallback — extract what we can
    budget_match = re.search(r"£\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:pound|gbp|£)", user_input, re.IGNORECASE)
    budget = float(budget_match.group(1) or budget_match.group(2)) if budget_match else None
    meal_type = None
    for m in ["breakfast", "lunch", "dinner", "snack"]:
        if m in user_input.lower():
            meal_type = m
            break
    diet = None
    low = user_input.lower()
    if "vegetarian" in low or "veggie" in low:
        diet = "Vegetarian"
    elif "non-veg" in low or "non veg" in low or "meat" in low:
        diet = "Non-Vegetarian"
    servings_match = re.search(r"(\d+)\s*(?:people|person|serving|of us)", low)
    servings = int(servings_match.group(1)) if servings_match else None
    cook_time = None
    if "quick" in low or "fast" in low:
        cook_time = "quick"
    elif "medium" in low:
        cook_time = "medium"
    return {
        "budget": budget,
        "meal_type": meal_type,
        "diet": diet,
        "servings": servings,
        "cook_time": cook_time,
        "restrictions": None,
    }


# ── Step 2: Price comparison across stores ────────────────────────────────────

def _fmt_end_date(raw: str | None) -> str:
    """Format an ISO date string (YYYY-MM-DD or timestamptz) as '04 Mar'."""
    if not raw:
        return "—"
    try:
        from datetime import datetime as _dt
        # Handle both '2026-03-04' and '2026-03-04T00:00:00+00:00'
        d = _dt.fromisoformat(raw[:10])
        return d.strftime("%d %b")
    except Exception:
        return str(raw)[:10]


def build_price_comparison(budget: float, ingredients: list[dict], diet: str = "None") -> str:
    """Build a markdown table showing affordable ingredients filtered by diet."""
    filtered = filter_by_diet(ingredients, diet)
    affordable = [i for i in filtered if i.get("discounted_price", 999) <= budget]
    affordable.sort(key=lambda x: x.get("discounted_price", 0))
    if not affordable:
        return "No discounted items found within your budget."
    lines = [
        "| Item | Store | Was | Now | Saving | Offer Ends |",
        "|------|-------|-----|-----|--------|------------|",
    ]
    for item in affordable[:10]:
        orig  = item.get("original_price") or item.get("was_price") or 0
        disc  = item.get("discounted_price") or item.get("current_price") or 0
        name  = item.get("name") or item.get("product_name", "?")
        store = item.get("store") or item.get("store_name", "?")
        ends  = _fmt_end_date(item.get("end_date"))
        lines.append(
            f"| {name} | {store} | £{orig:.2f} | £{disc:.2f} "
            f"| £{round(orig - disc, 2):.2f} off | {ends} |"
        )
    return "\n".join(lines)


# ── Refinement handler ────────────────────────────────────────────────────────


def format_numbered_product_list(ingredients: list[dict], diet: str, budget: float) -> tuple[str, list[dict]]:
    """Return a numbered markdown list of affordable diet-filtered products and the list itself."""
    filtered = filter_by_diet(ingredients, diet)
    affordable = [i for i in filtered if i.get("discounted_price", 999) <= budget]

    is_nonveg = not diet or diet.lower() in ("none", "non-vegetarian")

    def _sort_key(item):
        cat = (item.get("category") or "").lower()
        price = item.get("discounted_price", 999)
        # For non-veg: meat/fish first (priority 0), everything else after (priority 1)
        # For vegetarian: just sort by price
        priority = 0 if (is_nonveg and cat in MEAT_FISH_CATEGORIES) else 1
        return (priority, price)

    affordable.sort(key=_sort_key)
    affordable = affordable[:20]
    if not affordable:
        return "No products found within your budget.", []
    lines = []
    for idx, item in enumerate(affordable, 1):
        name     = item.get("name") or item.get("product_name", "?")
        store    = item.get("store") or item.get("store_name", "?")
        orig     = item.get("original_price") or 0
        disc     = item.get("discounted_price") or item.get("current_price") or 0
        category = (item.get("category") or "").capitalize()
        saving   = round(((orig - disc) / orig * 100) if orig > 0 else 0)
        if orig > disc and saving > 0:
            pricing = f"~~£{orig:.2f}~~ **£{disc:.2f}** `{saving}% off`"
        else:
            pricing = f"**£{disc:.2f}**"
        lines.append(f"**{idx}.** {name} — {pricing} _{store}_ · {category}")
    return "\n".join(lines), affordable


def parse_product_selection(user_input: str, product_list: list[dict]) -> list[dict]:
    """Parse '1, 3, 5' or '1 3 5' into selected products from product_list."""
    numbers = [int(n) for n in re.findall(r"\d+", user_input) if 1 <= int(n) <= len(product_list)]
    return [product_list[n - 1] for n in numbers] if numbers else product_list


# ── Refinement handler ────────────────────────────────────────────────────────

def handle_refinement(user_message: str, intent: dict, ingredients: list[dict]) -> tuple[dict, str]:
    """Parse a follow-up refinement message, update intent, regenerate suggestions."""
    msg = user_message.lower()

    # Budget change
    budget_match = re.search(r"£?(\d+(?:\.\d+)?)\s*(?:pound|£|budget)?", msg)
    if any(w in msg for w in ["budget", "cheaper", "less", "more", "pounds", "£"]) and budget_match:
        intent["budget"] = float(budget_match.group(1))

    # Diet change
    if "vegetarian" in msg or "veggie" in msg:
        intent["diet"] = "Vegetarian"
    elif "non-veg" in msg or "non-vegetarian" in msg or "meat" in msg or "non veg" in msg:
        intent["diet"] = "None"

    # Servings change
    servings_match = re.search(r"(\d+)\s*(?:people|person|serving|of us|guests)", msg)
    if servings_match:
        intent["servings"] = int(servings_match.group(1))

    # Cook time change
    if "quick" in msg or "fast" in msg or "faster" in msg:
        intent["cook_time"] = "quick"
    elif "medium" in msg:
        intent["cook_time"] = "medium"

    # Store change
    for store in KNOWN_STORES:
        if store.lower() in msg:
            intent["store"] = store
            break

    reply = generate_suggestions(intent, ingredients)
    return intent, reply


# ── Recipe name extraction ─────────────────────────────────────────────────────

def extract_recipe_choice(user_message: str, suggestions_text: str) -> str | None:
    """Return the recipe name the user picked, or None if unclear."""
    msg = user_message.lower().strip()

    # Numbered selection
    ordinal_map = {
        "recipe 1": 1, "first": 1, "1st": 1, "number 1": 1, "option 1": 1,
        "recipe 2": 2, "second": 2, "2nd": 2, "number 2": 2, "option 2": 2,
        "recipe 3": 3, "third": 3, "3rd": 3, "number 3": 3, "option 3": 3,
    }
    names = re.findall(r"\*\*Recipe \d+: (.+?)\*\*", suggestions_text)

    for trigger, num in ordinal_map.items():
        if trigger in msg and names and num <= len(names):
            return names[num - 1]

    # Name match
    for name in names:
        # Check if 2+ words of the recipe name appear in user message
        name_words = [w.lower() for w in name.split() if len(w) > 3]
        if sum(1 for w in name_words if w in msg) >= 2:
            return name

    # If message is short and we have suggestions, treat it as a name directly
    if len(user_message.split()) <= 8 and names:
        return user_message.strip()

    return None


def is_refinement(user_message: str) -> bool:
    """True if message looks like a refinement rather than a recipe name selection."""
    msg = user_message.lower()
    return any(w in msg for w in [
        "cheaper", "budget", "make it", "show", "vegetarian", "vegan",
        "faster", "quick", "people", "servings", "change", "different",
        "instead", "without", "no meat", "non-veg", "tesco", "asda",
        "sainsbury", "lidl", "aldi", "morrisons",
    ])


def generate_followup_suggestions(intent: dict, state: str) -> list[str]:
    """Generate 4 contextual follow-up suggestion chips using the LLM."""
    import json as _json
    budget = intent.get("budget") or 15
    diet = intent.get("diet") or "Non-Vegetarian"
    servings = intent.get("servings") or 2
    meal_type = intent.get("meal_type") or "any meal"
    cook_time = intent.get("cook_time") or "any"

    context = (
        f"Current: {meal_type}, budget £{budget}, {servings} servings, "
        f"diet {diet}, cook time {cook_time}. App state: {state}."
    )
    if state == "SUGGESTIONS_SHOWN":
        task = "Suggest 4 short follow-up questions a user might ask, such as changing budget, diet, servings, or cook time."
    elif state == "RECIPE_SHOWN":
        task = "Suggest 4 short follow-up questions like getting a new recipe, adjusting budget, changing diet, or getting a shopping list."
    else:
        task = "Suggest 4 short follow-up questions relevant to finding a budget recipe."

    prompt = (
        f"{context}\n\n{task}\n\n"
        f"Return ONLY a JSON array of exactly 4 short strings (under 8 words each). "
        f'Example: ["Make it vegetarian", "Reduce budget to £10", "Cook for 4 people", "Show quicker recipes"]'
    )
    try:
        response = get_llm().invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            items = _json.loads(match.group())
            if isinstance(items, list) and items:
                return [str(s) for s in items[:4]]
    except Exception as exc:
        logger.warning("Followup suggestions failed: %s", exc)
    return []


def _format_ingredients(ingredients: list[dict]) -> str:
    """Format ingredient list into a readable string for LLM prompts."""
    lines = []
    for i in ingredients:
        name = i.get("name") or i.get("product_name", "Unknown")
        store = i.get("store") or i.get("store_name", "")
        price = i.get("discounted_price") or i.get("current_price") or 0
        orig = i.get("original_price") or i.get("was_price") or 0
        lines.append(f"- {name} £{price:.2f} (was £{orig:.2f}) [{store}]")
    return "\n".join(lines)


def generate_suggestions(intent: dict, ingredients: list[dict], selected_items: list[dict] | None = None) -> str:
    """Generate 3 recipe title suggestions. Uses selected_items if provided, else filters by diet."""
    budget = intent.get("budget") or 15
    servings = intent.get("servings") or 2
    diet = intent.get("diet") or "None"
    meal_type = intent.get("meal_type") or "any meal"

    items_to_use = selected_items if selected_items else filter_by_diet(ingredients, diet)
    ingredient_text = _format_ingredients(items_to_use)
    if not ingredient_text:
        return "Sorry, I couldn't find any discounted ingredients in the database right now. Try running the data refresh."

    prompt = f"""You are a friendly UK budget cooking assistant.

A user wants recipe ideas for {meal_type}, budget £{budget}, {servings} servings, diet: {diet}.

Available discounted ingredients from UK supermarkets:
{ingredient_text}

Suggest exactly 3 recipe ideas that:
- Use ONLY ingredients from the list above (plus basic pantry staples: salt, pepper, oil, water, garlic, onion)
- Fit within the £{budget} total budget for {servings} servings
- Respect the diet restriction: {diet}

Format your response EXACTLY like this (no extra text):

\U0001f37d\ufe0f **Recipe 1: [Recipe Name]**
_One sentence description. Estimated cost: £X.XX_

\U0001f37d\ufe0f **Recipe 2: [Recipe Name]**
_One sentence description. Estimated cost: £X.XX_

\U0001f37d\ufe0f **Recipe 3: [Recipe Name]**
_One sentence description. Estimated cost: £X.XX_

After the 3 recipes, add one line:
\U0001f449 _Type a recipe name to get the full instructions, or ask me to adjust the budget, diet, or store._"""

    try:
        response = get_llm().invoke(prompt)
        return response.content if hasattr(response, "content") else str(response)
    except Exception as exc:
        return f"Sorry, I couldn't generate suggestions right now: {exc}"


def generate_recommendations_with_reasons(
    diet: str,
    ingredients: list[dict],
    count: int = 3,
    exclude_names: list[str] | None = None,
    user_context: dict | None = None,
) -> list[dict]:
    """Generate *count* recipe recommendations personalised to onboarding answers.

    Args:
        diet:          ``"Veg"`` or ``"Non-Veg"``
        ingredients:   list of available discounted product dicts
        count:         how many recipes to return (default 3)
        exclude_names: recipe names already shown — LLM is told to avoid these
        user_context:  dict with keys from onboarding: budget (str), time (str),
                       servings (int), diet (str). Used to personalise the prompt.

    Returns:
        list of *count* dicts, each with keys:
            name (str), description (str), estimated_cost (str),
            reasons (list of 3 str), category (str)
    """
    import json as _json

    diet_label = "Vegetarian" if diet == "Veg" else "Non-Vegetarian"
    filtered = filter_by_diet(ingredients, diet_label)
    # For non-veg: guarantee meat/fish items appear in the ingredient list the LLM sees.
    # Put meat/fish first, then shuffle the rest and fill up to 20 total.
    if diet_label == "Non-Vegetarian":
        meat_items  = [i for i in filtered if (i.get("category") or "").lower() in MEAT_FISH_CATEGORIES]
        other_items = [i for i in filtered if (i.get("category") or "").lower() not in MEAT_FISH_CATEGORIES]
        random.shuffle(meat_items)
        random.shuffle(other_items)
        shuffled = meat_items + other_items
    else:
        shuffled = filtered[:]
        random.shuffle(shuffled)
    ingredient_text = _format_ingredients(shuffled[:20])
    if not ingredient_text:
        ingredient_text = "Various UK supermarket products on discount."

    is_nonveg = diet_label == "Non-Vegetarian"

    # Variety hint changes every call so the LLM never repeats the same output
    _variety_themes = [
        "Mediterranean", "Asian-inspired", "British classic", "Middle-Eastern",
        "Mexican-inspired", "Indian-inspired", "Italian", "Caribbean", "French bistro",
        "American comfort food", "Scandinavian", "Japanese-inspired",
    ]
    _variety_hint = random.choice(_variety_themes)
    _variety_seed = random.randint(1000, 9999)

    _exclude_block = ""
    if exclude_names:
        _exclude_block = (
            "\nDo NOT suggest any of these already-shown recipes: "
            + ", ".join(f'"{n}"' for n in exclude_names)
            + ".\n"
        )

    # Build personalisation block from onboarding answers
    _ctx = user_context or {}
    _budget_hint  = _ctx.get("budget", "")
    _time_hint    = _ctx.get("time", "")
    _servings_hint = _ctx.get("servings", 2)
    _personalise_block = ""
    if _budget_hint or _time_hint:
        _personalise_block = "\nUSER PREFERENCES (follow strictly):\n"
        if _budget_hint:
            _personalise_block += f"- Budget per meal: {_budget_hint}. Keep estimated cost within this limit.\n"
        if _time_hint:
            _personalise_block += f"- Cooking time available: {_time_hint}. Only suggest recipes that fit within this time.\n"
        if _servings_hint and int(_servings_hint) != 2:
            _personalise_block += f"- Servings needed: {_servings_hint} people. Scale recipes accordingly.\n"
        _personalise_block += "\n"

    prompt = (
        "You are a UK budget cooking assistant. Your ONLY job right now is to recommend recipes.\n\n"
        "DIET RULE (STRICT — DO NOT IGNORE):\n"
        + (
            "The user has selected NON-VEGETARIAN. Every recipe MUST include at least one of: "
            "chicken, beef, lamb, pork, fish, tuna, salmon, prawn, shrimp, bacon, sausage, turkey, duck, mince, ham. "
            "NEVER suggest a vegetarian or plant-only recipe.\n\n"
            if is_nonveg else
            "The user has selected VEGETARIAN. Every recipe MUST be 100% plant-based or egg/dairy based. "
            "NO meat, poultry, fish, seafood, or any meat products whatsoever.\n\n"
        )
        + f"Variety theme for this session: {_variety_hint} (seed {_variety_seed})\n"
        + _exclude_block
        + _personalise_block
        + f"Available discounted UK supermarket ingredients:\n{ingredient_text}\n\n"
        f"Recommend exactly {count} DIFFERENT {diet_label.lower()} recipes. "
        f"Every recipe must have a unique name and distinct style — no repetition.\n"
        f"For each recipe choose EXACTLY 3 reasons from:\n"
        "- Uses currently available discounted ingredients\n"
        "- Budget-friendly and cost-effective\n"
        "- Well-balanced nutritional profile\n"
        f"- Matches {diet_label.lower()} preference perfectly\n"
        "- Quick preparation time (under 30 minutes)\n"
        "- Popular and family-friendly choice\n\n"
        "Return ONLY valid JSON — no extra text before or after:\n"
        '[\n'
        '  {\n'
        '    "name": "Recipe Name",\n'
        '    "description": "One appetising sentence about this dish",\n'
        '    "estimated_cost": "£X.XX for 2 servings",\n'
        '    "reasons": ["reason 1", "reason 2", "reason 3"]\n'
        '  }\n'
        ']'
    )

    try:
        response = _llm().invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            raw = match.group()
            # Repair common LLM JSON issues: trailing commas before ] or }
            raw = re.sub(r",\s*([\]}])", r"\1", raw)
            # Replace smart/curly quotes with straight quotes
            raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
            # Strip ALL control characters (including \r carriage return) except tab and newline
            raw = re.sub(r"[\x00-\x08\x0b-\x0c\x0d\x0e-\x1f\x7f]", "", raw)
            # Remove invalid JSON escape sequences (backslash before non-JSON-escape char)
            raw = re.sub(r'\\(?!["\\/bfnrtu])', r'', raw)
            try:
                result = _json.loads(raw)
            except _json.JSONDecodeError:
                # Pass 2: collapse literal newlines inside strings
                raw2 = re.sub(r'(?<!\\)\n', ' ', raw)
                try:
                    result = _json.loads(raw2)
                except _json.JSONDecodeError:
                    # Pass 3: field-by-field regex extraction
                    _names = re.findall(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
                    _descs = re.findall(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
                    _costs = re.findall(r'"estimated_cost"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
                    _all_reasons = re.findall(r'"reasons"\s*:\s*\[([^\]]*)\]', raw, re.DOTALL)
                    result = []
                    for _fi in range(min(len(_names), len(_descs), len(_costs), 3)):
                        _rblock = _all_reasons[_fi] if _fi < len(_all_reasons) else ""
                        _rsns = re.findall(r'"((?:[^"\\]|\\.)*)"', _rblock)[:3]
                        result.append({
                            "name":           _names[_fi],
                            "description":    _descs[_fi],
                            "estimated_cost": _costs[_fi],
                            "reasons":        _rsns,
                        })
                    if not result:
                        raise  # re-raise to fall through to static fallback
            if isinstance(result, list) and len(result) >= 1:
                # Deduplicate and collect up to `count` unique recipes
                cleaned = []
                seen_names: set[str] = set()
                for r in result:
                    _name = str(r.get("name", f"{diet_label} Recipe"))
                    if _name.lower() in seen_names:
                        continue
                    seen_names.add(_name.lower())
                    cleaned.append({
                        "name":           _name,
                        "description":    str(r.get("description", "A delicious budget-friendly dish.")),
                        "estimated_cost": str(r.get("estimated_cost", "~£6.00 for 2 servings")),
                        "reasons":        [str(s) for s in (r.get("reasons") or [])[:3]],
                    })
                    if len(cleaned) == count:
                        break
                if cleaned:
                    return cleaned
    except Exception:
        pass  # Connection/LLM failure

    # LLM unavailable — return empty so callers can show an appropriate message
    return []


def generate_onboarding_suggestions(
    diet: str,
    budget_str: str,
    cook_time: str,
    ingredients: list[dict],
) -> list[dict]:
    """Generate 8 personalised recipe suggestions based on onboarding Q&A.

    Args:
        diet:        ``"Veg"`` or ``"Non-Veg"``
        budget_str:  e.g. ``"Under £10"``, ``"£10–£20"``, ``"Over £35"``
        cook_time:   e.g. ``"Under 20 mins"``, ``"20–40 mins"``, ``"Any"``
        ingredients: list of available discounted product dicts

    Returns:
        list of 8 dicts with keys: name, description, estimated_cost, cook_time
    """
    import json as _json

    diet_label = "Vegetarian" if diet == "Veg" else "Non-Vegetarian"
    filtered = filter_by_diet(ingredients, diet_label)

    if diet_label == "Non-Vegetarian":
        meat_items  = [i for i in filtered if (i.get("category") or "").lower() in MEAT_FISH_CATEGORIES]
        other_items = [i for i in filtered if (i.get("category") or "").lower() not in MEAT_FISH_CATEGORIES]
        random.shuffle(meat_items)
        random.shuffle(other_items)
        shuffled = meat_items + other_items
    else:
        shuffled = filtered[:]
        random.shuffle(shuffled)

    ingredient_text = _format_ingredients(shuffled[:25])
    if not ingredient_text:
        ingredient_text = "Various UK supermarket products on discount."

    # ── Parse budget string ──────────────────────────────────────────────────
    budget_max = 20
    _bl = budget_str.lower()
    if "under" in _bl:
        _bm = re.search(r"(\d+)", budget_str)
        if _bm:
            budget_max = int(_bm.group(1))
    elif "\u2013" in budget_str or "-" in budget_str:
        _bp = re.findall(r"(\d+)", budget_str)
        if len(_bp) >= 2:
            budget_max = int(_bp[1])
    elif "over" in _bl:
        _bm = re.search(r"(\d+)", budget_str)
        if _bm:
            budget_max = int(_bm.group(1)) + 10

    # ── Parse cook time string ───────────────────────────────────────────────
    _ct = cook_time.lower()
    if "20" in _ct and "40" not in _ct:
        time_rule = "Each recipe MUST be ready in under 20 minutes (quick meals only)."
    elif "20" in _ct and "40" in _ct:
        time_rule = "Each recipe should take 20\u201340 minutes to prepare and cook."
    elif "40" in _ct and "60" in _ct:
        time_rule = "Each recipe should take 40\u201360 minutes to prepare and cook."
    else:
        time_rule = "No specific cook time constraint."

    is_nonveg = diet_label == "Non-Vegetarian"
    _seed = random.randint(1000, 9999)

    prompt = (
        f"You are a UK budget cooking assistant. Generate EXACTLY 8 unique {diet_label} recipe suggestions.\n\n"
        "DIET RULE (STRICT \u2014 DO NOT IGNORE):\n"
        + (
            "NON-VEGETARIAN: Every recipe MUST contain at least one of: "
            "chicken, beef, lamb, pork, fish, tuna, salmon, prawn, bacon, sausage, turkey, mince, ham, duck. "
            "NEVER suggest a vegetarian recipe.\n\n"
            if is_nonveg else
            "VEGETARIAN: Every recipe MUST be 100% plant-based or egg/dairy-based. "
            "Absolutely NO meat, poultry, fish or seafood.\n\n"
        )
        + f"BUDGET: Every recipe must cost under \u00a3{budget_max} for 2 servings.\n"
        f"COOK TIME: {time_rule}\n"
        f"Variety seed: {_seed}\n\n"
        f"Available discounted UK supermarket ingredients:\n{ingredient_text}\n\n"
        "Generate EXACTLY 8 different creative recipes. Vary cuisines "
        "(British, Italian, Indian, Mexican, Asian, Mediterranean, etc.). Every recipe must be unique.\n\n"
        "Return ONLY valid JSON \u2014 no text before or after:\n"
        "[\n"
        "  {\n"
        '    "name": "Recipe Name",\n'
        '    "description": "One appetising sentence about this dish",\n'
        '    "estimated_cost": "\u00a3X.XX for 2 servings",\n'
        '    "cook_time": "X mins"\n'
        "  }\n"
        "]"
    )

    try:
        response = _llm().invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            raw = match.group()
            raw = re.sub(r",\s*([\]}])", r"\1", raw)
            raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
            result = _json.loads(raw)
            if isinstance(result, list) and len(result) >= 1:
                cleaned = []
                for r in result[:8]:
                    cleaned.append({
                        "name":           str(r.get("name", f"{diet_label} Recipe")),
                        "description":    str(r.get("description", "A delicious budget-friendly dish.")),
                        "estimated_cost": str(r.get("estimated_cost", f"~\u00a3{max(2, budget_max // 3)} for 2")),
                        "cook_time":      str(r.get("cook_time", "30 mins")),
                    })
                while len(cleaned) < 8:
                    cleaned.append({
                        "name":           f"{diet_label} Recipe {len(cleaned) + 1}",
                        "description":    "A delicious budget-friendly dish.",
                        "estimated_cost": f"~\u00a3{max(2, budget_max // 3)} for 2",
                        "cook_time":      "30 mins",
                    })
                return cleaned[:8]
    except Exception as exc:
        logger.warning("generate_onboarding_suggestions failed: %s", exc)

    # LLM unavailable — return empty so callers can show an appropriate message
    return []


def handle_global_chat(
    message: str,
    app_state: str,
    diet: str,
    intent: dict,
    ingredients: list[dict],
    recommendations: list[dict] | None = None,
) -> dict:
    """Process any user chat message and return a structured action.

    Returns a dict with keys:
        action  – one of: "set_diet" | "show_recipe" | "manual_add" |
                           "refine_recipe" | "answer" | "load_recs"
        reply   – str  always present; the chat reply to show the user
        diet    – "Veg"|"Non-Veg"  (for set_diet / load_recs)
        recipe_name – str  (for show_recipe)
        ingredients – list[str]  (for manual_add)
        recipe_content – str  (for show_recipe when directly expanded)
    """
    import json as _json

    diet_label = "Vegetarian" if diet == "Veg" else "Non-Vegetarian"
    recs_text  = ""
    if recommendations:
        recs_text = "\n".join(
            f"{i+1}. {r.get('name','Recipe')} — {r.get('description','')}"
            for i, r in enumerate(recommendations[:3])
        )

    ingreds_text = "\n".join(
        f"- {i.get('name','')} £{i.get('discounted_price',0):.2f} [{i.get('store','')}]"
        for i in ingredients[:30]
    )

    system = (
        "You are a smart assistant for an Inflation-Busting Recipe Generator app.\n"
        f"Current screen: {app_state}. User's diet preference: {diet_label}.\n"
        "Available recipes on screen:\n" + (recs_text or "None yet") + "\n"
        "Available discounted ingredients:\n" + (ingreds_text or "None") + "\n\n"
        "Decide what the user wants to do. Return ONLY valid JSON — no extra text:\n"
        '{\n'
        '  "action": <"set_diet"|"show_recipe"|"manual_add"|"refine_recipe"|"load_recs"|"answer">,\n'
        '  "diet": <"Veg"|"Non-Veg" or null>,\n'
        '  "recipe_name": <exact recipe name string or null>,\n'
        '  "add_ingredients": <list of ingredient name strings to add to manual selection, or []>,\n'
        '  "reply": <friendly 1-2 sentence reply to the user>\n'
        "}\n\n"
        "Rules:\n"
        "- set_diet: user mentions veg/vegetarian → diet=Veg; non-veg/meat/chicken → diet=Non-Veg\n"
        "- show_recipe: user wants to see a specific recipe (by number 1/2/3 or name)\n"
        "- manual_add: user mentions specific ingredients they want to INCLUDE\n"
        "- refine_recipe: user is asking to change/adjust the current recipe (budget, servings, etc.)\n"
        "- load_recs: user wants new recommendations or to start over\n"
        "- answer: everything else — just answer helpfully"
    )

    try:
        response = _llm().invoke(f"{system}\n\nUser says: {message}")
        text = response.content if hasattr(response, "content") else str(response)
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if match:
            result = _json.loads(match.group())
            result.setdefault("action", "answer")
            result.setdefault("reply",  "I'm here to help! What would you like to do?")
            result.setdefault("diet",   diet)
            result.setdefault("recipe_name", None)
            result.setdefault("add_ingredients", [])
            return result
    except Exception as exc:
        logger.warning("handle_global_chat LLM failed: %s", exc)

    # Fallback: simple keyword detection
    msg_lower = message.lower()
    if any(w in msg_lower for w in ("vegetarian", "veg", "plant", "no meat")):
        return {"action": "set_diet", "diet": "Veg", "recipe_name": None,
                "add_ingredients": [],
                "reply": "Switching to Vegetarian mode — fetching fresh recommendations!"}
    if any(w in msg_lower for w in ("non-veg", "nonveg", "meat", "chicken", "fish", "beef", "pork")):
        return {"action": "set_diet", "diet": "Non-Veg", "recipe_name": None,
                "add_ingredients": [],
                "reply": "Switching to Non-Vegetarian mode — fetching fresh recommendations!"}
    if any(w in msg_lower for w in ("recipe 1", "first recipe", "recipe one")):
        name = recommendations[0].get("name") if recommendations else None
        return {"action": "show_recipe", "diet": diet, "recipe_name": name,
                "add_ingredients": [],
                "reply": f"Opening {name}…" if name else "Let me find that recipe."}
    if any(w in msg_lower for w in ("recipe 2", "second recipe", "recipe two")):
        name = recommendations[1].get("name") if (recommendations and len(recommendations) > 1) else None
        return {"action": "show_recipe", "diet": diet, "recipe_name": name,
                "add_ingredients": [],
                "reply": f"Opening {name}…" if name else "Let me find that recipe."}
    if any(w in msg_lower for w in ("recipe 3", "third recipe", "recipe three")):
        name = recommendations[2].get("name") if (recommendations and len(recommendations) > 2) else None
        return {"action": "show_recipe", "diet": diet, "recipe_name": name,
                "add_ingredients": [],
                "reply": f"Opening {name}…" if name else "Let me find that recipe."}
    return {"action": "answer", "diet": diet, "recipe_name": None,
            "add_ingredients": [],
            "reply": f"I can help with {diet_label} recipes! Try asking me to show a recipe, change your diet preference, or add specific ingredients."}


def expand_recipe(recipe_name: str, intent: dict, ingredients: list[dict]) -> str:
    """Generate the full detailed recipe for a chosen recipe name."""
    budget = intent.get("budget") or 15
    servings = intent.get("servings") or 2
    diet = intent.get("diet") or "None"

    ingredient_text = _format_ingredients(ingredients)

    prompt = f"""You are a friendly UK budget cooking assistant.

Generate a FULL detailed recipe for: **{recipe_name}**

Available discounted ingredients:
{ingredient_text}

User constraints:
- Budget: £{budget} total for {servings} servings
- Diet: {diet}
- Use ONLY the available ingredients above + basic pantry staples (salt, pepper, oil, water, garlic, onion)

Format your response in clean Markdown:

## {recipe_name}

**Servings:** {servings}
**Estimated total cost:** £X.XX
**Cost per serving:** £X.XX
**Prep time:** X minutes
**Cook time:** X minutes

### Ingredients
- ...

### Method
1. ...
2. ...

### Cost Breakdown
| Ingredient | Cost |
|-----------|------|
| ... | £X.XX |
| **Total** | **£X.XX** |

### Money-Saving Tip
...

### Nutritional Notes (per serving)
Approximate calories, protein, carbs — keep it brief."""

    try:
        response = get_llm().invoke(prompt)
        return response.content if hasattr(response, "content") else str(response)
    except Exception as exc:
        return f"Sorry, couldn't generate the full recipe: {exc}"


