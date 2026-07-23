"""One-off: score a sample job with winner logic + cover letter / tailored CV."""

import time
from uuid import uuid4

from main import (
    add_job_to_notion,
    analyze_job_with_gemini,
    format_job_message,
    load_active_cvs,
    parse_job_details,
    send_telegram_message,
)

TEST_TITLE = "IT Support Specialist - Dubai"
TEST_URL = f"https://example.com/jobs/it-support-winner-test-{uuid4().hex[:8]}"
TEST_SOURCE = "Test Run"
TEST_DESCRIPTION = (
    "Location: Dubai, UAE. "
    "We are hiring an IT Support Specialist / Help Desk technician. "
    "Requirements: Active Directory, Windows 10/11 troubleshooting, "
    "ticketing systems (ServiceNow or Jira), Office 365 administration, "
    "hardware/software support, and strong customer service. "
    "3+ years IT Support or System Administration experience preferred."
)


def main() -> None:
    active_cvs = load_active_cvs()
    if not active_cvs:
        raise SystemExit("No active CVs loaded.")

    best_score = -1
    best_cv_name = None
    best_keywords: list[str] = []
    best_cover_letter = ""
    best_tailored_cv = ""

    for cv in active_cvs:
        print(f"Evaluating CV: {cv['name']} ({len(cv['text'])} chars)")
        analysis = analyze_job_with_gemini(
            TEST_TITLE,
            TEST_DESCRIPTION,
            candidate_resume=cv["text"],
        )
        match_score = int(analysis["match_score"])
        keywords = list(analysis.get("keywords") or [])
        cover_letter = str(analysis.get("cover_letter") or "")
        tailored_cv = str(analysis.get("tailored_cv") or "")
        minimum_score = int(cv.get("minimum_score") or 0)
        print(
            f"  score={match_score} min={minimum_score} "
            f"keywords={keywords} "
            f"cover_letter_chars={len(cover_letter)} "
            f"tailored_cv_chars={len(tailored_cv)}"
        )

        if match_score >= minimum_score and match_score > best_score:
            best_score = match_score
            best_cv_name = cv["name"]
            best_keywords = keywords
            best_cover_letter = cover_letter
            best_tailored_cv = tailored_cv
            print(f"  -> new winner: {best_cv_name} @ {best_score}%")

        time.sleep(5)

    if best_cv_name is None:
        print("No CV met threshold; nothing logged.")
        return

    print(f"Winner: {best_cv_name} ({best_score}%)")
    print("--- cover letter preview ---")
    print(best_cover_letter[:400] or "(empty)")
    print("--- tailored CV preview ---")
    print(best_tailored_cv[:400] or "(empty)")

    add_job_to_notion(
        TEST_TITLE,
        TEST_URL,
        TEST_SOURCE,
        match_score=best_score,
        keywords=best_keywords,
        note=f"Matched for: {best_cv_name}",
        cover_letter=best_cover_letter,
        tailored_cv=best_tailored_cv,
    )
    details = parse_job_details(TEST_TITLE, TEST_DESCRIPTION)
    send_telegram_message(
        format_job_message(
            TEST_TITLE,
            TEST_SOURCE,
            TEST_URL,
            details["location"],
            details["salary"],
            best_score,
            best_keywords,
            cv_name=best_cv_name,
        ),
        disable_web_page_preview=False,
    )
    print("Logged winner to Notion (with page body) and sent Telegram.")


if __name__ == "__main__":
    main()
