from __future__ import annotations

from . import verified_enricher as _base
from .enricher import crawl_company, dedupe_contacts, domain_from_url
from .person_search import find_people_ddgs
from .reoon_integration import choose_and_verify_person_email

_ORIGINAL_ENRICH_RECORD = _base.enrich_record


def contact_rating(contact) -> str:
    if not contact or not getattr(contact, "name", ""):
        return "0/100"
    raw = max(0, min(int(getattr(contact, "score", 0) or 0), 160))
    score = round(55 + (raw / 160) * 44)
    if getattr(contact, "linkedin_url", ""):
        score = max(score, 90)
    return f"{min(99, max(55, score))}/100"


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

    # Person discovery and company crawling run together. The crawler supplies
    # domain-level email evidence used to learn the company's actual convention.
    people, evidence = await __import__("asyncio").gather(
        find_people_ddgs(company, website, positions),
        crawl_company(website, max_pages=min(max_pages, 8)),
    )
    public_emails, site_contacts, visited = evidence
    contacts = dedupe_contacts(people)
    best_contact = contacts[0] if contacts else None

    if not best_contact:
        return {
            "Company": company,
            "Website URL": website,
            "Requested Positions": "; ".join(positions),
            "Contact Name": "",
            "Job Title": "",
            "Contact Rating": "0/100",
            "Verified Email": "",
            "Email Status": "No strong contact found",
            "Ready to Email": "NO",
            "Review Candidate Email": "",
            "LinkedIn URL": "",
            "Email Source": "",
            "Contact Source": "",
            "Why This Contact": "",
            "Verification Note": "We did not find a strong enough person/company match to infer an email.",
            "Email Confidence": "",
            "Email Verification Score": "",
            "Email Pattern": "",
            "Mail Domain Used": "",
            "Pattern Evidence": "",
            "Other Public Emails": "; ".join(item.email for item in public_emails[:10]),
            "Pages Checked": len(visited),
            "Addresses Checked": 0,
        }

    chosen_email, confidence, email_source, verification, attempts, strategy = await choose_and_verify_person_email(
        public_emails,
        site_contacts,
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

    if not chosen_email and attempts:
        status = "Not valid"
        note = "The person was found, but the evidence-ranked email candidates were rejected."
    elif not chosen_email:
        status = "No email found"
        note = "The person was found, but no public or evidence-backed email could be confirmed."

    learned = strategy.get("learned_patterns") or []
    selected_reason = str(strategy.get("selected_reason") or "")
    pattern_used = ""
    if "learned-pattern:" in selected_reason:
        pattern_used = selected_reason.split("learned-pattern:", 1)[1].split(";", 1)[0]
    elif "fallback-pattern:" in selected_reason:
        pattern_used = selected_reason.split("fallback-pattern:", 1)[1].split(";", 1)[0]

    pattern_evidence = "; ".join(
        f"{item.get('domain')}:{item.get('pattern')} ({item.get('examples')} example(s), score {item.get('score')})"
        for item in learned[:3]
    )

    return {
        "Company": company,
        "Website URL": website,
        "Requested Positions": "; ".join(positions),
        "Contact Name": best_contact.name,
        "Job Title": best_contact.title,
        "Contact Rating": contact_rating(best_contact),
        "Verified Email": verified_email,
        "Email Status": status,
        "Ready to Email": "YES" if verdict == "valid" else "NO",
        "Review Candidate Email": review_email,
        "LinkedIn URL": best_contact.linkedin_url,
        "Email Source": email_source,
        "Contact Source": best_contact.source_url,
        "Why This Contact": best_contact.source_snippet,
        "Verification Note": note,
        "Email Confidence": confidence,
        "Email Verification Score": verification_score if verification_score is not None else "",
        "Email Pattern": pattern_used,
        "Mail Domain Used": "; ".join(strategy.get("mail_domains") or []),
        "Pattern Evidence": pattern_evidence,
        "Other Public Emails": "; ".join(item.email for item in public_emails[:10] if item.email != chosen_email),
        "Pages Checked": len(visited),
        "Addresses Checked": len(attempts),
    }
