from __future__ import annotations

import asyncio
import csv
import json
import os
from pathlib import Path

import uvicorn
from fastapi.responses import FileResponse, JSONResponse

from .main import app
from .reoon_verifier import check_reoon_balance
from .verified_enricher import enrich_rows

BASE = Path(__file__).resolve().parent
BATCH = BASE / "benchmarks" / "batch-2-of-5.csv"
OUT = Path(os.getenv("DATA_DIR", str(BASE.parent / "data"))) / "batch-2-live-contact-enrichment-results.csv"
SUMMARY = OUT.with_suffix(".summary.json")

CONTACT_AREAS = [
    "Marketing",
    "PR & Communications",
    "Partnerships & Business Development",
]


def _load_rows() -> list[dict]:
    with BATCH.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        {"Company": str(row.get("Company") or "").strip(), "Website URL": str(row.get("Website URL") or "").strip()}
        for row in rows
    ]


def _compact(index: int, result: dict) -> dict:
    keys = [
        "Company", "Website URL", "Contact Name", "Job Title", "Verified Email",
        "Generic Company Email", "Usable Contact Email", "Contact Type", "Verification Level",
        "Email Status", "Ready to Email", "Review Candidate Email", "LinkedIn URL",
        "Email Source", "Generic Email Source", "Contact Source", "Email Verification Score",
        "Email Pattern", "Mail Domain Used", "Pattern Evidence", "Pages Checked", "Addresses Checked",
    ]
    return {"benchmark_row": index + 107, **{key: result.get(key, "") for key in keys}}


def _summary(results: list[dict], balance_before: dict, balance_after: dict) -> dict:
    verified = sum(1 for row in results if row.get("Verified Email"))
    generic = sum(1 for row in results if row.get("Contact Type") == "Company fallback" and row.get("Usable Contact Email"))
    usable = sum(1 for row in results if row.get("Usable Contact Email"))
    people = sum(1 for row in results if row.get("Contact Name"))
    review = sum(1 for row in results if row.get("Review Candidate Email"))
    calls = sum(int(row.get("Addresses Checked") or 0) for row in results)
    total = len(results)
    return {
        "batch": 2,
        "benchmark_rows": "107-212",
        "companies": total,
        "people_found": people,
        "verified_named_person": verified,
        "company_fallback": generic,
        "usable_contacts": usable,
        "review_person_candidates": review,
        "no_usable_contact": total - usable,
        "reoon_attempts": calls,
        "person_found_rate": round(people / total, 4) if total else 0,
        "verified_person_rate": round(verified / total, 4) if total else 0,
        "usable_contact_coverage": round(usable / total, 4) if total else 0,
        "reoon_calls_per_verified_person": round(calls / verified, 3) if verified else None,
        "balance_before": {
            "daily": balance_before.get("remaining_daily_credits"),
            "instant": balance_before.get("remaining_instant_credits"),
        },
        "balance_after": {
            "daily": balance_after.get("remaining_daily_credits"),
            "instant": balance_after.get("remaining_instant_credits"),
        },
    }


async def run_batch() -> None:
    rows = _load_rows()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    balance_before = await check_reoon_balance()
    print("BATCH2_BALANCE_BEFORE=" + json.dumps({
        "daily": balance_before.get("remaining_daily_credits"),
        "instant": balance_before.get("remaining_instant_credits"),
    }), flush=True)

    def result_cb(index: int, result: dict, completed: int, total: int) -> None:
        print("BATCH2_ROW_JSON=" + json.dumps(_compact(index, result), ensure_ascii=False), flush=True)

    results = await enrich_rows(
        rows,
        requested_positions=CONTACT_AREAS,
        concurrency=3,
        use_search=True,
        max_pages=10,
        deep_verify=True,
        result_cb=result_cb,
    )

    fields: list[str] = []
    for row in results:
        for key in row.keys():
            if key not in fields:
                fields.append(key)
    with OUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    balance_after = await check_reoon_balance()
    summary = _summary(results, balance_before, balance_after)
    SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("BATCH2_SUMMARY_JSON=" + json.dumps(summary), flush=True)
    print(f"BATCH2_RESULTS_PATH={OUT}", flush=True)


@app.on_event("startup")
async def start_batch() -> None:
    asyncio.create_task(run_batch())


@app.get("/benchmark/batch2.csv")
async def benchmark_batch2_csv():
    if not OUT.exists():
        return JSONResponse({"status": "running"}, status_code=202)
    return FileResponse(OUT, media_type="text/csv", filename=OUT.name)


@app.get("/benchmark/batch2-summary")
async def benchmark_batch2_summary():
    if not SUMMARY.exists():
        return JSONResponse({"status": "running"}, status_code=202)
    return JSONResponse(json.loads(SUMMARY.read_text(encoding="utf-8")))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
