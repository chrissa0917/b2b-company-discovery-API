from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

VERIFALIA_API = "https://api.verifalia.com/v2.7"
VERIFALIA_USERNAME = os.getenv("VERIFALIA_USERNAME", "").strip()
VERIFALIA_PASSWORD = os.getenv("VERIFALIA_PASSWORD", "").strip()


def _entries_from_snapshot(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    entries = payload.get("entries")
    if isinstance(entries, dict):
        data = entries.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    if isinstance(entries, list):
        return [item for item in entries if isinstance(item, dict)]
    return []


def _overview_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    overview = payload.get("overview")
    if isinstance(overview, dict):
        return str(overview.get("id") or "").strip()
    return str(payload.get("id") or "").strip()


def _map_entry(entry: dict) -> dict:
    classification = str(entry.get("classification") or "Unknown").strip()
    status = str(entry.get("status") or "").strip()
    lowered = classification.lower()
    if lowered == "deliverable":
        verdict = "valid"
    elif lowered == "undeliverable":
        verdict = "invalid"
    elif lowered == "risky":
        verdict = "catch_all" if status.lower() == "serveriscatchall" else "risky"
    else:
        verdict = "unknown"
    return {
        "email": str(entry.get("inputData") or entry.get("emailAddress") or "").strip().lower(),
        "verdict": verdict,
        "classification": classification,
        "status": status,
        "provider": "verifalia",
        "is_disposable": entry.get("isDisposableEmailAddress"),
        "is_role_account": entry.get("isRoleAccount"),
        "is_free_email": entry.get("isFreeEmailAddress"),
    }


async def verify_emails_verifalia(emails: list[str]) -> dict[str, dict]:
    clean = list(dict.fromkeys((email or "").strip().lower() for email in emails if (email or "").strip()))
    if not clean:
        return {}
    if not VERIFALIA_USERNAME or not VERIFALIA_PASSWORD:
        return {email: {"email": email, "verdict": "not_configured", "provider": "verifalia"} for email in clean}

    auth = httpx.BasicAuth(VERIFALIA_USERNAME, VERIFALIA_PASSWORD)
    timeout = httpx.Timeout(40.0, connect=8.0)
    try:
        async with httpx.AsyncClient(auth=auth, timeout=timeout, follow_redirects=True) as client:
            response = await client.post(
                f"{VERIFALIA_API}/email-validations",
                params={"waitTime": "30000"},
                json={"entries": [{"inputData": email} for email in clean], "quality": "Standard"},
            )
            if response.status_code not in {200, 202}:
                detail = response.text[:500]
                return {email: {"email": email, "verdict": "unknown", "provider": "verifalia", "error": f"HTTP {response.status_code}: {detail}"} for email in clean}
            payload = response.json()
            entries = _entries_from_snapshot(payload)
            job_id = _overview_id(payload)

            # Free-plan jobs can be best-effort and may still be queued after the
            # initial 30-second wait. Poll a bounded number of times.
            for _ in range(3):
                if len(entries) >= len(clean):
                    break
                if not job_id:
                    break
                await asyncio.sleep(1.0)
                poll = await client.get(
                    f"{VERIFALIA_API}/email-validations/{job_id}",
                    params={"waitTime": "30000"},
                )
                if poll.status_code not in {200, 202}:
                    break
                payload = poll.json()
                entries = _entries_from_snapshot(payload)

            results = {_map_entry(entry)["email"]: _map_entry(entry) for entry in entries if _map_entry(entry)["email"]}
            for email in clean:
                results.setdefault(email, {"email": email, "verdict": "unknown", "provider": "verifalia", "error": "verification job did not return a completed entry in time"})
            return results
    except Exception as exc:
        return {email: {"email": email, "verdict": "unknown", "provider": "verifalia", "error": str(exc)[:300]} for email in clean}
