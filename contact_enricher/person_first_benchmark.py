from __future__ import annotations

import asyncio
import json
import time

from .crosslinked_people import enrich_person_crosslinked

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


async def main() -> None:
    # CrossLinked opens Google/Bing searches itself. Keep concurrency low so the
    # benchmark does not look like a bursty bot to the search engines.
    semaphore = asyncio.Semaphore(2)

    async def run_one(row: dict) -> dict:
        async with semaphore:
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    enrich_person_crosslinked(row["Company"], row["Website URL"], POSITIONS),
                    timeout=15,
                )
                result["Elapsed Seconds"] = round(time.perf_counter() - started, 2)
                return result
            except asyncio.TimeoutError:
                return {
                    "Company": row["Company"],
                    "Website URL": row["Website URL"],
                    "Status": "timeout",
                    "Elapsed Seconds": round(time.perf_counter() - started, 2),
                }
            except Exception as exc:
                return {
                    "Company": row["Company"],
                    "Website URL": row["Website URL"],
                    "Status": "error",
                    "Error": repr(exc)[:300],
                    "Elapsed Seconds": round(time.perf_counter() - started, 2),
                }

    tasks = [asyncio.create_task(run_one(row)) for row in CASES]
    results = []
    for task in asyncio.as_completed(tasks):
        result = await task
        results.append(result)
        print("PERSON_FIRST_ROW " + json.dumps(result, ensure_ascii=False), flush=True)

    people = [r for r in results if r.get("Contact Name")]
    candidates = [r for r in results if r.get("Review Candidate Email")]
    junk_titles = [r for r in results if len(str(r.get("Job Title") or "")) > 120]
    generic_candidates = [
        r for r in candidates
        if str(r.get("Review Candidate Email") or "").split("@", 1)[0].lower()
        in {"info", "hello", "contact", "support", "sales", "marketing", "press", "media"}
    ]
    summary = {
        "tested": len(results),
        "with_clean_person": len(people),
        "with_person_email_candidate": len(candidates),
        "generic_primary_candidates": len(generic_candidates),
        "titles_over_120_chars": len(junk_titles),
        "timeouts": sum(1 for r in results if r.get("Status") == "timeout"),
        "errors": sum(1 for r in results if r.get("Status") == "error"),
        "average_seconds": round(sum(float(r.get("Elapsed Seconds") or 0) for r in results) / len(results), 2),
    }
    print("PERSON_FIRST_SUMMARY " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
