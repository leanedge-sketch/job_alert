"""Job alert bot pipeline.

1. Load active CVs from Notion CV Vault (PDF via CV File, else page body),
   including Last Processed URL for RSS cursor state.
2. Poll Google Alerts RSS feeds (feedparser); stop when Last Processed URL is hit.
3. For each new job, score every active CV with Gemini (winner-takes-all).
4. If a winning CV meets its Minimum Score:
   - Log exactly one Notion Job Tracker row (Note: "Matched for: [Winner]")
   - Send exactly one Telegram notification
   - Generate a PDF (cover letter + tailored CV) and sendDocument via Telegram
5. Persist the newest RSS item URL back to each active CV's Last Processed URL.
"""

import argparse
import io
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
import trafilatura
from dotenv import load_dotenv
from google.api_core.exceptions import (
    InternalServerError,
    ResourceExhausted,
    ServiceUnavailable,
)
from notion_client import Client
from fpdf import FPDF
from pypdf import PdfReader

from google_backfill import fetch_google_jobs

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NOTION_API_TOKEN = os.getenv("NOTION_API_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_CV_VAULT_ID = os.getenv("NOTION_CV_VAULT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

SEEN_JOBS_FILE = Path(__file__).parent / "seen_jobs.json"
CONFIG_FILE = Path(__file__).parent / "config.json"
RESUME_FILE = Path(__file__).parent / "resume.txt"

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


def load_resume_text() -> str:
    """Load candidate resume used for Gemini match scoring."""
    try:
        text = RESUME_FILE.read_text(encoding="utf-8").strip()
        if not text:
            print(f"Warning: {RESUME_FILE.name} is empty.")
        return text
    except Exception as exc:
        print(f"Warning: could not read {RESUME_FILE.name} ({exc}).")
        return ""


resume_text = load_resume_text()

DEFAULT_SEARCH_CONFIG = {
    "job_titles": [
        "Support Specialist",
        "System Administrator",
        "Systems Administrator",
        "IT Administrator",
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


def _pdf_safe_text(text: str) -> str:
    """Normalize text for core PDF fonts (latin-1)."""
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # Common Unicode punctuation from Gemini → ASCII stand-ins.
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2022": "-",
        "\u00a0": " ",
    }
    for src, dst in replacements.items():
        cleaned = cleaned.replace(src, dst)
    return cleaned.encode("latin-1", "replace").decode("latin-1")


def generate_application_pdf(
    job_title: str,
    cover_letter: str,
    tailored_cv: str,
) -> str:
    """Build a local PDF with cover letter + tailored CV; return the file path."""
    safe_title = re.sub(r"[^\w\-]+", "_", job_title or "job").strip("_")[:80] or "job"
    pdf_filename = f"application_{safe_title}.pdf"
    pdf_path = str(Path(__file__).parent / pdf_filename)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Cover letter
    pdf.add_page()
    pdf.set_font("Arial", "B", 14)
    pdf.multi_cell(0, 8, _pdf_safe_text("Cover Letter"))
    pdf.ln(2)
    pdf.set_font("Arial", "B", 11)
    pdf.multi_cell(0, 6, _pdf_safe_text(job_title or ""))
    pdf.ln(4)
    pdf.set_font("Arial", size=11)
    pdf.multi_cell(
        0,
        5,
        _pdf_safe_text(cover_letter or "(Not generated)"),
    )

    # Tailored CV
    pdf.add_page()
    pdf.set_font("Arial", "B", 14)
    pdf.multi_cell(0, 8, _pdf_safe_text("Tailored CV"))
    pdf.ln(2)
    pdf.set_font("Arial", "B", 11)
    pdf.multi_cell(0, 6, _pdf_safe_text(job_title or ""))
    pdf.ln(4)
    pdf.set_font("Arial", size=11)
    pdf.multi_cell(
        0,
        5,
        _pdf_safe_text(tailored_cv or "(Not generated)"),
    )

    pdf.output(pdf_path)
    print(f"Generated application PDF: {pdf_path}")
    return pdf_path


def send_telegram_document(pdf_filename: str, caption: str = "") -> None:
    """Send a local PDF via Telegram Bot API sendDocument."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    data = {"chat_id": TELEGRAM_CHAT_ID}
    if caption:
        data["caption"] = caption[:1024]
    with open(pdf_filename, "rb") as doc:
        response = requests.post(
            url,
            data=data,
            files={"document": doc},
            timeout=60,
        )
    response.raise_for_status()
    print(f"Sent Telegram document: {pdf_filename}")


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
    match_score: int,
    keywords: list[str],
    cv_name: str = "",
) -> str:
    # Keep the raw URL as plain text at the end so Telegram builds a preview card.
    keyword_text = ", ".join(keywords) if keywords else "N/A"
    cv_line = (
        f"<b>Matched CV:</b> {html_escape(cv_name)}\n" if cv_name else ""
    )
    return (
        f"🚨 <b>New Job Match ({html_escape(source)})</b>\n\n"
        f"<b>Job Title:</b> {html_escape(title)}\n"
        f"{cv_line}"
        f"<b>Location:</b> {html_escape(location)}\n"
        f"<b>Salary:</b> {html_escape(salary)}\n\n"
        f"🎯 <b>Match Score:</b> {int(match_score)}%\n"
        f"🔑 <b>Keywords:</b> {html_escape(keyword_text)}\n\n"
        f"<b>Link:</b>\n"
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


def fetch_clean_job_text(job_url: str, rss_fallback: str = "") -> str:
    """Download a job page and extract clean body text via trafilatura.

    Strategy:
      1. Fetch HTML with requests (custom User-Agent).
      2. Extract main content with trafilatura.extract().
      3. If that fails, try trafilatura.fetch_url() as a second path.
      4. Fall back to the RSS snippet when the page cannot be scraped.
    """
    if not job_url:
        return rss_fallback or ""

    # Path 1: requests + trafilatura.extract (most reliable with our User-Agent).
    try:
        response = requests.get(
            job_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=30,
            allow_redirects=True,
        )
        response.raise_for_status()
        extracted = trafilatura.extract(
            response.text,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
            url=job_url,
        )
        if extracted and extracted.strip():
            text = extracted.strip()
            print(f"trafilatura extracted {len(text)} chars from {job_url}")
            return text
        print(f"trafilatura: no main content extracted from {job_url}")
    except Exception as exc:
        print(f"trafilatura/requests fetch failed for {job_url}: {exc}")

    # Path 2: trafilatura's built-in fetcher.
    try:
        downloaded = trafilatura.fetch_url(job_url)
        if downloaded:
            extracted = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
                url=job_url,
            )
            if extracted and extracted.strip():
                text = extracted.strip()
                print(
                    f"trafilatura.fetch_url extracted {len(text)} chars from {job_url}"
                )
                return text
    except Exception as exc:
        print(f"trafilatura.fetch_url failed for {job_url}: {exc}")

    if rss_fallback:
        print(
            f"trafilatura unavailable; using RSS snippet "
            f"({len(rss_fallback)} chars) for {job_url}"
        )
    return rss_fallback or ""


def _notion_client() -> Client | None:
    if not NOTION_API_TOKEN:
        return None
    return Client(auth=NOTION_API_TOKEN)


def _page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") != "title":
            continue
        return "".join(
            part.get("plain_text", "") for part in (prop.get("title") or [])
        ).strip() or "Unnamed CV"
    return "Unnamed CV"


def _page_minimum_score(page: dict, default: int = 0) -> int:
    """Read the CV Vault 'Minimum Score' number property."""
    props = page.get("properties") or {}
    prop = props.get("Minimum Score")
    if not prop or prop.get("type") != "number":
        return default
    value = prop.get("number")
    if value is None:
        return default
    try:
        score = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(100, score))


def _page_last_processed_url(page: dict) -> str | None:
    """Read the CV Vault 'Last Processed URL' URL property (RSS cursor)."""
    props = page.get("properties") or {}
    prop = props.get("Last Processed URL")
    if not prop or prop.get("type") != "url":
        return None
    url = prop.get("url")
    if not url or not str(url).strip():
        return None
    return str(url).strip()


def _shared_last_processed_url(active_cvs: list[dict]) -> str | None:
    """Return the first non-empty Last Processed URL among active CVs."""
    for cv in active_cvs:
        url = (cv.get("last_processed_url") or "").strip()
        if url:
            return url
    return None


def update_last_processed_url(active_cvs: list[dict], newest_url: str) -> None:
    """PATCH Last Processed URL on every active CV Vault page (Notion pages.update)."""
    newest_url = (newest_url or "").strip()
    if not newest_url:
        return

    notion = _notion_client()
    if notion is None:
        print("Skipping Last Processed URL update: Notion client unavailable.")
        return

    updated_any = False
    for cv in active_cvs:
        page_id = cv.get("page_id")
        if not page_id:
            continue
        try:
            notion.pages.update(
                page_id=page_id,
                properties={
                    "Last Processed URL": {"url": newest_url},
                },
            )
            cv["last_processed_url"] = newest_url
            updated_any = True
            print(
                f"CV Vault: set Last Processed URL for '{cv.get('name')}' "
                f"-> {newest_url}"
            )
        except Exception as exc:
            print(
                f"CV Vault: failed to update Last Processed URL "
                f"for '{cv.get('name')}': {exc}"
            )

    if not updated_any:
        print(
            "CV Vault: no active CV page_id available to store Last Processed URL."
        )


def _cv_file_url(page: dict) -> tuple[str | None, str]:
    """Return (url, filename) from the CV Vault 'CV File' Files & media property."""
    props = page.get("properties") or {}
    prop = props.get("CV File")
    if not prop or prop.get("type") != "files":
        return None, ""

    for item in prop.get("files") or []:
        name = (item.get("name") or "").strip()
        item_type = item.get("type")
        if item_type == "file":
            url = (item.get("file") or {}).get("url")
        elif item_type == "external":
            url = (item.get("external") or {}).get("url")
        else:
            url = None
        if url:
            return url, name or "cv.pdf"
    return None, ""


MIN_CV_TEXT_CHARS = 100

# Notion block types used when falling back to page body text.
_PAGE_BODY_BLOCK_TYPES = (
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
)


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract plain text from an in-memory PDF using pypdf."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    chunks: list[str] = []
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def _page_plain_text(notion: Client, page_id: str) -> str:
    """Fetch plain text from Notion page body via blocks.children.list."""
    chunks: list[str] = []
    cursor = None
    while True:
        kwargs = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        response = notion.blocks.children.list(**kwargs)
        for block in response.get("results", []):
            block_type = block.get("type")
            if block_type not in _PAGE_BODY_BLOCK_TYPES:
                continue
            payload = block.get(block_type) or {}
            rich_text = payload.get("rich_text") or []
            text = "".join(part.get("plain_text", "") for part in rich_text).strip()
            if text:
                chunks.append(text)
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    return "\n".join(chunks).strip()


def _cv_text_from_page(notion: Client, page: dict, applicant_name: str) -> str:
    """Load CV text from PDF, with Notion page body fallback if PDF is weak.

    Returns text with at least MIN_CV_TEXT_CHARS, or "" if neither source is usable.
    """
    cv_page_id = page.get("id")
    cv_text = ""
    file_url, file_name = _cv_file_url(page)

    if file_url:
        try:
            response = requests.get(file_url, timeout=60)
            response.raise_for_status()
            cv_text = _extract_pdf_text(response.content)
            if len(cv_text.strip()) >= MIN_CV_TEXT_CHARS:
                print(
                    f"CV Vault: downloaded and parsed PDF for '{applicant_name}' "
                    f"({file_name}, {len(cv_text)} chars)."
                )
                return cv_text.strip()
            print(
                "PDF text extraction returned less than 100 characters. "
                "Attempting fallback to Notion Page Body..."
            )
        except Exception as exc:
            print(
                f"CV Vault: PDF download/parse failed for '{applicant_name}' "
                f"({file_name}): {exc}"
            )
            print(
                "PDF text extraction returned less than 100 characters. "
                "Attempting fallback to Notion Page Body..."
            )
            cv_text = ""
    else:
        # No PDF attached — go straight to page body (same validation rules).
        print(
            f"CV Vault: no CV File on '{applicant_name}'; "
            "reading Notion Page Body..."
        )

    if cv_page_id:
        body_text = _page_plain_text(notion, cv_page_id)
        if len(body_text.strip()) >= MIN_CV_TEXT_CHARS:
            print(
                f"CV Vault: using Notion Page Body for '{applicant_name}' "
                f"({len(body_text)} chars)."
            )
            return body_text.strip()
        # Prefer whichever source had more text for the final check message.
        if len(body_text.strip()) > len(cv_text.strip()):
            cv_text = body_text

    if len(cv_text.strip()) < MIN_CV_TEXT_CHARS:
        print(
            "No readable CV text found in PDF or Notion page body. "
            "Skipping CV profile."
        )
        return ""

    return cv_text.strip()


def _query_active_cv_pages(notion: Client, database_id: str) -> list[dict]:
    """Query CV Vault for pages where Active checkbox is True."""
    active_filter = {"property": "Active", "checkbox": {"equals": True}}
    pages: list[dict] = []

    # Newer Notion API: query via data source when available.
    try:
        db = notion.databases.retrieve(database_id=database_id)
        data_sources = db.get("data_sources") or []
        if data_sources and hasattr(notion, "data_sources"):
            data_source_id = data_sources[0]["id"]
            cursor = None
            while True:
                kwargs = {
                    "data_source_id": data_source_id,
                    "filter": active_filter,
                    "page_size": 100,
                }
                if cursor:
                    kwargs["start_cursor"] = cursor
                response = notion.data_sources.query(**kwargs)
                pages.extend(response.get("results", []))
                if not response.get("has_more"):
                    break
                cursor = response.get("next_cursor")
            return pages
    except Exception as exc:
        print(f"CV Vault data_source query failed, trying databases.query: {exc}")

    # Fallback for older API shapes that still expose databases.query.
    if not hasattr(notion.databases, "query"):
        raise AttributeError("notion.databases.query is unavailable")

    cursor = None
    while True:
        kwargs = {
            "database_id": database_id,
            "filter": active_filter,
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor
        response = notion.databases.query(**kwargs)
        pages.extend(response.get("results", []))
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    return pages


def load_active_cvs() -> list[dict]:
    """Load active applicant profiles from Notion CV Vault.

    CV Vault required properties:
      - Title property (any name, type Title) -> Applicant Name
      - Active (Checkbox) -> must be True to be used
      - Minimum Score (Number) -> per-CV match threshold (0-100)
      - CV File (Files & media) -> optional PDF resume attachment
      - Last Processed URL (URL) -> RSS cursor (may be empty on first run)

    Resume text prefers PDF from CV File. If PDF text is under 100 characters,
    falls back to Notion page body (paragraph/heading/list blocks). Profiles
    with still-insufficient text are skipped.
    Returns list of dicts: {"name", "minimum_score", "text", "page_id",
    "last_processed_url"}.
    Falls back to local resume.txt when vault is empty/unavailable.
    """
    fallback_text = resume_text
    fallback = (
        [
            {
                "name": "Local resume.txt",
                "minimum_score": 0,
                "text": fallback_text,
                "page_id": None,
                "last_processed_url": None,
            }
        ]
        if fallback_text
        else []
    )

    if not NOTION_API_TOKEN or not NOTION_CV_VAULT_ID:
        print("CV Vault skipped: NOTION_API_TOKEN or NOTION_CV_VAULT_ID not set.")
        return fallback

    notion = _notion_client()
    if notion is None:
        return fallback

    try:
        pages = _query_active_cv_pages(notion, NOTION_CV_VAULT_ID)
        active_cvs: list[dict] = []
        for page in pages:
            name = _page_title(page)
            minimum_score = _page_minimum_score(page, default=0)
            last_processed_url = _page_last_processed_url(page)
            text = _cv_text_from_page(notion, page, name)
            if len(text.strip()) < MIN_CV_TEXT_CHARS:
                # Warning already logged inside _cv_text_from_page when both
                # PDF and page body failed the 100-character check.
                continue
            active_cvs.append(
                {
                    "name": name,
                    "minimum_score": minimum_score,
                    "text": text,
                    "page_id": page.get("id"),
                    "last_processed_url": last_processed_url,
                }
            )

        if not active_cvs:
            print("CV Vault: no active CVs with resume text; using resume.txt fallback.")
            return fallback

        print(f"CV Vault: loaded {len(active_cvs)} active CV(s).")
        for cv in active_cvs:
            cursor = cv.get("last_processed_url") or "(empty — first run)"
            print(
                f"  - {cv['name']} (minimum score: {cv['minimum_score']}, "
                f"{len(cv['text'])} chars, Last Processed URL: {cursor})"
            )
        return active_cvs
    except Exception as exc:
        print(f"CV Vault error (using resume.txt fallback): {exc}")
        return fallback


def safe_gemini_generate(prompt: str, max_retries: int = 3):
    """Call Gemini generate_content with exponential backoff on rate/service errors.

    Retries on ResourceExhausted, ServiceUnavailable, and InternalServerError.
    wait_time = 10 * (2 ** attempt) seconds between tries.
    Returns the response object, or None if all retries fail.
    """
    if not GEMINI_API_KEY:
        print("Gemini skipped: GEMINI_API_KEY not set.")
        return None

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        "gemini-3.6-flash",
        generation_config={
            "response_mime_type": "application/json",
            "temperature": 0.2,
        },
    )

    for attempt in range(max_retries):
        try:
            return model.generate_content(prompt)
        except (ResourceExhausted, ServiceUnavailable, InternalServerError) as exc:
            wait_time = 10 * (2 ** attempt)
            if attempt >= max_retries - 1:
                print(
                    f"ERROR: Gemini API failed after {max_retries} attempts "
                    f"({type(exc).__name__}: {exc}). Giving up."
                )
                return None
            print(
                f"API limit hit or service unavailable. "
                f"Retrying in {wait_time} seconds..."
            )
            time.sleep(wait_time)
        except Exception as exc:
            print(f"Gemini generate_content error: {exc}")
            return None

    return None


def analyze_job_with_gemini(
    title: str,
    description: str,
    candidate_resume: str = "",
) -> dict:
    """Compare a candidate resume to a job posting with Gemini.

    Returns {
      "match_score": int,
      "keywords": list[str],
      "cover_letter": str,
      "tailored_cv": str,
    }.
    On failure defaults to score 0, ["AI Parse Failed"], and empty strings.
    """
    fallback = {
        "match_score": 0,
        "keywords": ["AI Parse Failed"],
        "cover_letter": "",
        "tailored_cv": "",
    }
    if not GEMINI_API_KEY:
        print("Gemini skipped: GEMINI_API_KEY not set.")
        return fallback

    cv_content = candidate_resume or resume_text or "No resume provided."
    job_description = (
        f"Title: {title}\n\nDescription:\n{description or 'No description provided.'}"
    )
    prompt = (
        "You are an expert technical recruiter. Evaluate the provided job description "
        "against the candidate's CV. Return a strict JSON response with four keys: "
        "'match_score' (an integer from 0 to 100 representing the probability of a "
        "strong match based strictly on the candidate's documented experience), "
        "'keywords' (a list of 3 to 5 critical skills required by the job that are "
        "EXPLICITLY present in the candidate's CV. Do not list skills the job requires "
        "if the candidate does not possess them), "
        "'cover_letter' (a string: if match_score is high, write a highly persuasive "
        "cover letter focused on the overlapping skills between the CV and the job; "
        "otherwise use an empty string), and "
        "'tailored_cv' (a string: if match_score is high, rewrite the provided CV's "
        "summary and bullet points to specifically mirror the terminology and "
        "requirements of the job description WITHOUT inventing fake experience; "
        "otherwise use an empty string).\n\n"
        f"Candidate resume:\n{cv_content}\n\n"
        f"Job description:\n{job_description}"
    )

    try:
        response = safe_gemini_generate(prompt)
        if response is None:
            return fallback
        data = json.loads(response.text)
        match_score = int(data["match_score"])
        keywords = [
            str(k).strip()[:80]
            for k in data.get("keywords", [])
            if str(k).strip()
        ]
        if not 0 <= match_score <= 100:
            raise ValueError(f"match_score out of range: {match_score}")
        if not keywords:
            keywords = ["AI Parse Failed"]
        cover_letter = str(data.get("cover_letter") or "").strip()
        tailored_cv = str(data.get("tailored_cv") or "").strip()
        return {
            "match_score": match_score,
            "keywords": keywords[:5],
            "cover_letter": cover_letter,
            "tailored_cv": tailored_cv,
        }
    except Exception as exc:
        print(f"Gemini error: {exc}")
        return fallback


NOTION_RICH_TEXT_LIMIT = 2000


def _chunk_text_for_notion(text: str, limit: int = NOTION_RICH_TEXT_LIMIT) -> list[str]:
    """Split text into chunks that fit Notion rich_text content limits."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ["(Not generated)"]
    return [cleaned[i : i + limit] for i in range(0, len(cleaned), limit)]


def _notion_heading_2(title: str) -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [
                {"type": "text", "text": {"content": (title or "")[:NOTION_RICH_TEXT_LIMIT]}}
            ]
        },
    }


def _notion_paragraph_blocks(text: str) -> list[dict]:
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk}}]
            },
        }
        for chunk in _chunk_text_for_notion(text)
    ]


def _job_tracker_page_children(cover_letter: str, tailored_cv: str) -> list[dict]:
    """Build Notion page body blocks for cover letter + tailored CV."""
    children: list[dict] = [_notion_heading_2("Auto-Generated Cover Letter")]
    children.extend(_notion_paragraph_blocks(cover_letter))
    children.append(_notion_heading_2("Tailored CV"))
    children.extend(_notion_paragraph_blocks(tailored_cv))
    return children


def add_job_to_notion(
    job_title: str,
    job_url: str,
    source: str,
    match_score: int = 0,
    keywords: list[str] | None = None,
    note: str = "Auto-logged via Gemini Matcher",
    cover_letter: str = "",
    tailored_cv: str = "",
) -> None:
    """Create a Notion database row for a newly found job.

    Required Notion database property names (exact spelling/type):
      - Position    -> Title
      - Link        -> URL
      - Date Found  -> Date
      - Status      -> Select  (default option: To Apply)
      - Match Score -> Number
      - Keywords    -> Multi-select
      - Applied     -> Checkbox (default: unchecked)
      - Note        -> Rich text

    Page body children include Auto-Generated Cover Letter and Tailored CV.

    Env vars required:
      - NOTION_API_TOKEN
      - NOTION_DATABASE_ID
    """
    if not NOTION_API_TOKEN or not NOTION_DATABASE_ID:
        print("Notion skipped: NOTION_API_TOKEN or NOTION_DATABASE_ID not set.")
        return

    keyword_list = keywords or ["AI Parse Failed"]
    note_text = (note or "Auto-logged via Gemini Matcher")[:2000]
    children = _job_tracker_page_children(
        cover_letter=str(cover_letter or ""),
        tailored_cv=str(tailored_cv or ""),
    )
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
                # Must be named exactly "Status" and type Select (Kanban driver)
                "Status": {
                    "select": {"name": "To Apply"},
                },
                # Must be named exactly "Match Score" and type Number
                "Match Score": {
                    "number": int(match_score),
                },
                # Must be named exactly "Keywords" and type Multi-select
                "Keywords": {
                    "multi_select": [{"name": kw[:100]} for kw in keyword_list],
                },
                # Must be named exactly "Applied" and type Checkbox
                "Applied": {
                    "checkbox": False,
                },
                # Must be named exactly "Note" and type Rich text
                "Note": {
                    "rich_text": [
                        {"text": {"content": note_text}}
                    ],
                },
            },
            children=children,
        )
        print(f"Added to Notion ({source}): {job_title}")
    except Exception as exc:
        # Never block Telegram alerts if Notion fails or rate-limits.
        print(f"Notion error (continuing with Telegram): {exc}")


def notion_job_link_exists(job_url: str) -> bool:
    """Return True if Job Tracker already has a row with this Link URL."""
    if not NOTION_API_TOKEN or not NOTION_DATABASE_ID or not job_url:
        return False

    notion = _notion_client()
    if notion is None:
        return False

    link_filter = {"property": "Link", "url": {"equals": job_url}}
    try:
        db = notion.databases.retrieve(database_id=NOTION_DATABASE_ID)
        data_sources = db.get("data_sources") or []
        if data_sources and hasattr(notion, "data_sources"):
            response = notion.data_sources.query(
                data_source_id=data_sources[0]["id"],
                filter=link_filter,
                page_size=1,
            )
            return bool(response.get("results"))
    except Exception as exc:
        print(f"Notion link lookup (data_source) failed, trying databases.query: {exc}")

    try:
        if not hasattr(notion.databases, "query"):
            return False
        response = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter=link_filter,
            page_size=1,
        )
        return bool(response.get("results"))
    except Exception as exc:
        print(f"Notion link lookup failed (continuing without it): {exc}")
        return False


def _contains_phrase(text: str, phrase: str) -> bool:
    """Case-insensitive whole-phrase match (avoids 'intern' matching 'international')."""
    pattern = r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)"
    return re.search(pattern, text.lower()) is not None


def matches_search_filters(title: str, description: str = "", link: str = "") -> bool:
    """Apply job_titles / locations / blocked_words from SEARCH_CONFIG.

    Location may appear in the title, RSS/page text, or the job URL (common for
    Indeed/Bayt links), so the link is included in the location check.
    """
    title_lower = title.lower()
    combined = f"{title} {description} {link}".lower()

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
    active_cvs: list[dict] | None = None,
) -> bool:
    """Score one job against all active CVs; alert only for the single best winner.

    Winner logic:
      1. Skip if already seen / already in Job Tracker / fails search filters.
      2. Fetch job text (trafilatura, RSS fallback).
      3. For each CV: Gemini score + optional cover_letter/tailored_cv; keep the
         highest score that also meets that CV's Minimum Score.
      4. After all CVs: at most one Notion row (with cover letter + tailored CV in
         the page body) and one Telegram alert for the winner.
      5. time.sleep(5) after each CV Gemini call to throttle API usage.
    """
    if not title or not link:
        return False
    if link in seen:
        return False

    # Title / blocked-word gate first (cheap). Location may only appear after
    # trafilatura pulls the full page, so we re-check with page text below.
    if not matches_search_filters(title, description, link=link):
        # Soft retry: if title matches a job_title and isn't blocked, still
        # allow through when location is missing from the short RSS snippet.
        title_ok = any(
            job_title.lower() in title.lower()
            for job_title in SEARCH_CONFIG["job_titles"]
        )
        blocked = any(
            _contains_phrase(f"{title} {description}", blocked)
            for blocked in SEARCH_CONFIG["blocked_words"]
        )
        if not title_ok or blocked:
            return False
        print(
            f"Location not in RSS snippet; will verify after trafilatura: {title}"
        )

    # Durable dedup for GitHub Actions when seen_jobs.json cache is cold.
    if notion_job_link_exists(link):
        print(f"Already in Job Tracker; skipping duplicate: {title}")
        seen.add(link)
        return False

    cvs = active_cvs if active_cvs is not None else load_active_cvs()
    if not cvs:
        print(f"No active CVs available; skipping job: {title}")
        return False

    # Prefer full page text for Gemini; keep RSS snippet for filters/metadata.
    gemini_description = fetch_clean_job_text(link, rss_fallback=description)

    # Final filter using trafilatura page text + link (catches Dubai/UAE in body).
    if not matches_search_filters(title, gemini_description, link=link):
        print(f"Filtered out after page extract: {title}")
        seen.add(link)
        return False

    details = parse_job_details(title, description)
    display_source = source_label("", source, link)

    best_score = -1
    best_cv_name: str | None = None
    best_keywords: list[str] = []
    best_cover_letter = ""
    best_tailored_cv = ""

    for cv in cvs:
        applicant_name = cv.get("name") or "Unnamed CV"
        cv_text = cv.get("text") or ""
        minimum_score = int(cv.get("minimum_score") or 0)

        analysis = analyze_job_with_gemini(
            title,
            gemini_description,
            candidate_resume=cv_text,
        )
        match_score = int(analysis["match_score"])
        keywords = list(analysis["keywords"])
        cover_letter = str(analysis.get("cover_letter") or "")
        tailored_cv = str(analysis.get("tailored_cv") or "")

        print(
            f"Scored {applicant_name}: {match_score}% "
            f"(minimum {minimum_score}%) for {title}"
        )

        if match_score >= minimum_score and match_score > best_score:
            best_score = match_score
            best_cv_name = applicant_name
            best_keywords = keywords
            best_cover_letter = cover_letter
            best_tailored_cv = tailored_cv
            print(
                f"New winner: {best_cv_name} at {best_score}% "
                f"for {title}"
            )
        elif match_score < minimum_score:
            print(
                f"Below threshold ({match_score}% < {minimum_score}%) "
                f"for {applicant_name}: {title}"
            )

        # Proactive throttle between CV evaluations against the same job.
        time.sleep(5)

    sent = False
    if best_cv_name is not None:
        note = f"Matched for: {best_cv_name}"
        add_job_to_notion(
            title,
            link,
            display_source,
            match_score=best_score,
            keywords=best_keywords,
            note=note,
            cover_letter=best_cover_letter,
            tailored_cv=best_tailored_cv,
        )
        send_telegram_message(
            format_job_message(
                title,
                display_source,
                link,
                details["location"],
                details["salary"],
                best_score,
                best_keywords,
                cv_name=best_cv_name,
            ),
            disable_web_page_preview=False,
        )

        pdf_path = None
        try:
            pdf_path = generate_application_pdf(
                title,
                best_cover_letter,
                best_tailored_cv,
            )
            send_telegram_document(
                pdf_path,
                caption=(
                    f"Application pack for {title}\n"
                    f"Matched CV: {best_cv_name} ({best_score}%)"
                ),
            )
        except Exception as pdf_exc:
            print(f"PDF generate/send failed (continuing): {pdf_exc}")
        finally:
            if pdf_path and os.path.exists(pdf_path):
                try:
                    os.remove(pdf_path)
                    print(f"Deleted local PDF: {pdf_path}")
                except OSError as cleanup_exc:
                    print(f"Could not delete PDF {pdf_path}: {cleanup_exc}")

        sent = True
        print(
            f"Winner alert ({display_source}, {best_cv_name}, "
            f"{best_score}%): {title}"
        )
    else:
        print(f"No CV met threshold for: {title}")

    # Mark seen after evaluating all active CVs so we don't re-score forever.
    seen.add(link)
    return sent


def run_rss_feeds(seen: set[str], active_cvs: list[dict] | None = None) -> int:
    """Fetch Google Alerts RSS feeds and process only items newer than the cursor.

    Stops each feed when an entry.link matches Last Processed URL (from CV Vault).
    After all feeds, PATCHes Last Processed URL on active CV rows to the newest
    item from the first non-empty feed. Empty/None cursor = first run
    (process all current items, then save the newest URL).
    """
    feed_urls = load_feed_urls()
    if not feed_urls:
        print("No RSS_FEED_URLS configured; skipping RSS check.")
        return 0

    cvs = active_cvs if active_cvs is not None else load_active_cvs()
    last_processed_url = _shared_last_processed_url(cvs)
    if last_processed_url:
        print(f"RSS cursor (Last Processed URL): {last_processed_url}")
    else:
        print(
            "RSS cursor empty — first run (or unset). "
            "Processing current feed items, then saving newest URL."
        )

    new_alerts = 0
    newest_to_persist: str | None = None

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
        entries = list(feed.entries or [])
        print(
            f"[{source}] Fetched {len(entries)} "
            f"entr{'y' if len(entries) == 1 else 'ies'}."
        )
        if not entries:
            continue

        newest_link = (entries[0].get("link") or "").strip()
        if newest_to_persist is None and newest_link:
            newest_to_persist = newest_link

        for entry in entries:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not link:
                continue

            # Newest-first feed: hitting the saved cursor means older items
            # were already handled in a previous run — stop without Gemini.
            if last_processed_url and link == last_processed_url:
                print(
                    f"[{source}] Reached Last Processed URL; "
                    "stopping feed (no re-processing)."
                )
                break

            description = entry_description(entry)
            if process_job(
                title,
                link,
                source,
                seen,
                description=description,
                active_cvs=cvs,
            ):
                new_alerts += 1

    if newest_to_persist:
        if newest_to_persist != last_processed_url:
            update_last_processed_url(cvs, newest_to_persist)
        else:
            print(f"Last Processed URL already up to date ({newest_to_persist}).")
    else:
        print("No RSS items found; leaving Last Processed URL unchanged.")

    return new_alerts


def run_past_week_backfill(
    seen: set[str],
    days: int,
    active_cvs: list[dict] | None = None,
) -> int:
    jobs = fetch_google_jobs(days=days)
    cvs = active_cvs if active_cvs is not None else load_active_cvs()
    new_alerts = 0
    for job in jobs:
        if process_job(
            job.title,
            job.link,
            job.source,
            seen,
            description=job.title,
            active_cvs=cvs,
        ):
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
        print("Loading active CVs from Notion CV Vault...")
        active_cvs = load_active_cvs()
        seen = load_seen_jobs()
        print(f"Dedup state: {len(seen)} previously seen link(s).")
        print("Checking Google Alerts RSS feeds...")
        new_alerts = run_rss_feeds(seen, active_cvs=active_cvs)

        if args.backfill_days > 0:
            new_alerts += run_past_week_backfill(
                seen,
                days=args.backfill_days,
                active_cvs=active_cvs,
            )

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
