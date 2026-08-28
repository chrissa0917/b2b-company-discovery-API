from __future__ import annotations

import asyncio
import json

from .verified_enricher import enrich_record

CASES = [
    {"Company": "RobotsInternational.com", "Website URL": "https://www.robotsinternational.com"},
    {"Company": "Toshiba", "Website URL": "https://global.toshiba"},
    {"Company": "SharkNinja, Inc.", "Website URL": "https://sharkninja.com"},
    {"Company": "Moyotech", "Website URL": "https://moyotech.com"},
    {"Company": "Joby Aviation", "Website URL": "https://jobyaviation.com"},
    {"Company": "NAVER LABS", "Website URL": "https://naverlabs.com"},
    {"Company": "FarmBot", "Website URL": "https://farm.bot"},
    {"Company": "Humanoid Index", "Website URL": "https://humanoidindex.org"},
    {"Company": "Badger Technologies", "Website URL": "https://www.badger-technologies.com"},
    {"Company": "Berkshire Grey", "Website URL": "https://berkshiregrey.com"},
]
POSITIONS = [
    "Marketing",
    "PR & Communications",
    "Partnerships & Business Development",
    "Leadership / Founder",
]


async def run_one(row: dict) -> dict:
    try:
        result = await asyncio.wait_for(
            enrich_record(
                row,
                requested_positions=POSITIONS,
                use_search=True,
                deep_verify=False,
            ),
            timeout=30,
        )
        return result
    except Exception as exc:
        return {
            "Company": row["Company"],
            "Website URL": row["Website URL"],
            "Error": str(exc)[:300],
        }


async def main() -> None:
    results = []
    for row in CASES:
        result = await run_one(row)
        results.append(result)
        print("PERSON_FIRST_ROW " + json.dumps(result, ensure_ascii=False), flush=True)

    people = [r for r in results if r.get("Contact Name")]
    candidates = [r for r in results if r.get("Review Candidate Email") or r.get("Verified Email")]
    junk_titles = [r for r in results if len(str(r.get("Job Title") or "")) > 120]
    summary = {
        "tested": len(results),
        "with_clean_person": len(people),
        "with_person_email_candidate": len(candidates),
        "titles_over_120_chars": len(junk_titles),
        "errors": sum(1 for r in results if r.get("Error")),
    }
    print("PERSON_FIRST_SUMMARY " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
