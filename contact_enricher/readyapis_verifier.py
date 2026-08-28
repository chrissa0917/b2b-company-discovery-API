from __future__ import annotations

import httpx

READYAPIS_DEMO_PROBE = "https://readyapis.com/demo/api/v1/email/probe"


async def verify_email_readyapis(email: str) -> dict:
    """Run Ready APIs' free live SMTP RCPT probe over HTTPS.

    This avoids Railway's outbound SMTP restriction because Railway only makes
    an HTTPS request; the external verifier performs the HELO/MAIL FROM/RCPT TO
    conversation against the recipient MX.
    """
    email = (email or "").strip().lower()
    if not email:
        return {"verdict": "not_run", "provider": "readyapis", "error": "empty email"}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(14.0, connect=4.0)) as client:
            response = await client.get(
                READYAPIS_DEMO_PROBE,
                params={"email": email, "check_catch_all": "true"},
                headers={"User-Agent": "ChrissaAutomates-ContactEnricher/benchmark"},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        return {
            "verdict": "unknown",
            "provider": "readyapis",
            "error": str(exc)[:220],
        }

    data = payload.get("data") if isinstance(payload, dict) else None
    attrs = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(attrs, dict):
        attrs = {}

    raw = str(attrs.get("verdict") or attrs.get("status") or "").strip().lower()
    mapping = {
        "deliverable": "valid",
        "valid": "valid",
        "catch_all": "catch_all",
        "accept_all": "catch_all",
        "undeliverable": "invalid",
        "invalid": "invalid",
        "greylisted": "unknown",
        "unknown": "unknown",
    }
    verdict = mapping.get(raw, "unknown")
    if raw.startswith("inconclusive"):
        verdict = "unknown"

    return {
        "verdict": verdict,
        "provider": "readyapis",
        "raw_verdict": raw or "unknown",
        "confidence": attrs.get("confidence"),
        "mx_host": attrs.get("mx_host"),
        "rcpt_status_code": attrs.get("rcpt_status_code"),
        "rcpt_response_line": attrs.get("rcpt_response_line"),
        "catch_all_check_status": attrs.get("catch_all_check_status"),
        "is_catch_all": attrs.get("is_catch_all"),
        "error": attrs.get("error"),
        "findings": attrs.get("findings"),
    }
