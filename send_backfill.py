"""Send curated past-week jobs from backfill_week.json to Telegram."""

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

    jobs = json.loads(BACKFILL_FILE.read_text(encoding="utf-8"))
    seen = bot.load_seen_jobs()
    sent = 0

    bot.send_telegram_message(
        f"Past-week backfill starting: {len(jobs)} candidate job(s) from "
        "Indeed / LinkedIn / Naukrigulf / Bayt."
    )
    time.sleep(0.5)

    for job in jobs:
        title = job["title"].strip()
        link = job["link"].strip()
        source = job["source"].strip()
        if bot.process_job(title, link, source, seen):
            sent += 1

    bot.save_seen_jobs(seen)
    print(f"Done. Sent {sent} new alert(s).")


if __name__ == "__main__":
    main()
