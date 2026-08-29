from __future__ import annotations

import asyncio
import re
import unicodedata
from typing import Any

from ddgs import DDGS

from .enricher import ContactCandidate, domain_from_url, role_score

ROLE_HINTS = {
    "marketing": ["marketing", "cmo", "growth", "brand"],
    "pr & communications": ["communications", "public relations", "pr", "media relations"],
    "partnerships & business development": ["partnerships", "business development", "alliances"],
    "sales": ["sales", "commercial", "revenue"],
    "content & editorial": ["content", "editorial", "editor"],
    "seo & digital": ["seo", "digital", "growth"],
    "leadership / founder": ["founder", "ceo", "chief executive", "president", "owner"],
}

BAD_NAME_WORDS = {
    "linkedin", "marketing", "communications", "director", "manager", "president",
    "chief", "officer", "founder", "sales", "partnerships", "business", "development",
    "company", "team", "robotics", "technology", "technologies", "global", "official",
    "profile", "jobs", "careers", "people", "employees",
}


def _ascii(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", _ascii(value)).strip()


def _human_name(value: str) -> str:
    value = _norm(value).strip(" -|,;:·")
    if not value or len(value) > 65 or any(mark in value for mark in ["@", "://", "?", "!"]):
        return ""
    words = value.split()
    if not 2 <= len(words) <= 5:
        return ""
    tokens = [re.sub(r"[^a-z]", "", w.lower()) for w in words]
    if any(not token or len(token) < 2 or token in BAD_NAME_WORDS for token in tokens):
        return ""
    if any(not re.fullmatch(r"[A-Za-z'`.-]+", word) for word in words):
        return ""
    return " ".join(word if any(ch.isupper() for ch in word[1:]) else word.capitalize() for word in words)


def _role_terms(requested: list[str]) -> list[str]:
    terms: list[str] = []
    for item in requested:
        terms.extend(ROLE_HINTS.get(item.lower(), [item.lower()]))
    return list(dict.fromkeys(term.strip() for term in terms if term.strip()))


def _wanted_title(value: str, requested: list[str]) -> str:
    value = _norm(value).strip(" -|,;:·")
    if not value or len(value) > 120 or len(value.split()) > 16:
        return ""
    if any(mark in value.lower() for mark in ["http://", "https://"]):
        return ""
    if any(mark in value for mark in ["!", "?"]):
        return ""
    lower = value.lower()
    terms = _role_terms(requested)
    if any(term in lower for term in terms) or role_score(value, requested) > 0:
        return value
    return ""


def _company_tokens(company: str, website: str) -> list[str]:
    skip = {"company", "inc", "incorporated", "group", "global", "technology", "technologies", "robotics", "robot", "solutions"}
    tokens = [t for t in re.findall(r"[a-z0-9]+", _ascii(company).lower()) if len(t) >= 4 and t not in skip]
    domain = domain_from_url(website)
    if domain:
        label = domain.split(".")[0].replace("-", "")
        if len(label) >= 4:
            tokens.append(label)
    return list(dict.fromkeys(tokens))


def _company_match(company: str, website: str, text: str) -> bool:
    haystack = re.sub(r"[^a-z0-9]", "", _ascii(text).lower())
    return any(re.sub(r"[^a-z0-9]", "", token) in haystack for token in _company_tokens(company, website))


def _extract_name_title(title: str, body: str, requested: list[str]) -> tuple[str, str]:
    title = _norm(title)
    body = _norm(body)
    title = re.sub(r"\s*[|·]\s*LinkedIn\s*$", "", title, flags=re.I)

    for sep in [" - ", " – ", " — ", " | ", " · "]:
        if sep in title:
            left, right = title.split(sep, 1)
            name = _human_name(left)
            job = _wanted_title(re.split(r"\s+[|·]\s+", right, maxsplit=1)[0], requested)
            if name and job:
                return name, job

    name = _human_name(re.split(r"\s+[|·]\s+", title, maxsplit=1)[0])
    if name:
        role_patterns = [
            r"(?:^|[.;|])\s*([^.;|]{3,110}?\b(?:marketing|communications|public relations|partnerships|business development|sales|commercial|growth|brand|founder|chief executive|ceo|president)\b[^.;|]{0,60})",
            r"(?:current|experience)\s*[:\-]\s*([^.;|]{3,110})",
        ]
        for pattern in role_patterns:
            match = re.search(pattern, body, re.I)
            if match:
                job = _wanted_title(match.group(1), requested)
                if job:
                    return name, job

    comma = re.match(r"^([A-Za-z'`.-]+(?:\s+[A-Za-z'`.-]+){1,4}),\s*(.{3,110})$", title)
    if comma:
        name = _human_name(comma.group(1))
        job = _wanted_title(comma.group(2), requested)
        if name and job:
            return name, job
    return "", ""


def _search_sync(query: str, backend: str) -> list[dict[str, Any]]:
    try:
        results = DDGS(timeout=5).text(
            query,
            region="us-en",
            safesearch="off",
            max_results=10,
            backend=backend,
        )
        return results if isinstance(results, list) else []
    except Exception:
        return []


async def _search(query: str, backend: str) -> list[dict[str, Any]]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_search_sync, query, backend), timeout=7)
    except Exception:
        return []


async def find_people_ddgs(company: str, website: str, requested: list[str]) -> list[ContactCandidate]:
    """Find strong public person/company matches without opening LinkedIn pages."""
    terms = _role_terms(requested)[:8]
    role_query = " OR ".join(f'"{term}"' for term in terms)
    queries = [
        f'site:linkedin.com/in "{company}" ({role_query})',
        f'"{company}" ({role_query}) LinkedIn',
    ]
    backends = ["brave,duckduckgo,mojeek,startpage,yahoo", "auto"]
    groups = await asyncio.gather(*[
        asyncio.create_task(_search(query, backend))
        for query, backend in zip(queries, backends)
    ])

    candidates: list[ContactCandidate] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for row in group:
            if not isinstance(row, dict):
                continue
            result_title = str(row.get("title") or "")
            href = str(row.get("href") or row.get("url") or "").strip()
            body = str(row.get("body") or row.get("description") or "")
            combined = f"{result_title} {body} {href}"
            if not _company_match(company, website, combined):
                continue
            if "linkedin.com/in" not in href.lower() and "linkedin.com/in" not in combined.lower():
                continue
            name, job = _extract_name_title(result_title, body, requested)
            if not name or not job:
                continue
            key = (name.lower(), job.lower())
            if key in seen:
                continue
            seen.add(key)
            score = 120 + min(20, max(0, role_score(job, requested)) // 10)
            if "linkedin.com/in" in href.lower():
                score += 15
            candidates.append(ContactCandidate(
                name=name,
                title=job,
                linkedin_url=href if "linkedin.com/in" in href.lower() else "",
                source_url=href,
                source_snippet=f"{result_title} — {body}"[:350],
                score=score,
            ))

    return sorted(candidates, key=lambda item: item.score, reverse=True)[:8]
