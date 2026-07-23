"""Past-week job discovery via Google Custom Search JSON API.

Direct scraping of Indeed/Bayt/Naukrigulf/LinkedIn is blocked by Cloudflare.
Automated Google HTML scraping hits CAPTCHA. The supported approach is Google
Programmable Search (Custom Search JSON API).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

import requests

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

SOURCES = {
    "Indeed": "ae.indeed.com/viewjob",
    "Bayt": "bayt.com",
    "Naukrigulf": "naukrigulf.com",
    "LinkedIn": "linkedin.com/jobs/view",
}


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


def matches_keywords(title: str, description: str = "") -> bool:
    def contains_phrase(text: str, phrase: str) -> bool:
        pattern = r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)"
        return re.search(pattern, text.lower()) is not None

    title_lower = title.lower()
    combined = f"{title} {description}".lower()

    if any(contains_phrase(combined, blocked) for blocked in SEARCH_CONFIG["blocked_words"]):
        return False
    if not any(job_title.lower() in title_lower for job_title in SEARCH_CONFIG["job_titles"]):
        return False
    locations = SEARCH_CONFIG["locations"]
    if locations and not any(loc.lower() in combined for loc in locations):
        return False
    return True


def _keyword_query() -> str:
    return "(" + " OR ".join(f'"{kw}"' for kw in SEARCH_CONFIG["job_titles"]) + ")"


def _location_query() -> str:
    locations = SEARCH_CONFIG["locations"] or ["Dubai", "UAE"]
    return "(" + " OR ".join(f'"{loc}"' if " " in loc else loc for loc in locations) + ")"


def fetch_google_jobs(days: int = 7) -> list[Job]:
    api_key = os.getenv("GOOGLE_API_KEY")
    cse_id = os.getenv("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        raise SystemExit(
            "Past-week backfill needs GOOGLE_API_KEY and GOOGLE_CSE_ID in .env.\n"
            "Create a free Programmable Search Engine at "
            "https://programmablesearchengine.google.com/ "
            "and enable the Custom Search JSON API.\n"
            "Meanwhile, run: python send_backfill.py"
        )

    date_restrict = f"d{max(1, days)}"
    jobs: list[Job] = []
    seen: set[str] = set()

    for source, site in SOURCES.items():
        query = f"site:{site} {_keyword_query()} {_location_query()}"
        print(f"[{source}] Searching Google CSE (past {days} days)...")
        params = {
            "key": api_key,
            "cx": cse_id,
            "q": query,
            "num": 10,
            "dateRestrict": date_restrict,
        }
        try:
            response = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            print(f"[{source}] CSE request failed: {exc}")
            continue

        count = 0
        for item in data.get("items", []):
            title = (item.get("title") or "").strip()
            link = (item.get("link") or "").strip()
            if not title or not link or link in seen:
                continue
            if not matches_keywords(title):
                continue
            seen.add(link)
            jobs.append(Job(title=title, link=link, source=source))
            count += 1
        print(f"[{source}] Matched {count} job(s).")

    return jobs


def build_manual_search_urls(days: int = 7) -> dict[str, str]:
    """Useful Google search links if CSE is not configured."""
    tbs = "qdr:w" if days >= 7 else "qdr:d"
    urls = {}
    for source, site in SOURCES.items():
        q = f"site:{site} {_keyword_query()} {_location_query()}"
        urls[source] = (
            f"https://www.google.com/search?q={quote_plus(q)}&tbs={tbs}&num=20&hl=en"
        )
    return urls
