"""Free, automated job discovery sources.

Google Alerts RSS is unreliable as a *sole* source: alerts frequently deliver
nothing for days, so a cron that only reads RSS looks healthy while producing
zero alerts.

Sources used here (no paid API keys):

1. LinkedIn public guest job-search endpoint (deterministic HTML fragment).
2. Google News RSS + URL decoder for Indeed / Bayt / Naukrigulf.

Those three boards block direct scraping (Cloudflare / connection resets).
Google News still indexes their individual job pages, and
`googlenewsdecoder` recovers the real job URL from each RSS item.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus, urlencode

import feedparser
import requests

CONFIG_FILE = Path(__file__).parent / "config.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

LINKEDIN_GUEST_SEARCH = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)

# Requests per keyword group; LinkedIn throttles aggressively above this.
MAX_KEYWORD_GROUPS = 4
TITLES_PER_GROUP = 4

# Google News decode is rate-sensitive; keep per-board caps small.
GOOGLE_NEWS_DECODE_INTERVAL = 0.2
GOOGLE_NEWS_MAX_ENTRIES = 20
GOOGLE_NEWS_MAX_QUERIES = 3

UAE_LOCATION_HINTS = (
    "uae",
    "u.a.e",
    "united arab emirates",
    "dubai",
    "abu dhabi",
    "sharjah",
    "ajman",
    "ras al khaimah",
    "fujairah",
    "umm al quwain",
)

NON_UAE_HINTS = (
    "saudi",
    "riyadh",
    "jeddah",
    "qatar",
    "doha",
    "bahrain",
    "oman",
    "kuwait",
    "jordan",
    "egypt",
    "cairo",
    "india",
    "pakistan",
    "algeria",
    "tunisia",
    "morocco",
)


@dataclass(frozen=True)
class Job:
    title: str
    link: str
    source: str
    location: str = ""


def _strip_html(raw: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", raw or ""))).strip()


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "job_titles": [
                str(t) for t in (data.get("job_titles") or []) if str(t).strip()
            ],
            "locations": [
                str(loc) for loc in (data.get("locations") or []) if str(loc).strip()
            ],
        }
    except Exception as exc:
        print(f"job_sources: could not read config.json ({exc}); using defaults.")
        return {
            "job_titles": ["IT Support", "Help Desk", "System Administrator"],
            "locations": ["United Arab Emirates"],
        }


def _keyword_groups(job_titles: list[str]) -> list[str]:
    """Pack config titles into a few quoted OR queries to limit request count."""
    groups: list[str] = []
    for start in range(0, len(job_titles), TITLES_PER_GROUP):
        chunk = job_titles[start : start + TITLES_PER_GROUP]
        groups.append(" OR ".join(f'"{title}"' for title in chunk))
        if len(groups) >= MAX_KEYWORD_GROUPS:
            break
    return groups


def _search_locations(locations: list[str]) -> list[str]:
    """Prefer one country-wide query plus named cities, de-duplicated."""
    normalized: list[str] = []
    country_aliases = {"uae", "u.a.e.", "united arab emirates"}
    has_country = any(loc.strip().lower() in country_aliases for loc in locations)
    if has_country:
        normalized.append("United Arab Emirates")
    for loc in locations:
        clean = loc.strip()
        if not clean or clean.lower() in country_aliases:
            continue
        normalized.append(clean)
    return normalized or ["United Arab Emirates"]


def _title_or_query(job_titles: list[str], limit: int = 8) -> str:
    titles = [t.strip() for t in job_titles if t.strip()][:limit]
    if not titles:
        return '"IT Support"'
    return "(" + " OR ".join(f'"{t}"' for t in titles) + ")"


# --- LinkedIn -----------------------------------------------------------------


def _parse_linkedin_cards(html: str) -> list[Job]:
    """Parse job cards from the guest search fragment."""
    jobs: list[Job] = []
    for block in re.split(r"<li[\s>]", html or ""):
        job_id = re.search(r"urn:li:jobPosting:(\d+)", block)
        if not job_id:
            continue
        title_match = re.search(
            r"base-search-card__title[^>]*>(.*?)</h3>", block, re.S
        )
        title = _strip_html(title_match.group(1)) if title_match else ""
        if not title:
            continue
        company_match = re.search(
            r"base-search-card__subtitle[^>]*>(.*?)</h4>", block, re.S
        )
        location_match = re.search(
            r"job-search-card__location[^>]*>(.*?)</span>", block, re.S
        )
        company = _strip_html(company_match.group(1)) if company_match else ""
        location = _strip_html(location_match.group(1)) if location_match else ""
        jobs.append(
            Job(
                title=title,
                link=f"https://www.linkedin.com/jobs/view/{job_id.group(1)}/",
                source="LinkedIn",
                location=" - ".join(part for part in (company, location) if part),
            )
        )
    return jobs


def _fetch_linkedin_page(params: dict, attempts: int = 3) -> str:
    url = f"{LINKEDIN_GUEST_SEARCH}?{urlencode(params)}"
    for attempt in range(attempts):
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=20,
            )
            if response.status_code == 429:
                raise requests.HTTPError("429 rate limited")
            response.raise_for_status()
            return response.text
        except Exception as exc:
            if attempt >= attempts - 1:
                print(f"LinkedIn search failed ({params.get('keywords')}): {exc}")
                return ""
            time.sleep(2 * (attempt + 1))
    return ""


def fetch_linkedin_jobs(hours: int = 24, max_jobs: int = 40) -> list[Job]:
    """Return recent UAE jobs matching config titles from LinkedIn guest search."""
    config = _load_config()
    groups = _keyword_groups(config["job_titles"])
    locations = _search_locations(config["locations"])
    if not groups:
        print("LinkedIn search skipped: no job_titles configured.")
        return []

    window_seconds = max(1, int(hours)) * 3600
    collected: dict[str, Job] = {}

    for keywords in groups:
        for location in locations:
            html = _fetch_linkedin_page(
                {
                    "keywords": keywords,
                    "location": location,
                    "f_TPR": f"r{window_seconds}",
                    "sortBy": "DD",
                    "start": 0,
                }
            )
            found = _parse_linkedin_cards(html)
            new = 0
            for job in found:
                if job.link not in collected:
                    collected[job.link] = job
                    new += 1
            print(
                f"[LinkedIn] {location} | {keywords[:48]}... -> "
                f"{len(found)} card(s), {new} new"
            )
            if len(collected) >= max_jobs:
                break
            time.sleep(1)
        if len(collected) >= max_jobs:
            break

    jobs = list(collected.values())[:max_jobs]
    print(f"[LinkedIn] {len(jobs)} unique job(s) in the last {hours}h.")
    return jobs


# --- Google News -> Indeed / Bayt / Naukrigulf --------------------------------


def _looks_like_category_title(title: str, source: str = "") -> bool:
    """Reject Google News items that are search/category pages, not job posts."""
    text = title or ""
    if re.search(
        r"\d+\+|vacancies|\bsalaries\b|Jobs,\s*Employment|employment\s+\d",
        text,
        re.I,
    ):
        return True
    # Indeed individual posts are usually "<Role> - Indeed".
    # Search pages look like "It support jobs in Dubai - Indeed".
    if source == "Indeed" and re.search(r"jobs in\b", text, re.I):
        return True
    # Bayt category pages: "IT Support Specialist Jobs in UAE (Jul 2026)"
    if source == "Bayt" and re.search(
        r"Jobs in (?:UAE|Dubai|Abu Dhabi|the Middle East)\s*\(",
        text,
        re.I,
    ):
        return True
    return False


def _clean_news_title(title: str, source: str) -> str:
    text = (title or "").strip()
    # Strip trailing publisher labels Google News appends.
    for suffix in (
        " - Indeed",
        " - bayt.com",
        " - Bayt.com",
        " - bayt",
        " - Naukrigulf",
        " - NaukriGulf",
        " - United Arab Emirates - Naukrigulf",
        " - United Arab Emirates (UAE) - Naukrigulf",
    ):
        if text.lower().endswith(suffix.lower()):
            text = text[: -len(suffix)].strip()
    # Naukrigulf: "<role> jobs in <company> in <city> ..."
    if source == "Naukrigulf":
        text = re.sub(r"\s+jobs in .+$", "", text, flags=re.I).strip()
    # Bayt: "<role> at <company> - <city>"
    if source == "Bayt":
        text = re.sub(r"\s+at\s+.+$", "", text, flags=re.I).strip()
    return text or title


def _is_uae_context(title: str, url: str) -> bool:
    blob = f"{title} {url}".lower()
    if any(hint in blob for hint in NON_UAE_HINTS) and not any(
        hint in blob for hint in UAE_LOCATION_HINTS
    ):
        return False
    return any(hint in blob for hint in UAE_LOCATION_HINTS) or "ae.indeed.com" in blob


def _is_job_url(source: str, url: str) -> bool:
    u = (url or "").lower()
    if source == "Indeed":
        return "viewjob" in u and "jk=" in u
    if source == "Bayt":
        return bool(
            re.search(
                r"bayt\.com/en/(?:uae|dubai|abu-dhabi)/jobs/.+-\d{6,}/?",
                u,
            )
        ) and "-jobs/" not in u
    if source == "Naukrigulf":
        return bool(re.search(r"naukrigulf\.com/.+-jid-\d+", u))
    return False


def _decode_google_news_url(article_url: str) -> str:
    try:
        from googlenewsdecoder import new_decoderv1
    except ImportError:
        print(
            "googlenewsdecoder not installed; "
            "Indeed/Bayt/Naukrigulf discovery disabled."
        )
        return ""
    try:
        result = new_decoderv1(article_url, interval=GOOGLE_NEWS_DECODE_INTERVAL)
    except Exception as exc:
        print(f"Google News decode failed: {exc}")
        return ""
    if not result or not result.get("status"):
        return ""
    return (result.get("decoded_url") or "").strip()


def _google_news_queries(site_query: str, job_titles: list[str]) -> list[str]:
    """Build a few focused queries.

    One giant OR-query makes Google News return only Indeed/Bayt *search*
    pages. Smaller role-focused queries surface individual job posts.
    """
    # Prefer the highest-signal titles first; keep request count low.
    preferred = [
        "IT Support",
        "System Administrator",
        "Help Desk",
        "Helpdesk",
        "Technical Support",
        "Desktop Support",
        "Service Desk",
        "IT Administrator",
    ]
    titles = []
    lower_cfg = {t.lower() for t in job_titles}
    for title in preferred:
        if title.lower() in lower_cfg or not job_titles:
            titles.append(title)
    if not titles:
        titles = [t for t in job_titles[:6] if t.strip()]

    location = '(Dubai OR "Abu Dhabi" OR UAE OR "United Arab Emirates")'
    queries: list[str] = []
    # Pair titles to cut round-trips roughly in half.
    for start in range(0, len(titles), 2):
        chunk = titles[start : start + 2]
        role_q = " OR ".join(f'"{t}"' for t in chunk)
        queries.append(f"{site_query} ({role_q}) {location}")
    return queries[:GOOGLE_NEWS_MAX_QUERIES]


def _fetch_google_news_board(
    source: str,
    site_query: str,
    max_jobs: int,
) -> list[Job]:
    """Discover individual jobs for one board via Google News RSS."""
    config = _load_config()
    queries = _google_news_queries(site_query, config["job_titles"])
    collected: dict[str, Job] = {}
    decoded = 0
    entries_seen = 0

    for query in queries:
        if len(collected) >= max_jobs:
            break
        rss_url = (
            "https://news.google.com/rss/search?"
            f"q={quote_plus(query)}&hl=en-AE&gl=AE&ceid=AE:en"
        )
        try:
            response = requests.get(
                rss_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/rss+xml, application/xml, text/xml, */*",
                },
                timeout=30,
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)
        except Exception as exc:
            print(f"[{source}] Google News RSS failed: {exc}")
            continue

        entries = list(feed.entries or [])[:GOOGLE_NEWS_MAX_ENTRIES]
        entries_seen += len(entries)

        for entry in entries:
            if len(collected) >= max_jobs:
                break
            raw_title = (entry.get("title") or "").strip()
            if not raw_title or _looks_like_category_title(raw_title, source):
                continue
            article_link = (entry.get("link") or "").strip()
            if not article_link:
                continue

            job_url = _decode_google_news_url(article_link)
            decoded += 1
            if not job_url or not _is_job_url(source, job_url):
                continue
            if not _is_uae_context(raw_title, job_url):
                continue

            title = _clean_news_title(raw_title, source)
            if source == "Indeed":
                jk = re.search(r"[?&]jk=([a-f0-9]+)", job_url, re.I)
                if jk:
                    job_url = f"https://ae.indeed.com/viewjob?jk={jk.group(1)}"

            if job_url in collected:
                continue
            collected[job_url] = Job(
                title=title,
                link=job_url,
                source=source,
                location="United Arab Emirates",
            )

    jobs = list(collected.values())
    print(
        f"[{source}] Google News: {entries_seen} entries across "
        f"{len(queries)} quer{'y' if len(queries) == 1 else 'ies'}, "
        f"{decoded} decoded, {len(jobs)} job URL(s)."
    )
    return jobs


def fetch_indeed_jobs(max_jobs: int = 15) -> list[Job]:
    return _fetch_google_news_board("Indeed", "site:ae.indeed.com", max_jobs)


def fetch_bayt_jobs(max_jobs: int = 15) -> list[Job]:
    return _fetch_google_news_board("Bayt", "site:bayt.com", max_jobs)


def fetch_naukrigulf_jobs(max_jobs: int = 15) -> list[Job]:
    return _fetch_google_news_board("Naukrigulf", "site:naukrigulf.com", max_jobs)


def fetch_board_jobs(
    include_linkedin: bool = True,
    include_indeed: bool = True,
    include_bayt: bool = True,
    include_naukrigulf: bool = True,
    linkedin_hours: int = 24,
    linkedin_max: int = 30,
    board_max: int = 15,
) -> list[Job]:
    """Fetch jobs from all enabled free sources, de-duplicated by link."""
    collected: dict[str, Job] = {}

    fetchers = []
    if include_linkedin:
        fetchers.append(
            ("LinkedIn", lambda: fetch_linkedin_jobs(linkedin_hours, linkedin_max))
        )
    if include_indeed:
        fetchers.append(("Indeed", lambda: fetch_indeed_jobs(board_max)))
    if include_bayt:
        fetchers.append(("Bayt", lambda: fetch_bayt_jobs(board_max)))
    if include_naukrigulf:
        fetchers.append(("Naukrigulf", lambda: fetch_naukrigulf_jobs(board_max)))

    for name, fetcher in fetchers:
        try:
            for job in fetcher():
                if job.link not in collected:
                    collected[job.link] = job
        except Exception as exc:
            print(f"[{name}] discovery failed: {exc}")

    jobs = list(collected.values())
    print(f"Board discovery total: {len(jobs)} unique job(s).")
    return jobs
