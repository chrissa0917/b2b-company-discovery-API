from __future__ import annotations

import asyncio
import json
import time

from .open_source_email_scraper import scrape_public_contact_data


TEST_COMPANIES = [
    ("RobotsInternational.com", "https://www.robotsinternational.com"),
    ("Toshiba", "https://global.toshiba"),
    ("SharkNinja, Inc.", "https://sharkninja.com"),
    ("Moyotech", "https://moyotech.com"),
    ("Joby Aviation", "https://jobyaviation.com"),
    ("NAVER LABS", "https://naverlabs.com"),
    ("FarmBot", "https://farm.bot"),
    ("Berkshire Grey", "https://berkshiregrey.com"),
    ("Grand View Research", "https://www.grandviewresearch.com"),
    ("Humanoid Index", "https://humanoidindex.org"),
]


async def one(company: str, website: str) -> dict:
    started = time.perf_counter()
    try:
        result = await scrape_public_contact_data(
            website,
            timeout_seconds=25,
            depth=1,
            max_links_from_page=8,
        )
        return {
            "company": company,
            "website": website,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "emails": result.emails,
            "linkedin_urls": result.linkedin_urls,
            "pages_checked": result.pages_checked,
            "status": "ok",
        }
    except asyncio.TimeoutError:
        return {
            "company": company,
            "website": website,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "emails": [],
            "linkedin_urls": [],
            "pages_checked": [],
            "status": "timeout",
        }
    except Exception as exc:
        return {
            "company": company,
            "website": website,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "emails": [],
            "linkedin_urls": [],
            "pages_checked": [],
            "status": "error",
            "error": str(exc)[:300],
        }


async def main() -> None:
    print("OPEN_SOURCE_EMAIL_BENCHMARK_START", flush=True)
    results = []
    for company, website in TEST_COMPANIES:
        result = await one(company, website)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)

    successful = [r for r in results if r["status"] == "ok"]
    with_email = [r for r in successful if r["emails"]]
    timeouts = [r for r in results if r["status"] == "timeout"]
    errors = [r for r in results if r["status"] == "error"]
    summary = {
        "tested": len(results),
        "completed": len(successful),
        "with_email": len(with_email),
        "timeouts": len(timeouts),
        "errors": len(errors),
        "email_coverage_percent": round((len(with_email) / len(results)) * 100, 1) if results else 0,
        "average_seconds": round(sum(r["elapsed_seconds"] for r in results) / len(results), 2) if results else 0,
    }
    print("OPEN_SOURCE_EMAIL_BENCHMARK_SUMMARY " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
