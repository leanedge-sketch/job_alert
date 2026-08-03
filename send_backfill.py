"""Send curated past-week jobs from backfill_week.json through the normal pipeline.

Free-tier alternative to Google CSE: paste jobs you find via Google Alerts or
manual search into backfill_week.json, then run this script.
"""

import json
import time
from pathlib import Path

from dotenv import load_dotenv

import main as bot

load_dotenv()

BACKFILL_FILE = Path(__file__).parent / "backfill_week.json"


def main() -> None:
    if not bot.TELEGRAM_BOT_TOKEN or not bot.TELEGRAM_CHAT_ID:
        raise SystemExit("Missing Telegram credentials in .env")

    active_cvs = bot.load_active_cvs()
    packs = bot.process_telegram_pack_callbacks(active_cvs=active_cvs)
    if packs:
        print(f"Processed {packs} on-demand pack request(s).")

    jobs = json.loads(BACKFILL_FILE.read_text(encoding="utf-8"))
    seen = bot.load_seen_jobs()
    sent = 0

    bot.send_telegram_message(
        f"Past-week backfill starting: {len(jobs)} candidate job(s) from "
        "Indeed / LinkedIn / Naukrigulf / Bayt."
    )
    time.sleep(0.5)

    for job in jobs:
        if not bot.gemini_budget_available(reserve=bot.GEMINI_PACK_RESERVE):
            print("Gemini budget spent; remaining backfill jobs stay queued.")
            break
        title = job["title"].strip()
        link = job["link"].strip()
        source = job["source"].strip()
        if bot.process_job(
            title,
            link,
            source,
            seen,
            description=job.get("location", ""),
            active_cvs=active_cvs,
        ):
            sent += 1

    bot.save_seen_jobs(seen)
    print(f"Done. Sent {sent} new alert(s).")


if __name__ == "__main__":
    main()
