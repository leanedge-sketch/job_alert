import argparse
import json
import os
import re
import time
import traceback
from datetime import date
from html import unescape
from pathlib import Path

import feedparser
import google.generativeai as genai
import requests
from dotenv import load_dotenv
from notion_client import Client

from google_backfill import fetch_google_jobs

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NOTION_API_TOKEN = os.getenv("NOTION_API_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

SEEN_JOBS_FILE = Path(__file__).parent / "seen_jobs.json"
CONFIG_FILE = Path(__file__).parent / "config.json"

DEFAULT_SEARCH_CONFIG = {
    "job_titles": [
        "Support Specialist",
        "System Administrator",
        "Systems Administrator",
        "IT Support",
        "Helpdesk",
        "Help Desk",
        "Service Desk",
    ],
    "locations": [
        "UAE",
        "Dubai",
        "Abu Dhabi",
        "United Arab Emirates",
        "Retail",
    ],
    "blocked_words": [
        "internship",
        "intern",
        "unpaid",
        "director",
        "vice president",
        "vp of",
    ],
}


def load_search_config() -> dict:
    """Load search parameters from config.json, with safe hardcoded fallbacks."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        config = {
            "job_titles": list(data.get("job_titles") or []),
            "locations": list(data.get("locations") or []),
            "blocked_words": list(data.get("blocked_words") or []),
        }
        if not config["job_titles"]:
            raise ValueError("job_titles must be a non-empty array")
        return config
    except Exception as exc:
        print(
            f"Warning: could not load {CONFIG_FILE.name} ({exc}). "
            "Falling back to default search arrays."
        )
        return {
            "job_titles": list(DEFAULT_SEARCH_CONFIG["job_titles"]),
            "locations": list(DEFAULT_SEARCH_CONFIG["locations"]),
            "blocked_words": list(DEFAULT_SEARCH_CONFIG["blocked_words"]),
        }


SEARCH_CONFIG = load_search_config()

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


def html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_telegram_message(
    text: str,
    reply_markup: dict | None = None,
    *,
    disable_notification: bool = False,
    disable_web_page_preview: bool = True,
) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_web_page_preview,
        "disable_notification": disable_notification,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()


def parse_job_details(title: str, description: str = "") -> dict[str, str]:
    """Extract location and salary hints from title/description with safe defaults."""
    location = "Not specified / Check link"
    salary = "Not specified"
    try:
        combined = f"{title} {description}"
        combined_lower = combined.lower()

        # Prefer configured locations, then a few common fallbacks.
        location_candidates = list(SEARCH_CONFIG.get("locations") or []) + [
            "remote",
            "hybrid",
            "sharjah",
            "ajman",
            "ras al khaimah",
        ]
        for candidate in location_candidates:
            if candidate and candidate.lower() in combined_lower:
                location = candidate
                break

        salary_match = re.search(
            r"(?:"
            r"(?:salary|pay|compensation)\s*[:\-]?\s*[^\n.]{0,40}"
            r"|(?:AED|USD|EUR|SAR|GBP)\s*\d[\d,]*(?:\s*[-–to]+\s*(?:AED|USD|EUR|SAR|GBP)?\s*\d[\d,]*)?"
            r"|\$\s*\d[\d,]*(?:\s*[-–to]+\s*\$?\s*\d[\d,]*)?"
            r"|\d[\d,]*\s*(?:AED|USD|EUR|SAR|GBP)(?:\s*/?\s*(?:mo|month|yr|year|annum))?"
            r"|(?:AED|USD)\s*/?\s*(?:mo|month)"
            r"|(?:per month|per annum|/month|/mo|/year)"
            r")",
            combined,
            flags=re.IGNORECASE,
        )
        if salary_match:
            salary = re.sub(r"\s+", " ", salary_match.group(0)).strip(" :-")
    except Exception as exc:
        print(f"Detail parse fallback ({exc})")

    return {"location": location, "salary": salary}


def format_job_message(
    title: str,
    source: str,
    link: str,
    location: str,
    salary: str,
) -> str:
    # Keep the raw URL as plain text at the end so Telegram builds a preview card.
    return (
        f"🚨 <b>New Job Match ({html_escape(source)})</b>\n\n"
        f"<b>Role:</b> {html_escape(title)}\n"
        f"<b>Location:</b> {html_escape(location)}\n"
        f"<b>Salary:</b> {html_escape(salary)}\n\n"
        f"<i>Click to view and apply directly:</i>\n"
        f"{link}"
    )


def entry_description(entry) -> str:
    """Pull the best available job description text from an RSS entry."""
    raw = ""
    if entry.get("summary"):
        raw = entry.get("summary", "")
    elif entry.get("description"):
        raw = entry.get("description", "")
    elif entry.get("content"):
        parts = entry.get("content") or []
        if parts:
            raw = parts[0].get("value", "")

    text = unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def analyze_job_with_gemini(title: str, description: str) -> dict | None:
    """Score a job with Gemini 1.5 Flash and extract top requirements.

    Returns a dict like {"score": int, "bullets": list[str]} or None on failure.
    """
    if not GEMINI_API_KEY:
        print("Gemini skipped: GEMINI_API_KEY not set.")
        return None

    job_text = f"Title: {title}\n\nDescription:\n{description or 'No description provided.'}"
    prompt = (
        "You are evaluating job postings for a candidate with 3+ years of Virtual "
        "Assistant experience, plus IT Support and System Administration skills.\n\n"
        "From the job posting below:\n"
        "1) Extract the top 3 technical requirements as short bullet strings.\n"
        "2) Score the job from 1 to 10 for how well it matches that candidate profile.\n\n"
        "Return ONLY valid JSON with this exact schema and no other text:\n"
        '{"score": <integer 1-10>, "bullets": ["...", "...", "..."]}\n\n'
        f"Job posting:\n{job_text}"
    )

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(
            "gemini-1.5-flash",
            generation_config={
                "response_mime_type": "application/json",
                "temperature": 0.2,
            },
        )
        response = model.generate_content(prompt)
        data = json.loads(response.text)
        score = int(data["score"])
        bullets = [str(b).strip() for b in data.get("bullets", []) if str(b).strip()]
        if not 1 <= score <= 10 or len(bullets) < 1:
            print(f"Gemini returned invalid payload: {data}")
            return None
        return {"score": score, "bullets": bullets[:3]}
    except Exception as exc:
        print(f"Gemini error: {exc}")
        return None


def add_job_to_notion(job_title: str, job_url: str, source: str) -> None:
    """Create a Notion database row for a newly found job.

    Required Notion database property names (exact spelling/type):
      - Position   -> Title
      - Link       -> URL
      - Date Found -> Date
      - Status     -> Select  (must include an option named exactly: To Apply)

    Env vars required:
      - NOTION_API_TOKEN
      - NOTION_DATABASE_ID

    `source` is accepted for logging/context; add a Notion property later if desired.
    """
    if not NOTION_API_TOKEN or not NOTION_DATABASE_ID:
        print("Notion skipped: NOTION_API_TOKEN or NOTION_DATABASE_ID not set.")
        return

    try:
        notion = Client(auth=NOTION_API_TOKEN)
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={
                # Must be named exactly "Position" and type Title
                "Position": {
                    "title": [{"text": {"content": job_title}}],
                },
                # Must be named exactly "Link" and type URL
                "Link": {
                    "url": job_url,
                },
                # Must be named exactly "Date Found" and type Date
                "Date Found": {
                    "date": {"start": date.today().isoformat()},
                },
                # Must be named exactly "Status" and type Select
                # Must include select option named exactly "To Apply"
                "Status": {
                    "select": {"name": "To Apply"},
                },
            },
        )
        print(f"Added to Notion ({source}): {job_title}")
    except Exception as exc:
        # Never block Telegram alerts if Notion fails or rate-limits.
        print(f"Notion error (continuing with Telegram): {exc}")


def _contains_phrase(text: str, phrase: str) -> bool:
    """Case-insensitive whole-phrase match (avoids 'intern' matching 'international')."""
    pattern = r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)"
    return re.search(pattern, text.lower()) is not None


def matches_search_filters(title: str, description: str = "") -> bool:
    """Apply job_titles / locations / blocked_words from SEARCH_CONFIG."""
    title_lower = title.lower()
    combined = f"{title} {description}".lower()

    if any(_contains_phrase(combined, blocked) for blocked in SEARCH_CONFIG["blocked_words"]):
        return False

    if not any(job_title.lower() in title_lower for job_title in SEARCH_CONFIG["job_titles"]):
        return False

    locations = SEARCH_CONFIG["locations"]
    if locations and not any(loc.lower() in combined for loc in locations):
        return False

    return True


def source_label(feed_url: str, feed_title: str = "", job_link: str = "") -> str:
    combined = f"{feed_title} {feed_url} {job_link}".lower()
    if "indeed" in combined:
        return "Indeed"
    if "bayt" in combined:
        return "Bayt"
    if "naukrigulf" in combined or "naukri" in combined:
        return "Naukrigulf"
    if "linkedin" in combined:
        return "LinkedIn"
    try:
        from urllib.parse import urlparse

        host = urlparse(job_link or feed_url).netloc.lower().removeprefix("www.")
        if host:
            return host
    except Exception:
        pass
    return "Google Alert"


def process_job(
    title: str,
    link: str,
    source: str,
    seen: set[str],
    description: str = "",
) -> bool:
    if not title or not link:
        return False
    if link in seen:
        return False
    if not matches_search_filters(title, description):
        return False

    analysis = analyze_job_with_gemini(title, description)
    if analysis is None:
        # Fail closed: do not alert if Gemini is unavailable/unparseable.
        return False

    score = analysis["score"]
    if score < 5:
        seen.add(link)  # avoid re-scoring the same low-fit job forever
        print(f"Skipped low score ({score}/10) ({source}): {title}")
        return False

    details = parse_job_details(title, description)
    display_source = source_label("", source, link)

    add_job_to_notion(title, link, display_source)
    send_telegram_message(
        format_job_message(
            title,
            display_source,
            link,
            details["location"],
            details["salary"],
        ),
        disable_web_page_preview=False,
    )
    seen.add(link)
    print(f"Sent alert ({display_source}, {score}/10): {title}")
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
            description = entry_description(entry)
            if process_job(title, link, source, seen, description=description):
                new_alerts += 1

    return new_alerts


def run_past_week_backfill(seen: set[str], days: int) -> int:
    jobs = fetch_google_jobs(days=days)
    new_alerts = 0
    for job in jobs:
        if process_job(job.title, job.link, job.source, seen, description=job.title):
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

    try:
        seen = load_seen_jobs()
        new_alerts = run_rss_feeds(seen)

        if args.backfill_days > 0:
            new_alerts += run_past_week_backfill(seen, days=args.backfill_days)

        save_seen_jobs(seen)
        print(f"Done. Sent {new_alerts} new alert(s).")
    except Exception as exc:
        tb = traceback.format_exc()
        print(tb)
        try:
            send_system_health_alert(exc, tb)
        except Exception as notify_exc:
            print(f"Failed to send system health alert: {notify_exc}")
        raise


def send_system_health_alert(exc: Exception, tb: str) -> None:
    """Silent Telegram notice for unattended GitHub Actions failures."""
    header = (
        "<b>System Health Alert</b>\n"
        f"<b>Error:</b> {html_escape(type(exc).__name__)}\n"
        f"<b>Message:</b> {html_escape(str(exc))}\n\n"
        "<b>Traceback:</b>\n<pre>"
    )
    footer = "</pre>"
    # Telegram message hard limit is 4096 characters.
    budget = 4096 - len(header) - len(footer)
    escaped_tb = html_escape(tb)
    if len(escaped_tb) > budget:
        cut = max(0, budget - len(html_escape("\n...[truncated]")))
        escaped_tb = escaped_tb[:cut] + html_escape("\n...[truncated]")
    send_telegram_message(f"{header}{escaped_tb}{footer}", disable_notification=True)


if __name__ == "__main__":
    main()
