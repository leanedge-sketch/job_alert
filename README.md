# Job Alert Bot (free tier)

Python bot that discovers IT support roles in the UAE, scores them with Gemini
against Notion CV Vault resumes, logs hits to Notion, and sends Telegram alerts.

Free discovery sources (no paid APIs):

1. **Indeed / Bayt / Naukrigulf** via Google News RSS + URL decoder
   (those sites block direct scrapes with Cloudflare).
2. **LinkedIn** public guest search.
3. **Google Alerts RSS** — best-effort; often empty for days.

No paid search APIs. Google Custom Search JSON API is closed to new Cloud
customers, so past-week discovery is manual (`backfill_week.json`).

## Reliability model

The free tiers involved are small, so the bot is built to degrade predictably
rather than fail loudly:

- **Gemini self-rationing.** Free tier allows ~20 requests/day. The bot tracks
  its own usage in `bot_state.json` and stops at `GEMINI_MAX_CALLS_PER_DAY`
  (default 18), with `GEMINI_MAX_CALLS_PER_RUN` (default 8) bounding a single
  run and `GEMINI_PACK_RESERVE` held back for button taps.
- **No job is silently lost.** A link is only recorded in `seen_jobs.json` after
  a real decision (alert or below threshold). Jobs skipped because Gemini was
  unavailable stay queued for the next run.
- **No garbage alerts.** A quota or parse failure never produces a 0% "match".
  `MIN_ALERT_SCORE` (default 40) is a floor under each CV's Notion threshold.
- **Buttons survive lost state.** Pack requests are recovered from the Telegram
  message itself if `pack_requests.json` is gone, and consumed updates are
  acknowledged server-side so they are never replayed.
- **Heartbeat.** Every `HEARTBEAT_HOURS` (default 24) the bot sends a silent
  Telegram digest — sources checked, jobs scored, quota used, warnings — so a
  quiet day is distinguishable from a broken bot.

## Setup

1. Copy `.env.example` to `.env` and fill in:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `RSS_FEED_URLS` (comma-separated Google Alert RSS URLs)
   - `NOTION_API_TOKEN`, `NOTION_DATABASE_ID`, `NOTION_CV_VAULT_ID`
   - `GEMINI_API_KEY`
2. Install deps: `pip install -r requirements.txt`
3. Run once: `python main.py`

Job titles, locations and blocked words all come from `config.json`; the
LinkedIn source builds its queries from the same arrays, so editing that one
file changes both sources.

### Google Alerts (optional secondary source)

Create one alert per board with **plain** queries (no markdown), for example:

- `"IT Support" OR "Help Desk" OR "System Administrator" site:ae.indeed.com`
- `"IT Support" OR Helpdesk OR "System Administrator" site:linkedin.com/jobs`
- `"IT Support" OR Helpdesk site:bayt.com`
- `"IT Support" OR Helpdesk site:naukrigulf.com`

Delivery: **RSS** + **As-it-happens**. Open each feed URL in a browser and
confirm `<entry>` items appear, then put the URLs in `RSS_FEED_URLS`.

### Free past-week backfill

1. Open the manual search links from `python main.py --backfill-days 7`
2. Copy matching jobs into `backfill_week.json` as
   `{"source","title","link"}` objects
3. Run: `python send_backfill.py`

## GitHub Actions

The workflow runs every 30 minutes (and on manual dispatch). Overlapping runs
are blocked by a `concurrency` group so alerts and quota are never doubled, and
each run is capped at 20 minutes.

State (`seen_jobs.json`, `pack_requests.json`, `bot_state.json`) is carried
between runs by the Actions cache. Notion is the durable backstop for dedup, so
a cold cache causes no duplicate alerts.

Add these repository secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `RSS_FEED_URLS`
- `NOTION_API_TOKEN`
- `NOTION_DATABASE_ID`
- `NOTION_CV_VAULT_ID`
- `GEMINI_API_KEY`

Optional repository **variables** (not secrets) override the tuning defaults:
`GEMINI_MAX_CALLS_PER_DAY`, `GEMINI_MAX_CALLS_PER_RUN`, `MIN_ALERT_SCORE`,
`LINKEDIN_SEARCH_HOURS`, `LINKEDIN_MAX_JOBS`, `HEARTBEAT_HOURS`.

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

```bash
python main.py
python main.py --heartbeat         # force a Telegram status digest
python main.py --backfill-days 7   # prints free manual search links
python send_backfill.py            # process curated backfill_week.json
```

### Troubleshooting

| Symptom | Where to look |
| --- | --- |
| No alerts at all | Heartbeat digest: it names the source counts and warnings |
| `Gemini skipped: daily cap ... reached` | Quota rationing worked; jobs are queued, not lost |
| `CV Vault: no readable text for X` | Attach a text-based PDF, paste the CV into the page body, or untick `Active` |
| `All Google Alerts feeds returned 0 entries` | Expected; LinkedIn is the primary source |
| Want a job re-scored | Remove its link from `seen_jobs.json` and delete its Notion row |

Alerts are **score-only** by default (saves Gemini quota). Each Telegram alert includes a
**Generate cover letter + CV** button; packs are generated only when you tap it, and are
picked up on the next `main.py` / Actions run (every ~30 minutes).
