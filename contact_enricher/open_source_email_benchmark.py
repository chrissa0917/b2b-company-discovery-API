from __future__ import annotations

import asyncio
import csv
import json
import os
import time
from pathlib import Path

import httpx

from .open_source_email_scraper import scrape_public_contact_data


BATCH_FILE = Path(__file__).with_name("benchmark_batch_106.csv")
VERIFIER_BASE_URL = os.getenv(
    "EMAIL_VERIFIER_URL",
    "http://email-verifier.railway.internal:8080",
).rstrip("/")
VERIFIER_URL = (
    VERIFIER_BASE_URL
    if VERIFIER_BASE_URL.endswith("/v1/verify")
    else VERIFIER_BASE_URL + "/v1/verify"
)
CONCURRENCY = 4


def load_companies() -> list[tuple[str, str]]:
    companies: list[tuple[str, str]] = []
    with BATCH_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            company = (row.get("Company") or "").strip()
            website = (row.get("Website URL") or "").strip()
            if company and website:
                companies.append((company, website))
    return companies


async def verify_email(client: httpx.AsyncClient, email: str) -> dict:
    try:
        response = await client.post(VERIFIER_URL, json={"email": email})
        response.raise_for_status()
        payload = response.json()
        return {
            "verdict": str(payload.get("verdict") or "unknown"),
            "engine": str(payload.get("engine") or ""),
            "error": str(payload.get("error") or "")[:300],
        }
    except Exception as exc:
        return {
            "verdict": "verifier_error",
            "engine": "",
            "error": str(exc)[:300],
        }


async def one(
    company: str,
    website: str,
    semaphore: asyncio.Semaphore,
    verifier_client: httpx.AsyncClient,
) -> dict:
    async with semaphore:
        started = time.perf_counter()
        try:
            result = await scrape_public_contact_data(
                website,
                timeout_seconds=25,
                depth=1,
                max_links_from_page=8,
            )
            verification = {
                "verdict": "not_checked",
                "engine": "",
                "error": "",
            }
            primary_email = result.emails[0] if result.emails else ""
            if primary_email:
                verification = await verify_email(verifier_client, primary_email)

            return {
                "company": company,
                "website": website,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "emails": result.emails,
                "primary_email": primary_email,
                "verifier_verdict": verification["verdict"],
                "verifier_error": verification["error"],
                "linkedin_urls": result.linkedin_urls,
                "pages_checked": result.pages_checked,
                "used_browser_fallback": result.used_browser_fallback,
                "rejected_external_emails": result.rejected_external_emails or [],
                "status": "ok",
            }
        except asyncio.TimeoutError:
            return {
                "company": company,
                "website": website,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "emails": [],
                "primary_email": "",
                "verifier_verdict": "not_checked",
                "verifier_error": "",
                "linkedin_urls": [],
                "pages_checked": [],
                "used_browser_fallback": False,
                "rejected_external_emails": [],
                "status": "timeout",
            }
        except Exception as exc:
            return {
                "company": company,
                "website": website,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "emails": [],
                "primary_email": "",
                "verifier_verdict": "not_checked",
                "verifier_error": "",
                "linkedin_urls": [],
                "pages_checked": [],
                "used_browser_fallback": False,
                "rejected_external_emails": [],
                "status": "error",
                "error": str(exc)[:300],
            }


async def main() -> None:
    companies = load_companies()
    print(
        "OPEN_SOURCE_EMAIL_BENCHMARK_START "
        + json.dumps({"batch_size": len(companies), "concurrency": CONCURRENCY}),
        flush=True,
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=httpx.Timeout(22.0)) as verifier_client:
        tasks = [
            asyncio.create_task(one(company, website, semaphore, verifier_client))
            for company, website in companies
        ]
        results: list[dict] = []
        for task in asyncio.as_completed(tasks):
            result = await task
            results.append(result)
            print("BENCHMARK_ROW " + json.dumps(result, ensure_ascii=False), flush=True)

    successful = [r for r in results if r["status"] == "ok"]
    with_email = [r for r in successful if r["emails"]]
    timeouts = [r for r in results if r["status"] == "timeout"]
    errors = [r for r in results if r["status"] == "error"]
    valid = [r for r in with_email if r["verifier_verdict"] == "valid"]
    catch_all = [r for r in with_email if r["verifier_verdict"] == "catch_all"]
    unknown = [r for r in with_email if r["verifier_verdict"] == "unknown"]
    invalid = [r for r in with_email if r["verifier_verdict"] == "invalid"]
    verifier_errors = [r for r in with_email if r["verifier_verdict"] == "verifier_error"]
    browser_fallbacks = [r for r in results if r.get("used_browser_fallback")]

    elapsed = sorted(r["elapsed_seconds"] for r in results)
    p95_index = max(0, min(len(elapsed) - 1, int(round(0.95 * len(elapsed) + 0.5)) - 1)) if elapsed else 0

    summary = {
        "tested": len(results),
        "completed": len(successful),
        "with_email": len(with_email),
        "email_coverage_percent": round((len(with_email) / len(results)) * 100, 1) if results else 0,
        "timeouts": len(timeouts),
        "errors": len(errors),
        "verified_valid": len(valid),
        "verified_catch_all": len(catch_all),
        "verified_unknown": len(unknown),
        "verified_invalid": len(invalid),
        "verifier_errors": len(verifier_errors),
        "ready_to_email": len(valid),
        "browser_fallbacks": len(browser_fallbacks),
        "average_seconds": round(sum(elapsed) / len(elapsed), 2) if elapsed else 0,
        "p95_seconds": elapsed[p95_index] if elapsed else 0,
        "max_seconds": max(elapsed) if elapsed else 0,
    }
    print("OPEN_SOURCE_EMAIL_BENCHMARK_SUMMARY " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
