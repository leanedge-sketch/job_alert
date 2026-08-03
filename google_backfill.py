"""Free past-week job helpers (no paid APIs).

Google Custom Search JSON API is closed to new Cloud customers, so this module
does not call CSE. Use:

  1. Google Alerts RSS (primary) via main.py
  2. Curated backfill_week.json + python send_backfill.py
  3. Manual Google search links printed by --backfill-days
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

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
        "Desktop Support",
        "Technical Support",
        "Tech Support",
        "Application Support",
        "L1 Support",
        "L2 Support",
        "NOC",
        "SysAdmin",
        "Sys Admin",
        "IT Helpdesk",
        "IT Help Desk",
        "Infrastructure Support",
    ],
    "locations": [
        "UAE",
        "Dubai",
        "Abu Dhabi",
        "United Arab Emirates",
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

SOURCES = {
    "Indeed": "ae.indeed.com/viewjob",
    "Bayt": "bayt.com",
    "Naukrigulf": "naukrigulf.com",
    "LinkedIn": "linkedin.com/jobs/view",
}

UAE_LOCATION_LINK_MARKERS = (
    "ae.indeed.com",
    "indeed.ae",
    "bayt.com/en/uae",
    "bayt.com/en/dubai",
    "bayt.com/en/abu-dhabi",
    "naukrigulf.com",
    "/jobs-in-dubai",
    "/jobs-in-abu-dhabi",
    "/jobs-in-uae",
)


def load_search_config() -> dict:
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


@dataclass(frozen=True)
class Job:
    title: str
    link: str
    source: str


def matches_keywords(title: str, description: str = "", link: str = "") -> bool:
    def contains_phrase(text: str, phrase: str) -> bool:
        pattern = r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)"
        return re.search(pattern, text.lower()) is not None

    title_lower = title.lower()
    combined = f"{title} {description} {link}".lower()

    if any(contains_phrase(combined, blocked) for blocked in SEARCH_CONFIG["blocked_words"]):
        return False
    if not any(job_title.lower() in title_lower for job_title in SEARCH_CONFIG["job_titles"]):
        return False
    locations = SEARCH_CONFIG["locations"]
    if locations:
        text_match = any(loc.lower() in combined for loc in locations)
        link_match = any(marker in (link or "").lower() for marker in UAE_LOCATION_LINK_MARKERS)
        if not text_match and not link_match:
            return False
    return True


def _keyword_query() -> str:
    return "(" + " OR ".join(f'"{kw}"' for kw in SEARCH_CONFIG["job_titles"]) + ")"


def _location_query() -> str:
    locations = SEARCH_CONFIG["locations"] or ["Dubai", "UAE"]
    return "(" + " OR ".join(f'"{loc}"' if " " in loc else loc for loc in locations) + ")"


def build_manual_search_urls(days: int = 7) -> dict[str, str]:
    """Google web search links filtered to roughly the past day/week."""
    tbs = "qdr:w" if days >= 7 else "qdr:d"
    urls = {}
    for source, site in SOURCES.items():
        q = f"site:{site} {_keyword_query()} {_location_query()}"
        urls[source] = (
            f"https://www.google.com/search?q={quote_plus(q)}&tbs={tbs}&num=20&hl=en"
        )
    return urls


def fetch_google_jobs(days: int = 7) -> list[Job]:
    """Free-tier stub: print manual search links; return no auto-fetched jobs.

    Google Custom Search JSON API is closed to new customers (403 even when the
    API shows as Enabled). Automated CSE backfill is intentionally disabled.
    """
    print(
        "Automated Google CSE backfill is disabled (free tier).\n"
        "Custom Search JSON API is closed to new Google Cloud customers.\n"
        "\n"
        "Free options:\n"
        "  1. Primary: keep Google Alerts RSS feeds populated "
        "(Delivery = RSS, As-it-happens).\n"
        "  2. Paste matching jobs into backfill_week.json, then run:\n"
        "       python send_backfill.py\n"
        f"\nManual Google search links (past ~{max(1, days)} day(s)):"
    )
    for source, url in build_manual_search_urls(days).items():
        print(f"  [{source}] {url}")
    return []
