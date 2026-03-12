import os
import base64
import json

from dotenv import load_dotenv
from langchain_groq import ChatGroq

load_dotenv(override=True)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


def _jwt_role(key: str) -> str:
    """Decode the role claim from a Supabase JWT without verifying the signature."""
    try:
        payload = key.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)  # pad to valid base64
        return json.loads(base64.b64decode(payload)).get("role", "")
    except Exception:
        return ""


# Connect to Supabase (anon client — used for normal operations)
supabase = None
try:
    from supabase import create_client, Client
    if SUPABASE_URL and SUPABASE_KEY:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"[WARNING] Supabase connection failed: {e}")
    supabase = None

# Service-role client — bypasses RLS entirely.
# Only created when the key actually carries the 'service_role' role claim.
# If SUPABASE_SERVICE_KEY is blank or mistakenly set to the anon key, this stays None
# and the app falls back to using the user's JWT-authenticated anon client.
supabase_admin = None
_svc_key_role = _jwt_role(SUPABASE_SERVICE_KEY)
if _svc_key_role != "service_role":
    if SUPABASE_SERVICE_KEY:
        print(
            "[WARNING] SUPABASE_SERVICE_KEY does not carry the 'service_role' claim "
            f"(role='{_svc_key_role}'). "
            "Get your service-role key from: Supabase Dashboard → Project Settings → API → "
            "'service_role' (secret). supabase_admin will be None until corrected."
        )
else:
    try:
        from supabase import create_client as _cc
        if SUPABASE_URL and SUPABASE_SERVICE_KEY:
            supabase_admin = _cc(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    except Exception:
        supabase_admin = None


def get_llm():
    """Always read the API key fresh from .env so key changes take effect immediately."""
    load_dotenv(override=True)
    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("Groq API key missing: set GROQ_API_KEY in .env")
    return ChatGroq(
        model="llama-3.1-8b-instant",
        temperature=0.7,
        groq_api_key=key,
    )


