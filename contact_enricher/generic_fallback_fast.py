from __future__ import annotations

import asyncio
import csv
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .enricher import crawl_company
from .reoon_full_41_benchmark import CANDIDATES, choose_generic
from .reoon_verifier import check_reoon_balance

BASE = Path(__file__).resolve().parent
INPUT_PATH = BASE / "benchmark_batch_106.csv"
OUTPUT_DIR = Path("/app/output")
OUTPUT_CSV = OUTPUT_DIR / "reoon-41-verified-with-generic-fallback.csv"
SUMMARY_JSON = OUTPUT_DIR / "reoon-41-verified-with-generic-fallback-summary.json"

# Final Reoon Power results recovered directly from the completed 41-address run.
VALID = {9: 98, 61: 98, 67: 88, 89: 98, 98: 98}
CATCH_ALL = {3: 75, 25: 75, 46: 75, 56: 75, 65: 75, 69: 75, 85: 75, 90: 75, 97: 75}
UNKNOWN = {7, 8, 34, 35, 45, 52, 83}
INVALID = {1, 2, 5, 12, 14, 22, 26, 30, 32, 36, 38, 39, 40, 51, 53, 57, 66, 68, 76, 95}


def verdict_for(row_num: int) -> tuple[str, str, object]:
    if row_num in VALID:
        return "valid", "Ready", VALID[row_num]
    if row_num in CATCH_ALL:
        return "catch_all", "Review", CATCH_ALL[row_num]
    if row_num in UNKNOWN:
        return "unknown", "Review", ""
    if row_num in INVALID:
        return "invalid", "Reject", 3
    return "not_run", "Not checked", ""


def load_rows() -> list[dict]:
    with INPUT_PATH.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))[:106]


async def collect(rows: list[dict]) -> dict[int, dict]:
    sem = asyncio.Semaphore(12)
    out: dict[int, dict] = {}

    async def one(row_num: int, row: dict):
        verdict, status, _score = verdict_for(row_num)
        if status == "Ready":
            out[row_num] = {"email": "", "source": "", "status": "not_needed"}
            return
        website = str(row.get("Website URL") or "").strip()
        async with sem:
            try:
                emails, _contacts, _visited = await asyncio.wait_for(
                    crawl_company(website, max_pages=2), timeout=10
                )
                email, source = choose_generic(emails)
                out[row_num] = {
                    "email": email,
                    "source": source,
                    "status": "found" if email else "none",
                }
            except Exception as exc:
                out[row_num] = {
                    "email": "", "source": "", "status": "error", "error": str(exc)[:160]
                }

    await asyncio.gather(*(one(i, row) for i, row in enumerate(rows, start=1)))
    return out


def write(rows: list[dict], generics: dict[int, dict], balance: dict) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for row_num, row in enumerate(rows, start=1):
        candidate = CANDIDATES.get(row_num)
        verdict, status, score = verdict_for(row_num)
        generic = generics.get(row_num, {})
        generic_email = str(generic.get("email") or "")
        person_email = candidate[3] if candidate else ""
        preferred = ""
        preferred_type = ""
        if candidate and status == "Ready":
            preferred = person_email
            preferred_type = "Verified person"
        elif generic_email:
            preferred = generic_email
            preferred_type = "Public generic fallback"
        elif candidate and status == "Review":
            preferred = person_email
            preferred_type = "Person candidate - review only"

        records.append({
            "Benchmark Row": row_num,
            "Company": row.get("Company", ""),
            "Website URL": row.get("Website URL", ""),
            "Contact Name": candidate[0] if candidate else "",
            "Job Title": candidate[1] if candidate else "",
            "LinkedIn URL": candidate[2] if candidate else "",
            "Person Candidate Email": person_email,
            "Reoon Verdict": verdict,
            "Reoon Score": score,
            "Production Person Status": status,
            "Generic Fallback Email": generic_email,
            "Generic Fallback Source": generic.get("source", ""),
            "Preferred Contact Email": preferred,
            "Preferred Contact Type": preferred_type,
            "Generic Lookup Status": generic.get("status", ""),
        })

    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)

    summary = {
        "companies": len(records),
        "person_candidates_verified": len(CANDIDATES),
        "ready": len(VALID),
        "review": len(CATCH_ALL) + len(UNKNOWN),
        "reject": len(INVALID),
        "catch_all": len(CATCH_ALL),
        "unknown": len(UNKNOWN),
        "generic_fallbacks_found": sum(1 for r in records if r["Generic Fallback Email"]),
        "contactable_preferred": sum(1 for r in records if r["Preferred Contact Email"]),
        "reoon_balance_after_verification": balance,
        "output": str(OUTPUT_CSV),
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("GENERIC_FAST_SUMMARY " + json.dumps(summary), flush=True)
    print(f"GENERIC_FAST_CSV {OUTPUT_CSV}", flush=True)
    return summary


async def main():
    rows = load_rows()
    generics = await collect(rows)
    balance = await check_reoon_balance()
    write(rows, generics, balance)


def serve():
    os.chdir(OUTPUT_DIR)
    port = int(os.getenv("PORT", "8080"))
    print(f"GENERIC_FAST_FILE_SERVER http://0.0.0.0:{port}/{OUTPUT_CSV.name}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler).serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
    serve()
