from __future__ import annotations

import asyncio
import csv
import hmac
import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from openpyxl import Workbook, load_workbook

from .verified_enricher import enrich_rows

BASE = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("DATA_DIR", BASE / "data"))
DATA.mkdir(parents=True, exist_ok=True)
JOBS: dict[str, dict] = {}
API_KEY = os.getenv("CONTACT_ENRICHER_API_KEY", "")
DEFAULT_POSITIONS = [
    "Marketing Director",
    "Head of Marketing",
    "Marketing Manager",
    "Partnerships Manager",
    "Communications Director",
    "PR Manager",
    "Business Development Manager",
    "Founder",
    "CEO",
]

app = FastAPI(title="Chrissa Automates Contact Enricher", version="2.0.0")


def require_api_key(request: Request) -> None:
    supplied = request.headers.get("x-contact-enricher-key", "")
    if not API_KEY or not supplied or not hmac.compare_digest(supplied, API_KEY):
        raise HTTPException(401, "Unauthorized")


def require_job_owner(request: Request, job: dict) -> None:
    supplied = request.headers.get("x-contact-enricher-user", "")
    owner = str(job.get("owner_id") or "")
    if not owner or not supplied or not hmac.compare_digest(supplied, owner):
        raise HTTPException(403, "This job belongs to a different account.")


def parse_positions(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_POSITIONS[:5]
    try:
        value = json.loads(raw)
        if isinstance(value, list):
            positions = [str(item).strip() for item in value if str(item).strip()]
            return list(dict.fromkeys(positions))[:8] or DEFAULT_POSITIONS[:5]
    except Exception:
        pass
    positions = [item.strip() for item in raw.replace("\r", "").replace("\n", ",").split(",") if item.strip()]
    return list(dict.fromkeys(positions))[:8] or DEFAULT_POSITIONS[:5]


def read_headers(path: Path) -> list[str]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            return [str(x or "").strip() for x in next(reader, [])]
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        first = next(ws.iter_rows(values_only=True), ())
        return [str(x or "").strip() for x in first]
    return []


def validate_headers(headers: list[str]) -> None:
    if headers != ["Company", "Website URL"]:
        raise HTTPException(400, 'Your file must contain exactly two headers: "Company" and "Website URL".')


def read_input(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(x or "").strip() for x in rows[0]]
        return [dict(zip(headers, ["" if x is None else x for x in row])) for row in rows[1:]]
    raise ValueError("Only CSV and XLSX files are supported.")


def write_outputs(job_id: str, rows: list[dict]) -> tuple[Path, Path]:
    csv_path = DATA / f"{job_id}-enriched.csv"
    xlsx_path = DATA / f"{job_id}-enriched.xlsx"
    fields: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fields:
                fields.append(key)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    wb = Workbook()
    ws = wb.active
    ws.title = "Enriched Contacts"
    ws.append(fields)
    for row in rows:
        ws.append([row.get(key, "") for key in fields])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(xlsx_path)
    return csv_path, xlsx_path


async def run_job(
    job_id: str,
    input_path: Path,
    concurrency: int,
    requested_positions: list[str],
    max_pages: int,
):
    try:
        rows = read_input(input_path)
        JOBS[job_id].update(status="running", total=len(rows), completed=0)

        def progress(done, total):
            JOBS[job_id].update(completed=done, total=total)

        results = await enrich_rows(
            rows,
            requested_positions=requested_positions,
            concurrency=concurrency,
            use_search=True,
            max_pages=max_pages,
            deep_verify=True,
            progress_cb=progress,
        )
        csv_path, xlsx_path = write_outputs(job_id, results)
        JOBS[job_id].update(status="complete", completed=len(rows), csv=str(csv_path), xlsx=str(xlsx_path))
    except Exception as exc:
        JOBS[job_id].update(status="failed", error=str(exc))


@app.get("/health")
def health():
    return {"ok": True, "service": "chrissa-automates-contact-enricher"}


@app.get("/")
def home():
    return RedirectResponse("https://chrissaautomates.com/contact-enricher", status_code=302)


@app.post("/jobs/stage")
async def stage_job(
    request: Request,
    file: UploadFile = File(...),
    owner_id: str = Form(...),
    positions: str = Form(""),
    concurrency: int = Form(4),
    max_pages: int = Form(12),
):
    require_api_key(request)
    owner_id = owner_id.strip()
    if not owner_id:
        raise HTTPException(400, "Missing account owner.")
    suffix = Path(file.filename or "input.csv").suffix.lower()
    if suffix not in {".csv", ".xlsx", ".xlsm"}:
        raise HTTPException(400, "Upload a CSV or XLSX file.")

    job_id = uuid.uuid4().hex[:12]
    input_path = DATA / f"{job_id}-input{suffix}"
    input_path.write_bytes(await file.read())
    validate_headers(read_headers(input_path))
    rows = read_input(input_path)
    if not rows:
        raise HTTPException(400, "Your file has no company rows.")
    if len(rows) > 2000:
        raise HTTPException(400, "Please upload 2,000 companies or fewer per job.")

    requested_positions = parse_positions(positions)
    JOBS[job_id] = {
        "id": job_id,
        "owner_id": owner_id,
        "status": "staged",
        "completed": 0,
        "total": len(rows),
        "input_path": str(input_path),
        "positions": requested_positions,
        "concurrency": min(max(concurrency, 1), 8),
        "max_pages": min(max(max_pages, 3), 20),
    }
    return {
        "id": job_id,
        "status": "staged",
        "total": len(rows),
        "positions": requested_positions,
    }


@app.post("/jobs/{job_id}/start")
async def start_job(request: Request, job_id: str):
    require_api_key(request)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    require_job_owner(request, job)
    if job.get("status") not in {"staged", "failed"}:
        return {"id": job_id, "status": job.get("status"), "total": job.get("total", 0)}
    input_path = Path(job["input_path"])
    job["status"] = "queued"
    asyncio.create_task(
        run_job(
            job_id,
            input_path,
            int(job.get("concurrency", 4)),
            list(job.get("positions") or DEFAULT_POSITIONS[:5]),
            int(job.get("max_pages", 12)),
        )
    )
    return {"id": job_id, "status": "queued", "total": job.get("total", 0)}


@app.get("/jobs/{job_id}")
def job_status(request: Request, job_id: str):
    require_api_key(request)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found. Jobs are kept in memory until the service restarts.")
    require_job_owner(request, job)
    return {
        "id": job_id,
        "status": job.get("status"),
        "completed": job.get("completed", 0),
        "total": job.get("total", 0),
        "error": job.get("error", ""),
    }


@app.get("/jobs/{job_id}/download/{kind}")
def download(request: Request, job_id: str, kind: str):
    require_api_key(request)
    job = JOBS.get(job_id)
    if not job or job.get("status") != "complete":
        raise HTTPException(404, "Output is not ready.")
    require_job_owner(request, job)
    if kind not in {"csv", "xlsx"}:
        raise HTTPException(400, "Choose csv or xlsx.")
    path = job.get(kind)
    return FileResponse(path, filename=f"chrissa-automates-contact-enricher-{job_id}.{kind}")
