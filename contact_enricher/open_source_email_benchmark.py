from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import sys
import time
from pathlib import Path


BATCH_FILE = Path(__file__).with_name("benchmark_batch_106.csv")
CONCURRENCY = 4
HARD_TIMEOUT_SECONDS = 15


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


async def one(company: str, website: str, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "contact_enricher.open_source_email_worker",
            website,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=HARD_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return {
                "company": company,
                "website": website,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "emails": [],
                "primary_email": "",
                "linkedin_urls": [],
                "pages_checked": [],
                "rejected_external_emails": [],
                "status": "timeout",
            }

        elapsed = round(time.perf_counter() - started, 2)
        if process.returncode != 0:
            return {
                "company": company,
                "website": website,
                "elapsed_seconds": elapsed,
                "emails": [],
                "primary_email": "",
                "linkedin_urls": [],
                "pages_checked": [],
                "rejected_external_emails": [],
                "status": "error",
                "error": stderr.decode("utf-8", errors="replace")[-300:],
            }

        lines = [line for line in stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1]) if lines else {}
        except Exception as exc:
            return {
                "company": company,
                "website": website,
                "elapsed_seconds": elapsed,
                "emails": [],
                "primary_email": "",
                "linkedin_urls": [],
                "pages_checked": [],
                "rejected_external_emails": [],
                "status": "error",
                "error": f"worker output parse error: {exc}",
            }

        emails = payload.get("emails") if isinstance(payload.get("emails"), list) else []
        return {
            "company": company,
            "website": website,
            "elapsed_seconds": elapsed,
            "emails": emails,
            "primary_email": emails[0] if emails else "",
            "linkedin_urls": payload.get("linkedin_urls") or [],
            "pages_checked": payload.get("pages_checked") or [],
            "rejected_external_emails": payload.get("rejected_external_emails") or [],
            "status": "ok",
        }


def emit_csv(results: list[dict]) -> None:
    output = io.StringIO(newline="")
    fieldnames = [
        "Company",
        "Website URL",
        "Status",
        "Elapsed Seconds",
        "Primary Email",
        "All Emails Found",
        "LinkedIn URLs",
        "Pages Checked",
        "Rejected External Emails",
        "Error",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in results:
        writer.writerow({
            "Company": row.get("company", ""),
            "Website URL": row.get("website", ""),
            "Status": row.get("status", ""),
            "Elapsed Seconds": row.get("elapsed_seconds", ""),
            "Primary Email": row.get("primary_email", ""),
            "All Emails Found": "; ".join(row.get("emails") or []),
            "LinkedIn URLs": "; ".join(row.get("linkedin_urls") or []),
            "Pages Checked": "; ".join(row.get("pages_checked") or []),
            "Rejected External Emails": "; ".join(row.get("rejected_external_emails") or []),
            "Error": row.get("error", ""),
        })
    encoded = base64.b64encode(output.getvalue().encode("utf-8-sig")).decode("ascii")
    print("OPEN_SOURCE_EMAIL_BENCHMARK_CSV_BASE64 " + encoded, flush=True)


async def main() -> None:
    companies = load_companies()
    print(
        "OPEN_SOURCE_EMAIL_BENCHMARK_START "
        + json.dumps({
            "batch_size": len(companies),
            "concurrency": CONCURRENCY,
            "browser_fallback": False,
            "verification": False,
            "hard_process_timeout_seconds": HARD_TIMEOUT_SECONDS,
        }),
        flush=True,
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)
    tasks = [
        asyncio.create_task(one(company, website, semaphore))
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
    elapsed = sorted(r["elapsed_seconds"] for r in results)
    p95_index = max(0, min(len(elapsed) - 1, int(round(0.95 * len(elapsed) + 0.5)) - 1)) if elapsed else 0

    summary = {
        "tested": len(results),
        "completed": len(successful),
        "with_email": len(with_email),
        "email_coverage_percent": round((len(with_email) / len(results)) * 100, 1) if results else 0,
        "timeouts": len(timeouts),
        "errors": len(errors),
        "average_seconds": round(sum(elapsed) / len(elapsed), 2) if elapsed else 0,
        "p95_seconds": elapsed[p95_index] if elapsed else 0,
        "max_seconds": max(elapsed) if elapsed else 0,
        "browser_fallback_enabled": False,
        "verification_enabled": False,
        "hard_process_timeout_seconds": HARD_TIMEOUT_SECONDS,
    }
    print("OPEN_SOURCE_EMAIL_BENCHMARK_SUMMARY " + json.dumps(summary), flush=True)
    emit_csv(results)


if __name__ == "__main__":
    asyncio.run(main())
