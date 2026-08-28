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


async def _submit(client: httpx.AsyncClient, clean: list[str]) -> httpx.Response:
    return await client.post(
        f"{VERIFALIA_API}/email-validations",
        params={"waitTime": "30000"},
        json={"entries": [{"inputData": email} for email in clean], "quality": "Standard"},
    )


async def _bearer_token(timeout: httpx.Timeout) -> tuple[str, str]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as auth_client:
            response = await auth_client.post(
                f"{VERIFALIA_API}/auth/tokens",
                json={"username": VERIFALIA_USERNAME, "password": VERIFALIA_PASSWORD},
            )
            if response.status_code != 200:
                return "", f"token HTTP {response.status_code}: {response.text[:400]}"
            payload = response.json()
            token = str(payload.get("accessToken") or "").strip() if isinstance(payload, dict) else ""
            if not token:
                return "", "token response did not contain accessToken"
            return token, ""
    except Exception as exc:
        return "", str(exc)[:300]


async def verify_emails_verifalia(emails: list[str]) -> dict[str, dict]:
    clean = list(dict.fromkeys((email or "").strip().lower() for email in emails if (email or "").strip()))
    if not clean:
        return {}
    if not VERIFALIA_USERNAME or not VERIFALIA_PASSWORD:
        return {email: {"email": email, "verdict": "not_configured", "provider": "verifalia"} for email in clean}

    timeout = httpx.Timeout(40.0, connect=8.0)
    try:
        # Try HTTP Basic first because it is the fastest for a one-off request.
        async with httpx.AsyncClient(
            auth=httpx.BasicAuth(VERIFALIA_USERNAME, VERIFALIA_PASSWORD),
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            response = await _submit(client, clean)

        auth_mode = "basic"
        if response.status_code == 401:
            # Verifalia may have Basic authentication disabled for a user. In
            # that case request a JWT bearer token with the same credentials.
            token, token_error = await _bearer_token(timeout)
            if not token:
                detail = token_error or response.text[:400]
                return {
                    email: {
                        "email": email,
                        "verdict": "unknown",
                        "provider": "verifalia",
                        "auth_mode": "bearer_failed",
                        "error": detail,
                    }
                    for email in clean
                }
            auth_mode = "bearer"
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                response = await _submit(client, clean)

        if response.status_code not in {200, 202}:
            detail = response.text[:500]
            return {
                email: {
                    "email": email,
                    "verdict": "unknown",
                    "provider": "verifalia",
                    "auth_mode": auth_mode,
                    "error": f"HTTP {response.status_code}: {detail}",
                }
                for email in clean
            }

        payload = response.json()
        entries = _entries_from_snapshot(payload)
        job_id = _overview_id(payload)

        # Poll with the same successful authentication mode.
        if auth_mode == "bearer":
            token, token_error = await _bearer_token(timeout)
            if not token:
                return {
                    email: {
                        "email": email,
                        "verdict": "unknown",
                        "provider": "verifalia",
                        "auth_mode": auth_mode,
                        "error": token_error or "could not refresh bearer token",
                    }
                    for email in clean
                }
            poll_client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, timeout=timeout, follow_redirects=True
            )
        else:
            poll_client = httpx.AsyncClient(
                auth=httpx.BasicAuth(VERIFALIA_USERNAME, VERIFALIA_PASSWORD),
                timeout=timeout,
                follow_redirects=True,
            )

        async with poll_client as client:
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

        results: dict[str, dict] = {}
        for entry in entries:
            mapped = _map_entry(entry)
            if mapped["email"]:
                mapped["auth_mode"] = auth_mode
                results[mapped["email"]] = mapped
        for email in clean:
            results.setdefault(
                email,
                {
                    "email": email,
                    "verdict": "unknown",
                    "provider": "verifalia",
                    "auth_mode": auth_mode,
                    "error": "verification job did not return a completed entry in time",
                },
            )
        return results
    except Exception as exc:
        return {
            email: {
                "email": email,
                "verdict": "unknown",
                "provider": "verifalia",
                "error": str(exc)[:300],
            }
            for email in clean
        }
