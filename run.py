"""run.py — Single entry point for the Inflation-Busting Recipe Chatbot.

Usage:
    python run.py

What this does automatically:
  1. Checks .env keys (SUPABASE_URL, SUPABASE_ANON_KEY, GROQ_API_KEY)
  2. Seeds Supabase with real UK products from the Open Food Facts API if empty
  3. Launches the Streamlit app
"""

import logging
import os
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def check_env() -> bool:
    """Verify required .env keys are present."""
    from dotenv import load_dotenv
    load_dotenv(override=True)

    required = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "GROQ_API_KEY")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.error("Missing keys in .env: %s", ", ".join(missing))
        log.error("Add them to your .env file and re-run.")
        return False
    log.info("All .env keys found -- OK.")
    return True


def seed_database() -> None:
    """Fetch real UK products from Open Food Facts and load into Supabase if empty.

    Skipped automatically if data already exists, so it is safe to call on
    every startup.
    """
    try:
        from config import supabase
        if supabase is None:
            log.warning(
                "Supabase not connected -- skipping seed. "
                "Check SUPABASE_URL / SUPABASE_ANON_KEY in .env."
            )
            return

        # Check if active offers already exist
        result = (
            supabase.table("offers")
            .select("id", count="exact")
            .eq("is_discount", True)
            .execute()
        )
        existing = result.count or 0
        if existing > 0:
            log.info("Supabase already has %d active offers -- skipping seed.", existing)
            return

        log.info("Seeding Supabase with real UK products from Open Food Facts...")
        from data_ingestion.fetch_open_food_facts import fetch_off_discounts
        from data_ingestion.load_to_supabase import upsert_offers

        discounts = fetch_off_discounts(products_per_category=10)
        log.info("Fetched %d real UK products from Open Food Facts.", len(discounts))
        count = upsert_offers(discounts)
        log.info("Seed complete -- %d offers loaded into Supabase.", count)

    except Exception as exc:
        log.warning("Seed step skipped (app will still launch): %s", exc)


def launch_app() -> None:
    """Start the Streamlit chatbot app."""
    log.info("Starting Streamlit app on http://localhost:8501 ...")
    cmd = [
        sys.executable, "-m", "streamlit", "run", "app.py",
        "--server.headless", "true",
        "--server.port", "8501",
    ]
    subprocess.run(cmd, check=False)


if __name__ == "__main__":
    if not check_env():
        sys.exit(1)
    seed_database()
    launch_app()
