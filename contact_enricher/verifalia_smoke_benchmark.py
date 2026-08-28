from __future__ import annotations

import asyncio
import json
import time

from .ddgs_people import enrich_person_ddgs
from .verifalia_verifier import verify_emails_verifalia

POSITIONS = [
    "Marketing",
    "PR & Communications",
    "Partnerships & Business Development",
    "Leadership / Founder",
]

# Eight companies that produced clean named contacts in earlier person-first runs.
# At most three candidate emails are verified per company = at most 24 free credits.
CASES = [
    ("SharkNinja, Inc.", "https://sharkninja.com"),
    ("Joby Aviation", "https://jobyaviation.com"),
    ("Grand View Research", "https://www.grandviewresearch.com"),
    ("Badger Technologies", "https://www.badger-technologies.com"),
    ("Pudu Robotics", "https://pudurobotics.com"),
    ("Ozobot", "https://ozobot.com"),
    ("Aigen", "https://aigen.com"),
    ("Running Brains Robotics", "https://www.runningbrainsrobotics.com"),
]


async def run_case(company: str, website: str) -> dict:
    started = time.perf_counter()
    try:
        discovery = await asyncio.wait_for(
            enrich_person_ddgs(company, website, POSITIONS), timeout=18
        )
    except Exception as exc:
        return {
            "company": company,
            "website": website,
            "status": "discovery_error",
            "error": str(exc)[:250],
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }

    candidates = [
        item.strip()
        for item in str(discovery.get("Candidate Emails") or "").split(";")
        if item.strip()
    ][:3]
    verification = await verify_emails_verifalia(candidates) if candidates else {}

    selected_email = ""
    selected_verdict = "not_run"
    selected_classification = ""
    selected_status = ""
    attempts: list[dict] = []
    review_email = ""

    for email in candidates:
        item = verification.get(email, {"email": email, "verdict": "unknown", "provider": "verifalia"})
        attempts.append(item)
        verdict = str(item.get("verdict") or "unknown").lower()
        if verdict == "valid" and not selected_email:
            selected_email = email
            selected_verdict = verdict
            selected_classification = str(item.get("classification") or "")
            selected_status = str(item.get("status") or "")
            break
        if verdict in {"risky", "catch_all", "unknown"} and not review_email:
            review_email = email
            selected_verdict = verdict
            selected_classification = str(item.get("classification") or "")
            selected_status = str(item.get("status") or "")

    ready = bool(selected_email and selected_verdict == "valid")
    if not ready and not review_email and candidates:
        selected_verdict = "invalid"

    return {
        "company": company,
        "website": website,
        "contact_name": discovery.get("Contact Name", ""),
        "job_title": discovery.get("Job Title", ""),
        "linkedin_url": discovery.get("LinkedIn URL", ""),
        "candidate_emails": candidates,
        "verification_attempts": attempts,
        "verified_email": selected_email,
        "review_email": "" if ready else review_email,
        "verification_verdict": selected_verdict,
        "verification_classification": selected_classification,
        "verification_status": selected_status,
        "ready_to_email": "YES" if ready else "NO",
        "addresses_checked": len(attempts),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "status": "complete",
    }


async def main() -> None:
    results: list[dict] = []
    # Run sequentially to keep Verifalia free-plan usage predictable and avoid
    # racing multiple best-effort jobs against the same daily credit pool.
    for company, website in CASES:
        result = await run_case(company, website)
        results.append(result)
        print("VERIFALIA_SMOKE_ROW " + json.dumps(result, ensure_ascii=False), flush=True)

    attempts = [a for r in results for a in r.get("verification_attempts", [])]
    classifications = [str(a.get("classification") or "") for a in attempts]
    verdicts = [str(a.get("verdict") or "unknown") for a in attempts]
    summary = {
        "companies_tested": len(results),
        "contacts_found": sum(1 for r in results if r.get("contact_name")),
        "addresses_checked": len(attempts),
        "deliverable": sum(1 for c in classifications if c.lower() == "deliverable"),
        "risky": sum(1 for c in classifications if c.lower() == "risky"),
        "undeliverable": sum(1 for c in classifications if c.lower() == "undeliverable"),
        "unknown": sum(1 for c in classifications if c.lower() == "unknown" or not c),
        "ready_to_email": sum(1 for r in results if r.get("ready_to_email") == "YES"),
        "auth_or_http_errors": sum(1 for a in attempts if a.get("error")),
        "verifalia_valid_verdicts": sum(1 for v in verdicts if v == "valid"),
        "free_credit_ceiling": 24,
    }
    summary["passed_verifier"] = bool(
        summary["addresses_checked"] > 0
        and summary["auth_or_http_errors"] == 0
        and (summary["deliverable"] + summary["risky"] + summary["undeliverable"]) > 0
    )
    print("VERIFALIA_SMOKE_SUMMARY " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
