from __future__ import annotations

import os

import httpx

REOON_VERIFY_URL = "https://emailverifier.reoon.com/api/v1/verify"
REOON_BALANCE_URL = "https://emailverifier.reoon.com/api/v1/check-account-balance/"


def _api_key() -> str:
    return os.getenv("REOON_API_KEY", "").strip()


def _map_status(payload: dict) -> dict:
    raw = str(payload.get("status") or "unknown").strip().lower()
    if raw == "safe":
        verdict = "valid"
    elif raw in {"invalid", "disabled", "disposable", "spamtrap"}:
        verdict = "invalid"
    elif raw == "catch_all":
        verdict = "catch_all"
    elif raw in {"role_account", "inbox_full"}:
        verdict = "risky"
    else:
        verdict = "unknown"

    return {
        "email": str(payload.get("email") or "").strip().lower(),
        "verdict": verdict,
        "provider": "reoon",
        "raw_status": raw,
        "overall_score": payload.get("overall_score"),
        "is_safe_to_send": payload.get("is_safe_to_send"),
        "is_deliverable": payload.get("is_deliverable"),
        "is_catch_all": payload.get("is_catch_all"),
        "is_role_account": payload.get("is_role_account"),
        "is_disposable": payload.get("is_disposable"),
        "can_connect_smtp": payload.get("can_connect_smtp"),
        "mx_accepts_mail": payload.get("mx_accepts_mail"),
        "has_inbox_full": payload.get("has_inbox_full"),
        "verification_mode": payload.get("verification_mode"),
    }


async def check_reoon_balance() -> dict:
    key = _api_key()
    if not key:
        return {"provider": "reoon", "configured": False, "error": "REOON_API_KEY is missing"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            response = await client.get(REOON_BALANCE_URL, params={"key": key})
            response.raise_for_status()
            data = response.json()
            return {
                "provider": "reoon",
                "configured": str(data.get("api_status") or "").lower() == "active",
                "remaining_daily_credits": data.get("remaining_daily_credits"),
                "remaining_instant_credits": data.get("remaining_instant_credits"),
                "status": data.get("status"),
            }
    except Exception as exc:
        return {"provider": "reoon", "configured": False, "error": str(exc)[:300]}


async def verify_email_reoon(email: str, mode: str = "power") -> dict:
    email = (email or "").strip().lower()
    key = _api_key()
    if not email:
        return {"email": email, "verdict": "not_run", "provider": "reoon", "error": "empty email"}
    if not key:
        return {"email": email, "verdict": "not_configured", "provider": "reoon", "error": "REOON_API_KEY is missing"}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=6.0)) as client:
            response = await client.get(
                REOON_VERIFY_URL,
                params={"email": email, "key": key, "mode": mode},
                headers={"User-Agent": "ChrissaAutomates-ContactEnricher/2.1"},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        return {"email": email, "verdict": "unknown", "provider": "reoon", "error": str(exc)[:300]}

    if str(payload.get("status") or "").lower() == "error":
        return {
            "email": email,
            "verdict": "unknown",
            "provider": "reoon",
            "error": str(payload.get("reason") or payload)[:300],
        }

    result = _map_status(payload)
    if not result.get("email"):
        result["email"] = email
    return result
