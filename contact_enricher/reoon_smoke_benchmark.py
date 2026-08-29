from __future__ import annotations

import asyncio
import json
import time

from .ddgs_people import enrich_person_ddgs
from .reoon_verifier import check_reoon_balance, verify_email_reoon

POSITIONS = [
    "Marketing",
    "PR & Communications",
    "Partnerships & Business Development",
    "Leadership / Founder",
]

# Same companies used in the previous verifier smoke test so provider results
# are directly comparable. The run stops after 12 verification attempts.
CASES = [
    ("SharkNinja, Inc.", "https://sharkninja.com"),
    ("Joby Aviation", "https://jobyaviation.com"),
    ("Badger Technologies", "https://www.badger-technologies.com"),
    ("Ozobot", "https://ozobot.com"),
    ("Aigen", "https://aigen.com"),
    ("Running Brains Robotics", "https://www.runningbrainsrobotics.com"),
    ("Pudu Robotics", "https://pudurobotics.com"),
    ("Grand View Research", "https://www.grandviewresearch.com"),
]
MAX_VERIFICATION_ATTEMPTS = 12


async def main() -> None:
    balance_before = await check_reoon_balance()
    print("REOON_BALANCE_BEFORE " + json.dumps(balance_before), flush=True)
    if not balance_before.get("configured"):
        print("REOON_SMOKE_SUMMARY " + json.dumps({
            "passed_verifier": False,
            "reason": "reoon_not_configured",
            "balance": balance_before,
        }), flush=True)
        return

    rows: list[dict] = []
    attempts_used = 0

    for company, website in CASES:
        if attempts_used >= MAX_VERIFICATION_ATTEMPTS:
            break
        started = time.perf_counter()
        try:
            discovery = await asyncio.wait_for(
                enrich_person_ddgs(company, website, POSITIONS), timeout=18
            )
        except Exception as exc:
            row = {
                "company": company,
                "website": website,
                "status": "discovery_error",
                "error": str(exc)[:250],
                "elapsed_seconds": round(time.perf_counter() - started, 2),
            }
            rows.append(row)
            print("REOON_SMOKE_ROW " + json.dumps(row, ensure_ascii=False), flush=True)
            continue

        candidates = [
            item.strip()
            for item in str(discovery.get("Candidate Emails") or "").split(";")
            if item.strip()
        ][:3]

        verification_attempts: list[dict] = []
        verified_email = ""
        review_email = ""
        selected_verdict = "not_run"

        for email in candidates:
            if attempts_used >= MAX_VERIFICATION_ATTEMPTS:
                break
            result = await verify_email_reoon(email, mode="power")
            attempts_used += 1
            verification_attempts.append(result)
            verdict = str(result.get("verdict") or "unknown").lower()

            if verdict == "valid":
                verified_email = email
                selected_verdict = "valid"
                break
            if verdict in {"catch_all", "risky", "unknown"} and not review_email:
                review_email = email
                selected_verdict = verdict
            elif selected_verdict == "not_run":
                selected_verdict = verdict

        ready = bool(verified_email)
        if not ready and candidates and not review_email and verification_attempts:
            selected_verdict = "invalid"

        row = {
            "company": company,
            "website": website,
            "contact_name": discovery.get("Contact Name", ""),
            "job_title": discovery.get("Job Title", ""),
            "linkedin_url": discovery.get("LinkedIn URL", ""),
            "candidate_emails": candidates,
            "verification_attempts": verification_attempts,
            "verified_email": verified_email,
            "review_email": "" if ready else review_email,
            "verification_verdict": selected_verdict,
            "ready_to_email": "YES" if ready else "NO",
            "addresses_checked": len(verification_attempts),
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "status": "complete",
        }
        rows.append(row)
        print("REOON_SMOKE_ROW " + json.dumps(row, ensure_ascii=False), flush=True)

    attempts = [a for r in rows for a in r.get("verification_attempts", [])]
    verdicts = [str(a.get("verdict") or "unknown").lower() for a in attempts]
    errors = [a for a in attempts if a.get("error")]
    summary = {
        "companies_tested": len(rows),
        "contacts_found": sum(1 for r in rows if r.get("contact_name")),
        "addresses_checked": len(attempts),
        "valid": sum(1 for v in verdicts if v == "valid"),
        "catch_all": sum(1 for v in verdicts if v == "catch_all"),
        "risky": sum(1 for v in verdicts if v == "risky"),
        "invalid": sum(1 for v in verdicts if v == "invalid"),
        "unknown": sum(1 for v in verdicts if v == "unknown"),
        "ready_to_email": sum(1 for r in rows if r.get("ready_to_email") == "YES"),
        "api_errors": len(errors),
        "max_attempts": MAX_VERIFICATION_ATTEMPTS,
    }
    summary["passed_verifier"] = bool(
        summary["addresses_checked"] > 0
        and summary["api_errors"] == 0
        and (summary["valid"] + summary["catch_all"] + summary["risky"] + summary["invalid"]) > 0
    )
    print("REOON_SMOKE_SUMMARY " + json.dumps(summary), flush=True)

    balance_after = await check_reoon_balance()
    print("REOON_BALANCE_AFTER " + json.dumps(balance_after), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
