from __future__ import annotations

import re

from . import verified_enricher as _base
from .enricher import ContactCandidate, EmailCandidate, email_rank
from .reoon_verifier import verify_email_reoon

# Preserve the existing company-email behavior. Reoon is reserved for named
# person searches so the free company-email flow does not consume Reoon credits.
_ORIGINAL_CHOOSE_AND_VERIFY = _base.choose_and_verify_email


async def _verify_person_email(email: str) -> dict:
    """Prefer Reoon Power mode; fall back to the existing verifier on API uncertainty."""
    data = await verify_email_reoon(email, mode="power")
    verdict = str(data.get("verdict") or "unknown").lower()
    if verdict in {"valid", "invalid", "catch_all", "risky"}:
        return data

    fallback = await _base.verify_email_address(email)
    fallback_verdict = str(fallback.get("verdict") or "unknown").lower()
    if fallback_verdict in {"valid", "invalid", "catch_all"}:
        return fallback
    return data


def _high_yield_patterns(name: str, domain: str) -> list[str]:
    parts = [re.sub(r"[^a-z]", "", part.lower()) for part in (name or "").split()]
    parts = [part for part in parts if part]
    if len(parts) < 2 or not domain:
        return []
    first, last = parts[0], parts[-1]
    # Same three candidates used in the successful Reoon benchmark.
    return list(dict.fromkeys([
        f"{first}@{domain}",
        f"{first}.{last}@{domain}",
        f"{first}{last}@{domain}",
    ]))


async def choose_and_verify_email(
    public_emails: list[EmailCandidate],
    contact: ContactCandidate | None,
    domain: str,
    deep_verify: bool,
) -> tuple[str, str, str, dict, list[str]]:
    """Person-first email selection for targeted searches.

    Named-contact mode never promotes a generic company inbox as the person's
    email. It checks direct public emails that match the person's name, then the
    same three high-yield patterns that passed the Reoon Power benchmark.
    Basic company-email mode keeps the original behavior unchanged.
    """
    named_contact = bool(contact and contact.name and len(contact.name.split()) >= 2)
    if not named_contact:
        return await _ORIGINAL_CHOOSE_AND_VERIFY(
            public_emails, contact, domain, deep_verify
        )
    if not deep_verify:
        return "", "", "", {"verdict": "not_run"}, []

    attempts: list[str] = []
    review_candidate: tuple[str, str, str, dict] | None = None

    direct_public = [
        item for item in public_emails
        if _base.contact_name_match(item.email, contact)
    ]
    direct_public = sorted(
        direct_public,
        key=lambda item: email_rank(item.email, domain),
        reverse=True,
    )

    for item in direct_public[:2]:
        data = await _verify_person_email(item.email)
        verdict = str(data.get("verdict") or "unknown").lower()
        attempts.append(f"{item.email}={verdict}")
        if verdict == "valid":
            return item.email, "verified named contact", item.source_url, data, attempts
        if verdict in {"catch_all", "risky", "unknown"} and review_candidate is None:
            review_candidate = (item.email, item.confidence or "review candidate", item.source_url, data)

    existing = {item.email for item in public_emails}
    for candidate in _high_yield_patterns(contact.name, domain):
        if candidate in existing and any(item.email == candidate for item in direct_public):
            continue
        data = await _verify_person_email(candidate)
        verdict = str(data.get("verdict") or "unknown").lower()
        attempts.append(f"{candidate}={verdict}")
        if verdict == "valid":
            return candidate, "verified contact email", contact.source_url, data, attempts
        if verdict in {"catch_all", "risky", "unknown"} and review_candidate is None:
            review_candidate = (candidate, "review candidate", contact.source_url, data)

    if review_candidate:
        return review_candidate[0], review_candidate[1], review_candidate[2], review_candidate[3], attempts
    return "", "", "", {"verdict": "invalid" if attempts else "not_run"}, attempts
