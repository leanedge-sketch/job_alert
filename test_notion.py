"""One-off Notion + Telegram integration test for multi-CV triage."""

from main import (
    add_job_to_notion,
    analyze_job_with_gemini,
    format_job_message,
    load_active_cvs,
    parse_job_details,
    send_telegram_message,
)

TEST_TITLE = "IT Support Specialist (CV Threshold Test)"
TEST_URL = "https://ae.indeed.com/viewjob?jk=cv-threshold-test"
TEST_SOURCE = "Indeed"
TEST_DESCRIPTION = (
    "Location: Dubai. Salary: AED 6,000 per month. "
    "Looking for IT Support and System Administration experience, "
    "helpdesk ticketing, Active Directory, and Windows troubleshooting."
)


def main() -> None:
    details = parse_job_details(TEST_TITLE, TEST_DESCRIPTION)
    active_cvs = load_active_cvs()
    print(
        "Active CVs:",
        [
            {"name": cv["name"], "minimum_score": cv.get("minimum_score", 0)}
            for cv in active_cvs
        ],
    )

    for cv in active_cvs:
        analysis = analyze_job_with_gemini(
            TEST_TITLE,
            TEST_DESCRIPTION,
            candidate_resume=cv["text"],
        )
        match_score = int(analysis["match_score"])
        minimum_score = int(cv.get("minimum_score") or 0)
        print(
            f"Gemini ({cv['name']}): score={match_score} "
            f"minimum={minimum_score} keywords={analysis['keywords']}"
        )
        if match_score < minimum_score:
            print(f"Skipped below threshold for {cv['name']}")
            continue

        note = f"Matched for: {cv['name']}"
        add_job_to_notion(
            TEST_TITLE,
            TEST_URL,
            TEST_SOURCE,
            match_score=match_score,
            keywords=analysis["keywords"],
            note=note,
        )
        message = format_job_message(
            TEST_TITLE,
            TEST_SOURCE,
            TEST_URL,
            details["location"],
            details["salary"],
            match_score,
            analysis["keywords"],
            cv_name=cv["name"],
        )
        send_telegram_message(message, disable_web_page_preview=False)

    print("Done.")


if __name__ == "__main__":
    main()
