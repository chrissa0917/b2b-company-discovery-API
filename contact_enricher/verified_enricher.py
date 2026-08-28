from __future__ import annotations

import asyncio
import os
import re

import httpx

from .enricher import (
    ContactCandidate,
    EmailCandidate,
    crawl_company,
    domain_from_url,
    email_rank,
)
from .person_first_enrichment import build_person_first_inputs

EMAIL_VERIFIER_URL = os.getenv("EMAIL_VERIFIER_URL", "").rstrip("/")


async def verify_email_address(email: str) -> dict:
    if not email:
        return {"verdict": "not_run", "error": "empty email"}
    if not EMAIL_VERIFIER_URL:
        return {"verdict": "not_configured", "error": "verification service is unavailable"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=3.0)) as client:
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


async def choose_and_verify_company_email(
    public_emails: list[EmailCandidate],
    domain: str,
    deep_verify: bool,
) -> tuple[str, str, str, dict, list[str]]:
    if not public_emails:
        return "", "", "", {"verdict": "not_run"}, []

    ordered = sorted(public_emails, key=lambda item: email_rank(item.email, domain), reverse=True)
    if not deep_verify or not EMAIL_VERIFIER_URL:
        top = ordered[0]
        return top.email, top.confidence or "public company email", top.source_url, {"verdict": "not_run"}, []

    attempts: list[str] = []
    review_candidate: tuple[str, str, str, dict] | None = None
    for item in ordered[:3]:
        data = await verify_email_address(item.email)
        verdict = str(data.get("verdict") or "unknown").lower()
        attempts.append(f"{item.email}={verdict}")
        if verdict == "valid":
            return item.email, item.confidence or "verified public email", item.source_url, data, attempts
        if verdict in {"catch_all", "unknown"} and review_candidate is None:
            review_candidate = (item.email, item.confidence or "public company email", item.source_url, data)

    if review_candidate:
        return review_candidate[0], review_candidate[1], review_candidate[2], review_candidate[3], attempts
    return "", "", "", {"verdict": "invalid" if attempts else "not_run"}, attempts


async def choose_and_verify_person_email(
    ranked_candidates: list[dict],
    contact: ContactCandidate | None,
    deep_verify: bool,
) -> tuple[str, str, str, dict, list[str]]:
    if not contact or not ranked_candidates:
        return "", "", "", {"verdict": "not_run"}, []

    top = ranked_candidates[0]
    if not deep_verify or not EMAIL_VERIFIER_URL:
        return (
            str(top.get("email") or ""),
            f"{top.get('score', 0)}/100 — {top.get('evidence', 'person-first candidate')}",
            str(top.get("source_url") or contact.source_url),
            {"verdict": "not_run"},
            [],
        )

    attempts: list[str] = []
    review_candidate: tuple[str, str, str, dict] | None = None

    # Verify only the three strongest person-specific candidates. The previous
    # implementation could test 11 addresses sequentially and was a major source
    # of 75-second company timeouts.
    for candidate in ranked_candidates[:3]:
        email = str(candidate.get("email") or "")
        if not email:
            continue
        data = await verify_email_address(email)
        verdict = str(data.get("verdict") or "unknown").lower()
        attempts.append(f"{email}={verdict}")
        confidence = f"{candidate.get('score', 0)}/100 — {candidate.get('evidence', 'person-first candidate')}"
        source_url = str(candidate.get("source_url") or contact.source_url)
        if verdict == "valid":
            return email, confidence, source_url, data, attempts
        if verdict in {"catch_all", "unknown"} and review_candidate is None:
            review_candidate = (email, confidence, source_url, data)

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

    if basic_company_mode:
        # Keep the simpler company-email mode separate from person enrichment.
        public_emails, _, visited = await crawl_company(website, max_pages=min(max_pages, 6))
        chosen_email, confidence, email_source, verification, attempts = await choose_and_verify_company_email(
            public_emails, domain, deep_verify
        )
        verdict = str(verification.get("verdict") or "not_run").lower()
        status, note = plain_email_status(verification)
        verified_email = chosen_email if verdict == "valid" else ""
        review_email = chosen_email if chosen_email and verdict != "valid" else ""
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
            "Pages Checked": len(visited),
            "Addresses Checked": len(attempts),
        }

    # Targeted mode is now person-first: find a real person, collect public company
    # email evidence, learn the likely company pattern, then generate/rank that
    # person's addresses using the vendored Prospector pattern engine.
    public_emails, contacts, visited, person_meta = await build_person_first_inputs(
        company, website, positions
    )
    best_contact = contacts[0] if contacts else None
    ranked_candidates = person_meta.get("ranked_candidates") if isinstance(person_meta, dict) else []
    if not isinstance(ranked_candidates, list):
        ranked_candidates = []

    chosen_email, confidence, email_source, verification, attempts = await choose_and_verify_person_email(
        ranked_candidates, best_contact, deep_verify
    )
    verdict = str(verification.get("verdict") or "not_run").lower()
    status, note = plain_email_status(verification)
    verified_email = chosen_email if verdict == "valid" else ""
    review_email = chosen_email if chosen_email and verdict != "valid" else ""

    pattern_info = person_meta.get("pattern") if isinstance(person_meta, dict) else None
    pattern_note = ""
    if isinstance(pattern_info, (list, tuple)) and len(pattern_info) >= 3 and pattern_info[0]:
        label, pattern_conf, matched_pairs = pattern_info[:3]
        pattern_note = f"{label} ({int(float(pattern_conf) * 100)}% pattern confidence; {matched_pairs} matched public pair(s))"
    elif ranked_candidates:
        pattern_note = str(ranked_candidates[0].get("evidence") or "Prospector candidate ranking")

    if not best_contact:
        status = "Not checked"
        note = "No reliable person with a clean name and matching job title was found. Generic company emails were not substituted."
        verified_email = ""
        review_email = ""

    return {
        "Company": company,
        "Website URL": website,
        "Requested Positions": "; ".join(positions),
        "Contact Name": best_contact.name if best_contact else "",
        "Job Title": best_contact.title if best_contact else "",
        "Verified Email": verified_email,
        "Email Status": status,
        "Ready to Email": "YES" if verdict == "valid" and best_contact else "NO",
        "Review Candidate Email": review_email,
        "LinkedIn URL": best_contact.linkedin_url if best_contact else "",
        "Email Source": email_source,
        "Contact Source": best_contact.source_url if best_contact else "",
        "Why This Contact": best_contact.source_snippet if best_contact else "",
        "Verification Note": note,
        "Email Confidence": confidence,
        "Email Pattern Evidence": pattern_note,
        "Approved Company Email Domains": "; ".join(person_meta.get("approved_domains") or []) if isinstance(person_meta, dict) else domain,
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
    result_cb=None,
    existing_results: dict[int, dict] | None = None,
) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 4)))
    total = len(rows)
    results: list[dict | None] = [None] * total
    existing_results = existing_results or {}
    for index, result in existing_results.items():
        if 0 <= int(index) < total and isinstance(result, dict):
            results[int(index)] = result

    completed = sum(1 for result in results if result is not None)
    lock = asyncio.Lock()
    # Person-first targeted mode has bounded page scraping and only three verifier
    # attempts, so a 35-second hard ceiling is enough. Basic mode remains 45 seconds.
    company_timeout_seconds = 35 if use_search else 45

    if progress_cb:
        progress_cb(completed, total)

    async def one(index: int, row: dict):
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
                results[index] = result
                completed += 1
                if result_cb:
                    result_cb(index, result, completed, total)
                if progress_cb:
                    progress_cb(completed, total)
            return result

    pending = [one(index, row) for index, row in enumerate(rows) if results[index] is None]
    if pending:
        await asyncio.gather(*pending)

    return [
        result if result is not None else {
            "Company": str(rows[index].get("Company") or ""),
            "Website URL": str(rows[index].get("Website URL") or ""),
            "Ready to Email": "NO",
            "Error": "No result was produced for this company.",
        }
        for index, result in enumerate(results)
    ]
