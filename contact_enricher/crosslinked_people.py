from __future__ import annotations

import asyncio
import re
from typing import Any

from crosslinked.search import CrossLinked

from .enricher import ContactCandidate, role_score
from .person_first_fast import ROLE_HINTS, candidate_emails, email_evidence


def _wanted_title(title: str, requested: list[str]) -> bool:
    clean = re.sub(r"\s+", " ", title or "").strip(" -|,;:")
    if not clean or clean.lower() == "n/a" or len(clean) > 110 or len(clean.split()) > 14:
        return False
    lower = clean.lower()
    hints: list[str] = []
    for item in requested:
        hints.extend(ROLE_HINTS.get(item.lower(), [item.lower()]))
    return any(h and h in lower for h in hints) or role_score(clean, requested) > 0


def _clean_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(" -|,;:")
    words = value.split()
    if not 2 <= len(words) <= 5 or len(value) > 60:
        return ""
    if any(not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ'`.-]+", word) for word in words):
        return ""
    bad = {"linkedin", "marketing", "director", "manager", "company", "robotics", "technologies", "technology"}
    if any(word.lower().strip(".-") in bad for word in words):
        return ""
    # CrossLinked normalizes names to lowercase; return a readable title-cased form.
    return " ".join(word.capitalize() for word in words)


def _run_engine(engine: str, company: str) -> list[dict[str, Any]]:
    try:
        # CrossLinked's own timer is the primary bound. conn_timeout prevents a
        # blocked search-engine request from holding a company indefinitely.
        worker = CrossLinked(
            search_engine=engine,
            target=company,
            timeout=5,
            conn_timeout=3,
            proxies=[],
            jitter=0,
        )
        data = worker.search()
        return data if isinstance(data, list) else []
    except Exception:
        return []


async def find_people_crosslinked(company: str, requested: list[str]) -> list[ContactCandidate]:
    google_task = asyncio.create_task(asyncio.to_thread(_run_engine, "google", company))
    bing_task = asyncio.create_task(asyncio.to_thread(_run_engine, "bing", company))
    try:
        google, bing = await asyncio.wait_for(asyncio.gather(google_task, bing_task), timeout=8)
    except Exception:
        for task in (google_task, bing_task):
            if not task.done():
                task.cancel()
        return []

    contacts: list[ContactCandidate] = []
    seen: set[tuple[str, str]] = set()
    for row in list(google) + list(bing):
        if not isinstance(row, dict):
            continue
        name = _clean_name(str(row.get("name") or ""))
        title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip(" -|,;:")
        url = str(row.get("url") or "").strip()
        raw = str(row.get("text") or "").strip()
        if not name or not _wanted_title(title, requested):
            continue
        key = (name.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        contacts.append(ContactCandidate(
            name=name,
            title=title,
            linkedin_url=url if "linkedin.com/in" in url.lower() else "",
            source_url=url,
            source_snippet=raw[:350],
            score=120 + min(20, role_score(title, requested) // 10),
        ))

    return sorted(contacts, key=lambda item: item.score, reverse=True)[:8]


async def enrich_person_crosslinked(company: str, website: str, requested: list[str]) -> dict:
    from .enricher import domain_from_url

    people_task = asyncio.create_task(find_people_crosslinked(company, requested))
    evidence_task = asyncio.create_task(email_evidence(company, website))
    people, evidence = await asyncio.gather(people_task, evidence_task)
    public_emails, approved, pages = evidence
    person = people[0] if people else None
    domain = domain_from_url(website)
    candidates = candidate_emails(person, public_emails, people, approved, domain) if person else []

    return {
        "Company": company,
        "Website URL": website,
        "Contact Name": person.name if person else "",
        "Job Title": person.title if person else "",
        "LinkedIn URL": person.linkedin_url if person else "",
        "Contact Source": person.source_url if person else "",
        "Review Candidate Email": candidates[0]["email"] if candidates else "",
        "Email Confidence": f"{candidates[0]['score']}/100 — {candidates[0]['evidence']}" if candidates else "",
        "Email Pattern": candidates[0]["pattern"] if candidates else "",
        "Approved Email Domains": "; ".join(sorted(approved)),
        "Public Email Evidence": "; ".join(item.email for item in public_emails[:10]),
        "Candidate Emails": "; ".join(item["email"] for item in candidates[:6]),
        "People Found": len(people),
        "Pages Checked": len(pages),
        "Ready to Email": "NO",
    }
