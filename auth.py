"""auth.py - Enterprise-grade Login and Registration pages.

Uses Supabase Auth (email/password) and stores extended profile data
in the `user_profiles` table.

Public API:
  render_auth_page()  -> renders login/register UI; returns True when authenticated
  get_current_user()  -> returns the session_state user dict or None
  logout()            -> signs out and clears session
"""

from __future__ import annotations

import re
import time
import time as _time
import logging

import streamlit as st  # type: ignore
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supabase handles
# ---------------------------------------------------------------------------
def _get_supabase():
    try:
        from config import supabase
        return supabase
    except Exception:
        return None


def _get_admin():
    """Return the service-role client if configured, else anon client."""
    try:
        from config import supabase_admin, supabase
        return supabase_admin if supabase_admin is not None else supabase
    except Exception:
        return None


def _ensure_user_profiles_table() -> None:
    """Create user_profiles table via service-role if it doesn't exist."""
    admin = _get_admin()
    if admin is None:
        return
    try:
        admin.table("user_profiles").select("id").limit(1).execute()
        # Try to add onboarding_completed column if it doesn't exist yet
        try:
            admin.rpc("exec_sql", {
                "query": "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS onboarding_completed boolean DEFAULT false;"
            }).execute()
        except Exception:
            pass  # Column may already exist or RPC unavailable — safe to ignore
    except Exception:
        # Table missing — try creating via RPC
        ddl = """
        CREATE TABLE IF NOT EXISTS user_profiles (
            id                    uuid PRIMARY KEY,
            email                 text NOT NULL UNIQUE,
            full_name             text NOT NULL DEFAULT '',
            onboarding_completed  boolean DEFAULT false,
            created_at            timestamptz DEFAULT now(),
            updated_at            timestamptz DEFAULT now()
        );
        """
        try:
            admin.rpc("exec_sql", {"query": ddl}).execute()
        except Exception as e:
            logger.warning("Could not auto-create user_profiles: %s", e)


# ---------------------------------------------------------------------------
# Session persistence (browser cookie → survive page refresh)
# ---------------------------------------------------------------------------
_SESSION_COOKIE = "ibr_session_v1"


_SESSION_MAX_IDLE = 30 * 24 * 3600  # 30 days inactivity = session expires


def _write_session_cookie(user_info: dict, refresh_token: str) -> None:
    """Write a persistent 30-day browser cookie with user info + refresh token.
    The cookie lives for 30 days on the same device/browser, so returning
    users are not asked to log in again.  A different device or browser
    has no cookie and must log in separately (per-device security).
    """
    if not refresh_token:
        return
    import json as _json
    import base64 as _b64
    import streamlit.components.v1 as _comp
    payload = _b64.b64encode(_json.dumps({
        "id":           user_info.get("id", ""),
        "email":        user_info.get("email", ""),
        "full_name":    user_info.get("full_name", ""),
        "access_token": user_info.get("access_token", ""),
        "rt":           refresh_token,
        "last_seen":    int(_time.time()),
    }).encode()).decode()
    # Persistent 30-day cookie — same device stays logged in for 30 days.
    # A fresh browser / different device has no cookie and must log in.
    _max_age = 30 * 24 * 3600  # 30 days in seconds
    _comp.html(
        f"""<script>
(function(){{
  var v=encodeURIComponent('{payload}');
  var c='{_SESSION_COOKIE}='+v+'; path=/; max-age={_max_age}; SameSite=Lax';
  try{{parent.document.cookie=c;}}catch(e){{}}
  document.cookie=c;
}})();
</script>""",
        height=0,
    )


def _clear_session_cookie() -> None:
    """Inject JS to delete the session cookie."""
    import streamlit.components.v1 as _comp
    _comp.html(
        f"""<script>
(function(){{
  var c='{_SESSION_COOKIE}=; path=/; max-age=0; SameSite=Lax';
  try{{parent.document.cookie=c;}}catch(e){{}}
  document.cookie=c;
}})();
</script>""",
        height=0,
    )


def _restore_session_from_cookie() -> bool:
    """Restore an authenticated session from the browser cookie on page refresh.

    Rules:
    - No cookie → False (user must log in)
    - Cookie present but last_seen > 30 days ago → False (session expired, must log in)
    - Cookie valid → restore session and return True (user stays on home page)
    """
    try:
        # If the user just logged out, skip cookie restore — the JS that wipes
        # the cookie runs asynchronously and the old cookie is still readable
        # on the first rerun after logout.
        if st.session_state.get("_logged_out"):
            return False

        raw = st.context.cookies.get(_SESSION_COOKIE)
        if not raw:
            return False

        import json as _json
        import base64 as _b64
        from urllib.parse import unquote as _unquote
        data = _json.loads(_b64.b64decode(_unquote(raw)).decode())

        # ── Check 30-day inactivity expiry ────────────────────────────────────
        last_seen = data.get("last_seen", 0)
        if last_seen and (_time.time() - last_seen) > _SESSION_MAX_IDLE:
            # Session has been idle for more than 30 days → force login
            logger.debug("Session cookie expired (30-day inactivity)")
            return False

        rt     = data.get("rt", "")
        _id    = data.get("id", "")
        _email = data.get("email", "")
        if not (_id and _email):
            return False

        # ── Strategy 1: refresh Supabase session with stored RT ───────────────
        sb = _get_supabase()
        if sb is not None and rt:
            try:
                resp = sb.auth.refresh_session(rt)
                if resp.session and resp.user:
                    meta = resp.user.user_metadata or {}
                    user_info = {
                        "id":           str(resp.user.id),
                        "email":        resp.user.email or _email,
                        "full_name":    meta.get("full_name", data.get("full_name", "")),
                        "access_token": resp.session.access_token,
                        "_rt":          resp.session.refresh_token,
                    }
                    st.session_state["auth_user"] = user_info
                    _write_session_cookie(user_info, resp.session.refresh_token)
                    return True
            except Exception as _refresh_err:
                logger.debug("refresh_session failed, falling back to cookie data: %s", _refresh_err)

        # ── Strategy 2: restore from cookie data (access token may still be valid) ──
        # Keeps the user on the home page even if Supabase is temporarily unreachable.
        user_info = {
            "id":           _id,
            "email":        _email,
            "full_name":    data.get("full_name", ""),
            "access_token": data.get("access_token", ""),
            "_rt":          rt,
        }
        st.session_state["auth_user"] = user_info
        return True

    except Exception as _e:
        logger.debug("Session restore from cookie failed: %s", _e)
    return False


# ---------------------------------------------------------------------------
# Helper: validation
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _validate_email(email: str) -> str | None:
    """Return error string or None if valid."""
    if not email:
        return "Email is required."
    if not _EMAIL_RE.match(email):
        return "Please enter a valid email address."
    return None


def _validate_password(pw: str) -> str | None:
    if not pw:
        return "Password is required."
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    return None


def _validate_name(name: str) -> str | None:
    if not name or not name.strip():
        return "Full name is required."
    if len(name.strip()) < 2:
        return "Name must be at least 2 characters."
    return None


# ---------------------------------------------------------------------------
# Supabase Auth helpers
# ---------------------------------------------------------------------------

def _parse_rate_limit(msg: str) -> int:
    """Extract wait-seconds from Supabase rate-limit error. Returns 0 if not found."""
    m = re.search(r"after (\d+) second", msg, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _sign_up(email: str, password: str, full_name: str) -> tuple[bool, str, dict]:
    """
    Register a new user.
    On success: immediately signs in, writes user_profiles, returns (True, msg, user_info).
    Returns (False, msg, {}) on any failure.
    """
    sb = _get_supabase()
    if sb is None:
        return False, "Database connection unavailable. Check your configuration.", {}
    try:
        resp = sb.auth.sign_up({
            "email": email,
            "password": password,
            "options": {"data": {"full_name": full_name}},
        })
        user = resp.user
        if user is None:
            return False, "Registration failed. This email may already be in use.", {}

        # Supabase (v2+) returns a fake-success with an empty identities list
        # when the email is already registered, instead of raising an error.
        # Detect this and block duplicate registration.
        if hasattr(user, "identities") and user.identities is not None and len(user.identities) == 0:
            return False, "An account with this email already exists. Please sign in instead.", {}

        # ── Get an authenticated session (try sign_up session first, then sign-in) ──
        user_info: dict = {}
        access_token = ""

        # Supabase returns a session in sign_up if email confirmation is OFF
        if resp.session:
            access_token  = resp.session.access_token or ""
            meta = user.user_metadata or {}
            user_info = {
                "id":           str(user.id),
                "email":        user.email or email,
                "full_name":    meta.get("full_name", full_name),
                "access_token": access_token,
                "_rt":          resp.session.refresh_token or "",
            }

        # If email confirmation is ON, sign_up gives no session — try explicit sign-in
        if not access_token:
            try:
                login_resp = sb.auth.sign_in_with_password({"email": email, "password": password})
                if login_resp.user and login_resp.session:
                    access_token   = login_resp.session.access_token or ""
                    _refresh_token = login_resp.session.refresh_token or ""
                    meta = login_resp.user.user_metadata or {}
                    user_info = {
                        "id":           str(login_resp.user.id),
                        "email":        login_resp.user.email or email,
                        "full_name":    meta.get("full_name", full_name),
                        "access_token": access_token,
                        "_rt":          _refresh_token,
                    }
            except Exception as login_err:
                logger.warning("Auto-login after register failed: %s", login_err)

        # ── Write to user_profiles ────────────────────────────────────────────
        # Use service-role client (bypasses RLS) when available,
        # otherwise use the user's own JWT with the anon client.
        try:
            from config import supabase_admin as _sa2
        except Exception:
            _sa2 = None
        try:
            if _sa2 is not None:
                # Service-role: bypasses RLS entirely
                _sa2.table("user_profiles").upsert({
                    "id":                   str(user.id),
                    "email":                email,
                    "full_name":            full_name,
                    "onboarding_completed": False,
                }, on_conflict="id").execute()
            elif access_token:
                # Authenticated anon client: set JWT header so RLS check passes
                sb.postgrest.auth(access_token)
                sb.table("user_profiles").upsert({
                    "id":                   str(user.id),
                    "email":                email,
                    "full_name":            full_name,
                    "onboarding_completed": False,
                }, on_conflict="id").execute()
            else:
                logger.warning("user_profiles write skipped: no auth token available")
                return True, "Account created successfully.", user_info
            logger.info("user_profiles row created for %s", email)
        except Exception:
            pass  # RLS/network failure on profile write — silently ignored

        return True, "Account created successfully.", user_info

    except Exception as exc:
        msg = str(exc)
        wait = _parse_rate_limit(msg)
        if wait > 0:
            return False, f"rate_limit:{wait}", {}
        if "already registered" in msg.lower() or "already exists" in msg.lower():
            return False, "An account with this email already exists. Please sign in instead.", {}
        if "invalid email" in msg.lower():
            return False, "The email address appears to be invalid.", {}
        if "password" in msg.lower():
            return False, "Password does not meet requirements. Use at least 8 characters.", {}
        return False, "Registration failed. Please try again or contact support.", {}


def _sign_in(email: str, password: str) -> tuple[bool, str, dict]:
    """Sign in. Returns (success, message, user_info_dict)."""
    sb = _get_supabase()
    if sb is None:
        return False, "Database connection unavailable.", {}
    try:
        resp = sb.auth.sign_in_with_password({"email": email, "password": password})
        user    = resp.user
        session = resp.session
        if user is None or session is None:
            return False, "Invalid email or password.", {}
        # Fetch extended profile (use the now-authenticated anon client or admin)
        profile = {}
        try:
            read_client = _get_admin() or sb
            pr = read_client.table("user_profiles").select("*").eq("id", str(user.id)).single().execute()
            profile = pr.data or {}
        except Exception:
            profile = {}

        full_name_val = (
            profile.get("full_name")
            or (user.user_metadata or {}).get("full_name", "")
            or ""
        )
        user_info = {
            "id":           str(user.id),
            "email":        user.email or email,
            "full_name":    full_name_val,
            "access_token": session.access_token,
            "_rt":          session.refresh_token or "",
        }

        # Ensure user_profiles row exists — write here using the authenticated client
        if not profile:
            try:
                from config import supabase_admin as _sa
                _row = {
                    "id":                   str(user.id),
                    "email":                user.email or email,
                    "full_name":            full_name_val,
                    "onboarding_completed": True,
                }
                if _sa is not None:
                    # Service-role client: bypasses RLS entirely
                    _sa.table("user_profiles").upsert(_row, on_conflict="id").execute()
                else:
                    # Authenticate anon client with user JWT so RLS (auth.uid() = id) passes
                    sb.postgrest.auth(session.access_token)
                    sb.table("user_profiles").upsert(_row, on_conflict="id").execute()
            except Exception:
                pass  # RLS/network failure on profile upsert — silently ignored

        # Only flag onboarding if the user hasn't completed it yet.
        # If the profile row exists but onboarding_completed is False it means
        # a previous mark_onboarding_complete() call failed (e.g. wrong service key).
        # Heal it silently: if the user already has saved sessions they've clearly
        # been through the app before — skip onboarding and fix the DB now.
        if not profile.get("onboarding_completed", True):
            _already_used = False
            try:
                _heal_client = _get_admin() or sb
                _heal_client.postgrest.auth(session.access_token)
                _sessions_check = (
                    _heal_client.table("chat_sessions")
                    .select("id", count="exact")
                    .eq("user_id", str(user.id))
                    .limit(1)
                    .execute()
                )
                _already_used = bool(
                    (_sessions_check.count or 0) > 0
                    or (_sessions_check.data and len(_sessions_check.data) > 0)
                )
            except Exception:
                pass
            if _already_used:
                # User has prior data — they completed onboarding before but the DB
                # record was stale. Heal silently and skip showing it again.
                try:
                    _heal_admin = _get_admin()
                    if _heal_admin is not None:
                        _heal_admin.table("user_profiles").upsert(
                            {"id": str(user.id), "email": user.email or email,
                             "full_name": full_name_val, "onboarding_completed": True},
                            on_conflict="id"
                        ).execute()
                        logger.info("Auto-healed onboarding_completed for user %s", user.id)
                except Exception:
                    pass
            else:
                st.session_state["onboarding_needed"] = True
        return True, "Authenticated.", user_info  # _rt key consumed by caller
    except Exception as exc:
        msg = str(exc)
        wait = _parse_rate_limit(msg)
        if wait > 0:
            return False, f"rate_limit:{wait}", {}
        _ml = msg.lower()
        if "invalid" in _ml or "credentials" in _ml or "wrong" in _ml:
            return False, "Invalid email or password. Please check your details and try again.", {}
        if any(w in _ml for w in ("not confirmed", "email not confirmed", "confirmation")):
            return False, "Your email address is not yet verified. Please check your inbox and click the confirmation link before signing in.", {}
        if "user not found" in _ml or "no user" in _ml:
            return False, "No account found with this email. Please register first.", {}
        logger.warning("_sign_in unexpected error: %s", msg)
        return False, "Sign in failed. Please check your email and password and try again.", {}


def _sign_out() -> None:
    sb = _get_supabase()
    if sb is not None:
        try:
            sb.auth.sign_out()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------
def get_current_user() -> dict | None:
    return st.session_state.get("auth_user")


def mark_onboarding_complete() -> None:
    """Persist onboarding completion to user_profiles so it's not shown again on next login.
    Uses upsert (not update) so it works even if the profile row was never created."""
    user = get_current_user()
    if not user:
        return
    access_token = user.get("access_token", "")
    user_id      = user.get("id", "")
    email        = user.get("email", "")
    full_name    = user.get("full_name", "")
    if not user_id:
        return
    row = {
        "id":                   user_id,
        "email":                email,
        "full_name":            full_name,
        "onboarding_completed": True,
    }
    # Prefer admin (service-role) client — bypasses RLS
    admin = _get_admin()
    sb    = _get_supabase()
    try:
        if admin is not None:
            admin.table("user_profiles").upsert(row, on_conflict="id").execute()
        elif sb and access_token:
            sb.postgrest.auth(access_token)
            sb.table("user_profiles").upsert(row, on_conflict="id").execute()
        logger.info("Onboarding marked complete for user %s", user_id)
    except Exception as e:
        logger.warning("Could not mark onboarding complete: %s", e)


def logout() -> None:
    _clear_session_cookie()
    _sign_out()
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.session_state["auth_page"] = "login"
    # Flag: prevent cookie-restore on the immediate post-logout rerun
    # (_clear_session_cookie is JS-injected and runs after the rerun,
    #  so without this flag the old cookie would restore the session)
    st.session_state["_logged_out"] = True


# ---------------------------------------------------------------------------
# Full-page CSS — split-panel enterprise layout
# ---------------------------------------------------------------------------
_AUTH_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

/* ── Reset & base ─────────────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; }

html, body,
[data-testid="stAppViewContainer"],
[data-testid="stAppViewContainer"] > .main {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: #f8fafc !important;
    margin: 0; padding: 0;
}

[data-testid="stHeader"],
[data-testid="stToolbar"],
[data-testid="stDecoration"],
footer { display: none !important; }

[data-testid="block-container"] {
    padding: 0 !important;
    max-width: 100% !important;
}

/* ── Left branding panel ──────────────────────────────────────────────────── */
.auth-panel-left {
    background:
        radial-gradient(ellipse 75% 45% at 115% 5%, rgba(13,148,136,0.28) 0%, transparent 55%),
        radial-gradient(ellipse 55% 35% at -15% 95%, rgba(217,119,6,0.17) 0%, transparent 55%),
        linear-gradient(155deg, #042f2e 0%, #021713 45%, #010c0b 100%);
    padding: 64px 56px;
    position: relative;
    overflow: hidden;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    justify-content: center;
}
.auth-panel-left::before {
    content: "";
    position: absolute;
    inset: 0;
    background-image:
        linear-gradient(rgba(13,148,136,0.06) 1px, transparent 1px),
        linear-gradient(90deg, rgba(13,148,136,0.06) 1px, transparent 1px);
    background-size: 40px 40px;
    pointer-events: none;
}
.auth-panel-left::after {
    content: "";
    position: absolute;
    bottom: -70px; right: 30px;
    width: 280px; height: 280px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(217,119,6,0.13) 0%, transparent 70%);
    pointer-events: none;
}

.auth-brand-mark {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 52px; height: 52px;
    background: linear-gradient(135deg, #0d9488, #0f766e);
    border-radius: 14px;
    margin-bottom: 36px;
    box-shadow: 0 8px 28px rgba(13,148,136,0.40);
    flex-shrink: 0;
}

.auth-headline {
    font-size: 2.25rem;
    font-weight: 800;
    color: #f8fafc;
    line-height: 1.2;
    margin: 0 0 14px;
    letter-spacing: -0.5px;
    max-width: 360px;
}
.auth-subline {
    font-size: 0.96rem;
    color: #94a3b8;
    line-height: 1.65;
    margin: 0 0 44px;
    max-width: 330px;
    font-weight: 400;
}

.auth-features { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 20px; }
.auth-feat-item { display: flex; align-items: flex-start; gap: 14px; }
.auth-feat-icon {
    width: 38px; height: 38px; flex-shrink: 0;
    background: rgba(13,148,136,0.10);
    border: 1px solid rgba(13,148,136,0.28);
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    transition: background 0.2s;
}
.auth-feat-icon svg {
    width: 17px; height: 17px;
    stroke: #5eead4; fill: none;
    stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round;
}
.auth-feat-text { display: flex; flex-direction: column; gap: 3px; }
.auth-feat-title { font-size: 0.88rem; font-weight: 600; color: #e2e8f0; }
.auth-feat-desc  { font-size: 0.78rem; color: #64748b; font-weight: 400; }

.auth-divider-line {
    width: 48px; height: 2px;
    background: linear-gradient(90deg, #0d9488, transparent);
    border-radius: 2px;
    margin: 40px 0;
}
.auth-trust {
    display: flex; align-items: center; gap: 8px;
    font-size: 0.76rem; color: #475569; font-weight: 500;
}
.auth-trust-dot {
    width: 6px; height: 6px;
    background: #14b8a6;
    border-radius: 50%;
    box-shadow: 0 0 0 3px rgba(20,184,166,0.2);
}

/* ── Right form panel ─────────────────────────────────────────────────────── */
.auth-form-header { margin-bottom: 32px; }
.auth-form-header h2 {
    font-size: 1.7rem;
    font-weight: 800;
    color: #0f172a;
    margin: 0 0 8px;
    letter-spacing: -0.4px;
}
.auth-form-header p {
    font-size: 0.9rem; color: #64748b;
    margin: 0; font-weight: 400;
}

/* ── Input fields ─────────────────────────────────────────────────────────── */
.stTextInput > div > div > input,
.stTextInput > div > div > div > input {
    border: 1.5px solid #dcdcdc !important;
    border-radius: 10px !important;
    padding: 14px 16px !important;
    font-size: 0.93rem !important;
    font-family: 'Inter', sans-serif !important;
    color: #0f172a !important;
    background: #fafbff !important;
    transition: border-color 0.22s ease, box-shadow 0.22s ease, background 0.18s !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06) !important;
    cursor: text !important;
    caret-color: #0d9488 !important;
    pointer-events: auto !important;
    user-select: text !important;
    -webkit-user-select: text !important;
    outline: none !important;
}
/* Autofill: kill browser yellow/grey */
.stTextInput > div > div > input:-webkit-autofill,
.stTextInput > div > div > div > input:-webkit-autofill {
    -webkit-box-shadow: 0 0 0 30px #fafbff inset !important;
    -webkit-text-fill-color: #0f172a !important;
    transition: background-color 9999s ease !important;
}
.stTextInput,
.stTextInput > div,
.stTextInput > div > div,
.stTextInput > div > div > div {
    cursor: text !important;
    pointer-events: auto !important;
}
/* Password show/hide button */
.stTextInput button,
.stTextInput [data-testid="InputInstructions"] button {
    cursor: pointer !important;
    pointer-events: auto !important;
}
/* Hover */
.stTextInput > div > div > input:hover,
.stTextInput > div > div > div > input:hover {
    border-color: #2dd4bf !important;
    box-shadow: 0 1px 5px rgba(13,148,136,0.10) !important;
    cursor: text !important;
}
/* Focus: green glow */
.stTextInput > div > div > input:focus,
.stTextInput > div > div > div > input:focus {
    border-color: #0d9488 !important;
    background: #fff !important;
    box-shadow: 0 0 0 3px rgba(13,148,136,0.15) !important;
    outline: none !important;
    cursor: text !important;
}
/* Remove red invalid border */
.stTextInput > div > div > input:invalid,
.stTextInput > div > div > div > input:invalid {
    border-color: #dcdcdc !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06) !important;
}
.stTextInput > div > div > input::placeholder,
.stTextInput > div > div > div > input::placeholder { color: #b0bac7 !important; }

/* Labels */
label {
    font-size: 0.83rem !important;
    font-weight: 600 !important;
    color: #374151 !important;
    letter-spacing: 0.01em !important;
    margin-bottom: 4px !important;
}

/* Extra vertical spacing between form fields */
.stTextInput { margin-bottom: 6px !important; }

/* ── Primary button ───────────────────────────────────────────────────────── */
.stButton > button[kind="primary"] {
    width: 100% !important;
    background: linear-gradient(135deg, #0d9488 0%, #0f766e 100%) !important;
    color: #fff !important;
    border: none !important;
    border-radius: 10px !important;
    padding: 14px 20px !important;
    font-size: 0.95rem !important;
    font-weight: 600 !important;
    font-family: 'Inter', sans-serif !important;
    letter-spacing: 0.01em !important;
    transition: background 0.2s ease, transform 0.15s ease, box-shadow 0.2s ease !important;
    box-shadow: 0 4px 14px rgba(13,148,136,0.30) !important;
    margin-top: 8px !important;
}
.stButton > button[kind="primary"]:hover {
    background: linear-gradient(135deg, #0f766e 0%, #115e59 100%) !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(13,148,136,0.40) !important;
}
.stButton > button[kind="primary"]:active {
    background: linear-gradient(135deg, #134e4a 0%, #0f766e 100%) !important;
    transform: translateY(0) !important;
    box-shadow: 0 2px 8px rgba(13,148,136,0.25) !important;
}

/* Hide 'Press Enter to submit form' hint */
[data-testid="InputInstructions"] { display: none !important; }

/* ── form_submit_button ───────────────────────────────────────────────────── */
[data-testid="stFormSubmitButton"] > button {
    width: 100% !important;
    background: linear-gradient(135deg, #0d9488 0%, #0f766e 100%) !important;
    color: #fff !important;
    border: none !important;
    border-radius: 10px !important;
    padding: 14px 20px !important;
    font-size: 0.95rem !important;
    font-weight: 600 !important;
    font-family: 'Inter', sans-serif !important;
    letter-spacing: 0.01em !important;
    transition: background 0.2s ease, transform 0.15s ease, box-shadow 0.2s ease !important;
    box-shadow: 0 4px 14px rgba(13,148,136,0.30) !important;
    margin-top: 8px !important;
    cursor: pointer !important;
}
[data-testid="stFormSubmitButton"] > button:hover {
    background: linear-gradient(135deg, #0f766e 0%, #115e59 100%) !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(13,148,136,0.40) !important;
}
[data-testid="stFormSubmitButton"] > button:active {
    background: linear-gradient(135deg, #134e4a 0%, #0f766e 100%) !important;
    transform: translateY(0) !important;
}

/* Strip default Streamlit form chrome */
[data-testid="stForm"] {
    border: none !important;
    background: transparent !important;
    padding: 0 !important;
    box-shadow: none !important;
}

/* ── Secondary / outline button ───────────────────────────────────────────── */
.stButton > button:not([kind="primary"]) {
    width: 100% !important;
    background: transparent !important;
    color: #0d9488 !important;
    border: 1.5px solid #ccfbf1 !important;
    border-radius: 10px !important;
    padding: 12px 20px !important;
    font-size: 0.88rem !important;
    font-weight: 600 !important;
    font-family: 'Inter', sans-serif !important;
    transition: background 0.18s, border-color 0.18s, transform 0.15s !important;
}
.stButton > button:not([kind="primary"]):hover {
    background: #f0fdfa !important;
    border-color: #0d9488 !important;
    transform: translateY(-1px) !important;
}

/* ── Alert banners ────────────────────────────────────────────────────────── */
.a-alert {
    border-radius: 10px; padding: 12px 16px;
    font-size: 0.87rem; font-weight: 500;
    margin-bottom: 18px; line-height: 1.6;
    display: flex; align-items: flex-start; gap: 10px;
}
.a-alert-error   { background:#fef2f2; border:1.5px solid #fecaca; border-left:4px solid #dc2626; color:#991b1b; }
.a-alert-success { background:#f0fdfa; border:1.5px solid #bbf7d0; border-left:4px solid #0d9488; color:#115e59; }
.a-alert-rate    { background:#fffbeb; border:1.5px solid #fde68a; border-left:4px solid #d97706; color:#92400e; }
.a-alert-info    { background:#eff6ff; border:1.5px solid #bfdbfe; border-left:4px solid #2563eb; color:#1e40af; }

/* ── Separator ────────────────────────────────────────────────────────────── */
.auth-sep {
    display: flex; align-items: center; gap: 12px;
    margin: 22px 0;
}
.auth-sep-line  { flex: 1; height: 1px; background: #e9edf3; }
.auth-sep-text  { font-size: 0.78rem; color: #9ca3af; font-weight: 500; white-space: nowrap; }

/* ── Strength bar ─────────────────────────────────────────────────────────── */
.str-track { height: 3px; background: #e9edf3; border-radius: 4px; margin: 6px 0 3px; overflow: hidden; }
.str-fill  { height: 100%; border-radius: 4px; transition: width 0.3s; }
.str-label { font-size: 0.74rem; font-weight: 600; }

/* ── Security footer ──────────────────────────────────────────────────────── */
.auth-form-footer {
    margin-top: 28px;
    display: flex; align-items: center; gap: 8px;
    font-size: 0.75rem; color: #9ca3af;
}
.auth-form-footer svg {
    flex-shrink: 0; width: 13px; height: 13px;
    stroke: #9ca3af; fill: none;
    stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round;
}

/* ── Layout: full-height split columns ────────────────────────────────────── */
[data-testid="stHorizontalBlock"] {
    gap: 0 !important;
    align-items: stretch !important;
}
[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
    padding: 0 !important;
}
/* Left column */
[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(1) > div[data-testid="stVerticalBlock"] {
    padding: 0 !important;
    min-height: 100vh !important;
    box-sizing: border-box !important;
}
/* Right column: white with generous padding */
[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(2) {
    background: #fff !important;
}
[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(2) > div[data-testid="stVerticalBlock"] {
    padding: 64px 52px !important;
    min-height: 100vh !important;
    background: #fff !important;
    box-sizing: border-box !important;
}
</style>
"""


# ---------------------------------------------------------------------------
# Left panel HTML + SVG icons
# ---------------------------------------------------------------------------
_LEFT_PANEL = """
<div class="auth-panel-left">
  <div class="auth-brand-mark">
    <!-- Fork & knife icon -->
    <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="#fff"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2"/>
      <path d="M7 2v20"/>
      <path d="M21 15V2a5 5 0 0 0-5 5v6h3.5a1.5 1.5 0 0 1 1.5 1.5v.5"/>
      <path d="M18 22v-7"/>
    </svg>
  </div>

  <h2 class="auth-headline">Beat inflation.<br>Eat brilliantly.</h2>
  <p class="auth-subline">AI-powered recipes built around today's cheapest UK supermarket deals — slash your food bill without compromising on taste.</p>

  <ul class="auth-features">
    <li class="auth-feat-item">
      <div class="auth-feat-icon">
        <!-- Tag / deals icon -->
        <svg viewBox="0 0 24 24"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>
      </div>
      <div class="auth-feat-text">
        <span class="auth-feat-title">Live UK Supermarket Deals</span>
        <span class="auth-feat-desc">Discounted products from Tesco, Asda, Aldi &amp; more</span>
      </div>
    </li>
    <li class="auth-feat-item">
      <div class="auth-feat-icon">
        <!-- Chef hat / sparkle icon -->
        <svg viewBox="0 0 24 24"><path d="M6 13.87A4 4 0 0 1 7.41 6a5.11 5.11 0 0 1 1.05-1.54 5 5 0 0 1 7.08 0A5.11 5.11 0 0 1 16.59 6 4 4 0 0 1 18 13.87V21H6Z"/><line x1="6" y1="17" x2="18" y2="17"/></svg>
      </div>
      <div class="auth-feat-text">
        <span class="auth-feat-title">AI Recipe Generation</span>
        <span class="auth-feat-desc">Personalised meals with step-by-step instructions</span>
      </div>
    </li>
    <li class="auth-feat-item">
      <div class="auth-feat-icon">
        <!-- Pound / budget icon -->
        <svg viewBox="0 0 24 24"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
      </div>
      <div class="auth-feat-text">
        <span class="auth-feat-title">Budget Optimisation</span>
        <span class="auth-feat-desc">Tell us your weekly spend — we find what fits it</span>
      </div>
    </li>
    <li class="auth-feat-item">
      <div class="auth-feat-icon">
        <!-- Device-lock / secure per-device icon -->
        <svg viewBox="0 0 24 24"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>
      </div>
      <div class="auth-feat-text">
        <span class="auth-feat-title">Per-Device Login</span>
        <span class="auth-feat-desc">Each device signs in separately — your sessions stay private</span>
      </div>
    </li>
  </ul>

  <div class="auth-divider-line"></div>
  <div class="auth-trust">
    <div class="auth-trust-dot"></div>
    Secured with Supabase &nbsp;&middot;&nbsp; Data stored in the EU
  </div>
</div>
"""

_LOCK_SVG = """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
  <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
  <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
</svg>"""


# ---------------------------------------------------------------------------
# Password strength
# ---------------------------------------------------------------------------
def _pw_strength(pw: str) -> tuple[int, str, str]:
    score = 0
    if len(pw) >= 8:
        score += 1
    if re.search(r"[A-Z]", pw):
        score += 1
    if re.search(r"[0-9]", pw):
        score += 1
    if re.search(r"[^A-Za-z0-9]", pw):
        score += 1
    labels  = ["", "Weak",    "Fair",    "Good",    "Strong" ]
    colours = ["", "#dc2626", "#f97316", "#0d9488", "#1d4ed8"]
    return score, labels[score] if score else "", colours[score] if score else ""


# ---------------------------------------------------------------------------
# Password eye toggle (CSS only — Streamlit native button is used)
# ---------------------------------------------------------------------------
def _inject_pw_toggle() -> None:
    """Style the native Streamlit password show/hide button."""
    import streamlit.components.v1 as _cv1  # type: ignore
    _cv1.html("""<script>
(function(){
  var DUMMY='placeholder_so_this_block_still_closes_properly';
  // Native Streamlit password toggle is used instead of custom JS.
  // Just ensure cursor is pointer on the built-in button.
  function styleNative(){
    var d=window.parent.document;
    d.querySelectorAll('[data-testid="InputInstructions"], [data-baseweb="input"] button, .stTextInput button').forEach(function(b){
      b.style.cursor='pointer';
      b.style.opacity='1';
    });
  }
  setTimeout(styleNative,200); setTimeout(styleNative,700); setTimeout(styleNative,1800);
  new MutationObserver(styleNative).observe(document.body,{childList:true,subtree:true});
" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
  var CE='<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
  function add(inp){
    if(inp.dataset.et) return;
    inp.dataset.et='1';
    var w=inp.parentElement; if(!w) return;
    w.style.position='relative';
    inp.style.paddingRight='40px';
    var b=document.createElement('button');
    b.type='button'; b.innerHTML=OE; b.title='Show password';
    b.style.cssText='position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer !important;padding:4px;border-radius:6px;color:#9ca3af;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s;z-index:20;line-height:1';
    b.addEventListener('mouseenter',function(){this.style.background='rgba(13,148,136,.12)';this.style.color='#0d9488';});
    b.addEventListener('mouseleave',function(){this.style.background='none';this.style.color=inp.type==='password'?'#9ca3af':'#0d9488';});
    b.addEventListener('click',function(e){
      e.preventDefault();e.stopPropagation();
      var shown=inp.type==='text';
      inp.type=shown?'password':'text';
      this.innerHTML=shown?OE:CE;
      this.title=shown?'Show password':'Hide password';
      this.style.color=shown?'#9ca3af':'#0d9488';
      inp.focus();
    });
    w.appendChild(b);
  }
  function scan(){
    var d=window.parent.document;
    d.querySelectorAll('input[type="password"]:not([data-et])').forEach(add);
  }
  setTimeout(scan,150); setTimeout(scan,600); setTimeout(scan,1500);
  new MutationObserver(scan).observe(window.parent.document.body,{childList:true,subtree:true});
})();
</script>""", height=0, scrolling=False)


# ---------------------------------------------------------------------------
# Login form
# ---------------------------------------------------------------------------
def _render_login() -> None:
    st.markdown(
        "<div class='auth-form-header'>"
        "<h2>Welcome back</h2>"
        "<p>Sign in to access your saved recipes and meal plans.</p>"
        "</div>",
        unsafe_allow_html=True,
    )

    if st.session_state.get("login_info"):
        st.markdown(
            f"<div class='a-alert a-alert-info'>{st.session_state.login_info}</div>",
            unsafe_allow_html=True,
        )
        st.session_state.pop("login_info", None)

    if st.session_state.get("login_error"):
        st.markdown(
            f"<div class='a-alert a-alert-error'>{st.session_state.login_error}</div>",
            unsafe_allow_html=True,
        )
        st.session_state.pop("login_error", None)

    with st.form("login_form", clear_on_submit=False):
        email    = st.text_input("Email address", key="login_email",    placeholder="you@example.com")
        password = st.text_input("Password",      type="password", key="login_password", placeholder="Enter your password")
        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        submitted = st.form_submit_button("Sign In")

    if submitted:
        err = _validate_email(email) or _validate_password(password)
        if err:
            st.session_state["login_error"] = err
            st.rerun()
        else:
            with st.spinner("Authenticating..."):
                ok, msg, user_info = _sign_in(email.strip().lower(), password)
            if ok:
                _rt = user_info.pop("_rt", "")
                user_info["_rt"] = _rt  # keep RT in session for rolling cookie renewal
                st.session_state["auth_user"] = user_info
                _write_session_cookie(user_info, _rt)
                st.toast(f"Welcome back, {user_info.get('full_name') or user_info['email']}")
                time.sleep(0.3)
                st.rerun()
            elif msg.startswith("rate_limit:"):
                secs = int(msg.split(":")[1])
                st.session_state["login_error"] = (
                    f"Too many attempts. Please wait {secs} second{'s' if secs != 1 else ''} before trying again."
                )
                st.rerun()
            else:
                st.session_state["login_error"] = msg
                st.rerun()

    st.markdown(
        "<div class='auth-sep'><div class='auth-sep-line'></div>"
        "<span class='auth-sep-text'>Don't have an account?</span>"
        "<div class='auth-sep-line'></div></div>",
        unsafe_allow_html=True,
    )
    if st.button("Create an account", key="goto_register"):
        st.session_state["auth_page"] = "register"
        st.session_state.pop("login_error", None)
        st.rerun()


# ---------------------------------------------------------------------------
# Register form
# ---------------------------------------------------------------------------
def _render_register() -> None:
    st.markdown(
        "<div class='auth-form-header'>"
        "<h2>Create your account</h2>"
        "<p>Start saving on your grocery bill with AI-powered meal plans.</p>"
        "</div>",
        unsafe_allow_html=True,
    )

    # Rate-limit cooldown banner
    cooldown_until = st.session_state.get("reg_cooldown_until", 0)
    now = _time.time()
    if cooldown_until > now:
        remaining = int(cooldown_until - now) + 1
        st.markdown(
            f"<div class='a-alert a-alert-rate'>"
            f"<strong>Request limit reached.</strong> For security, you can submit again in "
            f"<strong>{remaining} second{'s' if remaining != 1 else ''}</strong>. "
            f"Please wait before trying again."
            f"</div>",
            unsafe_allow_html=True,
        )
        _time.sleep(1)
        st.rerun()
        return

    if st.session_state.get("reg_error"):
        st.markdown(
            f"<div class='a-alert a-alert-error'>{st.session_state.reg_error}</div>",
            unsafe_allow_html=True,
        )
        st.session_state.pop("reg_error", None)
    if st.session_state.get("reg_success"):
        st.markdown(
            f"<div class='a-alert a-alert-success'>{st.session_state.reg_success}</div>",
            unsafe_allow_html=True,
        )
        st.session_state.pop("reg_success", None)

    with st.form("register_form", clear_on_submit=False):
        full_name  = st.text_input("Full name",        key="reg_name",       placeholder="Jane Smith")
        email      = st.text_input("Email address",    key="reg_email",      placeholder="you@example.com")
        password   = st.text_input("Password",         type="password", key="reg_password",   placeholder="Min. 8 characters")
        st.markdown(
            "<p style='font-size:0.75rem;color:#9ca3af;margin:2px 0 8px;line-height:1.5'>"
            "Use 8+ characters with uppercase, numbers, and symbols.</p>",
            unsafe_allow_html=True,
        )
        confirm_pw = st.text_input("Confirm password", type="password", key="reg_confirm_pw", placeholder="Repeat your password")
        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        submitted = st.form_submit_button("Create Account")

    if submitted:
        err = _validate_name(full_name) or _validate_email(email) or _validate_password(password)
        if err:
            st.session_state["reg_error"] = err
            st.rerun()
        elif password != confirm_pw:
            st.session_state["reg_error"] = "Passwords do not match."
            st.rerun()
        else:
            with st.spinner("Creating your account..."):
                ok, msg, user_info = _sign_up(email.strip().lower(), password, full_name.strip())
            if ok:
                if user_info:
                    _rt = user_info.pop("_rt", "")
                    user_info["_rt"] = _rt  # keep RT in session for rolling cookie renewal
                    st.session_state["auth_user"] = user_info
                    _write_session_cookie(user_info, _rt)
                    st.session_state["onboarding_needed"] = True
                    st.session_state.pop("reg_error", None)
                    st.session_state.pop("reg_success", None)
                    time.sleep(0.3)
                else:
                    st.session_state["auth_page"] = "login"
                    st.session_state["login_info"] = (
                        "<strong>Account created!</strong> A verification email has been sent to "
                        f"<strong>{email.strip().lower()}</strong>. "
                        "Please check your inbox (and spam folder) and click the confirmation link, "
                        "then sign in below."
                    )
                    st.session_state.pop("reg_error", None)
                    st.session_state.pop("reg_success", None)
                st.rerun()
            elif msg.startswith("rate_limit:"):
                secs = int(msg.split(":")[1])
                st.session_state["reg_cooldown_until"] = _time.time() + secs
                st.rerun()
            else:
                st.session_state["reg_error"] = msg
                st.rerun()

    st.markdown(
        "<div class='auth-sep'><div class='auth-sep-line'></div>"
        "<span class='auth-sep-text'>Already have an account?</span>"
        "<div class='auth-sep-line'></div></div>",
        unsafe_allow_html=True,
    )
    if st.button("Back to Sign In", key="goto_login"):
        st.session_state["auth_page"] = "login"
        st.session_state.pop("reg_error", None)
        st.session_state.pop("reg_success", None)
        st.rerun()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def render_login_dialog_content() -> None:
    """Render login/register form suitable for use inside a Streamlit dialog.

    Uses inline error display (no rerun on validation errors) so the dialog
    stays open.  Sets st.session_state['_show_login_dialog'] = True before
    calling st.rerun() whenever the dialog must reopen (error, page-switch).
    On successful login it calls st.rerun() WITHOUT re-setting the flag so
    the dialog closes cleanly.
    """
    import time as _dlg_time

    page = st.session_state.get("auth_page", "login")

    if page == "login":
        st.markdown(
            "<div style='font-size:1.05rem;font-weight:700;color:#f1f5f9;margin-bottom:4px'>Welcome back</div>"
            "<div style='font-size:0.88rem;color:#94a3b8;margin-bottom:16px'>"
            "Sign in to continue with unlimited access.</div>",
            unsafe_allow_html=True,
        )
        if st.session_state.get("_dlg_msg"):
            _msg_type, _msg_text = st.session_state.pop("_dlg_msg")
            if _msg_type == "error":
                st.error(_msg_text)
            else:
                st.success(_msg_text)

        with st.form("dlg_login_form", clear_on_submit=False):
            _dlg_email = st.text_input("Email address", placeholder="you@example.com", key="dlg_email")
            _dlg_pw    = st.text_input("Password", type="password", placeholder="Your password", key="dlg_pw")
            st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
            _dlg_sub = st.form_submit_button("Sign In", type="primary", use_container_width=True)

        if _dlg_sub:
            _err = _validate_email(_dlg_email) or _validate_password(_dlg_pw)
            if _err:
                st.session_state["_dlg_msg"] = ("error", _err)
                st.session_state["_show_login_dialog"] = True
                st.rerun()
            else:
                with st.spinner("Signing in…"):
                    _ok, _msg, _uinfo = _sign_in(_dlg_email.strip().lower(), _dlg_pw)
                if _ok:
                    _rt = _uinfo.pop("_rt", "")
                    _uinfo["_rt"] = _rt
                    st.session_state["auth_user"] = _uinfo
                    _write_session_cookie(_uinfo, _rt)
                    st.toast(f"Welcome back, {_uinfo.get('full_name') or _uinfo['email']}!")
                    _dlg_time.sleep(0.2)
                    st.rerun()  # closes dialog; page refreshes as logged-in user
                elif _msg.startswith("rate_limit:"):
                    _secs = int(_msg.split(":")[1])
                    st.session_state["_dlg_msg"] = ("error", f"Too many attempts — wait {_secs}s before retrying.")
                    st.session_state["_show_login_dialog"] = True
                    st.rerun()
                else:
                    st.session_state["_dlg_msg"] = ("error", _msg)
                    st.session_state["_show_login_dialog"] = True
                    st.rerun()

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
        st.caption("Don't have an account?")
        if st.button("Create a free account →", key="dlg_to_register", use_container_width=True):
            st.session_state["auth_page"] = "register"
            st.session_state["_show_login_dialog"] = True
            st.rerun()

    else:  # register
        st.markdown(
            "<div style='font-size:1.05rem;font-weight:700;color:#f1f5f9;margin-bottom:4px'>Create your account</div>"
            "<div style='font-size:0.88rem;color:#94a3b8;margin-bottom:16px'>"
            "Free forever — no credit card required.</div>",
            unsafe_allow_html=True,
        )
        if st.session_state.get("_dlg_msg"):
            _msg_type, _msg_text = st.session_state.pop("_dlg_msg")
            if _msg_type == "error":
                st.error(_msg_text)
            else:
                st.success(_msg_text)

        with st.form("dlg_register_form", clear_on_submit=False):
            _dlg_name  = st.text_input("Full name",     placeholder="Jane Smith",           key="dlg_reg_name")
            _dlg_email = st.text_input("Email address", placeholder="you@example.com",       key="dlg_reg_email")
            _dlg_pw    = st.text_input("Password",    type="password", placeholder="Min. 8 characters", key="dlg_reg_pw")
            _dlg_pw2   = st.text_input("Confirm password", type="password", placeholder="Repeat password",   key="dlg_reg_pw2")
            st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
            _dlg_sub = st.form_submit_button("Create Account", type="primary", use_container_width=True)

        if _dlg_sub:
            _err = _validate_name(_dlg_name) or _validate_email(_dlg_email) or _validate_password(_dlg_pw)
            if _err:
                st.session_state["_dlg_msg"] = ("error", _err)
                st.session_state["_show_login_dialog"] = True
                st.rerun()
            elif _dlg_pw != _dlg_pw2:
                st.session_state["_dlg_msg"] = ("error", "Passwords do not match.")
                st.session_state["_show_login_dialog"] = True
                st.rerun()
            else:
                with st.spinner("Creating your account…"):
                    _ok, _msg, _uinfo = _sign_up(_dlg_email.strip().lower(), _dlg_pw, _dlg_name.strip())
                if _ok:
                    if _uinfo:
                        _rt = _uinfo.pop("_rt", "")
                        _uinfo["_rt"] = _rt
                        st.session_state["auth_user"] = _uinfo
                        _write_session_cookie(_uinfo, _rt)
                        st.session_state["onboarding_needed"] = True
                        st.toast("Account created! Welcome!")
                        _dlg_time.sleep(0.2)
                        st.rerun()
                    else:
                        st.session_state["_dlg_msg"] = (
                            "success",
                            f"Account created! Check {_dlg_email.strip().lower()} for a verification link, then sign in.",
                        )
                        st.session_state["auth_page"] = "login"
                        st.session_state["_show_login_dialog"] = True
                        st.rerun()
                elif _msg.startswith("rate_limit:"):
                    _secs = int(_msg.split(":")[1])
                    st.session_state["_dlg_msg"] = ("error", f"Rate limited — wait {_secs}s.")
                    st.session_state["_show_login_dialog"] = True
                    st.rerun()
                else:
                    st.session_state["_dlg_msg"] = ("error", _msg)
                    st.session_state["_show_login_dialog"] = True
                    st.rerun()

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
        if st.button("← Back to Sign In", key="dlg_to_login", use_container_width=True):
            st.session_state["auth_page"] = "login"
            st.session_state["_show_login_dialog"] = True
            st.rerun()


def render_auth_page() -> bool:
    """Restore session from cookie if available. Guests are allowed through.

    Behaviour on refresh:
    - Authenticated user  → cookie refreshed (rolling 30-day window), returns True
    - Valid cookie        → session restored, returns True
    - No cookie / guest  → returns True (guest access allowed)
    """
    if st.session_state.get("auth_user"):
        # Re-write cookie on every authenticated render → keeps 30-day window rolling
        _u = st.session_state["auth_user"]
        _stored_rt = _u.get("_rt", "")
        if _stored_rt:
            _write_session_cookie(_u, _stored_rt)
        return True

    # Try to restore session from browser cookie (survives page refresh)
    if _restore_session_from_cookie():
        return True

    # Attempt to ensure the user_profiles table exists (best effort)
    _ensure_user_profiles_table()

    # Guests are allowed through — auth is now optional
    return True

