from __future__ import annotations

import asyncio
import os
import re

import httpx

from .enricher import (
    ContactCandidate,
    EmailCandidate,
    crawl_company,
    dedupe_contacts,
    domain_from_url,
    duckduckgo_decision_makers,
    email_rank,
    generate_email_patterns,
)

EMAIL_VERIFIER_URL = os.getenv("EMAIL_VERIFIER_URL", "").rstrip("/")


async def verify_email_address(email: str) -> dict:
    if not email:
        return {"verdict": "not_run", "error": "empty email"}
    if not EMAIL_VERIFIER_URL:
        return {"verdict": "not_configured", "error": "verification service is unavailable"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            response = await client.post(f"{EMAIL_VERIFIER_URL}/v1/verify", json={"email": email})
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {"verdict": "unknown", "error": "invalid response"}
    except Exception as exc:
        return {"verdict": "unknown", "error": str(exc)[:220]}


def contact_name_match(email: str, contact: ContactCandidate | None) -> bool:
    if not contact or not contact.name:
        return False
    local = email.split("@", 1)[0].lower()
    tokens = [re.sub(r"[^a-z]", "", part.lower()) for part in contact.name.split()]
    return any(len(token) >= 3 and token in local for token in tokens if token)


def plain_email_status(data: dict) -> tuple[str, str]:
    verdict = str(data.get("verdict") or "unknown").lower()
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    smtp = result.get("smtp") if isinstance(result.get("smtp"), dict) else {}
    if verdict == "valid":
        return "Verified", "The address passed the delivery check."
    if verdict == "catch_all":
        return "Review", "The company accepts many addresses, so this exact address could not be confirmed."
    if verdict == "invalid":
        return "Not valid", "The address was rejected or could not receive mail."
    if verdict in {"not_run", "not_configured"}:
        return "Not checked", "The address was not fully checked."
    if smtp and smtp.get("catch_all"):
        return "Review", "The company accepts many addresses, so this exact address could not be confirmed."
    return "Review", "The address could not be confirmed with enough confidence."


async def choose_and_verify_email(
    public_emails: list[EmailCandidate],
    contact: ContactCandidate | None,
    domain: str,
    deep_verify: bool,
) -> tuple[str, str, str, dict, list[str]]:
    if not deep_verify or not EMAIL_VERIFIER_URL:
        return "", "", "", {"verdict": "not_run"}, []

    ordered = sorted(
        public_emails,
        key=lambda item: (1 if contact_name_match(item.email, contact) else 0, email_rank(item.email, domain)),
        reverse=True,
    )
    attempts: list[str] = []
    review_candidate: tuple[str, str, str, dict] | None = None

    for item in ordered[:5]:
        data = await verify_email_address(item.email)
        verdict = str(data.get("verdict") or "unknown").lower()
        attempts.append(f"{item.email}={verdict}")
        if verdict == "valid":
            confidence = "verified named contact" if contact_name_match(item.email, contact) else "verified public email"
            return item.email, confidence, item.source_url, data, attempts
        if verdict in {"catch_all", "unknown"} and review_candidate is None:
            review_candidate = (item.email, item.confidence, item.source_url, data)

    if contact and contact.name and len(contact.name.split()) >= 2:
        existing = {item.email for item in public_emails}
        for candidate in generate_email_patterns(contact.name, domain)[:6]:
            if candidate in existing:
                continue
            data = await verify_email_address(candidate)
            verdict = str(data.get("verdict") or "unknown").lower()
            attempts.append(f"{candidate}={verdict}")
            if verdict == "valid":
                return candidate, "verified contact email", contact.source_url, data, attempts
            if verdict in {"catch_all", "unknown"} and review_candidate is None:
                review_candidate = (candidate, "review candidate", contact.source_url, data)

    if review_candidate:
        return review_candidate[0], review_candidate[1], review_candidate[2], review_candidate[3], attempts
    return "", "", "", {"verdict": "invalid" if attempts else "not_run"}, attempts


async def enrich_record(
    record: dict,
    requested_positions: list[str] | None = None,
    use_search: bool = True,
    max_pages: int = 12,
    deep_verify: bool = True,
) -> dict:
    company = str(record.get("Company") or "").strip()
    website = str(record.get("Website URL") or "").strip()
    domain = domain_from_url(website)
    positions = [p.strip() for p in (requested_positions or []) if p.strip()]
    basic_company_mode = not use_search and not positions

    public_emails, site_contacts, visited = await crawl_company(website, max_pages=max_pages)
    search_contacts = (
        await duckduckgo_decision_makers(company, website, positions)
        if use_search and company and positions else []
    )

    contacts = [] if basic_company_mode else dedupe_contacts(search_contacts + site_contacts)
    best_contact = contacts[0] if contacts else None

    chosen_email, confidence, email_source, verification, attempts = await choose_and_verify_email(
        public_emails, best_contact, domain, deep_verify
    )
    verdict = str(verification.get("verdict") or "not_run").lower()
    status, note = plain_email_status(verification)
    verified_email = chosen_email if verdict == "valid" else ""
    review_email = chosen_email if chosen_email and verdict != "valid" else ""

    if basic_company_mode:
        return {
            "Company": company,
            "Website URL": website,
            "Company Email": verified_email,
            "Email Status": status,
            "Ready to Email": "YES" if verdict == "valid" else "NO",
            "Review Email": review_email,
            "Email Source": email_source,
            "Other Public Company Emails": "; ".join(item.email for item in public_emails[:10] if item.email != chosen_email),
            "Verification Note": note,
        }

    return {
        "Company": company,
        "Website URL": website,
        "Requested Positions": "; ".join(positions),
        "Contact Name": best_contact.name if best_contact else "",
        "Job Title": best_contact.title if best_contact else "",
        "Verified Email": verified_email,
        "Email Status": status,
        "Ready to Email": "YES" if verdict == "valid" else "NO",
        "Review Candidate Email": review_email,
        "LinkedIn URL": best_contact.linkedin_url if best_contact else "",
        "Email Source": email_source,
        "Contact Source": best_contact.source_url if best_contact else "",
        "Why This Contact": best_contact.source_snippet if best_contact else "",
        "Verification Note": note,
        "Email Confidence": confidence,
        "Other Public Emails": "; ".join(item.email for item in public_emails[:10] if item.email != chosen_email),
        "Pages Checked": len(visited),
        "Addresses Checked": len(attempts),
    }


async def enrich_rows(
    rows: list[dict],
    requested_positions: list[str] | None = None,
    concurrency: int = 4,
    use_search: bool = True,
    max_pages: int = 12,
    deep_verify: bool = True,
    progress_cb=None,
) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 4)))
    total = len(rows)
    completed = 0
    lock = asyncio.Lock()
    company_timeout_seconds = 75 if use_search else 45

    async def one(row: dict):
        nonlocal completed
        async with semaphore:
            try:
                result = await asyncio.wait_for(
                    enrich_record(
                        row,
                        requested_positions=requested_positions,
                        use_search=use_search,
                        max_pages=max_pages,
                        deep_verify=deep_verify,
                    ),
                    timeout=company_timeout_seconds,
                )
            except asyncio.TimeoutError:
                result = {
                    "Company": str(row.get("Company") or ""),
                    "Website URL": str(row.get("Website URL") or ""),
                    "Ready to Email": "NO",
                    "Error": f"This company took longer than {company_timeout_seconds} seconds, so it was skipped and the batch continued.",
                }
            except Exception as exc:
                result = {
                    "Company": str(row.get("Company") or ""),
                    "Website URL": str(row.get("Website URL") or ""),
                    "Ready to Email": "NO",
                    "Error": str(exc)[:250],
                }
            async with lock:
                completed += 1
                if progress_cb:
                    progress_cb(completed, total)
            return result

    return await asyncio.gather(*(one(row) for row in rows))
