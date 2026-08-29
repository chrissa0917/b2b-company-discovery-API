from __future__ import annotations

import asyncio
import re

from . import verified_enricher as _base
from .company_identity import company_website_alignment
from .enricher import crawl_company, dedupe_contacts, domain_from_url
from .live_person_discovery import find_people_live, looks_human_name, matches_requested_role
from .live_sources import augment_email_evidence, select_generic_company_email
from .reoon_integration import choose_and_verify_person_email

_ORIGINAL_ENRICH_RECORD = _base.enrich_record


def contact_rating(contact) -> str:
    if not contact or not getattr(contact, "name", ""):
        return "0/100"
    raw = max(0, min(int(getattr(contact, "score", 0) or 0), 240))
    score = round(55 + (raw / 240) * 44)
    if getattr(contact, "linkedin_url", ""):
        score = max(score, 88)
    if getattr(contact, "source_url", "") and not getattr(contact, "linkedin_url", ""):
        score = max(score, 72)
    return f"{min(99, max(55, score))}/100"


def _official_site_people(site_contacts, positions):
    out = []
    for item in site_contacts:
        if not item.name or len(item.name.split()) < 2 or not looks_human_name(item.name):
            continue
        role_evidence = item.title or item.source_snippet
        if not matches_requested_role(role_evidence, positions):
            continue
        item.score = max(int(item.score or 0), 205)
        out.append(item)
    return out


def _generic_email_belongs_to_company(email: str, company: str, website: str) -> bool:
    """Reject partner/vendor inboxes that merely appear on an official company page."""
    if "@" not in (email or ""):
        return False
    host = email.rsplit("@", 1)[1].lower().strip(".")
    website_domain = domain_from_url(website)
    if website_domain and (host == website_domain or host.endswith("." + website_domain)):
        return True

    skip = {
        "company", "group", "global", "international", "technology", "technologies",
        "robot", "robots", "robotics", "solutions", "systems", "services", "team",
        "official", "inc", "corp", "corporation", "limited", "ltd", "llc",
    }
    tokens = [
        token for token in re.findall(r"[a-z0-9]+", (company or "").lower())
        if len(token) >= 4 and token not in skip
    ]
    for label in (website_domain or "").split("."):
        label = re.sub(r"[^a-z0-9]", "", label.lower())
        if len(label) >= 4 and label not in {"www", "com", "net", "org", "global"}:
            tokens.append(label)

    compact_host = re.sub(r"[^a-z0-9]", "", host)
    return any(re.sub(r"[^a-z0-9]", "", token) in compact_host for token in dict.fromkeys(tokens))


def _identity_mismatch_result(company: str, website: str, positions: list[str], score: int, note: str) -> dict:
    return {
        "Company": company,
        "Website URL": website,
        "Requested Positions": "; ".join(positions),
        "Contact Name": "",
        "Job Title": "",
        "Contact Rating": "0/100",
        "Verified Email": "",
        "Generic Company Email": "",
        "Usable Contact Email": "",
        "Contact Type": "Company/website mismatch",
        "Verification Level": "Source row needs review",
        "Email Status": "Company/website mismatch",
        "Ready to Email": "NO",
        "Review Candidate Email": "",
        "LinkedIn URL": "",
        "Email Source": "",
        "Generic Email Source": "",
        "Contact Source": "",
        "Why This Contact": "",
        "Verification Note": "The supplied website does not appear to belong to the named company, so no outreach email was returned.",
        "Email Confidence": "",
        "Email Verification Score": "",
        "Email Pattern": "",
        "Mail Domain Used": "",
        "Pattern Evidence": "",
        "Other Public Emails": "",
        "Pages Checked": 0,
        "Addresses Checked": 0,
        "Company Identity Score": score,
        "Company Identity Note": note,
    }


async def enrich_record(
    record: dict,
    requested_positions: list[str] | None = None,
    use_search: bool = True,
    max_pages: int = 12,
    deep_verify: bool = True,
) -> dict:
    positions = [p.strip() for p in (requested_positions or []) if p.strip()]
    if not use_search or not positions:
        return await _ORIGINAL_ENRICH_RECORD(
            record,
            requested_positions=requested_positions,
            use_search=use_search,
            max_pages=max_pages,
            deep_verify=deep_verify,
        )

    company = str(record.get("Company") or "").strip()
    website = str(record.get("Website URL") or "").strip()
    domain = domain_from_url(website)

    identity_ok, identity_score, identity_note = company_website_alignment(company, website)
    if not identity_ok:
        return _identity_mismatch_result(company, website, positions, identity_score, identity_note)

    search_people, evidence = await asyncio.gather(
        find_people_live(company, website, positions, site_contacts=[]),
        crawl_company(website, max_pages=min(max_pages, 10)),
    )
    public_emails, site_contacts, visited = evidence

    contacts = dedupe_contacts([*_official_site_people(site_contacts, positions), *search_people])
    best_contact = contacts[0] if contacts else None

    all_public_emails, pattern_contacts = await augment_email_evidence(
        company,
        website,
        best_contact,
        public_emails,
        site_contacts,
    )

    generic_email, generic_source, generic_note = select_generic_company_email(
        all_public_emails,
        website,
        requested_positions=positions,
    )
    if generic_email and not _generic_email_belongs_to_company(generic_email, company, website):
        generic_email = ""
        generic_source = ""
        generic_note = ""

    chosen_email = ""
    confidence = ""
    email_source = ""
    verification = {"verdict": "not_run"}
    attempts: list[str] = []
    strategy = {"mail_domains": [], "learned_patterns": [], "blind_fallback_disabled": True}

    if best_contact:
        chosen_email, confidence, email_source, verification, attempts, strategy = await choose_and_verify_person_email(
            all_public_emails,
            pattern_contacts,
            best_contact,
            domain,
            deep_verify,
            max_verifications=2,
        )

    verdict = str(verification.get("verdict") or "not_run").lower()
    status, note = _base.plain_email_status(verification)
    verified_email = chosen_email if verdict == "valid" else ""
    review_email = chosen_email if chosen_email and verdict != "valid" else ""
    verification_score = verification.get("overall_score")

    if verified_email:
        usable_email = verified_email
        contact_type = "Verified named person"
        verification_level = "Reoon verified mailbox"
        ready = "YES"
    elif generic_email:
        usable_email = generic_email
        contact_type = "Company fallback"
        verification_level = "Public company inbox with MX"
        ready = "YES"
        status = "Company fallback"
        note = generic_note or "No named-person mailbox was safely verified; use the public company inbox."
    elif review_email:
        usable_email = ""
        contact_type = "Person candidate for review"
        verification_level = "Not safe enough for cold outreach"
        ready = "NO"
    else:
        usable_email = ""
        contact_type = "No usable contact"
        verification_level = "No usable address found"
        ready = "NO"
        if best_contact and attempts:
            status = "Not valid"
            note = "The person was found, but the evidence-backed email candidates were rejected."
        elif best_contact:
            status = "No email found"
            note = "The person was found, but no evidence-backed person email or clean company inbox was found."
        else:
            status = "No strong contact found"
            note = "No strong current person match or clean public company inbox was found."

    learned = strategy.get("learned_patterns") or []
    selected_reason = str(strategy.get("selected_reason") or "")
    pattern_used = ""
    if "learned-pattern:" in selected_reason:
        pattern_used = selected_reason.split("learned-pattern:", 1)[1].split(";", 1)[0]

    pattern_evidence = "; ".join(
        f"{item.get('domain')}:{item.get('pattern')} ({item.get('examples')} example(s), score {item.get('score')})"
        for item in learned[:3]
    )

    excluded = {value for value in (chosen_email, generic_email) if value}
    other_public = "; ".join(
        item.email for item in all_public_emails[:12] if item.email not in excluded
    )

    return {
        "Company": company,
        "Website URL": website,
        "Requested Positions": "; ".join(positions),
        "Contact Name": best_contact.name if best_contact else "",
        "Job Title": best_contact.title if best_contact else "",
        "Contact Rating": contact_rating(best_contact),
        "Verified Email": verified_email,
        "Generic Company Email": generic_email,
        "Usable Contact Email": usable_email,
        "Contact Type": contact_type,
        "Verification Level": verification_level,
        "Email Status": status,
        "Ready to Email": ready,
        "Review Candidate Email": review_email,
        "LinkedIn URL": best_contact.linkedin_url if best_contact else "",
        "Email Source": email_source,
        "Generic Email Source": generic_source,
        "Contact Source": best_contact.source_url if best_contact else "",
        "Why This Contact": best_contact.source_snippet if best_contact else "",
        "Verification Note": note,
        "Email Confidence": confidence,
        "Email Verification Score": verification_score if verification_score is not None else "",
        "Email Pattern": pattern_used,
        "Mail Domain Used": "; ".join(strategy.get("mail_domains") or []),
        "Pattern Evidence": pattern_evidence,
        "Other Public Emails": other_public,
        "Pages Checked": len(visited),
        "Addresses Checked": len(attempts),
        "Company Identity Score": identity_score,
        "Company Identity Note": identity_note,
    }
