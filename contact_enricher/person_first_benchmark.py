from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INPUT_PATH = Path(__file__).with_name("benchmark_batch_106.csv")
OUTPUT_DIR = Path("/app/output")
OUTPUT_CSV = OUTPUT_DIR / "person-first-benchmark-106.csv"
SUMMARY_JSON = OUTPUT_DIR / "person-first-benchmark-106-summary.json"
CONCURRENCY = 3
HARD_TIMEOUT_SECONDS = 48
GENERIC_LOCALS = {"info", "hello", "contact", "support", "sales", "marketing", "press", "media"}


def load_rows() -> list[dict]:
    with INPUT_PATH.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [
        {
            "Company": str(row.get("Company") or "").strip(),
            "Website URL": str(row.get("Website URL") or "").strip(),
        }
        for row in rows
        if str(row.get("Company") or "").strip()
    ]


async def run_one(index: int, row: dict, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "contact_enricher.person_first_worker",
            row["Company"],
            row["Website URL"],
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
                "Benchmark Row": index + 1,
                "Company": row["Company"],
                "Website URL": row["Website URL"],
                "Status": "timeout",
                "Elapsed Seconds": round(time.perf_counter() - started, 2),
                "Error": f"Hard process timeout after {HARD_TIMEOUT_SECONDS} seconds.",
                "Ready to Email": "NO",
            }

        elapsed = round(time.perf_counter() - started, 2)
        if process.returncode != 0:
            return {
                "Benchmark Row": index + 1,
                "Company": row["Company"],
                "Website URL": row["Website URL"],
                "Status": "error",
                "Elapsed Seconds": elapsed,
                "Error": stderr.decode("utf-8", errors="replace")[-500:],
                "Ready to Email": "NO",
            }

        lines = [line for line in stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1]) if lines else {}
        except Exception as exc:
            return {
                "Benchmark Row": index + 1,
                "Company": row["Company"],
                "Website URL": row["Website URL"],
                "Status": "error",
                "Elapsed Seconds": elapsed,
                "Error": f"worker output parse error: {exc}",
                "Ready to Email": "NO",
            }

        if not isinstance(payload, dict):
            payload = {}
        payload["Benchmark Row"] = index + 1
        payload["Status"] = "complete"
        payload["Elapsed Seconds"] = elapsed
        payload.setdefault("Ready to Email", "NO")
        return payload


async def run_benchmark() -> tuple[list[dict], dict]:
    rows = load_rows()
    semaphore = asyncio.Semaphore(CONCURRENCY)
    tasks = [
        asyncio.create_task(run_one(index, row, semaphore))
        for index, row in enumerate(rows)
    ]
    results: list[dict] = []
    for task in asyncio.as_completed(tasks):
        result = await task
        results.append(result)
        print("PERSON_FIRST_106_ROW " + json.dumps(result, ensure_ascii=False), flush=True)

    results.sort(key=lambda item: int(item.get("Benchmark Row") or 0))
    people = [r for r in results if r.get("Contact Name")]
    generated = [r for r in results if r.get("Candidate Emails")]
    public_evidence = [r for r in results if r.get("Public Email Evidence")]
    verified = [r for r in results if r.get("Verified Email")]
    review = [r for r in results if r.get("Email Status") == "Review"]
    invalid = [r for r in results if r.get("Email Status") == "Not valid"]
    not_checked = [r for r in results if r.get("Email Status") == "Not checked"]
    ready = [r for r in results if str(r.get("Ready to Email") or "").upper() == "YES"]
    primary_emails = [
        str(r.get("Verified Email") or r.get("Review Candidate Email") or "")
        for r in results
        if r.get("Verified Email") or r.get("Review Candidate Email")
    ]
    generic_candidates = [
        email for email in primary_emails
        if email.split("@", 1)[0].lower() in GENERIC_LOCALS
    ]
    junk_titles = [r for r in results if len(str(r.get("Job Title") or "")) > 120]
    elapsed = sorted(float(r.get("Elapsed Seconds") or 0) for r in results)
    p95_index = max(0, min(len(elapsed) - 1, int(round(0.95 * len(elapsed) + 0.5)) - 1)) if elapsed else 0

    summary = {
        "tested": len(results),
        "completed": sum(1 for r in results if r.get("Status") == "complete"),
        "with_clean_person": len(people),
        "with_generated_person_email_candidates": len(generated),
        "with_public_email_evidence": len(public_evidence),
        "verified_ready": len(ready),
        "verified_email_count": len(verified),
        "review_count": len(review),
        "invalid_count": len(invalid),
        "not_checked_count": len(not_checked),
        "verification_addresses_checked": sum(int(r.get("Addresses Checked") or 0) for r in results),
        "generic_primary_candidates": len(generic_candidates),
        "titles_over_120_chars": len(junk_titles),
        "timeouts": sum(1 for r in results if r.get("Status") == "timeout"),
        "errors": sum(1 for r in results if r.get("Status") == "error"),
        "average_seconds": round(sum(elapsed) / len(elapsed), 2) if elapsed else 0,
        "p95_seconds": elapsed[p95_index] if elapsed else 0,
        "max_seconds": max(elapsed) if elapsed else 0,
        "concurrency": CONCURRENCY,
        "hard_process_timeout_seconds": HARD_TIMEOUT_SECONDS,
        "verification_enabled": True,
        "input_file": INPUT_PATH.name,
    }
    print("PERSON_FIRST_106_SUMMARY " + json.dumps(summary), flush=True)
    return results, summary


def write_outputs(results: list[dict], summary: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    preferred_fields = [
        "Benchmark Row", "Company", "Website URL", "Contact Name", "Job Title",
        "LinkedIn URL", "Contact Source", "Verified Email", "Review Candidate Email",
        "Email Status", "Verification Verdict", "Verification Attempts", "Addresses Checked",
        "Email Confidence", "Email Pattern", "Approved Email Domains", "Public Email Evidence",
        "Candidate Emails", "People Found", "Pages Checked", "Ready to Email",
        "Status", "Elapsed Seconds", "Error",
    ]
    extras: list[str] = []
    for row in results:
        for key in row:
            if key not in preferred_fields and key not in extras:
                extras.append(key)
    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=preferred_fields + extras, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"PERSON_FIRST_106_CSV {OUTPUT_CSV}", flush=True)


async def main() -> None:
    results, summary = await run_benchmark()
    write_outputs(results, summary)


def serve_outputs() -> None:
    os.chdir(OUTPUT_DIR)
    port = int(os.getenv("PORT", "8080"))
    print(f"BENCHMARK_FILE_SERVER http://0.0.0.0:{port}/person-first-benchmark-106.csv", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler).serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
    serve_outputs()
