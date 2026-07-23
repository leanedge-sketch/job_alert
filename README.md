# Job Alert Bot

Python bot that checks Google Alert RSS feeds for matching IT support roles and sends Telegram notifications.

## Setup

1. Copy `.env.example` to `.env` and fill in:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `RSS_FEED_URLS` (comma-separated Google Alert RSS URLs)
2. Install deps: `pip install -r requirements.txt`
3. Run once: `python main.py`

## GitHub Actions

The workflow runs every 15 minutes (and on manual dispatch).

Add these repository secrets:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `RSS_FEED_URLS`
- `NOTION_API_TOKEN`
- `NOTION_DATABASE_ID`
- `NOTION_CV_VAULT_ID`
- `GEMINI_API_KEY`

Notion Job Tracker properties must be named exactly:
- `Position` (Title)
- `Link` (URL)
- `Date Found` (Date)
- `Status` (Select; default `To Apply`)
- `Match Score` (Number)
- `Keywords` (Multi-select)
- `Applied` (Checkbox; default unchecked)
- `Note` (Rich text; set to `Matched for: [Applicant Name]`)

Notion CV Vault properties:
- Title property (any name) for the applicant name
- `Active` (Checkbox) — set True to include that CV
- `Minimum Score` (Number) — only log jobs with match_score >= this value
- `CV File` (Files & media) — optional PDF resume; preferred over page body
- `Last Processed URL` (URL) — RSS cursor; leave empty on first run
- Put resume text in the page body as a fallback when no PDF is attached

Also keep optional local fallback `resume.txt` if no active CV Vault rows are found.

## Local runs

For a one-off check: `python main.py`

Automated checks run via GitHub Actions every 15 minutes.
