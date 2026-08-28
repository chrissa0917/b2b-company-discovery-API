from __future__ import annotations

import asyncio
import csv
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook, load_workbook

from .verified_enricher import enrich_rows

BASE = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("DATA_DIR", BASE / "data"))
DATA.mkdir(parents=True, exist_ok=True)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
JOBS: dict[str, dict] = {}

app = FastAPI(title="BuyAndRentRobots Contact Enricher", version="1.1.0")


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
    fields = []
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


async def run_job(job_id: str, input_path: Path, concurrency: int, use_search: bool, max_pages: int, deep_verify: bool):
    try:
        rows = read_input(input_path)
        JOBS[job_id].update(status="running", total=len(rows), completed=0)
        def progress(done, total):
            JOBS[job_id].update(completed=done, total=total)
        results = await enrich_rows(rows, concurrency=concurrency, use_search=use_search, max_pages=max_pages, deep_verify=deep_verify, progress_cb=progress)
        csv_path, xlsx_path = write_outputs(job_id, results)
        JOBS[job_id].update(status="complete", completed=len(rows), csv=str(csv_path), xlsx=str(xlsx_path))
    except Exception as exc:
        JOBS[job_id].update(status="failed", error=str(exc))


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return TEMPLATES.TemplateResponse("index.html", {"request": request})


@app.post("/jobs")
async def create_job(
    file: UploadFile = File(...),
    concurrency: int = Form(4),
    use_search: bool = Form(True),
    max_pages: int = Form(12),
    deep_verify: bool = Form(True),
):
    suffix = Path(file.filename or "input.csv").suffix.lower()
    if suffix not in {".csv", ".xlsx", ".xlsm"}:
        raise HTTPException(400, "Upload a CSV or XLSX file.")
    job_id = uuid.uuid4().hex[:12]
    input_path = DATA / f"{job_id}-input{suffix}"
    input_path.write_bytes(await file.read())
    JOBS[job_id] = {"id": job_id, "status": "queued", "completed": 0, "total": 0}
    asyncio.create_task(run_job(job_id, input_path, min(max(concurrency, 1), 10), bool(use_search), min(max(max_pages, 3), 25), bool(deep_verify)))
    return JOBS[job_id]


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "Job not found. Jobs are kept in memory until the service restarts.")
    return JOBS[job_id]


@app.get("/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "complete":
        raise HTTPException(404, "Output is not ready.")
    if kind not in {"csv", "xlsx"}:
        raise HTTPException(400, "Choose csv or xlsx.")
    path = job.get(kind)
    return FileResponse(path, filename=Path(path).name)
