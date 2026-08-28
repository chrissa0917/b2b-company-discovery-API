from __future__ import annotations

import asyncio
import json
import sys

from .ddgs_people import enrich_person_ddgs
from .verified_enricher import verify_email_address

POSITIONS = [
    "Marketing",
    "PR & Communications",
    "Partnerships & Business Development",
    "Leadership / Founder",
]


async def verify_candidates(result: dict) -> dict:
    if not result.get("Contact Name"):
        result["Verified Email"] = ""
        result["Email Status"] = "Not checked"
        result["Verification Verdict"] = "not_run"
        result["Verification Attempts"] = ""
        result["Addresses Checked"] = 0
        result["Ready to Email"] = "NO"
        result["Review Candidate Email"] = ""
        return result

    candidates = [
        item.strip()
        for item in str(result.get("Candidate Emails") or "").split(";")
        if item.strip()
    ][:3]
    if not candidates:
        result["Verified Email"] = ""
        result["Email Status"] = "Not checked"
        result["Verification Verdict"] = "not_run"
        result["Verification Attempts"] = ""
        result["Addresses Checked"] = 0
        result["Ready to Email"] = "NO"
        result["Review Candidate Email"] = ""
        return result

    attempts: list[str] = []
    review_email = ""
    review_verdict = ""

    for email in candidates:
        data = await verify_email_address(email)
        verdict = str(data.get("verdict") or "unknown").lower()
        attempts.append(f"{email}={verdict}")

        if verdict == "valid":
            result["Verified Email"] = email
            result["Review Candidate Email"] = ""
            result["Email Status"] = "Verified"
            result["Verification Verdict"] = "valid"
            result["Verification Attempts"] = "; ".join(attempts)
            result["Addresses Checked"] = len(attempts)
            result["Ready to Email"] = "YES"
            return result

        if verdict in {"catch_all", "unknown", "not_configured"} and not review_email:
            review_email = email
            review_verdict = verdict

    result["Verified Email"] = ""
    result["Verification Attempts"] = "; ".join(attempts)
    result["Addresses Checked"] = len(attempts)
    result["Ready to Email"] = "NO"

    if review_email:
        result["Review Candidate Email"] = review_email
        result["Email Status"] = "Review"
        result["Verification Verdict"] = review_verdict or "unknown"
    else:
        result["Review Candidate Email"] = ""
        result["Email Status"] = "Not valid"
        result["Verification Verdict"] = "invalid"

    return result


async def main() -> None:
    company = sys.argv[1]
    website = sys.argv[2]
    result = await enrich_person_ddgs(company, website, POSITIONS)
    result = await verify_candidates(result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
