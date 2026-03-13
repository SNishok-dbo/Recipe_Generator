"""app.py - Inflation-Busting Recipe Generator

User flow:
  1. VEG_SELECT   -- pick Veg or Non-Veg
  2. LOADING_RECS -- auto-fetch ingredients + generate recommendations
  3. RECOMMENDATIONS -- 3 auto-suggested recipes with "why recommended" reasons
  4. RECIPE_VIEW  -- full recipe, progress tracking, PDF download
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

import streamlit as st  # type: ignore
import streamlit.components.v1 as components  # type: ignore

from chatbot import (
    get_all_ingredients,
    filter_by_diet,
    generate_recommendations_with_reasons,
    generate_onboarding_suggestions,
    expand_recipe,
    handle_refinement,
    handle_global_chat,
)

try:
    from utils.pdf_utils import recipe_to_pdf
except ImportError:
    def recipe_to_pdf(recipe_text: str, recipe_name: str) -> bytes:  # noqa: F811
        return recipe_text.encode("utf-8")

try:
    from config import supabase as _supabase
except Exception:
    _supabase = None

try:
    from config import supabase_admin as _supabase_admin
except Exception:
    _supabase_admin = None

try:
    from auth import (
        render_auth_page, get_current_user, logout as auth_logout,
        mark_onboarding_complete, render_login_dialog_content,
    )
except Exception as _auth_import_err:
    def render_auth_page(): return True               # type: ignore[misc]
    def get_current_user(): return None               # type: ignore[misc]
    def auth_logout(): pass                           # type: ignore[misc]
    def mark_onboarding_complete(): pass              # type: ignore[misc]
    def render_login_dialog_content(): pass           # type: ignore[misc]

logger = logging.getLogger(__name__)

# ---- Page config --------------------------------------------------------------
st.set_page_config(
    page_title="Inflation-Busting Recipe Generator",
    layout="wide",
)

# ---- Session restore (cookie-based) -----------------------------------------
# Restores authenticated session from browser cookie when available.
# Guests are allowed through without logging in.
render_auth_page()

# ---- Onboarding (new users only - shown once after first registration) --------
# Deferred import: render_onboarding is defined further below in this file.
# We use a flag in session_state set by auth.py after successful registration.
if st.session_state.get("onboarding_needed") and st.session_state.get("auth_user"):
    # render_onboarding is defined after DEFAULTS; call it via a late-binding lambda
    _run_onboarding = True
else:
    _run_onboarding = False

# ---- Auto-seed Supabase -------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _seed_database_once():
    try:
        from config import supabase
        if supabase is None:
            return "no_supabase"
        result = supabase.table("offers").select("id", count="exact").eq("is_discount", True).execute()  # type: ignore[call-overload]
        if result.count and result.count > 0:
            return "already_seeded"
        from data_ingestion.load_to_supabase import upsert_offers
        from data_ingestion.fetch_open_food_facts import fetch_off_discounts
        offers = fetch_off_discounts(products_per_category=10)
        if not offers:
            return "error:no products"
        count = upsert_offers(offers)
        return f"seeded:{count}"
    except Exception as exc:
        return f"error:{exc}"


def _refresh_off_data() -> str:
    try:
        from data_ingestion.fetch_open_food_facts import fetch_off_discounts
        from data_ingestion.load_to_supabase import upsert_offers
        offers = fetch_off_discounts(products_per_category=10)
        if not offers:
            cached_count = 0
            try:
                from config import supabase
                if supabase is not None:
                    r = supabase.table("offers").select("id", count="exact").eq("is_discount", True).execute()  # type: ignore[call-overload]
                    cached_count = r.count or 0
            except Exception:
                pass
            if cached_count > 0:
                return (
                    f"Warning: Open Food Facts API is currently unreachable - "
                    f"continuing with {cached_count} cached products already in Supabase. "
                    f"Try refreshing again when your network is available."
                )
            return "Warning: Open Food Facts API is unreachable and no cached products were found."
        count = upsert_offers(offers)
        return f"Loaded {count} real UK products from Open Food Facts API into Supabase!"
    except Exception as exc:
        return f"Refresh failed: {exc}"


_seed_database_once()


# ---- Authenticated Supabase client for current user -------------------------
def _refresh_user_token() -> str:
    """Refresh the user's JWT using the stored refresh token.
    Updates st.session_state['auth_user'] in place and returns the new access token."""
    user = st.session_state.get("auth_user") or {}
    rt = user.get("_rt", "")
    if not rt or _supabase is None:
        return user.get("access_token", "")
    try:
        resp = _supabase.auth.refresh_session(rt)
        if resp and resp.session:
            new_token = resp.session.access_token or ""
            new_rt    = resp.session.refresh_token or rt
            user["access_token"] = new_token
            user["_rt"]          = new_rt
            st.session_state["auth_user"] = user
            logger.debug("JWT refreshed successfully")
            return new_token
    except Exception as _re:
        logger.debug("Token refresh failed: %s", _re)
    return user.get("access_token", "")


def _get_authed_db():
    """Return the best available Supabase client.
    1. Service-role admin client — only if it genuinely has service_role privileges.
    2. Anon client with a proactively-refreshed user JWT."""
    # Only use admin client if it's a real service-role client (bypasses RLS)
    if _supabase_admin is not None:
        return _supabase_admin
    # User-JWT path: always refresh the token before returning the client
    # so we never hand back an expired JWT.
    user = get_current_user()
    if _supabase is None or not user:
        return None
    token = _refresh_user_token()   # proactively refresh every call
    if not token:
        return None
    try:
        _supabase.postgrest.auth(token)
    except Exception:
        pass
    return _supabase


# ---- Supabase chat_sessions table --------------------------------------------
@st.cache_resource(show_spinner=False)
def _ensure_chat_table():
    # Use admin client for the check — anon client is blocked by RLS before login
    _check_client = _supabase_admin or _supabase
    if _check_client is None:
        return False
    try:
        _check_client.table("chat_sessions").select("id").limit(1).execute()
        return True
    except Exception:
        pass
    ddl = """
    CREATE TABLE IF NOT EXISTS chat_sessions (
        id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id  uuid REFERENCES auth.users(id) ON DELETE CASCADE,
        name     text NOT NULL,
        content  text,
        messages jsonb DEFAULT '[]'::jsonb,
        intent   jsonb DEFAULT '{}'::jsonb,
        saved_at timestamptz DEFAULT now(),
        CONSTRAINT chat_sessions_user_id_name_key UNIQUE (user_id, name)
    );"""
    try:
        _check_client.rpc("exec_sql", {"query": ddl}).execute()
        return True
    except Exception:
        return False


_CHAT_TABLE_OK: bool = _ensure_chat_table()


# ---- Saved recipe helpers ----------------------------------------------------
# Session-state cache key for in-memory recipe store (works even if DB fails)
_SS_RECIPES = "_saved_recipes_cache"


def _ss_recipes() -> dict:
    """Return the in-memory recipe store from session state (always available)."""
    if _SS_RECIPES not in st.session_state:
        st.session_state[_SS_RECIPES] = {}
    return st.session_state[_SS_RECIPES]


def _is_auth_error(exc: Exception) -> bool:
    """Return True if the exception looks like a JWT/auth failure."""
    msg = str(exc).lower()
    return "jwt" in msg or "expired" in msg or "pgrst303" in msg or "42501" in msg


def load_saved_recipes() -> dict:
    """Load recipes — merges DB rows into session-state cache, then returns cache."""
    user = get_current_user()
    uid  = (user or {}).get("id", "")
    db   = _get_authed_db()
    if _CHAT_TABLE_OK and db is not None and uid:
        def _do_load(client):
            return (
                client.table("chat_sessions")
                .select("name, content, messages, intent, saved_at")
                .eq("user_id", uid)
                .order("saved_at", desc=True)
                .execute()
            )
        try:
            result = _do_load(db)
        except Exception as exc:
            if _is_auth_error(exc) and _supabase_admin is None:
                # Force a token refresh and retry once
                _refresh_user_token()
                db = _get_authed_db()
                try:
                    result = _do_load(db) if db else None
                except Exception as exc2:
                    logger.warning("Supabase load failed: %s", exc2)
                    result = None
            else:
                logger.warning("Supabase load failed: %s", exc)
                result = None
        if result is not None:
            cache = _ss_recipes()
            for row in (result.data or []):
                if not isinstance(row, dict):
                    continue
                _row_name: str = str(row.get("name") or "")
                if not _row_name:
                    continue
                _saved_at_raw: str = str(row.get("saved_at") or "")
                cache[_row_name] = {
                    "content":  row.get("content") or "",
                    "saved_at": _saved_at_raw[:16].replace("T", " "),
                    "messages": row.get("messages") or [],
                    "intent":   row.get("intent") or {},
                }
    # Always return the in-memory cache (populated from DB above, or from prior saves)
    return _ss_recipes()


def save_recipe(name: str, content: str, messages: list | None = None, intent: dict | None = None) -> None:
    saved_at = datetime.now(timezone.utc).isoformat()
    entry = {
        "content":  content,
        "saved_at": saved_at[:16].replace("T", " "),
        "messages": messages or [],
        "intent":   intent or {},
    }
    # Always write to session-state cache first — guarantees sidebar shows it immediately
    _ss_recipes()[name] = entry

    # Best-effort persist to Supabase
    user = get_current_user()
    uid  = (user or {}).get("id", "")
    db   = _get_authed_db()
    if not uid:
        logger.warning("save_recipe: no user_id — recipe saved to session cache only")
        return
    if not _CHAT_TABLE_OK or db is None:
        logger.warning("save_recipe: DB unavailable (CHAT_TABLE_OK=%s) — session cache only", _CHAT_TABLE_OK)
        return
    payload = {"user_id": uid, "name": name, "content": content,
               "messages": messages or [], "intent": intent or {},
               "saved_at": saved_at}
    def _do_save(client):
        client.table("chat_sessions").upsert(payload, on_conflict="user_id,name").execute()
    try:
        _do_save(db)
        logger.debug("save_recipe: persisted '%s' for user %s", name, uid)
    except Exception as exc:
        if _is_auth_error(exc) and _supabase_admin is None:
            _refresh_user_token()
            db = _get_authed_db()
            try:
                if db:
                    _do_save(db)
            except Exception as exc2:
                logger.warning("Supabase save failed (retry): %s", exc2)
        else:
            logger.warning("Supabase save failed: %s", exc)


def delete_recipe(name: str) -> None:
    # Remove from session-state cache immediately
    _ss_recipes().pop(name, None)

    user = get_current_user()
    uid  = (user or {}).get("id", "")
    db   = _get_authed_db()
    if _CHAT_TABLE_OK and db is not None and uid:
        try:
            db.table("chat_sessions").delete().eq("user_id", uid).eq("name", name).execute()
        except Exception as exc:
            logger.warning("Supabase delete failed: %s", exc)


def rename_recipe(old_name: str, new_name: str) -> None:
    # Update session-state cache immediately
    cache = _ss_recipes()
    if old_name in cache:
        cache[new_name] = cache.pop(old_name)
        cache[new_name]["saved_at"] = datetime.now(timezone.utc).isoformat()[:16].replace("T", " ")

    user = get_current_user()
    uid  = (user or {}).get("id", "")
    db   = _get_authed_db()
    if _CHAT_TABLE_OK and db is not None and uid:
        try:
            _res = db.table("chat_sessions").select("*").eq("user_id", uid).eq("name", old_name).execute()
            _rows = _res.data or []
            if _rows and isinstance(_rows[0], dict):
                _row = _rows[0]
                db.table("chat_sessions").upsert(
                    {
                        "user_id": uid,
                        "name": new_name,
                        "content": _row.get("content") or "",
                        "messages": _row.get("messages") or [],
                        "intent": _row.get("intent") or {},
                        "saved_at": datetime.now(timezone.utc).isoformat(),
                    },
                    on_conflict="user_id,name",
                ).execute()
            db.table("chat_sessions").delete().eq("user_id", uid).eq("name", old_name).execute()
        except Exception as exc:
            logger.warning("Supabase rename failed: %s", exc)


# ---- Activity log helpers (session-only, not persisted) ---------------------
def log_activity(action: str, recipe_name: str) -> None:
    entry = {
        "action":      action,
        "recipe_name": recipe_name,
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if "activity_log" not in st.session_state:
        st.session_state.activity_log = []
    st.session_state.activity_log.insert(0, entry)


def load_activity_log() -> list:
    return list(st.session_state.get("activity_log") or [])


# ---- Session state -----------------------------------------------------------
DEFAULTS: dict = {
    "app_state":              "VEG_SELECT",
    "diet":                   None,
    "recommendations":        [],
    "current_recipe_name":    "",
    "current_recipe_content": "",
    "ingredients":            [],
    "intent":                 {},
    "activity_log":           [],
    "followup_suggestions":   [],
    "pending_input":          None,
    "messages":               [],
    "manual_recs":            [],
    "manual_selected_names":  [],
    "global_chat_messages":   [],
    "pending_global_input":   None,
    "landing_chat_messages":  [],
    "landing_pending_input":  None,
    "ob_answers":             {},
    "ob_suggestions":         [],
    "_products_refreshed":    False,
    "guest_prompt_count":     0,
    "_show_login_dialog":     False,
}
for _k, _v in DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# -- Per-user isolation: reset app state whenever a different user logs in ------
# Compares the active user's ID against the ID stored at last login.
# On mismatch (new login or user switch), all chat/recipe state is wiped so
# every user always starts with a completely fresh session.
_cur_uid = (get_current_user() or {}).get("id", "")
if _cur_uid and st.session_state.get("_session_user_id", "") != _cur_uid:
    _saved_auth = st.session_state.get("auth_user")
    _saved_ob   = st.session_state.get("onboarding_needed", False)
    for _k, _v in DEFAULTS.items():
        st.session_state[_k] = _v
    st.session_state["auth_user"]         = _saved_auth
    st.session_state["onboarding_needed"] = _saved_ob
    st.session_state["_session_user_id"]  = _cur_uid

# Auto-refresh: clear module-level ingredient cache so every new browser session
# (page refresh) gets the latest data from Supabase rather than a stale in-memory copy.
if not st.session_state.get("_products_refreshed"):
    st.session_state["_products_refreshed"] = True
    try:
        import chatbot as _chatbot_mod
        _chatbot_mod._INGREDIENTS_CACHE = []
        _chatbot_mod._CACHE_EXPIRES_AT  = 0.0
    except Exception:
        pass

# Pre-load ingredient cache on first run so diet buttons are faster
if not st.session_state.get("ingredients"):
    try:
        st.session_state["ingredients"] = get_all_ingredients()
    except Exception:
        pass


def _reset_to_start() -> None:
    for k, v in DEFAULTS.items():
        st.session_state[k] = v


def _check_guest_limit() -> bool:
    """Check whether a guest user has exceeded the 5-prompt free allowance.

    - Logged-in users: always allowed (returns True immediately).
    - Guests under the limit: increments counter, returns True.
    - Guests at/over the limit: sets _show_login_dialog flag, calls st.rerun()
      (never returns False in practice — the rerun happens first).
    """
    if get_current_user():
        return True
    count = st.session_state.get("guest_prompt_count", 0)
    if count >= 5:
        st.session_state["_show_login_dialog"] = True
        st.rerun()
        return False  # unreachable after rerun, but satisfies type checker
    st.session_state["guest_prompt_count"] = count + 1
    return True


# ==============================================================================
# ONBOARDING  (first-time only, triggered after new user registration)
# ==============================================================================
def render_onboarding() -> None:
    """3-question onboarding wizard shown once after a new user registers."""
    user = get_current_user()
    first_name = (user.get("full_name") or "").split()[0] if user else "there"

    st.markdown("""
    <style>
    .ob-badge {
        display:inline-block; background:#ede9fe; color:#6d28d9;
        font-size:.75rem; font-weight:700; border-radius:20px;
        padding:4px 14px; letter-spacing:.05em; margin-bottom:14px;
    }
    .ob-title   { font-size:2rem; font-weight:800; color:#f1f5f9; margin:0 0 8px; }
    .ob-sub     { font-size:1rem; color:#94a3b8; margin:0 0 32px; }
    .ob-qlabel  { font-size:.95rem; font-weight:700; color:#1e293b; margin:0 0 4px; }
    .ob-qsub    { font-size:.82rem; color:#94a3b8; margin:0 0 12px; }
    .ob-preview { font-size:.78rem; color:#0d9488; font-weight:600; margin:6px 0 0; }
    </style>
    """, unsafe_allow_html=True)

    # -- Centered layout --------------------------------------------------------
    _, mid, _ = st.columns([1, 3, 1])
    with mid:
        st.markdown(
            f"<div style='text-align:center'>"
            f"<span class='ob-badge'>GETTING STARTED</span>"
            f"<h1 class='ob-title'>Welcome, {first_name}!</h1>"
            f"<p class='ob-sub'>Answer 3 quick questions so we can personalise your recipe suggestions.</p>"
            f"</div>",
            unsafe_allow_html=True,
        )

        # -- Q1: Diet ----------------------------------------------------------
        with st.container(border=True):
            st.markdown(
                "<p class='ob-qlabel'>1. What's your dietary preference?</p>"
                "<p class='ob-qsub'>We'll only suggest recipes that match your diet.</p>",
                unsafe_allow_html=True,
            )
            ob_diet = st.radio(
                "Diet", ["Vegetarian", "Non-Vegetarian", "Both"],
                horizontal=True, label_visibility="collapsed", key="ob_diet_radio",
            )

        # -- Q2: Budget --------------------------------------------------------
        with st.container(border=True):
            st.markdown(
                "<p class='ob-qlabel'>2. What's your typical budget per meal?</p>"
                "<p class='ob-qsub'>Maximum spend per meal for 2 people (in GBP).</p>",
                unsafe_allow_html=True,
            )
            ob_budget_val = st.number_input(
                "Budget", min_value=3, max_value=100, value=15, step=1,
                label_visibility="collapsed", key="ob_budget_num",
            )
            ob_budget = f"Under \u00a3{ob_budget_val}"
            st.markdown(
                f"<p class='ob-preview'>\u00a3{ob_budget_val} per meal</p>",
                unsafe_allow_html=True,
            )

        # -- Q3: Cooking time (minute-by-minute) -------------------------------
        with st.container(border=True):
            st.markdown(
                "<p class='ob-qlabel'>3. How much time do you usually have to cook?</p>"
                "<p class='ob-qsub'>Adjust in 1-minute steps (10 - 120 mins).</p>",
                unsafe_allow_html=True,
            )
            ob_time_val = st.number_input(
                "Cook time", min_value=10, max_value=120, value=30, step=1,
                label_visibility="collapsed", key="ob_time_min",
            )
            ob_time = f"{ob_time_val} mins"
            st.markdown(
                f"<p class='ob-preview'>{ob_time_val} minutes</p>",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        if st.button("Get My Recipe Suggestions", type="primary", key="ob_submit"):
            if ob_diet == "Vegetarian":
                st.session_state["diet"] = "Veg"
            elif ob_diet == "Non-Vegetarian":
                st.session_state["diet"] = "Non-Veg"
            else:
                st.session_state["diet"] = "Veg"
            st.session_state["ob_answers"] = {
                "diet":   ob_diet,
                "budget": ob_budget,
                "time":   ob_time,
            }
            st.session_state["ob_suggestions"] = []
            st.session_state["onboarding_needed"] = False
            st.session_state.pop("onboarding_step", None)
            mark_onboarding_complete()
            st.session_state["app_state"] = "VEG_SELECT"
            st.rerun()



# ---- Chat helpers -----------------------------------------------------------
def _render_rec_cards_in_chat(recs: list[dict], diet_icon: str, diet_name: str, msg_idx: "int | str" = 0) -> None:
    """Render compact recipe cards inside a chat bubble."""
    for _i, _r in enumerate(recs[:3]):
        _n   = _r.get("name", f"Recipe {_i+1}")
        _d   = _r.get("description", "")
        _c   = _r.get("estimated_cost", "~\u00a36.00")
        if _c and '\u00a3' not in _c and '£' not in _c:
            _c = f"~\u00a3{_c.lstrip('~').strip()}"
        _rs  = _r.get("reasons", [])
        st.markdown(
            f"<div class='rec-card' style='margin-bottom:12px'>"
            f"<h4 style='margin:0 0 4px'>{_n}</h4>"
            f"<p style='color:#555;font-size:.88rem;margin:0 0 6px'>{_d}</p>"
            f"<span class='rec-tag budget'>{_c}</span>"
            f"<span class='rec-tag'>{diet_name}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        for _reason in _rs[:3]:
            st.markdown(
                f"<div class='reason-item' style='font-size:.82rem'>"
                f"<span class='reason-icon'>-</span><span>{_reason}</span></div>",
                unsafe_allow_html=True,
            )
        if st.button(f"View Full Recipe \u2014 {_n[:32]}", key=f"chat_view_{msg_idx}_{_i}_{hash(_n)}", type="primary", use_container_width=True):
            _check_guest_limit()
            with st.spinner(f"Generating full recipe for {_n}\u2026"):
                _full = expand_recipe(_n, st.session_state.intent or {}, st.session_state.ingredients or [])
            st.session_state.current_recipe_name    = _n
            st.session_state.current_recipe_content = _full
            st.session_state.app_state              = "RECIPE_VIEW"
            save_recipe(_n, _full, list(st.session_state.global_chat_messages), st.session_state.intent or {})
            log_activity("viewed recipe", _n)
            st.toast(f"'{_n}' opened!")
            st.rerun()
        st.markdown("")


def render_landing_chat() -> None:
    """Front-page standalone chat (ChatGPT style) style.
    Messages persist and accumulate; new message/response rendered inline.
    """
    import re as _rlc

    # -- Render full chat history ----------------------------------------------
    for _lmi, _lmsg in enumerate(st.session_state.landing_chat_messages):
        _lrole   = _lmsg.get("role", "user")
        with st.chat_message(_lrole):
            st.markdown(_lmsg.get("content", ""))
            if _lmsg.get("type") == "rec_cards":
                _ldiet = _lmsg.get("diet", "Veg")
                _ldn   = "Vegetarian" if _ldiet == "Veg" else "Non-Vegetarian"
                _render_rec_cards_in_chat(_lmsg.get("recs", []), "", _ldn, msg_idx=f"lc_{_lmi}")

    # -- Quick chips -----------------------------------------------------------
    _lchips = [
        "Quick vegetarian meal",
        "Non-veg dinner ideas",
        "Budget recipes under \u00a310",
    ]
    st.markdown(
        "<p style='color:#64748b;font-size:0.75rem;margin:8px 0 6px;letter-spacing:0.05em'>"
        "TRY ASKING:</p>",
        unsafe_allow_html=True,
    )
    _lcc = st.columns(3, gap="small")
    for _li, (_lcol, _lchip) in enumerate(zip(_lcc, _lchips)):
        with _lcol:
            if st.button(_lchip, key=f"landing_chip_{_li}", use_container_width=True):
                st.session_state.landing_pending_input = _lchip
                st.rerun()

    # -- Chat input pinned to bottom -------------------------------------------
    _lpending = st.session_state.get("landing_pending_input")
    if _lpending:
        st.session_state.landing_pending_input = None

    _linput = st.chat_input(
        "e.g. \u201ccheap chicken dinner\u201d \u00b7 \u201cpasta for 4 under \u00a312\u201d \u00b7 \u201c20-min veggie meal\u201d",
        key="landing_chat_input",
    ) or _lpending

    if not _linput:
        return

    # -- Check guest prompt limit before any AI call --------------------------
    _check_guest_limit()

    # -- Detect diet from message ----------------------------------------------
    _llower = _linput.lower()

    _nonveg_kws = [
        "non-veg", "non veg", "nonveg", "non-vegetarian", "nonvegetarian",
        "meat", "chicken", "beef", "fish", "prawn", "shrimp", "lamb",
        "pork", "tuna", "salmon", "mutton", "turkey", "bacon", "sausage",
        "seafood", "mince", "steak", "ham", "duck", "crab", "lobster",
    ]
    _veg_kws = [
        "vegetarian", "veggie", "veg meal", "veg recipe", "veg food",
        "plant-based", "no meat", "without meat", "meatless", "meat free",
        "purely veg", "only veg",
    ]

    if any(kw in _llower for kw in _nonveg_kws):
        _ldiet_use = "Non-Veg"
    elif any(kw in _llower for kw in _veg_kws):
        _ldiet_use = "Veg"
    else:
        # Fall back to the diet the user selected via the preference buttons
        _ldiet_use = st.session_state.diet or "Veg"

    _ldiet_label = "Non-Vegetarian" if _ldiet_use == "Non-Veg" else "Vegetarian"

    # Sync main card diet if user explicitly asked for one
    _lexplicit = any(kw in _llower for kw in _nonveg_kws) or any(kw in _llower for kw in _veg_kws)
    if _lexplicit and st.session_state.diet != _ldiet_use:
        st.session_state.diet = _ldiet_use
        st.session_state.recommendations = []  # force regeneration of main cards

    # -- Parse budget / servings -----------------------------------------------
    _lintent = {"budget": 15, "servings": 2}
    _lbm = (_rlc.search(r"\u00a3(\d+)", _linput)
            or _rlc.search(r"(\d+)\s*(?:pound|pounds|quid)", _llower))
    if _lbm:
        _lintent["budget"] = int(_lbm.group(1))
    _lsm = _rlc.search(r"(\d+)\s*(?:people|servings|persons?|of us)", _llower)
    if _lsm:
        _lintent["servings"] = int(_lsm.group(1))

    # -- Show user message inline ----------------------------------------------
    st.session_state.landing_chat_messages.append({"role": "user", "content": _linput})
    with st.chat_message("user"):
        st.markdown(_linput)

    # -- Generate & show assistant response inline -----------------------------
    if not st.session_state.ingredients:
        with st.spinner("Loading products\u2026"):
            st.session_state.ingredients = get_all_ingredients()

    with st.spinner(f"Finding {_ldiet_label} recipes\u2026"):
        _lrecs = generate_recommendations_with_reasons(_ldiet_use, st.session_state.ingredients)

    _ldi2  = ""
    _lintro = (
        f"Here are **3 recipe suggestions** "
        f"for {_lintent['servings']} people with a budget of \u00a3{_lintent['budget']}:\n\n"
        f"*Click \u201cView Full Recipe\u201d on any card for step-by-step instructions.*"
    )
    _lasst_msg = {
        "role": "assistant", "content": _lintro,
        "type": "rec_cards", "recs": _lrecs, "diet": _ldiet_use,
    }
    st.session_state.landing_chat_messages.append(_lasst_msg)

    msg_idx_new = f"lc_{len(st.session_state.landing_chat_messages) - 1}"
    with st.chat_message("assistant"):
        st.markdown(_lintro)
        _render_rec_cards_in_chat(_lrecs, _ldi2, _ldiet_label, msg_idx=msg_idx_new)


def render_home_chat() -> None:
    """Chat section: examples + chips + input at TOP, then messages flow BELOW.
    Only rendered after a diet has been selected.
    """
    import re as _re2

    diet      = st.session_state.diet or "Veg"
    diet_name = "Vegetarian" if diet == "Veg" else "Non-Vegetarian"

    st.divider()
    st.markdown(
        "<h4 style='margin-bottom:4px'>Recipe Chat</h4>",
        unsafe_allow_html=True,
    )

    # -- How to chat examples --------------------------------------------------
    st.markdown(
        "<div class='how-to-chat-box'>"
        "<p>How to use the chat</p>"
        "<div class='htc-grid'>"
        "<span><em>Suggest chicken recipes</em></span>"
        "<span><em>Budget \u00a310, non-veg meals</em></span>"
        "<span><em>Quick vegetarian dinner</em></span>"
        "<span><em>Meals for 4 under \u00a315</em></span>"
        "<span><em>Change diet to vegetarian</em></span>"
        "<span><em>Show me a pasta recipe</em></span>"
        "</div></div>",
        unsafe_allow_html=True,
    )

    # -- Chat history ----------------------------------------------------------
    for _mi, _msg in enumerate(st.session_state.global_chat_messages):
        _role   = _msg.get("role", "user")
        with st.chat_message(_role):
            st.markdown(_msg.get("content", ""))
            if _msg.get("type") == "rec_cards":
                _render_rec_cards_in_chat(_msg.get("recs", []), "", diet_name, msg_idx=_mi)

    # -- Quick action chips ----------------------------------------------------
    _chips = [
        "Suggest dinner recipes",
        "Budget meals under \u00a310",
        "Quick meals under 20 mins",
    ]
    st.caption("Try asking:")
    _cc = st.columns(3)
    for _ci, (_col, _chip) in enumerate(zip(_cc, _chips)):
        with _col:
            if st.button(_chip, key=f"home_chip_{_ci}"):
                st.session_state.pending_global_input = _chip
                st.rerun()

    # -- Chat input form -------------------------------------------------------
    _pending = st.session_state.get("pending_global_input")
    if _pending:
        st.session_state.pending_global_input = None

    _user_input = None
    with st.form("home_chat_form", clear_on_submit=True):
        _hc1, _hc2 = st.columns([6, 1])
        with _hc1:
            _htyped = st.text_input(
                "home_chat_input",
                placeholder="e.g. \u201cbudget \u00a310, non-veg\u201d \u00b7 \u201cquick pasta\u201d \u00b7 \u201csuggest dinner for 4\u201d",
                label_visibility="collapsed",
            )
        with _hc2:
            _hsubmitted = st.form_submit_button("Send \u2192", type="primary")
        if _hsubmitted and _htyped.strip():
            _user_input = _htyped.strip()

    if _pending and not _user_input:
        _user_input = _pending

    if not _user_input:
        return

    # Guard: don't append the same user message twice
    _gchat = st.session_state.global_chat_messages
    if _gchat and _gchat[-1].get("role") == "user" and _gchat[-1].get("content") == _user_input:
        return

    # -- Check guest prompt limit before any AI call ---------------------------
    _check_guest_limit()

    # -- Process user message --------------------------------------------------
    st.session_state.global_chat_messages.append({"role": "user", "content": _user_input})

    with st.spinner("Thinking\u2026"):
        _action = handle_global_chat(
            message         = _user_input,
            app_state       = st.session_state.app_state,
            diet            = diet,
            intent          = st.session_state.intent or {},
            ingredients     = st.session_state.ingredients or [],
            recommendations = st.session_state.recommendations or [],
        )

    _act         = _action.get("action", "answer")
    _reply       = _action.get("reply", "")
    _new_diet    = _action.get("diet") or diet
    _recipe_name = _action.get("recipe_name")
    _add_ingreds = _action.get("add_ingredients") or []

    # Keyword fallback - recipe-related messages always produce 3 cards
    _recipe_kws = {"suggest", "recommend", "recipe", "meal", "dinner", "lunch",
                   "breakfast", "cook", "eat", "food", "dish", "make", "ideas",
                   "snack", "supper", "what can", "what should", "how about"}
    if _act == "answer" and any(_kw in _user_input.lower() for _kw in _recipe_kws):
        _act = "load_recs"
    if _act == "set_diet" and any(_kw in _user_input.lower() for _kw in _recipe_kws):
        _act = "load_recs"

    if _act == "set_diet":
        st.session_state.diet = _new_diet
        _dn = "Vegetarian" if _new_diet == "Veg" else "Non-Vegetarian"
        with st.spinner(f"Loading {_dn} products\u2026"):
            _ings = get_all_ingredients()
            st.session_state.ingredients = _ings
            st.session_state.intent["diet"] = _dn
        _diet_reply = f"{_reply}\n\nDiet set to **{_dn}**. Ask me to suggest recipes anytime!"
        st.session_state.global_chat_messages.append({"role": "assistant", "content": _diet_reply})
        st.rerun()

    elif _act in ("load_recs", "show_recipe") and not _recipe_name:
        if not st.session_state.ingredients:
            with st.spinner("Loading products\u2026"):
                st.session_state.ingredients = get_all_ingredients()
        if not st.session_state.intent:
            st.session_state.intent = {
                "diet": diet_name, "budget": 15, "servings": 2,
                "cook_time": "any", "meal_type": "any meal", "restrictions": "none",
            }

        import re as _re3
        _msg_low = _user_input.lower()
        _nonveg_kws = [
            "non-veg", "non veg", "nonveg", "non-vegetarian", "nonvegetarian",
            "meat", "chicken", "beef", "fish", "prawn", "shrimp", "lamb",
            "pork", "tuna", "salmon", "mutton", "turkey", "bacon", "sausage",
            "seafood", "mince", "steak", "ham", "duck", "crab",
        ]
        _veg_kws = [
            "vegetarian", "veggie", "veg meal", "veg recipe", "veg food",
            "plant-based", "no meat", "without meat", "meatless", "meat free",
        ]
        if any(kw in _msg_low for kw in _nonveg_kws):
            diet = "Non-Veg"
            st.session_state.diet = "Non-Veg"
            st.session_state.intent["diet"] = "Non-Vegetarian"
        elif any(kw in _msg_low for kw in _veg_kws):
            diet = "Veg"
            st.session_state.diet = "Veg"
            st.session_state.intent["diet"] = "Vegetarian"

        _bm = _re3.search(r"\u00a3(\d+)", _user_input) or _re3.search(r"(\d+)\s*(?:pound|pounds|quid)", _msg_low)
        if _bm:
            st.session_state.intent["budget"] = int(_bm.group(1))
        _sm = _re3.search(r"(\d+)\s*(?:people|servings|persons?|of us)", _msg_low)
        if _sm:
            st.session_state.intent["servings"] = int(_sm.group(1))

        with st.spinner("Finding the best recipes for you\u2026"):
            _recs = generate_recommendations_with_reasons(diet, st.session_state.ingredients)
        st.session_state.recommendations = _recs

        _di   = ""
        _dn2  = "Vegetarian" if diet == "Veg" else "Non-Vegetarian"
        _budget   = st.session_state.intent.get("budget", 15)
        _servings = st.session_state.intent.get("servings", 2)
        _intro = (
            f"Here are **3 {_dn2} recipe recommendations** for {_servings} people "
            f"with a budget of \u00a3{_budget}:\n\n"
            f"*Click \u201cView Full Recipe\u201d on any card to see step-by-step instructions.*"
        )
        st.session_state.global_chat_messages.append({
            "role": "assistant", "content": _intro,
            "type": "rec_cards", "recs": _recs,
        })
        st.rerun()

    elif _act == "show_recipe" and _recipe_name:
        if not st.session_state.ingredients:
            st.session_state.ingredients = get_all_ingredients()
        with st.spinner(f"Generating recipe for {_recipe_name}\u2026"):
            _full = expand_recipe(_recipe_name, st.session_state.intent or {}, st.session_state.ingredients)
        st.session_state.current_recipe_name    = _recipe_name
        st.session_state.current_recipe_content = _full
        st.session_state.app_state              = "RECIPE_VIEW"
        st.session_state.global_chat_messages.append({"role": "assistant", "content": f"Opening **{_recipe_name}**\u2026"})
        save_recipe(_recipe_name, _full, list(st.session_state.global_chat_messages), st.session_state.intent or {})
        log_activity("viewed recipe", _recipe_name)
        st.rerun()

    elif _act == "manual_add" and _add_ingreds:
        if not st.session_state.ingredients:
            st.session_state.ingredients = get_all_ingredients()
        _cur = list(st.session_state.manual_selected_names or [])
        _all_map = {i.get("name","").strip().lower(): i.get("name","").strip() for i in st.session_state.ingredients}
        _added = []
        for _req in _add_ingreds:
            for _lk, _dv in _all_map.items():
                if _req.lower() in _lk or _lk in _req.lower():
                    if _dv not in _cur:
                        _cur.append(_dv)
                        _added.append(_dv)
                    break
        st.session_state.manual_selected_names = _cur
        _full_reply = _reply
        if _added:
            _full_reply += f"\n\nAdded to manual selection: **{', '.join(_added)}**\n\nOpen the ingredient table above to review."
        st.session_state.global_chat_messages.append({"role": "assistant", "content": _full_reply})
        st.rerun()

    elif _act == "refine_recipe":
        with st.spinner("Updating recommendations\u2026"):
            try:
                _upd_intent, _new_sug = handle_refinement(
                    _user_input, st.session_state.intent or {}, st.session_state.ingredients or []
                )
                st.session_state.intent = _upd_intent
                _names = _re2.findall(r"\*\*Recipe \d+: (.+?)\*\*", _new_sug)
                if _names:
                    _new_recs = generate_recommendations_with_reasons(diet, st.session_state.ingredients)
                    st.session_state.recommendations = _new_recs
                    _ref_reply = f"{_reply}\n\nHere are updated recommendations based on your request:"
                    st.session_state.global_chat_messages.append({
                        "role": "assistant", "content": _ref_reply,
                        "type": "rec_cards", "recs": _new_recs,
                    })
                else:
                    st.session_state.global_chat_messages.append({"role": "assistant", "content": _reply})
            except Exception as _exc:
                st.session_state.global_chat_messages.append({"role": "assistant", "content": f"Error: Could not update: {_exc}"})
        st.rerun()

    else:
        st.session_state.global_chat_messages.append({"role": "assistant", "content": _reply})
        st.rerun()


# ---- Global CSS --------------------------------------------------------------

# Trigger onboarding (functions are now all defined above)
if _run_onboarding:
    render_onboarding()
    st.stop()

# ---- Login popup dialog ------------------------------------------------------
# Shown when a guest user hits the 5-prompt free limit, or clicks "Login".
@st.dialog("Sign in to continue")
def _login_dialog():
    """Modal login/register form for guest users."""
    # Clear the flag immediately so that pressing ✕ doesn't re-open the dialog.
    st.session_state["_show_login_dialog"] = False
    if not get_current_user():
        st.markdown(
            "<p style='color:#64748b;font-size:0.9rem;margin-bottom:14px'>"
            "You've used your 5 free guest requests. "
            "Sign in or create a free account for unlimited access \u2014 it's free!</p>",
            unsafe_allow_html=True,
        )
        render_login_dialog_content()

# Trigger the dialog when the flag is set (guest limit hit or Login button clicked)
if st.session_state.get("_show_login_dialog") and not get_current_user():
    _login_dialog()
elif st.session_state.get("_show_login_dialog") and get_current_user():
    # User just logged in via the dialog — clear the flag
    st.session_state["_show_login_dialog"] = False

# ---- Global CSS --------------------------------------------------------------
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

/* -- Base ----------------------------------------------------------- */
*, *::before, *::after { box-sizing: border-box; }
html, body, [data-testid="stAppViewContainer"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
}
[data-testid="stAppViewContainer"] > .main { padding-top: 1.4rem; }
/* All headings � bright white, sharp on dark backgrounds */
h1 { font-size: 1.65rem !important; font-weight: 800 !important; letter-spacing: -0.5px !important; color: #f8fafc !important; }
h2 { font-size: 1.3rem  !important; font-weight: 700 !important; color: #f1f5f9 !important; }
h3 { font-size: 1.1rem  !important; font-weight: 700 !important; color: #f1f5f9 !important; }
h4 { font-size: 0.97rem !important; font-weight: 700 !important; color: #e2e8f0 !important; }
p  { color: #cbd5e1 !important; line-height: 1.6; }
/* Universal text override � white on dark */
[data-testid="stAppViewContainer"] .main h1,
[data-testid="stAppViewContainer"] .main h2,
[data-testid="stAppViewContainer"] .main h3,
[data-testid="stAppViewContainer"] .main h4 {
    color: #f1f5f9 !important;
}
/* Labels, captions, markdown text */
[data-testid="stAppViewContainer"] label,
[data-testid="stAppViewContainer"] .stMarkdown p,
[data-testid="stAppViewContainer"] .stCaption,
[data-testid="stAppViewContainer"] [class*="caption"] {
    color: #cbd5e1 !important;
}
/* Sidebar headings stay bright on dark sidebar */
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3,
[data-testid="stSidebar"] h4 {
    color: #f1f5f9 !important;
}

/* -- Primary buttons (main app � indigo) ---------------------------- */
.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button {
    background: linear-gradient(135deg, #0d9488 0%, #0f766e 100%) !important;
    color: #fff !important;
    border: none !important;
    border-radius: 10px !important;
    font-weight: 600 !important;
    font-size: 0.88rem !important;
    letter-spacing: 0.01em !important;
    box-shadow: 0 3px 10px rgba(13,148,136,0.30) !important;
    transition: all 0.2s ease !important;
    font-family: 'Inter', sans-serif !important;
    padding: 10px 16px !important;
}
.stButton > button[kind="primary"]:hover,
[data-testid="stFormSubmitButton"] > button:hover {
    background: linear-gradient(135deg, #0f766e 0%, #115e59 100%) !important;
    box-shadow: 0 6px 18px rgba(13,148,136,0.42) !important;
    transform: translateY(-1px) !important;
}
.stButton > button[kind="primary"]:active {
    transform: translateY(0) !important;
    box-shadow: 0 2px 8px rgba(99,102,241,0.2) !important;
}

/* -- Secondary / default buttons (main area) ------------------------ */
.stButton > button:not([kind="primary"]) {
    background: rgba(255,255,255,0.06) !important;
    color: #e2e8f0 !important;
    border: 1.5px solid rgba(255,255,255,0.12) !important;
    border-radius: 10px !important;
    font-weight: 500 !important;
    font-size: 0.85rem !important;
    font-family: 'Inter', sans-serif !important;
    transition: all 0.18s !important;
    padding: 9px 14px !important;
}
.stButton > button:not([kind="primary"]):hover {
    border-color: #0d9488 !important;
    color: #99f6e4 !important;
    background: rgba(13,148,136,0.12) !important;
}

/* -- Download buttons ----------------------------------------------- */
.stDownloadButton > button {
    background: rgba(255,255,255,0.06) !important;
    color: #e2e8f0 !important;
    border: 1.5px solid rgba(255,255,255,0.12) !important;
    border-radius: 10px !important;
    font-weight: 500 !important;
    font-size: 0.85rem !important;
    transition: all 0.18s !important;
}
.stDownloadButton > button:hover {
    border-color: #0d9488 !important;
    color: #99f6e4 !important;
    background: rgba(13,148,136,0.12) !important;
}

/* -- SIDEBAR -------------------------------------------------------- */
[data-testid="stSidebar"] {
    background: #111827 !important;
    border-right: 1px solid rgba(13,148,136,0.13) !important;
}
[data-testid="stSidebar"] h2 {
    font-size: 0.70rem !important;
    font-weight: 700 !important;
    color: #9ca3af !important;
    text-transform: uppercase !important;
    letter-spacing: 0.09em !important;
    margin: 0 !important;
}
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] span {
    color: #d1d5db !important;
    font-size: 0.84rem !important;
}
[data-testid="stSidebar"] hr {
    border-color: rgba(255,255,255,0.08) !important;
    margin: 8px 0 !important;
}

/* Sidebar: all buttons wrap long text */
div[data-testid="stSidebar"] .stButton button {
    text-align: left !important;
    white-space: normal !important;
    word-break: break-word !important;
    font-family: 'Inter', sans-serif !important;
}

/* Sidebar: New Session primary button � indigo */
div[data-testid="stSidebar"] .stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #0d9488, #0f766e) !important;
    color: #fff !important;
    border: none !important;
    border-radius: 10px !important;
    font-weight: 700 !important;
    font-size: 0.86rem !important;
    box-shadow: 0 3px 14px rgba(13,148,136,0.35) !important;
    padding: 11px 14px !important;
    letter-spacing: 0.01em !important;
}
div[data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
    background: linear-gradient(135deg, #0f766e, #115e59) !important;
    box-shadow: 0 6px 20px rgba(13,148,136,0.50) !important;
    transform: translateY(-1px) !important;
}

/* Sidebar: session item buttons — base style */
div[data-testid="stSidebar"] .stButton button:not([kind="primary"]) {
    font-size: 0.82rem !important;
    padding: 0 12px !important;
    border-radius: 9px !important;
    border: 1px solid rgba(255,255,255,0.08) !important;
    color: #e2e8f0 !important;
    background: rgba(255,255,255,0.04) !important;
    line-height: 1.45 !important;
    transition: background 0.15s, border-color 0.15s, color 0.15s !important;
    height: 42px !important;
    min-height: 42px !important;
    max-height: 42px !important;
    overflow: hidden !important;
    white-space: nowrap !important;
    width: 100% !important;
}
div[data-testid="stSidebar"] .stButton button:not([kind="primary"]) p {
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    max-width: 100% !important;
    margin: 0 !important;
}
div[data-testid="stSidebar"] .stButton button:not([kind="primary"]):hover {
    background: rgba(13,148,136,0.14) !important;
    border-color: rgba(13,148,136,0.40) !important;
    color: #99f6e4 !important;
}
/* Sidebar: session rows — JS marks them with data-sb-row; use CSS grid for pixel-perfect layout */
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"][data-sb-row="1"] {
    display: grid !important;
    grid-template-columns: 1fr 36px !important;
    gap: 4px !important;
    margin-bottom: 4px !important;
    height: 42px !important;
    min-height: 42px !important;
    align-items: center !important;
}
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"][data-sb-row="1"] > [data-testid="stColumn"] {
    width: unset !important;
    min-width: 0 !important;
    max-width: unset !important;
    padding: 0 !important;
    overflow: hidden !important;
    height: 42px !important;
    display: flex !important;
    align-items: center !important;
}
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"][data-sb-row="1"] > [data-testid="stColumn"] > div,
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"][data-sb-row="1"] > [data-testid="stColumn"] .stButton {
    width: 100% !important;
    height: 42px !important;
}
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"][data-sb-row="1"] > [data-testid="stColumn"]:last-child {
    justify-content: center !important;
    overflow: visible !important;
}
/* Sidebar: non-session horizontal blocks (profile, etc.) */
div[data-testid="stSidebar"] [data-testid="stHorizontalBlock"]:not([data-sb-row]) {
    align-items: center !important;
    gap: 6px !important;
}
/* Sidebar: tooltip */
.sb-recipe-tip {
    position: fixed;
    z-index: 99999;
    background: #1a1f2e;
    border: 1px solid rgba(255,255,255,0.18);
    border-radius: 10px;
    padding: 8px 14px;
    font-size: 0.79rem;
    font-family: 'Inter', sans-serif;
    color: #f1f5f9;
    box-shadow: 0 8px 28px rgba(0,0,0,0.65), 0 1px 4px rgba(0,0,0,0.30);
    pointer-events: none;
    max-width: 240px;
    word-break: break-word;
    line-height: 1.5;
    opacity: 0;
    transform: translateY(6px);
    transition: opacity 0.18s ease, transform 0.18s ease;
    white-space: pre-wrap;
}
.sb-recipe-tip.sb-tip-vis {
    opacity: 1;
    transform: translateY(0);
}

/* Sidebar: ? popover button */
div[data-testid="stSidebar"] div[data-testid="stPopover"] button {
    font-size: 1.15rem !important;
    font-weight: 700 !important;
    color: #64748b !important;
    background: transparent !important;
    border: none !important;
    border-radius: 50% !important;
    width: 28px !important;
    height: 28px !important;
    padding: 0 !important;
    line-height: 28px !important;
    text-align: center !important;
    transition: all 0.15s !important;
}
div[data-testid="stSidebar"] div[data-testid="stPopover"] button:hover {
    background: rgba(255,255,255,0.10) !important;
    color: #e2e8f0 !important;
}

/* Sidebar: user profile ? button */
div[data-testid="stSidebar"] .stColumns div[data-testid="stPopover"] button {
    font-size: 1.1rem !important;
    font-weight: 900 !important;
    color: #9ca3af !important;
    background: transparent !important;
    border: none !important;
    border-radius: 6px !important;
    padding: 4px 7px !important;
    line-height: 1 !important;
    letter-spacing: .1em;
    transition: background .15s, color .15s !important;
}
div[data-testid="stSidebar"] .stColumns div[data-testid="stPopover"] button:hover {
    color: #e2e8f0 !important;
    background: rgba(255,255,255,0.10) !important;
}

/* -- Sidebar brand / section / empty state -------------------------- */
.sb-brand {
    display: flex; align-items: center; gap: 13px;
    padding: 20px 4px 18px; margin-bottom: 4px;
    border-bottom: 1px solid rgba(255,255,255,0.07);
}
.sb-brand-icon {
    width: 40px; height: 40px; flex-shrink: 0;
    background: linear-gradient(135deg, #0d9488, #0f766e);
    border-radius: 12px; display: flex; align-items: center; justify-content: center;
    box-shadow: 0 4px 16px rgba(13,148,136,0.40);
}
.sb-brand-name {
    font-weight: 800; font-size: 0.95rem; color: #f1f5f9;
    letter-spacing: -0.2px; line-height: 1.2;
}
.sb-brand-tagline {
    font-size: 0.70rem; color: #9ca3af; font-weight: 400; margin-top: 3px;
    letter-spacing: 0.01em;
}
.sb-section-label {
    font-size: 0.68rem; font-weight: 700; color: #9ca3af;
    text-transform: uppercase; letter-spacing: 0.10em;
    display: flex; align-items: center; gap: 7px;
    padding: 14px 0 7px;
}
.sb-count-badge {
    background: rgba(13,148,136,0.20); color: #5eead4;
    border-radius: 20px; padding: 1px 8px;
    font-size: 0.63rem; font-weight: 700;
}
.sb-empty {
    background: rgba(255,255,255,0.03);
    border: 1.5px dashed rgba(255,255,255,0.10);
    border-radius: 10px; padding: 20px 14px;
    text-align: center; font-size: 0.80rem;
    color: #9ca3af !important; line-height: 1.7;
}

/* -- Recipe cards (chat + rec views) ------------------------------- */
.rec-card {
    border: 1.5px solid rgba(255,255,255,0.10);
    border-left: 4px solid #0d9488;
    border-radius: 12px;
    padding: 16px 16px 14px;
    background: #1e2130;
    margin-bottom: 10px;
    box-shadow: 0 2px 8px rgba(0,0,0,.25);
    transition: box-shadow 0.18s;
}
.rec-card:hover { box-shadow: 0 6px 20px rgba(0,0,0,.40); }
.rec-card h4 { color: #f1f5f9 !important; margin: 0 0 5px; font-weight: 700; font-size: 0.93rem; line-height: 1.35; }
.rec-card p  { color: #94a3b8 !important; font-size: 0.83rem; margin: 0 0 8px; line-height: 1.55; }

/* -- Home recipe grid cards ---------------------------------------- */
.home-rec-card {
    border: 1.5px solid rgba(255,255,255,0.09);
    border-top: 3px solid #0d9488;
    border-radius: 12px;
    padding: 18px 16px 14px;
    background: #1e2130;
    height: 300px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: 0 2px 10px rgba(0,0,0,.30);
    transition: box-shadow 0.22s, transform 0.18s;
    margin-bottom: 6px;
}
.home-rec-card:hover {
    box-shadow: 0 8px 26px rgba(13,148,136,0.20), 0 2px 10px rgba(0,0,0,.25);
    transform: translateY(-2px);
}
.home-rec-card-name {
    font-weight: 700; font-size: 0.93rem; color: #f1f5f9;
    line-height: 1.35; margin-bottom: 6px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden;
}
.home-rec-card-desc {
    font-size: 0.80rem; color: #94a3b8;
    line-height: 1.6; margin-bottom: 8px;
    flex: 1 1 auto;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
    overflow: hidden;
}
.home-rec-reasons {
    list-style: none; margin: 0 0 8px 0; padding: 0;
    overflow: hidden; max-height: 72px;
}
.home-rec-reasons li {
    font-size: 0.76rem; color: #94a3b8;
    padding: 3px 0 3px 18px; position: relative; line-height: 1.5;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.home-rec-reasons li::before {
    content: '£'; position: absolute; left: 0;
    color: #2dd4bf; font-weight: 700; font-size: 0.72rem; top: 4px;
}
.home-rec-card-footer {
    margin-top: auto; flex-shrink: 0; display: flex; flex-wrap: wrap; gap: 4px;
    align-items: center; padding-top: 8px;
    border-top: 1px solid rgba(255,255,255,0.07);
}

/* -- Quick Recipe Idea chips --------------------------------------- */
.section-label {
    font-size: 1.05rem; font-weight: 700; letter-spacing: 0.04em;
    text-transform: uppercase; color: #5eead4;
    margin: 0 0 10px 2px;
}
div[data-testid="stButton"] > button[data-qchip="1"] {
    background: rgba(13,148,136,0.12) !important;
    border: 1.5px solid rgba(13,148,136,0.35) !important;
    color: #e2e8f0 !important;
    border-radius: 30px !important;
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    padding: 6px 14px !important;
    transition: background 0.18s, border-color 0.18s, color 0.18s !important;
}
div[data-testid="stButton"] > button[data-qchip="1"]:hover {
    background: rgba(13,148,136,0.28) !important;
    border-color: #0d9488 !important;
    color: #fff !important;
}
.ask-recipe-box {
    background: #161b27;
    border: 1.5px solid rgba(255,255,255,0.09);
    border-radius: 14px;
    padding: 20px 22px 16px;
    margin-top: 4px;
    margin-bottom: 12px;
}
.quick-ideas-box {
    background: #161b27;
    border: 1.5px solid rgba(255,255,255,0.09);
    border-radius: 14px;
    padding: 20px 22px 16px;
    margin-bottom: 12px;
}

/* Recipe Recommendations flex header (title left / Refresh right) */
div[data-testid="stHorizontalBlock"]:has(.rr-header-title) {
    background: #161b27;
    border: 1.5px solid rgba(255,255,255,0.09);
    border-radius: 14px;
    padding: 18px 22px 14px !important;
    margin-bottom: 12px;
    align-items: flex-start !important;
}
div[data-testid="stHorizontalBlock"]:has(.rr-header-title) > div[data-testid="stColumn"] {
    padding: 0 2px !important;
}
div[data-testid="stHorizontalBlock"]:has(.rr-header-title) .stButton > button {
    background: rgba(255,255,255,0.06) !important;
    border: 1px solid rgba(255,255,255,0.12) !important;
    color: #94a3b8 !important;
    border-radius: 8px !important;
    font-size: .80rem !important;
    padding: 5px 10px !important;
    width: 100% !important;
    transition: background 0.2s, border-color 0.2s, color 0.2s !important;
    white-space: nowrap !important;
}
div[data-testid="stHorizontalBlock"]:has(.rr-header-title) .stButton > button:hover {
    background: rgba(13,148,136,0.18) !important;
    border-color: #0d9488 !important;
    color: #5eead4 !important;
}

/* -- Tags ---------------------------------------------------------- */
.rec-tag {
    display: inline-block; background: rgba(13,148,136,0.18); color: #5eead4;
    border: 1px solid rgba(13,148,136,0.30); border-radius: 20px;
    padding: 2px 10px; font-size: 0.71rem; font-weight: 600;
    margin-right: 4px; margin-bottom: 4px;
}
.rec-tag.budget { background: rgba(251,191,36,0.15); color: #fbbf24; border-color: rgba(251,191,36,0.30); }
.rec-tag.time   { background: rgba(99,102,241,0.15); color: #5eead4; border-color: rgba(13,148,136,0.30); }

/* -- Reason items -------------------------------------------------- */
.reason-item {
    display: flex; align-items: flex-start; gap: 8px;
    margin: 3px 0; font-size: 0.83rem; color: #94a3b8 !important; line-height: 1.5;
}
.reason-icon { color: #2dd4bf; font-weight: 700; flex-shrink: 0; margin-top: 1px; }

/* -- Activity badges ----------------------------------------------- */
.badge {
    display: inline-block; border-radius: 12px;
    padding: 2px 9px; font-size: 0.71rem; font-weight: 600; margin-right: 4px;
}
.badge-viewed     { background:#dbeafe; color:#1d4ed8; }
.badge-started    { background:#fef3c7; color:#92400e; }
.badge-completed  { background:#ede9fe; color:#5b21b6; }
.badge-downloaded { background:rgba(13,148,136,0.10); color:#0d9488; }

/* -- Suggest chips ------------------------------------------------- */
.suggest-label { font-size:.75rem; font-weight:600; margin:10px 0 4px; }

/* -- Sidebar: scrollable content with pinned profile ---------------- */
section[data-testid="stSidebar"] > div {
    overflow-y: auto !important;
    overflow-x: visible !important;
    height: 100vh !important;
}
[data-testid="stSidebarContent"],
[data-testid="stSidebarUserContent"] {
    min-height: 100% !important;
    display: flex !important;
    flex-direction: column !important;
    padding-bottom: 0 !important;
    overflow: visible !important;
}
[data-testid="stSidebarContent"] > div:has(.sb-profile-marker),
[data-testid="stSidebarUserContent"] > div:has(.sb-profile-marker) {
    flex: 1 1 auto !important;
    min-height: 20px !important;
}
[data-testid="stSidebarContent"] > div:has(.sb-profile-marker) ~ div,
[data-testid="stSidebarUserContent"] > div:has(.sb-profile-marker) ~ div {
    flex-shrink: 0 !important;
    background: #0e1420 !important;
    border-top: 1px solid rgba(255,255,255,0.07) !important;
    z-index: 999 !important;
    overflow: visible !important;
}
/* Profile popover panel: ensure it's never clipped */
div[data-testid="stSidebar"] .stColumns [data-testid="stPopover"],
div[data-testid="stSidebar"] .stColumns [data-testid="stPopoverBody"] {
    overflow: visible !important;
}
div[data-testid="stSidebar"] .stColumns [data-baseweb="popover"],
div[data-testid="stSidebar"] .stColumns [data-baseweb="tooltip"] {
    z-index: 99999 !important;
    max-height: 80vh !important;
    overflow-y: auto !important;
}

/* -- Sidebar: session row columns alignment ------------------------- */
div[data-testid="stSidebar"] [data-testid="stHorizontalBlock"] {
    gap: 4px !important;
    align-items: center !important;
}
div[data-testid="stSidebar"] [data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
    padding: 0 2px !important;
}

/* -- Chat messages: same dark background as main app --------------- */
[data-testid="stChatMessage"] {
    background: transparent !important;
    border: none !important;
}
[data-testid="stChatMessage"] > div {
    background: transparent !important;
}
/* User bubble: subtle teal tint */
[data-testid="stChatMessage"][data-testid="stChatMessageUser"],
.stChatMessage[data-testid*="user"] {
    background: transparent !important;
}
/* Chat input area */
[data-testid="stChatInput"] {
    background: #1e2130 !important;
    border-top: 1px solid rgba(255,255,255,0.08) !important;
}
[data-testid="stChatInput"] textarea {
    background: #1e2130 !important;
    color: #f1f5f9 !important;
    border: none !important;
}

/* -- How-to-chat hint: dark surface --------------------------------- */
.how-to-chat-box {
    background: rgba(255,255,255,0.04) !important;
    border: 1px solid rgba(255,255,255,0.09) !important;
    border-radius: 10px; padding: 12px 16px; margin: 6px 0 12px;
}
.how-to-chat-box p {
    font-weight: 700; margin: 0 0 6px; font-size: .83rem;
    color: #e2e8f0 !important;
}
.how-to-chat-box .htc-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 4px 24px;
    font-size: .78rem; color: #94a3b8;
}
.how-to-chat-box .htc-grid span em { color: #94a3b8; }
</style>
""", unsafe_allow_html=True)


# ---- Right-click delete (JS context menu in parent DOM) ----------------------
components.html("""
<script>
(function() {
  var parent = window.parent;
  var overlay = parent.document.getElementById('ctx-overlay');
  if (!overlay) {
    overlay = parent.document.createElement('div');
    overlay.id = 'ctx-overlay';
    overlay.style.cssText =
      'display:none;position:fixed;z-index:9999;background:#1e2130;' +
      'border:1px solid rgba(255,255,255,0.12);border-radius:8px;padding:4px 0;' +
      'box-shadow:0 4px 20px rgba(0,0,0,.50);min-width:180px;';
    var delBtn = parent.document.createElement('div');
    delBtn.id = 'ctx-del-btn';
    delBtn.innerText = 'Delete conversation';
    delBtn.style.cssText =
      'padding:9px 16px;cursor:pointer;font-size:0.88rem;color:#f87171;font-family:Inter,sans-serif;';
    delBtn.onmouseenter = function(){ delBtn.style.background='rgba(248,113,113,0.10)'; };
    delBtn.onmouseleave = function(){ delBtn.style.background=''; };
    overlay.appendChild(delBtn);
    parent.document.body.appendChild(overlay);

    parent.document.addEventListener('click', function(){
      overlay.style.display = 'none';
    });

    delBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      var name = overlay.dataset.targetName;
      overlay.style.display = 'none';
      if (name) {
        var url = new URL(parent.location.href);
        url.searchParams.set('ctx_delete', encodeURIComponent(name));
        parent.location.href = url.toString();
      }
    });
  }

  function attach() {
    var sidebar = parent.document.querySelector('[data-testid="stSidebarContent"]');
    if (!sidebar) return;
    sidebar.querySelectorAll('button').forEach(function(btn) {
      if (btn.dataset.ctxAttached) return;
      btn.dataset.ctxAttached = '1';
      btn.addEventListener('contextmenu', function(e) {
        var label = btn.innerText.trim().split('\n')[0].trim();
        if (!label || label.startsWith('+') || label.startsWith('Refresh')) return;
        e.preventDefault(); e.stopPropagation();
        overlay.dataset.targetName = label;
        overlay.style.display = 'block';
        overlay.style.left = e.clientX + 'px';
        overlay.style.top  = e.clientY + 'px';
      });
    });
  }
  setInterval(attach, 800);
  attach();

  // ---- Sidebar session rows: grid layout marker + tooltip ----
  (function() {
    var doc = window.parent.document;
    // Create/reuse shared tooltip element
    var tip = doc.getElementById('sb-recipe-tip');
    if (!tip) {
      tip = doc.createElement('div');
      tip.id = 'sb-recipe-tip';
      tip.className = 'sb-recipe-tip';
      doc.body.appendChild(tip);
    }
    var hideTimer;

    function showTip(btn) {
      var raw = (btn.textContent || btn.innerText || '').trim();
      // Strip leading bullet point
      raw = raw.replace(/^[\u2022\u25cf]\\s+/, '').trim();
      if (!raw || raw.length < 2) return;
      clearTimeout(hideTimer);
      tip.textContent = raw;
      tip.style.left = '-9999px';
      tip.style.top  = '-9999px';
      tip.classList.add('sb-tip-vis');
      requestAnimationFrame(function() {
        var rect = btn.getBoundingClientRect();
        var tw = tip.offsetWidth;
        var th = tip.offsetHeight;
        // Centre tooltip above the button
        var left = rect.left + (rect.width - tw) / 2;
        var top  = rect.top - th - 8;
        var vpW  = doc.documentElement.clientWidth;
        if (left + tw > vpW - 8) left = vpW - tw - 8;
        if (left < 8) left = 8;
        // Flip below if no room above
        if (top < 8) top = rect.bottom + 8;
        tip.style.left = left + 'px';
        tip.style.top  = top + 'px';
      });
    }

    function hideTip() {
      clearTimeout(hideTimer);
      tip.classList.remove('sb-tip-vis');
    }

    function scan() {
      var sidebar = doc.querySelector('[data-testid="stSidebarContent"]');
      if (!sidebar) return;
      sidebar.querySelectorAll('[data-testid="stHorizontalBlock"]').forEach(function(block) {
        // Only process 2-column blocks that haven't been classified yet
        if (block.dataset.sbRowScanned) return;
        var cols = block.querySelectorAll(':scope > [data-testid="stColumn"]');
        if (cols.length !== 2) { block.dataset.sbRowScanned = '1'; return; }
        // Session row = first column has a <button>, second column has a popover trigger
        var firstBtn   = cols[0].querySelector('button');
        var hasPopover = !!cols[1].querySelector('[data-testid="stPopover"]');
        if (firstBtn && hasPopover) {
          block.dataset.sbRow = '1';
        }
        block.dataset.sbRowScanned = '1';
      });

      // Attach tooltip only to session-row buttons
      sidebar.querySelectorAll('[data-testid="stHorizontalBlock"][data-sb-row="1"]').forEach(function(block) {
        var cols = block.querySelectorAll(':scope > [data-testid="stColumn"]');
        if (!cols.length) return;
        var btn = cols[0].querySelector('button');
        if (!btn || btn.dataset.tipAttached) return;
        btn.dataset.tipAttached = '1';
        btn.addEventListener('mouseenter',  function() { showTip(btn); });
        btn.addEventListener('mouseleave',  hideTip);
        btn.addEventListener('touchstart',  function() { showTip(btn); }, {passive: true});
        btn.addEventListener('touchend',    function() { hideTimer = setTimeout(hideTip, 1400); });
        btn.addEventListener('touchcancel', hideTip);
      });
    }

    setInterval(scan, 900);
    scan();
  })();
</script>
""", height=0, width=0)

# Handle right-click delete triggered by url query param
_qp = st.query_params
_ctx_delete = _qp.get("ctx_delete")
if _ctx_delete:
    _name_to_delete = _ctx_delete if isinstance(_ctx_delete, str) else str(_ctx_delete)
    delete_recipe(_name_to_delete)
    if st.session_state.current_recipe_name == _name_to_delete:
        _reset_to_start()
    st.query_params.clear()
    st.rerun()


# ==============================================================================
# SIDEBAR
# ==============================================================================
# Pre-load saved recipes from DB once per user per Streamlit session.
# The guard stores the logged-in user's ID (not just True) so that:
#   • Switching accounts on the same device always reloads the correct user's data
#   • Subsequent reruns (button clicks, etc.) skip the DB call and use the cache
#   • A page refresh wipes session_state so the DB is queried again fresh
_current_uid = (get_current_user() or {}).get("id", "")
if st.session_state.get("_recipes_db_loaded") != _current_uid:
    _ss_recipes().clear()          # discard any stale cache from a previous user
    load_saved_recipes()
    st.session_state["_recipes_db_loaded"] = _current_uid

with st.sidebar:
    # Brand header
    st.markdown(
        "<div class='sb-brand'>"
        "<div class='sb-brand-icon'>"
        "<svg width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='#fff' "
        "stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'>"
        "<path d='M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2'/>"
        "<path d='M7 2v20'/>"
        "<path d='M21 15V2a5 5 0 0 0-5 5v6h3.5a1.5 1.5 0 0 1 1.5 1.5v.5'/>"
        "<path d='M18 22v-7'/>"
        "</svg></div>"
        "<div>"
        "<div class='sb-brand-name'>Recipe AI</div>"
        "<div class='sb-brand-tagline'>Inflation-Busting Generator</div>"
        "</div></div>",
        unsafe_allow_html=True,
    )
    if st.button("+ New Session", key="new_session_btn", type="primary"):
        _reset_to_start()
        st.rerun()
    st.divider()

    # Saved sessions — cache already populated from DB at top of script
    saved = _ss_recipes()
    if not saved:
        st.markdown(
            "<div class='sb-empty'>"
            "No saved sessions yet.<br>Complete a recipe to start your history."
            "</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div class='sb-section-label'>"
            f"Recent Sessions"
            f"<span class='sb-count-badge'>{len(saved)}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        for r_name, r_data in saved.items():
            saved_at = r_data.get("saved_at", "")
            content  = r_data.get("content", "")
            _is_cur  = (
                st.session_state.get("app_state") == "RECIPE_VIEW" and
                st.session_state.get("current_recipe_name") == r_name
            )
            _sc1, _sc2 = st.columns([5, 1])
            with _sc1:
                _lbl = ("\u2022 " if _is_cur else "") + r_name
                if st.button(
                    _lbl,
                    key=f"chat_{r_name}",
                    help=f"Saved: {saved_at}" if saved_at else r_name,
                    type=("primary" if _is_cur else "secondary"),
                    use_container_width=True,
                ):
                    st.session_state.app_state               = "RECIPE_VIEW"
                    st.session_state.current_recipe_name     = r_name
                    st.session_state.current_recipe_content  = content
                    st.session_state.global_chat_messages    = r_data.get("messages") or []
                    st.session_state.messages                = []
                    st.session_state.intent                  = r_data.get("intent") or {}
                    st.rerun()
            with _sc2:
                with st.popover("\u22ee"):
                    st.markdown(
                        f"<div style='font-weight:700;font-size:.85rem;color:#f1f5f9;"
                        f"padding:2px 0 8px;border-bottom:1px solid rgba(255,255,255,0.1);margin-bottom:8px'>"
                        f"{r_name[:30]}{'…' if len(r_name)>30 else ''}</div>",
                        unsafe_allow_html=True,
                    )
                    _new_name = st.text_input(
                        "Rename session",
                        value=r_name,
                        key=f"rename_input_{r_name}",
                        label_visibility="collapsed",
                        placeholder="New session name",
                    )
                    if st.button("Rename", key=f"rename_btn_{r_name}"):
                        _clean = (_new_name or "").strip()
                        if _clean and _clean != r_name:
                            rename_recipe(r_name, _clean)
                            if st.session_state.current_recipe_name == r_name:
                                st.session_state.current_recipe_name = _clean
                            st.rerun()
                    if st.button("Delete", key=f"del_{r_name}", type="primary"):
                        delete_recipe(r_name)
                        if "activity_log" in st.session_state:
                            st.session_state.activity_log = [
                                _ae for _ae in st.session_state.activity_log
                                if _ae.get("recipe_name") != r_name
                            ]
                        if st.session_state.current_recipe_name == r_name:
                            _reset_to_start()
                        st.rerun()

    # ---- User profile card pinned to bottom ----------------------------------
    _auth_user = get_current_user()
    if _auth_user:
        _display_name = _auth_user.get("full_name") or _auth_user.get("email", "User")
        _user_email   = _auth_user.get("email", "")
        _initials     = "".join(w[0].upper() for w in _display_name.split()[:2]) or "U"
        _short_name   = _display_name[:18] + ("…" if len(_display_name) > 18 else "")
        _short_email  = _user_email[:22] + ("…" if len(_user_email) > 22 else "")
        # Push profile to bottom of sidebar
        st.markdown('<div class="sb-profile-marker"></div>', unsafe_allow_html=True)
        st.divider()

        # Profile row: avatar+name+email (left) | menu ⋮ (right)
        _pc_info, _pc_menu = st.columns([5, 1])
        with _pc_info:
            st.markdown(
                f"<div style='display:flex;align-items:center;gap:10px;padding:6px 2px'>"
                f"<div style='width:36px;height:36px;border-radius:50%;flex-shrink:0;"
                f"background:linear-gradient(135deg,#0d9488,#0f766e);"
                f"color:#fff;display:flex;align-items:center;justify-content:center;"
                f"font-weight:800;font-size:.9rem;letter-spacing:.02em'>{_initials}</div>"
                f"<div style='min-width:0;overflow:hidden'>"
                f"<div style='font-weight:600;font-size:.82rem;color:#f1f5f9;"
                f"white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>{_short_name}</div>"
                f"<div style='font-size:.70rem;color:#94a3b8;margin-top:1px;"
                f"white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>{_short_email}</div>"
                f"</div></div>",
                unsafe_allow_html=True,
            )
        with _pc_menu:
            st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
            with st.popover("⋮"):
                st.markdown(
                    f"<div style='font-size:.80rem;font-weight:600;color:#f1f5f9;"
                    f"padding:2px 0 10px;border-bottom:1px solid rgba(255,255,255,0.08);margin-bottom:10px'>"
                    f"{_display_name}"
                    f"<div style='font-size:.70rem;font-weight:400;color:#94a3b8;margin-top:2px'>{_user_email}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    "<div style='font-size:0.75rem;font-weight:600;color:#cbd5e1;margin-bottom:6px'>Edit Display Name</div>",
                    unsafe_allow_html=True,
                )
                _edit_name = st.text_input(
                    "Name",
                    value=_display_name,
                    key="sidebar_profile_name",
                    label_visibility="collapsed",
                    placeholder="Your display name",
                )
                if st.button("Save Changes", key="sidebar_save_profile"):
                    _clean_name = (_edit_name or "").strip()
                    if _clean_name and _clean_name != _display_name:
                        try:
                            from config import supabase_admin as _sa, supabase as _sc
                            if _sa is not None:
                                # Service-role bypasses RLS entirely
                                _sa.table("user_profiles").update(
                                    {"full_name": _clean_name}
                                ).eq("id", _auth_user["id"]).execute()
                            elif _sc is not None:
                                # Authenticate with user's JWT so RLS (auth.uid()=id) passes
                                _token = _auth_user.get("access_token", "")
                                if _token:
                                    _sc.postgrest.auth(_token)
                                _sc.table("user_profiles").update(
                                    {"full_name": _clean_name}
                                ).eq("id", _auth_user["id"]).execute()
                        except Exception:
                            pass  # DB write failed silently; local state still updated
                        # Always update the local session so the UI reflects the change
                        st.session_state["auth_user"]["full_name"] = _clean_name
                        st.rerun()
                st.divider()
                if st.button("Sign Out", key="btn_logout", type="primary"):
                    auth_logout()
                    st.rerun()

    else:
        # ---- Guest: show Login button pinned to bottom of sidebar --------
        st.markdown('<div class="sb-profile-marker"></div>', unsafe_allow_html=True)
        st.divider()
        _guest_count = st.session_state.get("guest_prompt_count", 0)
        _remaining   = max(0, 5 - _guest_count)
        st.markdown(
            f"<div style='padding:6px 2px 4px'>"
            f"<div style='font-size:.75rem;color:#9ca3af;margin-bottom:8px'>"
            f"Guest mode \u00b7 {_remaining} free request{'s' if _remaining != 1 else ''} remaining</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
        if st.button("Sign In / Register", key="sidebar_login_btn",
                     type="primary", use_container_width=True):
            st.session_state["_show_login_dialog"] = True
            st.rerun()


# ==============================================================================
# MAIN AREA header
# ==============================================================================
h1, h2 = st.columns([6, 1])
with h1:
    st.markdown(
        "<h1 style='margin-bottom:3px;color:#f1f5f9'>Inflation-Busting Recipe Generator</h1>"
        "<p style='color:#94a3b8;font-size:.9rem;margin:0;font-weight:400'>"
        "AI-powered recipes built around today\u2019s UK supermarket deals.</p>",
        unsafe_allow_html=True,
    )
with h2:
    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
    if st.button("\u21ba Start Over", key="start_over_btn"):
        _reset_to_start()
        st.rerun()

st.divider()


# ==============================================================================
# STATE: VEG_SELECT  (Home page - always shows recipe cards + ingredient table)
# ==============================================================================
if st.session_state.app_state == "VEG_SELECT":

    # -- Auto-detect diet from onboarding answers or keep existing selection ---
    _ob_answers = st.session_state.get("ob_answers") or {}
    if st.session_state.diet is None:
        _ob_raw = _ob_answers.get("diet", "Vegetarian")
        if _ob_raw == "Non-Vegetarian":
            st.session_state.diet = "Non-Veg"
        else:
            st.session_state.diet = "Veg"

    _cur_diet = st.session_state.diet  # "Veg" | "Non-Veg"
    _cur_dn   = "Vegetarian" if _cur_diet == "Veg" else "Non-Vegetarian"

    # Ensure ingredients are loaded
    if not st.session_state.get("ingredients"):
        with st.spinner("Loading today's deals..."):
            st.session_state.ingredients = get_all_ingredients()

    # Auto-generate recipe cards if none exist yet (single call for 6 distinct recipes)
    if not st.session_state.get("recommendations"):
        with st.spinner(f"Building your personalised {_cur_dn} recipes..."):
            st.session_state.recommendations = generate_recommendations_with_reasons(
                _cur_diet, st.session_state.ingredients, count=6,
                user_context=_ob_answers,
            )
        if not st.session_state.intent:
            try:
                import re as _bre
                _budget_str = str(_ob_answers.get("budget", "15") or "15")
                _bnum = _bre.search(r"\d+", _budget_str)
                _bval = int(_bnum.group()) if _bnum else 15
            except Exception:
                _bval = 15
            st.session_state.intent = {
                "diet":         _cur_dn,
                "budget":       _bval,
                "servings":     2,
                "cook_time":    _ob_answers.get("time", "any"),
                "meal_type":    "any meal",
                "restrictions": "none",
            }

    # -- Recipe card grid header + Refresh button ----------------------------
    _rr_left, _rr_right = st.columns([5, 1])
    with _rr_left:
        st.markdown(
            "<div class='rr-header-title'>"
            "<div class='section-label' style='margin-bottom:6px'>Recipe Recommendations</div>"
            "<p style='color:#94a3b8;font-size:.82rem;margin:0'>"
            "Personalised picks based on today\u2019s UK supermarket deals \u2014 click any card to view the full recipe.</p>"
            "</div>",
            unsafe_allow_html=True,
        )
    with _rr_right:
        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        if st.button("\u21bb Refresh", key="home_refresh_recs", use_container_width=True):
            st.session_state.recommendations = []
            with st.spinner("Getting fresh recipes..."):
                st.session_state.recommendations = generate_recommendations_with_reasons(
                    _cur_diet, st.session_state.ingredients, count=6,
                    user_context=_ob_answers,
                )
            st.rerun()

    # -- Recipe cards in 3-column grid (6 cards total) ----------------------
    _home_recs = st.session_state.recommendations or []
    if _home_recs:
        for _hrow_start in range(0, min(len(_home_recs), 6), 3):
            _hrow_items = _home_recs[_hrow_start: _hrow_start + 3]
            _hcols = st.columns(len(_hrow_items), gap="small")
            for _hci, (_hcol, _hrec) in enumerate(zip(_hcols, _hrow_items)):
                with _hcol:
                    _hr_name    = _hrec.get("name", f"Recipe {_hrow_start + _hci + 1}")
                    _hr_desc    = _hrec.get("description", "A delicious dish.")
                    _hr_cost    = _hrec.get("estimated_cost", "~\u00a36.00")
                    if _hr_cost and '\u00a3' not in _hr_cost and '£' not in _hr_cost:
                        _hr_cost = f"~\u00a3{_hr_cost.lstrip('~').strip()}"
                    _hr_time    = _hrec.get("cook_time", "")
                    _hr_reasons = _hrec.get("reasons") or []
                    _time_tag = (
                        f"<span class='rec-tag time'>{_hr_time}</span>"
                        if _hr_time else ""
                    )
                    _reasons_html = ""
                    if _hr_reasons:
                        _li_items = "".join(
                            f"<li>{r}</li>" for r in _hr_reasons[:3]
                        )
                        _reasons_html = f"<ul class='home-rec-reasons'>{_li_items}</ul>"
                    st.markdown(
                        f"<div class='home-rec-card'>"
                        f"<div class='home-rec-card-name'>{_hr_name}</div>"
                        f"<div class='home-rec-card-desc'>{_hr_desc}</div>"
                        + _reasons_html
                        + f"<div class='home-rec-card-footer'>"
                        f"<span class='rec-tag budget'>{_hr_cost}</span>"
                        f"{_time_tag}"
                        f"</div></div>",
                        unsafe_allow_html=True,
                    )
                    if st.button(
                        "View Recipe",
                        key=f"home_card_{_hrow_start}_{_hci}",
                        type="primary",
                        use_container_width=True,
                    ):
                        _check_guest_limit()
                        with st.spinner(f"Generating full recipe for {_hr_name}..."):
                            _hr_full = expand_recipe(
                                _hr_name, st.session_state.intent or {}, st.session_state.ingredients or []
                            )
                        st.session_state.current_recipe_name    = _hr_name
                        st.session_state.current_recipe_content = _hr_full
                        st.session_state.app_state              = "RECIPE_VIEW"
                        save_recipe(_hr_name, _hr_full, list(st.session_state.global_chat_messages), st.session_state.intent or {})
                        log_activity("viewed recipe", _hr_name)
                        st.toast(f"Opening '{_hr_name}'...")
                        st.rerun()
            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    else:
        st.info("No recommendations yet. Click \U0001f504 Refresh to generate recipes.")

    st.divider()

    # -- Manual ingredient table (always visible) ----------------------------
    st.markdown(
        "<div class='quick-ideas-box'>"
        "<div class='section-label'>Products on Sale Today</div>"
        "<p style='color:#94a3b8;font-size:.82rem;margin:0 0 14px'>"
        "Tick products below to build your own recipe, or ask the chat to suggest recipes automatically.</p>",
        unsafe_allow_html=True,
    )
    with st.expander("Select products for a custom recipe", expanded=True):
        _hi = st.session_state.get("ingredients", [])
        if not _hi:
            with st.spinner("Loading products..."):
                _hi = get_all_ingredients()
                st.session_state.ingredients = _hi
        _hf = filter_by_diet(_hi, _cur_dn)
        if not _hf:
            st.warning("No products found. Try refreshing in the sidebar.")
        else:
            import pandas as _pd2  # type: ignore
            _hnm: dict[str, dict] = {}
            for _hx in _hf[:40]:
                _nn = _hx.get("name", "").strip()
                if _nn and _nn not in _hnm:
                    _hnm[_nn] = _hx
            _hpre = set(st.session_state.get("manual_selected_names") or [])
            _hrows = [
                {
                    "Select": _hn in _hpre,
                    "Product": _hn,
                    "Store": _hd.get("store", ""),
                    "Sale Price": f"\u00a3{_hd.get('discounted_price', 0):.2f}",
                    "Was": f"\u00a3{_hd.get('original_price', 0):.2f}",
                    "Category": _hd.get("category", ""),
                }
                for _hn, _hd in sorted(_hnm.items())
            ]
            _hdf = _pd2.DataFrame(_hrows)
            st.caption("Tick products to select, then generate recipes.")
            _hedf = st.data_editor(
                _hdf,
                column_config={
                    "Select": st.column_config.CheckboxColumn("Select", width="small"),
                    "Product": st.column_config.TextColumn("Product", width="large"),
                    "Store": st.column_config.TextColumn("Store", width="medium"),
                    "Sale Price": st.column_config.TextColumn("Sale Price", width="small"),
                    "Was": st.column_config.TextColumn("Was", width="small"),
                    "Category": st.column_config.TextColumn("Category", width="medium"),
                },
                hide_index=True,
                key="home_table_editor",
                disabled=["Product", "Store", "Sale Price", "Was", "Category"],
            )
            _hsel = list(_hedf.loc[_hedf["Select"], "Product"])
            st.session_state.manual_selected_names = _hsel
            if _hsel:
                st.success(f"**{len(_hsel)} selected:** {', '.join(_hsel[:5])}" + (" ..." if len(_hsel) > 5 else ""))
            else:
                st.caption("Tick at least 2 products, then click Generate.")
            if len(_hsel) >= 2:
                if st.button("Generate Recipes From My Selection", type="primary", key="home_manual_gen"):
                    _check_guest_limit()
                    _hdicts = [_hnm[n] for n in _hsel if n in _hnm]
                    with st.spinner("Generating recipes..."):
                        _hmrecs = generate_recommendations_with_reasons(_cur_diet, _hdicts)
                    st.session_state.manual_recs = _hmrecs
                    st.session_state.recommendations = _hmrecs
                    st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    st.divider()

    # -- Quick Recipe Idea chips ---------------------------------------------
    st.markdown(
        "<div class='quick-ideas-box'>"
        "<div class='section-label'>Quick Recipe Ideas</div>"
        "<p style='color:#94a3b8;font-size:.82rem;margin:0 0 14px'>"
        "Pick a shortcut to instantly generate a recipe tailored to today\u2019s UK deals.</p>",
        unsafe_allow_html=True,
    )
    _qchips = [
        ("Veg Dinner for 4 ~\u00a315",   "Vegetarian dinner for 4 people under 15 pounds"),
        ("Chicken Meal for 2 ~\u00a310",  "Chicken meal for 2 people under 10 pounds"),
        ("Budget Pasta ~\u00a35",          "Cheap pasta recipe for 2 under 5 pounds"),
        ("Healthy Soup ~\u00a36",          "Healthy vegetable soup for 4 under 6 pounds"),
        ("Rice Bowl for 2 ~\u00a37",       "Rice bowl recipe for 2 people under 7 pounds"),
        ("Veg Stir Fry for 4 ~\u00a39",   "Vegetarian stir fry for 4 people under 9 pounds"),
    ]
    _qcols = st.columns(3, gap="small")
    for _qi, (_qlabel, _qquery) in enumerate(_qchips):
        with _qcols[_qi % 3]:
            if st.button(_qlabel, key=f"qchip_{_qi}", use_container_width=True):
                st.session_state.landing_pending_input = _qquery
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    st.divider()

    # -- Ask for a Recipe Chat ------------------------------------------------
    st.markdown(
        "<div class='quick-ideas-box'>"
        "<div class='section-label'>Ask for a Recipe</div>"
        "<p style='color:#94a3b8;font-size:.82rem;margin:0 0 14px'>"
        "Get instant recipe ideas based on today\u2019s UK deals \u2014 type anything below.</p>",
        unsafe_allow_html=True,
    )
    render_landing_chat()
    st.markdown("</div>", unsafe_allow_html=True)


# ==============================================================================
# STATE: LOADING_RECS
# ==============================================================================
elif st.session_state.app_state == "LOADING_RECS":
    diet      = st.session_state.diet or "Veg"
    diet_name = "Vegetarian" if diet == "Veg" else "Non-Vegetarian"

    with st.spinner(f"Fetching today's UK deals and building your {diet_name} recommendations"):
        ingredients = get_all_ingredients()
        st.session_state.ingredients = ingredients
        recs = generate_recommendations_with_reasons(diet, ingredients)
        st.session_state.recommendations = recs
        st.session_state.intent = {
            "diet":         "Vegetarian" if diet == "Veg" else "Non-Vegetarian",
            "budget":       15,
            "servings":     2,
            "cook_time":    "any",
            "meal_type":    "any meal",
            "restrictions": "none",
        }

    st.session_state.app_state = "RECOMMENDATIONS"
    st.rerun()


# ==============================================================================
# STATE: RECOMMENDATIONS
# ==============================================================================
elif st.session_state.app_state == "RECOMMENDATIONS":
    diet      = st.session_state.diet or "Veg"
    diet_name = "Vegetarian" if diet == "Veg" else "Non-Vegetarian"
    recs      = st.session_state.recommendations

    st.markdown(
        f"<h3>{diet_name} Recommendations \u2014 Today's Best Deals</h3>"
        f"<p style='color:#666'>Based on discounted UK supermarket products available right now.</p>",
        unsafe_allow_html=True,
    )

    sw1, sw2, _gap = st.columns([1,1,4])
    with sw1:
        if st.button("Switch to Vegetarian", disabled=(diet=="Veg"), key="sw_veg"):
            st.session_state.diet      = "Veg"
            st.session_state.app_state = "LOADING_RECS"
            st.rerun()
    with sw2:
        if st.button("Switch to Non-Vegetarian", disabled=(diet=="Non-Veg"), key="sw_nv"):
            st.session_state.diet      = "Non-Veg"
            st.session_state.app_state = "LOADING_RECS"
            st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    # -- Manual Ingredient Selection (top of page) ------------------------------
    st.markdown(
        "<div style='background:#f8f9fa;border-radius:12px;padding:18px 24px;"
        "border:1px solid #e0e0e0;margin-bottom:16px'>"
        "<h4 style='margin:0 0 4px'>Select Your Own Ingredients</h4>"
        "<p style='color:#666;font-size:.9rem;margin:0'>"
        "Tick products from the table and generate 3 personalised "
        f"{diet_name} recipes - or scroll down for today's auto-recommendations.</p>"
        "</div>",
        unsafe_allow_html=True,
    )
    with st.expander("Open Ingredient Table & Generate Recipes", expanded=False):
        _all_ingreds   = st.session_state.get("ingredients", [])
        _filt_ingreds  = filter_by_diet(_all_ingreds, "Vegetarian" if diet == "Veg" else "Non-Vegetarian")
        if not _filt_ingreds:
            st.warning("No ingredients available. Try refreshing product data in the sidebar.")
        else:
            import pandas as _pd  # type: ignore
            _ingred_name_map: dict[str, dict] = {}
            for _i in _filt_ingreds[:40]:
                _n = _i.get("name", "").strip()
                if _n and _n not in _ingred_name_map:
                    _ingred_name_map[_n] = _i
            _pre_sel = set(st.session_state.get("manual_selected_names") or [])
            _trows = []
            for _tname, _tdat in sorted(_ingred_name_map.items()):
                _trows.append({
                    "Select": _tname in _pre_sel,
                    "Product": _tname,
                    "Store": _tdat.get("store", ""),
                    "Sale Price": f"\u00a3{_tdat.get('discounted_price', 0):.2f}",
                    "Was": f"\u00a3{_tdat.get('original_price', 0):.2f}",
                    "Category": _tdat.get("category", ""),
                })
            _tdf = _pd.DataFrame(_trows)
            st.caption(f"Showing {diet_name} products only. Tick to select, then click Generate.")
            _edf = st.data_editor(
                _tdf,
                column_config={
                    "Select": st.column_config.CheckboxColumn("Select", width="small"),
                    "Product": st.column_config.TextColumn("Product", width="large"),
                    "Store": st.column_config.TextColumn("Store", width="medium"),
                    "Sale Price": st.column_config.TextColumn("Sale Price", width="small"),
                    "Was": st.column_config.TextColumn("Was", width="small"),
                    "Category": st.column_config.TextColumn("Category", width="medium"),
                },
                hide_index=True,
                key="manual_top_table",
                disabled=["Product", "Store", "Sale Price", "Was", "Category"],
            )
            _sel_names = list(_edf.loc[_edf["Select"], "Product"])
            st.session_state.manual_selected_names = _sel_names
            if _sel_names:
                st.success(f"**{len(_sel_names)} selected:** {', '.join(_sel_names[:5])}" + (" \u2026" if len(_sel_names) > 5 else ""))
            else:
                st.caption("Select at least 2 products to enable recipe generation.")
            if len(_sel_names) >= 2:
                if st.button("Generate My 3 Recipes", type="primary", key="manual_top_gen_btn"):
                    _check_guest_limit()
                    _sel_dicts = [_ingred_name_map[n] for n in _sel_names if n in _ingred_name_map]
                    with st.spinner(f"Generating {diet_name} recipes for your selection\u2026"):
                        _mt_recs = generate_recommendations_with_reasons(diet, _sel_dicts)
                    st.session_state.manual_recs = _mt_recs
                    log_activity("manual process used", f"{diet_name} \u2013 {', '.join(_sel_names[:3])}")
                    st.rerun()
        _mt_shown: list[dict] = st.session_state.get("manual_recs") or []
        if _mt_shown:
            st.divider()
            st.markdown(f"**3 personalised {diet_name} recipes for your selection:**")
            for _mi, _mrec in enumerate(_mt_shown[:3]):
                _mname    = _mrec.get("name", f"Recipe {_mi+1}")
                _mdesc    = _mrec.get("description", "A delicious dish.")
                _mcost    = _mrec.get("estimated_cost", "~\u00a36.00")
                _mreasons = _mrec.get("reasons", [])
                st.markdown(
                    f"<div class='rec-card'>"
                    f"<h4>{_mname}</h4>"
                    f"<p style='color:#555;margin:4px 0 8px'>{_mdesc}</p>"
                    f"<div style='margin-bottom:8px'>"
                    f"  <span class='rec-tag budget'>{_mcost}</span>"
                    f"  <span class='rec-tag'>{diet_name}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                for _mr in _mreasons[:3]:
                    st.markdown(f"<div class='reason-item'><span class='reason-icon'>-</span><span>{_mr}</span></div>", unsafe_allow_html=True)
                st.markdown("</div>", unsafe_allow_html=True)
                if st.button(f"View Full Recipe \u2014 {_mname[:30]}", key=f"manual_top_view_{_mi}", type="primary", use_container_width=True):
                    _check_guest_limit()
                    with st.spinner(f"Generating full recipe for {_mname}\u2026"):
                        _mfull = expand_recipe(_mname, st.session_state.intent, st.session_state.ingredients)
                    st.session_state.current_recipe_name    = _mname
                    st.session_state.current_recipe_content = _mfull
                    st.session_state.app_state              = "RECIPE_VIEW"
                    save_recipe(_mname, _mfull, list(st.session_state.global_chat_messages), st.session_state.intent)
                    log_activity("viewed recipe", _mname)
                    st.toast(f"'{_mname}' saved!")
                    st.rerun()
                st.markdown("")
            if st.button("Clear Manual Results", key="manual_top_clear"):
                st.session_state.manual_recs = []
                st.session_state.manual_selected_names = []
                st.rerun()

    st.divider()

    if not recs:
        st.warning("Could not generate recommendations. Try refreshing the product data in the sidebar.")
        st.stop()

    for i, rec in enumerate(recs):
        name    = rec.get("name",           f"Recipe {i+1}")
        desc    = rec.get("description",    "A delicious budget-friendly dish.")
        cost    = rec.get("estimated_cost", "~�6.00")
        reasons = rec.get("reasons",        [])

        st.markdown(
            f"<div class='rec-card'>"
            f"<h3>Recipe {i+1}: {name}</h3>"
            f"<p style='color:#555;margin:4px 0 10px'>{desc}</p>"
            f"<div style='margin-bottom:10px'>"
            f"  <span class='rec-tag budget'>{cost}</span>"
            f"  <span class='rec-tag'>{diet_name}</span>"
            f"</div>"
            f"<div style='font-size:.85rem;font-weight:600;color:#333;margin-bottom:6px'>"
            f"Why this recipe is recommended:</div>",
            unsafe_allow_html=True,
        )
        for reason in reasons[:3]:
            st.markdown(
                f"<div class='reason-item'><span class='reason-icon'>-</span>"
                f"<span>{reason}</span></div>",
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)

        if st.button(f"View Full Recipe {i+1}", key=f"view_{i}", type="primary", use_container_width=True):
                _check_guest_limit()
                with st.spinner(f"Generating full recipe for {name}..."):
                    full = expand_recipe(name, st.session_state.intent,
                                        st.session_state.ingredients)
                st.session_state.current_recipe_name    = name
                st.session_state.current_recipe_content = full
                st.session_state.app_state              = "RECIPE_VIEW"
                save_recipe(name, full, list(st.session_state.global_chat_messages), st.session_state.intent)
                log_activity("viewed recipe", name)
                st.toast(f"'{name}' saved to your sessions!")
                st.rerun()
        _summary = (
            f"# {name}\n\n{desc}\n\n*Estimated cost: {cost}*\n\n"
            "## Why recommended\n"
            + "\n".join(f"- {r}" for r in reasons[:3])
            + "\n\nOpen the full recipe in the app for step-by-step instructions."
        )
        st.download_button(
            label="Download Summary PDF",
            data=recipe_to_pdf(_summary, name),
            file_name=f"{name[:40].replace(' ','_')}_summary.pdf",
            mime="application/pdf",
            key=f"dl_rec_{i}",
        )

        st.markdown("")

    st.divider()

    # Recommendation history
    activity = load_activity_log()
    if activity:
        with st.expander("\U0001f4cb Activity History", expanded=False):
            _aik2 = {"viewed recipe":"\U0001f441 Viewed","started cooking":"\U0001f373 Cooking",
                     "completed":"\u2705 Done","downloaded recipe":"\u2b07 Downloaded",
                     "manual process used":"\U0001f527 Manual"}
            ah1, ah2, ah3 = st.columns([2, 3, 2])
            ah1.markdown("<span style='font-size:.78rem;font-weight:700;color:#6b7280'>TIME</span>", unsafe_allow_html=True)
            ah2.markdown("<span style='font-size:.78rem;font-weight:700;color:#6b7280'>RECIPE</span>", unsafe_allow_html=True)
            ah3.markdown("<span style='font-size:.78rem;font-weight:700;color:#6b7280'>ACTION</span>", unsafe_allow_html=True)
            for _e in activity[:30]:
                c1, c2, c3 = st.columns([2, 3, 2])
                c1.caption(_e.get("timestamp", ""))
                c2.markdown(_e.get("recipe_name", ""))
                c3.markdown(_aik2.get(_e.get("action", ""), _e.get("action", "")))




# ==============================================================================
# STATE: RECIPE_VIEW
# ==============================================================================
elif st.session_state.app_state == "RECIPE_VIEW":
    recipe_name    = st.session_state.current_recipe_name
    recipe_content = st.session_state.current_recipe_content

    # -- Header row: Back  |  Title + subtitle  |  PDF download --------------
    pdf_bytes = recipe_to_pdf(recipe_content, recipe_name)
    st.markdown(
        f"<div style='background:#042f2e;border:1px solid #115e59;border-radius:14px;"
        f"padding:18px 24px;margin-bottom:16px;display:flex;align-items:center;gap:12px'>"
        f"<div style='flex:1'>"
        f"<h2 style='margin:0 0 4px;font-size:1.4rem;font-weight:800;"
        f"color:#f8fafc;line-height:1.25;letter-spacing:-0.3px'>{recipe_name}</h2>"
        f"<p style='margin:0;font-size:0.83rem;color:#94a3b8'>"
        f"Step-by-step recipe with ingredients and instructions.</p>"
        f"</div></div>",
        unsafe_allow_html=True,
    )
    _bk_col, _spacer_col, _pdf_col = st.columns([1.2, 5.5, 1.8], gap="small")
    with _bk_col:
        if st.button("← Back", key="back_btn"):
            st.session_state.app_state = "VEG_SELECT"
            st.rerun()
    with _pdf_col:
        st.download_button(
            label="⬇ Download PDF",
            data=pdf_bytes,
            file_name=f"{recipe_name[:40].replace(' ','_')}.pdf",
            mime="application/pdf",
            key="dl_full_recipe",
        )

    all_act = load_activity_log()
    st.divider()

    # -- Full recipe content ------------------------------------------------
    with st.container(border=True):
        st.markdown(recipe_content)

    st.divider()

    # Chat visible on recipe page
    render_home_chat()

    # Recommendation / cooking history
    if all_act:
        with st.expander("Activity History", expanded=False):
            st.markdown(
                "<p style='font-size:.83rem;color:#94a3b8;margin:0 0 10px'>"
                "Everything you viewed, started, completed, and downloaded.</p>",
                unsafe_allow_html=True,
            )
            _imap = {"viewed recipe": "Viewed", "started cooking": "Cooking",
                     "completed": "✅ Done", "downloaded recipe": "⬇ Downloaded"}
            hc1, hc2, hc3 = st.columns([2, 3, 2])
            hc1.markdown("<span style='font-size:.78rem;font-weight:700;color:#6b7280'>TIME</span>", unsafe_allow_html=True)
            hc2.markdown("<span style='font-size:.78rem;font-weight:700;color:#6b7280'>RECIPE</span>", unsafe_allow_html=True)
            hc3.markdown("<span style='font-size:.78rem;font-weight:700;color:#6b7280'>ACTION</span>", unsafe_allow_html=True)
            for _e in all_act[:50]:
                c1, c2, c3 = st.columns([2, 3, 2])
                _icon = _imap.get(_e.get("action", ""), "")
                c1.caption(_e.get("timestamp", ""))
                _hl = "**" if _e.get("recipe_name") == recipe_name else ""
                c2.markdown(f"{_hl}{_e.get('recipe_name', '')}{_hl}")
                c3.markdown(f"{_icon}")


