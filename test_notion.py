"""One-off Notion + Telegram integration test."""

from main import (
    add_job_to_notion,
    format_job_message,
    parse_job_details,
    send_telegram_message,
)

TEST_TITLE = "IT Support Specialist (Notion Test)"
TEST_URL = "https://ae.indeed.com/viewjob?jk=notion-test"
TEST_SOURCE = "Indeed"
TEST_DESCRIPTION = "Location: Dubai. Salary: AED 6,000 per month. IT Support role."


def main() -> None:
    details = parse_job_details(TEST_TITLE, TEST_DESCRIPTION)
    print("Parsed details:", details)

    add_job_to_notion(TEST_TITLE, TEST_URL, TEST_SOURCE)

    message = format_job_message(
        TEST_TITLE,
        TEST_SOURCE,
        TEST_URL,
        details["location"],
        details["salary"],
    )
    send_telegram_message(message, disable_web_page_preview=False)
    print("Telegram test message sent.")


if __name__ == "__main__":
    main()
