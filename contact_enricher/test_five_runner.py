from __future__ import annotations

import asyncio
import json
import os

import uvicorn

from .main import app
from .verified_enricher import enrich_rows

TEST_ROWS = [
    {"Company": "RobotsInternational.com", "Website URL": "https://www.robotsinternational.com"},
    {"Company": "Toshiba", "Website URL": "https://global.toshiba"},
    {"Company": "SharkNinja, Inc.", "Website URL": "https://sharkninja.com"},
    {"Company": "Moyotech", "Website URL": "https://moyotech.com"},
    {"Company": "Joby Aviation", "Website URL": "https://jobyaviation.com"},
]

CONTACT_AREAS = [
    "Marketing",
    "PR & Communications",
    "Partnerships & Business Development",
]


async def run_test() -> None:
    try:
        results = await enrich_rows(
            TEST_ROWS,
            requested_positions=CONTACT_AREAS,
            concurrency=3,
            use_search=True,
            max_pages=12,
            deep_verify=True,
        )
        print("TEST_FIVE_RESULT_JSON=" + json.dumps(results, ensure_ascii=False), flush=True)
    except Exception as exc:
        print("TEST_FIVE_ERROR=" + repr(exc), flush=True)


@app.on_event("startup")
async def start_test() -> None:
    asyncio.create_task(run_test())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
