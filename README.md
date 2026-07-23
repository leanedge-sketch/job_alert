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

The workflow runs every 2 hours (and on manual dispatch).

Add these repository secrets:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `RSS_FEED_URLS`

## Local schedule (Windows)

`JobAlertBot` Task Scheduler job runs `run_bot.bat` every 30 minutes.
