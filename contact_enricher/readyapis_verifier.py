from __future__ import annotations

import os
import httpx

SMTP_PROBE_URL = os.getenv("SMTP_PROBE_URL", "").strip()
SMTP_PROBE_TOKEN = os.getenv("SMTP_PROBE_TOKEN", "").strip()


async def verify_email_readyapis(email: str) -> dict:
    """Run the free Supabase-hosted SMTP RCPT probe over HTTPS.

    Railway makes only an HTTPS request. The Supabase Edge Function performs
    the MX lookup, SMTP RCPT TO mailbox probe, and catch-all test.
    """
    email = (email or "").strip().lower()
    if not email:
        return {"verdict": "not_run", "provider": "supabase-smtp-probe", "error": "empty email"}
    if not SMTP_PROBE_URL or not SMTP_PROBE_TOKEN:
        return {"verdict": "unknown", "provider": "supabase-smtp-probe", "error": "probe not configured"}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(18.0, connect=5.0)) as client:
            response = await client.post(
                SMTP_PROBE_URL,
                json={"email": email},
                headers={
                    "x-probe-token": SMTP_PROBE_TOKEN,
                    "User-Agent": "ChrissaAutomates-ContactEnricher/benchmark",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        return {
            "verdict": "unknown",
            "provider": "supabase-smtp-probe",
            "error": str(exc)[:220],
        }

    raw = str(payload.get("verdict") or "unknown").strip().lower() if isinstance(payload, dict) else "unknown"
    verdict = raw if raw in {"valid", "invalid", "catch_all", "unknown"} else "unknown"

    return {
        "verdict": verdict,
        "provider": "supabase-smtp-probe",
        "raw_verdict": raw,
        "mx_host": payload.get("mx_host") if isinstance(payload, dict) else None,
        "rcpt_status_code": payload.get("rcpt_code") if isinstance(payload, dict) else None,
        "rcpt_response_line": payload.get("rcpt_response") if isinstance(payload, dict) else None,
        "is_catch_all": payload.get("catch_all") if isinstance(payload, dict) else None,
        "catch_all_rcpt_code": payload.get("catch_all_rcpt_code") if isinstance(payload, dict) else None,
        "error": payload.get("error") if isinstance(payload, dict) else None,
    }
