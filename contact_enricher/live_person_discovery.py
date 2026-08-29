from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import defaultdict
from urllib.parse import urlparse

from .enricher import ContactCandidate, dedupe_contacts, domain_from_url, role_score
from .live_sources import _search, company_match_score
from .person_search import _extract_name_title, _role_terms


def _norm_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z]", "", value.lower())


def _source_host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_official_source(url: str, website: str) -> bool:
    domain = domain_from_url(website)
    host = _source_host(url)
    return bool(domain and host and (host == domain or host.endswith("." + domain)))


def _contains_role_term(text: str, term: str) -> bool:
    lower = (text or "").lower()
    term = (term or "").strip().lower()
    if not term:
        return False
    if len(term) <= 3:
        return bool(re.search(rf"\b{re.escape(term)}\b", lower))
    return term in lower


def matches_requested_role(text: str, requested: list[str]) -> bool:
    """Require evidence for the requested role families, not any built-in leadership title."""
    terms = _role_terms([item.strip() for item in requested if item.strip()])
    return any(_contains_role_term(text, term) for term in terms)


def _freshness_adjustment(text: str) -> int:
    lower = (text or "").lower()
    score = 0
    if any(token in lower for token in ("2026", "2025", "current", "currently", "today", "appointed", "joins", "named")):
        score += 10
    years = [int(match.group(0)) for match in re.finditer(r"\b20(?:1\d|2\d)\b", lower)]
    if years and max(years) <= 2022 and not any(token in lower for token in ("current", "currently")):
        score -= 15
    return score


def _candidate_score(candidate: ContactCandidate, company: str, website: str, requested: list[str]) -> int:
    evidence = f"{candidate.title} {candidate.source_snippet} {candidate.source_url}"
    score = role_score(evidence, requested)
    score += company_match_score(company, website, evidence, candidate.source_url)
    if _is_official_source(candidate.source_url, website):
        score += 55
    if "linkedin.com/in/" in (candidate.linkedin_url or candidate.source_url).lower():
        score += 20
    score += _freshness_adjustment(evidence)
    if candidate.name and len(candidate.name.split()) >= 2:
        score += 10
    return score


async def find_people_live(
    company: str,
    website: str,
    requested: list[str],
    site_contacts: list[ContactCandidate] | None = None,
) -> list[ContactCandidate]:
    """Find current decision makers using official pages first, then public search evidence.

    LinkedIn result URLs/snippets may contribute evidence but LinkedIn profile pages are never opened.
    """
    requested = [item.strip() for item in requested if item.strip()]
    terms = _role_terms(requested)[:14]
    role_query = " OR ".join(f'"{term}"' for term in terms) or '"marketing"'
    domain = domain_from_url(website)

    queries: list[str] = []
    if domain:
        queries.append(f'site:{domain} ({role_query})')
    queries.append(f'"{company}" ({role_query}) 2026')
    queries.append(f'site:linkedin.com/in "{company}" ({role_query})')

    groups = await asyncio.gather(*[asyncio.create_task(_search(q, 8)) for q in queries[:3]])
    candidates: list[ContactCandidate] = []

    for item in site_contacts or []:
        if not item.name or len(item.name.split()) < 2:
            continue
        role_evidence = item.title or item.source_snippet
        if not matches_requested_role(role_evidence, requested):
            continue
        if not _is_official_source(item.source_url, website):
            continue
        item.score = max(item.score, _candidate_score(item, company, website, requested))
        candidates.append(item)

    for rows in groups:
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "")
            body = str(row.get("body") or row.get("description") or "")
            href = str(row.get("href") or row.get("url") or "").strip()
            combined = f"{title} {body} {href}"
            company_score = company_match_score(company, website, combined, href)
            if company_score < 40:
                continue
            name, job = _extract_name_title(title, body, requested)
            if not name or not job or not matches_requested_role(job, requested):
                continue
            linkedin = href if "linkedin.com/in/" in href.lower() else ""
            candidate = ContactCandidate(
                name=name,
                title=job,
                linkedin_url=linkedin,
                source_url=href,
                source_snippet=f"{title} — {body}"[:350],
                score=0,
            )
            candidate.score = _candidate_score(candidate, company, website, requested)
            candidates.append(candidate)

    grouped: defaultdict[str, list[ContactCandidate]] = defaultdict(list)
    for item in candidates:
        key = _norm_name(item.name)
        if key:
            grouped[key].append(item)

    merged: list[ContactCandidate] = []
    for items in grouped.values():
        items.sort(key=lambda item: item.score, reverse=True)
        best = items[0]
        unique_hosts = {_source_host(item.source_url) for item in items if _source_host(item.source_url)}
        if len(unique_hosts) >= 2:
            best.score += min(25, 10 * (len(unique_hosts) - 1))
            best.source_snippet = (best.source_snippet + f" · corroborated by {len(unique_hosts)} public sources")[:350]
        official = next((item for item in items if _is_official_source(item.source_url, website)), None)
        if official and best is not official:
            best.source_snippet = (best.source_snippet + f" · official company evidence: {official.source_url}")[:350]
            best.score += 15
        merged.append(best)

    merged = dedupe_contacts(merged)
    return sorted(merged, key=lambda item: item.score, reverse=True)[:8]
