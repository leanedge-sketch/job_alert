import argparse
import json
import os
import time
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

from google_backfill import fetch_google_jobs, matches_keywords

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SEEN_JOBS_FILE = Path(__file__).parent / "seen_jobs.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def load_feed_urls() -> list[str]:
    raw = os.getenv("RSS_FEED_URLS") or os.getenv("RSS_FEED_URL", "")
    return [url.strip() for url in raw.split(",") if url.strip()]


def load_seen_jobs() -> set[str]:
    if not SEEN_JOBS_FILE.exists():
        return set()
    with open(SEEN_JOBS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return set(data)


def save_seen_jobs(seen: set[str]) -> None:
    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)


def fetch_feed(feed_url: str):
    response = requests.get(
        feed_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml",
        },
        timeout=30,
    )
    response.raise_for_status()
    return feedparser.parse(response.content)


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    response.raise_for_status()


def format_job_message(title: str, link: str, source: str) -> str:
    return f"New job match ({source})\n\n{title}\n{link}"


def source_label(feed_url: str, feed_title: str) -> str:
    combined = f"{feed_title} {feed_url}".lower()
    if "indeed" in combined:
        return "Indeed"
    if "bayt" in combined:
        return "Bayt"
    if "naukrigulf" in combined or "naukri" in combined:
        return "Naukrigulf"
    if "linkedin" in combined:
        return "LinkedIn"
    return "Google Alert"


def process_job(title: str, link: str, source: str, seen: set[str]) -> bool:
    if not title or not link:
        return False
    if link in seen:
        return False
    if not matches_keywords(title):
        return False

    send_telegram_message(format_job_message(title, link, source))
    seen.add(link)
    print(f"Sent alert ({source}): {title}")
    time.sleep(0.4)  # avoid Telegram flood limits
    return True


def run_rss_feeds(seen: set[str]) -> int:
    feed_urls = load_feed_urls()
    if not feed_urls:
        print("No RSS_FEED_URLS configured; skipping RSS check.")
        return 0

    new_alerts = 0
    for feed_url in feed_urls:
        try:
            feed = fetch_feed(feed_url)
        except requests.RequestException as exc:
            print(f"Failed to fetch feed {feed_url}: {exc}")
            continue

        if feed.bozo and not feed.entries:
            print(f"Failed to parse feed {feed_url}: {feed.bozo_exception}")
            continue

        source = source_label(feed_url, feed.feed.get("title", ""))
        print(
            f"[{source}] Fetched {len(feed.entries)} "
            f"entr{'y' if len(feed.entries) == 1 else 'ies'}."
        )

        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if process_job(title, link, source, seen):
                new_alerts += 1

    return new_alerts


def run_past_week_backfill(seen: set[str], days: int) -> int:
    jobs = fetch_google_jobs(days=days)
    new_alerts = 0
    for job in jobs:
        if process_job(job.title, job.link, job.source, seen):
            new_alerts += 1
    return new_alerts


def main() -> None:
    parser = argparse.ArgumentParser(description="Job alert bot")
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="Also search Google for matching jobs from the past N days "
        "(use 7 for one week). Direct site scraping is blocked by Cloudflare.",
    )
    args = parser.parse_args()

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. Set them in your .env file."
        )

    seen = load_seen_jobs()
    new_alerts = run_rss_feeds(seen)

    if args.backfill_days > 0:
        new_alerts += run_past_week_backfill(seen, days=args.backfill_days)

    save_seen_jobs(seen)
    print(f"Done. Sent {new_alerts} new alert(s).")


if __name__ == "__main__":
    main()
