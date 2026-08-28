from __future__ import annotations

import asyncio
import csv
import json
import os
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .ddgs_people import enrich_person_ddgs

POSITIONS = [
    "Marketing",
    "PR & Communications",
    "Partnerships & Business Development",
    "Leadership / Founder",
]
INPUT_PATH = Path(__file__).with_name("benchmark_batch_106.csv")
OUTPUT_DIR = Path("/app/output")
OUTPUT_CSV = OUTPUT_DIR / "person-first-benchmark-106.csv"
SUMMARY_JSON = OUTPUT_DIR / "person-first-benchmark-106-summary.json"
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


async def run_benchmark() -> tuple[list[dict], dict]:
    rows = load_rows()
    semaphore = asyncio.Semaphore(3)

    async def run_one(index: int, row: dict) -> dict:
        async with semaphore:
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    enrich_person_ddgs(row["Company"], row["Website URL"], POSITIONS),
                    timeout=16,
                )
                result["Status"] = "complete"
            except asyncio.TimeoutError:
                result = {
                    "Company": row["Company"],
                    "Website URL": row["Website URL"],
                    "Status": "timeout",
                    "Error": "Company exceeded the 16-second benchmark limit.",
                }
            except Exception as exc:
                result = {
                    "Company": row["Company"],
                    "Website URL": row["Website URL"],
                    "Status": "error",
                    "Error": repr(exc)[:300],
                }
            result["Benchmark Row"] = index + 1
            result["Elapsed Seconds"] = round(time.perf_counter() - started, 2)
            print("PERSON_FIRST_106_ROW " + json.dumps(result, ensure_ascii=False), flush=True)
            return result

    tasks = [asyncio.create_task(run_one(index, row)) for index, row in enumerate(rows)]
    completed: list[dict] = []
    for task in asyncio.as_completed(tasks):
        completed.append(await task)

    completed.sort(key=lambda item: int(item.get("Benchmark Row") or 0))
    people = [r for r in completed if r.get("Contact Name")]
    candidates = [r for r in completed if r.get("Review Candidate Email")]
    public_evidence = [r for r in completed if r.get("Public Email Evidence")]
    generic_candidates = [
        r for r in candidates
        if str(r.get("Review Candidate Email") or "").split("@", 1)[0].lower() in GENERIC_LOCALS
    ]
    junk_titles = [r for r in completed if len(str(r.get("Job Title") or "")) > 120]
    elapsed = [float(r.get("Elapsed Seconds") or 0) for r in completed]

    summary = {
        "tested": len(completed),
        "with_clean_person": len(people),
        "with_person_email_candidate": len(candidates),
        "with_public_email_evidence": len(public_evidence),
        "generic_primary_candidates": len(generic_candidates),
        "titles_over_120_chars": len(junk_titles),
        "timeouts": sum(1 for r in completed if r.get("Status") == "timeout"),
        "errors": sum(1 for r in completed if r.get("Status") == "error"),
        "average_seconds": round(sum(elapsed) / len(elapsed), 2) if elapsed else 0,
        "max_seconds": round(max(elapsed), 2) if elapsed else 0,
        "positions": POSITIONS,
        "input_file": INPUT_PATH.name,
    }
    print("PERSON_FIRST_106_SUMMARY " + json.dumps(summary), flush=True)
    return completed, summary


def write_outputs(results: list[dict], summary: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    preferred_fields = [
        "Benchmark Row", "Company", "Website URL", "Contact Name", "Job Title",
        "LinkedIn URL", "Contact Source", "Review Candidate Email", "Email Confidence",
        "Email Pattern", "Approved Email Domains", "Public Email Evidence",
        "Candidate Emails", "People Found", "Pages Checked", "Ready to Email",
        "Status", "Elapsed Seconds", "Error",
    ]
    extra_fields = []
    for row in results:
        for key in row:
            if key not in preferred_fields and key not in extra_fields:
                extra_fields.append(key)
    fields = preferred_fields + extra_fields
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"PERSON_FIRST_106_CSV {OUTPUT_CSV}", flush=True)


def serve_outputs() -> None:
    os.chdir(OUTPUT_DIR)
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    print(f"Serving benchmark output on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


async def main() -> None:
    results, summary = await run_benchmark()
    write_outputs(results, summary)


if __name__ == "__main__":
    asyncio.run(main())
    serve_outputs()
