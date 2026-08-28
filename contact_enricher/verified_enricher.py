from __future__ import annotations

import asyncio
import os
import re

import httpx

from .enricher import (
    EMAIL_RE,
    ContactCandidate,
    EmailCandidate,
    choose_primary_email,
    crawl_company,
    dedupe_contacts,
    domain_from_url,
    duckduckgo_decision_makers,
    email_rank,
    generate_email_patterns,
    mx_valid,
)

EMAIL_VERIFIER_URL = os.getenv("EMAIL_VERIFIER_URL", "").rstrip("/")


async def verify_email_address(email: str) -> dict:
    if not email:
        return {"verdict": "not_run", "error": "empty email"}
    if not EMAIL_VERIFIER_URL:
        return {"verdict": "not_configured", "error": "EMAIL_VERIFIER_URL is not configured"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            response = await client.post(f"{EMAIL_VERIFIER_URL}/v1/verify", json={"email": email})
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {"verdict": "unknown", "error": "invalid verifier response"}
    except Exception as exc:
        return {"verdict": "unknown", "error": str(exc)[:220]}


def verification_fields(data: dict) -> dict:
    result = data.get("result") if isinstance(data, dict) else None
    result = result if isinstance(result, dict) else {}
    smtp = result.get("smtp") if isinstance(result.get("smtp"), dict) else {}
    return {
        "Email Verification": str(data.get("verdict") or "unknown").upper().replace("_", "-"),
        "Email Reachable": str(result.get("reachable") or "unknown"),
        "SMTP Deliverable": str(bool(smtp.get("deliverable"))).upper() if smtp else "",
        "Catch All": str(bool(smtp.get("catch_all"))).upper() if smtp else "",
        "Disposable": str(bool(result.get("disposable"))).upper() if result else "",
        "Role Account": str(bool(result.get("role_account"))).upper() if result else "",
        "Has MX Records": str(bool(result.get("has_mx_records"))).upper() if result else "",
        "Verification Engine": str(data.get("engine") or ""),
        "Verification Error": str(data.get("error") or "")[:250],
    }


def contact_name_match(email: str, contact: ContactCandidate | None) -> bool:
    if not contact or not contact.name:
        return False
    local = email.split("@", 1)[0].lower()
    tokens = [re.sub(r"[^a-z]", "", part.lower()) for part in contact.name.split()]
    return any(len(token) >= 3 and token in local for token in tokens if token)


async def choose_and_verify_email(
    public_emails: list[EmailCandidate],
    contact: ContactCandidate | None,
    domain: str,
    deep_verify: bool,
) -> tuple[str, str, str, dict, list[str]]:
    if not deep_verify or not EMAIL_VERIFIER_URL:
        email, confidence, source = choose_primary_email(public_emails, contact, domain)
        return email, confidence, source, {"verdict": "not_run"}, []

    ordered = sorted(
        public_emails,
        key=lambda item: (1 if contact_name_match(item.email, contact) else 0, email_rank(item.email, domain)),
        reverse=True,
    )
    attempts: list[str] = []
    fallback: tuple[str, str, str, dict] | None = None

    for item in ordered[:3]:
        data = await verify_email_address(item.email)
        verdict = str(data.get("verdict") or "unknown")
        attempts.append(f"{item.email}={verdict}")
        if verdict == "valid":
            confidence = "A-smtp-verified-named" if contact_name_match(item.email, contact) else "A-smtp-verified-public"
            return item.email, confidence, item.source_url, data, attempts
        if verdict in {"catch_all", "unknown"} and fallback is None:
            fallback = (item.email, item.confidence, item.source_url, data)

    if contact and contact.name and len(contact.name.split()) >= 2:
        existing = {item.email for item in public_emails}
        for candidate in generate_email_patterns(contact.name, domain)[:3]:
            if candidate in existing:
                continue
            data = await verify_email_address(candidate)
            verdict = str(data.get("verdict") or "unknown")
            attempts.append(f"{candidate}={verdict}")
            if verdict == "valid":
                return candidate, "A-smtp-verified-pattern", contact.source_url, data, attempts

    if fallback:
        return fallback[0], fallback[1], fallback[2], fallback[3], attempts
    if ordered:
        item = ordered[0]
        return item.email, item.confidence, item.source_url, {"verdict": "invalid"}, attempts
    if contact and contact.name:
        patterns = generate_email_patterns(contact.name, domain)
        if patterns:
            return patterns[0], "D-pattern-only-do-not-auto-send", contact.source_url, {"verdict": "unknown"}, attempts
    return "", "", "", {"verdict": "not_run"}, attempts


async def enrich_record(record: dict, use_search: bool = True, max_pages: int = 12, deep_verify: bool = True) -> dict:
    company = str(
        record.get("Company") or record.get("Company Name") or record.get("company_name") or
        record.get("company") or record.get("name") or ""
    ).strip()
    website = str(
        record.get("Website") or record.get("website") or record.get("Website URL") or
        record.get("website_url") or ""
    ).strip()
    listing = str(
        record.get("BuyAndRentRobots Listing URL") or record.get("Listing URL") or
        record.get("listing_url") or ""
    ).strip()
    existing_email = str(
        record.get("contact_email") or record.get("Contact Email") or record.get("Email") or
        record.get("email") or ""
    ).strip().lower()
    existing_linkedin = str(record.get("social_linkedin") or record.get("LinkedIn URL") or "").strip()
    domain = domain_from_url(website)

    public_emails, site_contacts, visited = await crawl_company(website, max_pages=max_pages)

    if existing_email and EMAIL_RE.fullmatch(existing_email) and all(item.email != existing_email for item in public_emails):
        host = existing_email.split("@")[-1]
        existing_mx = await mx_valid(host)
        public_emails.insert(0, EmailCandidate(
            email=existing_email,
            source_url=str(record.get("contact_page_url") or record.get("source_url") or ""),
            source_type="existing",
            mx_valid=existing_mx,
            confidence="A-existing-mx" if existing_mx else "C-existing-unverified",
        ))

    search_contacts = await duckduckgo_decision_makers(company, website) if use_search and company else []
    contacts = dedupe_contacts(site_contacts + search_contacts)
    best_contact = contacts[0] if contacts else None
    if best_contact and not best_contact.linkedin_url and existing_linkedin:
        best_contact.linkedin_url = existing_linkedin

    primary_email, confidence, email_source, verification, attempts = await choose_and_verify_email(
        public_emails, best_contact, domain, deep_verify
    )

    general = next((item.email for item in public_emails if item.email.split("@")[0] in {"info", "contact", "hello", "sales"}), "")
    marketing = next((item.email for item in public_emails if item.email.split("@")[0] in {"marketing", "press", "media", "pr", "communications", "partnerships"}), "")
    verdict = str(verification.get("verdict") or "not_run")
    ready = "YES" if verdict == "valid" else "NO" if verdict == "invalid" else "REVIEW"

    result = dict(record)
    result.update({
        "Company": company,
        "Website": website,
        "BuyAndRentRobots Listing URL": listing,
        "Contact Name": best_contact.name if best_contact else str(record.get("contact_first_name") or ""),
        "Job Title": best_contact.title if best_contact else "",
        "LinkedIn URL": best_contact.linkedin_url if best_contact else existing_linkedin,
        "Best Email": primary_email,
        "Email Confidence": confidence,
        "Email Source URL": email_source,
        "Marketing/PR Email": marketing,
        "General Email": general,
        "All Public Emails": "; ".join(item.email for item in public_emails[:12]),
        "Contact Source URL": best_contact.source_url if best_contact else "",
        "Contact Evidence": best_contact.source_snippet if best_contact else "",
        "Pages Crawled": len(visited),
        "Verification Attempts": len(attempts),
        "Checked Candidate Emails": "; ".join(attempts[:12]),
        "Ready to Email": ready,
    })
    result.update(verification_fields(verification))
    return result


async def enrich_rows(
    rows: list[dict],
    concurrency: int = 4,
    use_search: bool = True,
    max_pages: int = 12,
    deep_verify: bool = True,
    progress_cb=None,
) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 10)))
    total = len(rows)
    completed = 0
    lock = asyncio.Lock()

    async def one(row: dict):
        nonlocal completed
        async with semaphore:
            try:
                result = await enrich_record(row, use_search=use_search, max_pages=max_pages, deep_verify=deep_verify)
            except Exception as exc:
                result = dict(row)
                result["Error"] = str(exc)[:250]
            async with lock:
                completed += 1
                if progress_cb:
                    progress_cb(completed, total)
            return result

    return await asyncio.gather(*(one(row) for row in rows))
