from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import Counter, defaultdict
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from .enricher import ContactCandidate, EmailCandidate, domain_from_url, role_score
from .open_source_email_scraper import scrape_public_contact_data

# Pattern order comes from the MIT-licensed Prospector engine vendored in
# third_party/prospector-email-finder/. The upstream LICENSE is preserved there.
PATTERNS = [
    "first", "first.last", "firstlast", "flast", "f.last", "first.l",
    "firstl", "last.first", "lastfirst", "last", "first_last", "first-last",
]

ROLE_HINTS = {
    "marketing": ["marketing", "cmo", "growth"],
    "pr & communications": ["communications", "public relations", "pr ", "media relations"],
    "partnerships & business development": ["partnerships", "business development"],
    "sales": ["sales", "commercial"],
    "content & editorial": ["content", "editorial", "editor"],
    "seo & digital": ["seo", "digital", "growth"],
    "leadership / founder": ["founder", "ceo", "chief executive", "president", "owner"],
}
BAD_NAME_WORDS = {
    "marketing", "communications", "director", "manager", "president", "chief", "officer",
    "founder", "sales", "partnerships", "business", "development", "company", "team", "robotics",
    "technology", "technologies", "global", "solutions", "linkedin", "profile", "official",
}
GENERIC_LOCALS = {
    "info", "hello", "contact", "admin", "office", "team", "support", "sales", "marketing",
    "press", "media", "pr", "communications", "partnerships", "privacy", "legal", "careers",
    "jobs", "hr", "webmaster", "dmca", "abuse", "security", "service", "noreply", "no-reply",
}
FREE_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com",
    "proton.me", "protonmail.com", "gmx.com", "mail.com", "yandex.com",
}
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9%._-]+/?", re.I)


def _ascii(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def _tokens(value: str) -> list[str]:
    return [re.sub(r"[^a-z]", "", p) for p in _ascii(value).lower().split() if re.sub(r"[^a-z]", "", p)]


def _clean_result_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc:
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) if target else href
    return href


def _is_name(value: str) -> bool:
    value = re.sub(r"\s+", " ", value or "").strip(" -|,;:")
    if not value or len(value) > 60 or any(c in value for c in ".!?@:/"):
        return False
    words = value.split()
    toks = _tokens(value)
    if not 2 <= len(words) <= 5 or len(toks) != len(words):
        return False
    if any(len(t) < 2 or t in BAD_NAME_WORDS for t in toks):
        return False
    return True


def _wanted_title(value: str, requested: list[str]) -> bool:
    clean = re.sub(r"\s+", " ", value or "").strip(" -|,;:")
    if not clean or len(clean) > 110 or len(clean.split()) > 14:
        return False
    if "http" in clean.lower() or any(mark in clean for mark in [". ", "!", "?"]):
        return False
    lower = clean.lower()
    hints = []
    for item in requested:
        hints.extend(ROLE_HINTS.get(item.lower(), [item.lower()]))
    return any(h and h in lower for h in hints) or role_score(clean, requested) > 0


def _parse_person(title: str, snippet: str, requested: list[str]) -> tuple[str, str]:
    combined = re.sub(r"\s+", " ", f"{title} | {snippet}").strip()
    combined = re.sub(r"\s*\|\s*LinkedIn\s*", " | ", combined, flags=re.I)
    pieces = [p.strip() for p in re.split(r"\s*[|–—]\s*|\s+-\s+", combined) if p.strip()]
    name = ""
    job = ""
    for piece in pieces[:8]:
        candidate = re.split(r"\s+at\s+|\s+@\s+", piece, maxsplit=1, flags=re.I)[0].strip()
        if not name and _is_name(candidate):
            name = candidate
            continue
        if not job and _wanted_title(candidate, requested):
            job = candidate
    # Common search format: "Jane Doe - Director of Marketing at Company"
    if name and not job:
        m = re.search(rf"{re.escape(name)}\s*[-|–—]\s*([^|]{{3,100}}?)(?:\s+at\s+|\s*[|]|$)", combined, re.I)
        if m and _wanted_title(m.group(1), requested):
            job = m.group(1).strip()
    return name, job


def _company_match(company: str, domain: str, text: str) -> bool:
    lower = _ascii(text).lower().replace("-", "")
    domain_label = domain.split(".")[0].lower().replace("-", "")
    company_tokens = [t for t in re.findall(r"[a-z0-9]+", _ascii(company).lower()) if len(t) >= 4]
    company_tokens = [t for t in company_tokens if t not in {"company", "technologies", "technology", "robotics", "global", "group", "solutions"}]
    if len(domain_label) >= 5 and domain_label in lower:
        return True
    return any(t in lower for t in company_tokens[:3])


async def _ddg(query: str, requested: list[str], company: str, domain: str) -> list[ContactCandidate]:
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    found: list[ContactCandidate] = []
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; ChrissaAutomatesContactEnricher/2.1)"},
            follow_redirects=True,
            timeout=httpx.Timeout(6.0, connect=3.0),
        ) as client:
            response = await client.get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        for result in soup.select(".result")[:8]:
            a = result.select_one(".result__a")
            snippet_el = result.select_one(".result__snippet")
            if not a:
                continue
            result_title = a.get_text(" ", strip=True)
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            text = f"{result_title} {snippet}"
            if not _company_match(company, domain, text):
                continue
            name, job = _parse_person(result_title, snippet, requested)
            if not name or not job:
                continue
            target = _clean_result_url(a.get("href", ""))
            linkedin = target.rstrip("/") if LINKEDIN_RE.fullmatch(target.rstrip("/")) else ""
            score = 100 + (10 if linkedin else 0) + min(20, role_score(job, requested) // 10)
            found.append(ContactCandidate(
                name=name,
                title=job,
                linkedin_url=linkedin,
                source_url=target or url,
                source_snippet=f"{result_title} — {snippet}"[:350],
                score=score,
            ))
    except Exception:
        return []
    return found


async def find_people_fast(company: str, website: str, requested: list[str]) -> list[ContactCandidate]:
    domain = domain_from_url(website)
    role_words = []
    for item in requested[:4]:
        role_words.extend(ROLE_HINTS.get(item.lower(), [item]))
    role_words = list(dict.fromkeys(role_words))[:6]
    roles = " OR ".join(f'"{r}"' for r in role_words)
    queries = [
        f'site:linkedin.com/in "{company}" ({roles})',
        f'"{company}" ({roles}) "{domain}"',
    ]
    groups = await asyncio.gather(*[_ddg(q, requested, company, domain) for q in queries])
    merged: list[ContactCandidate] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for item in group:
            key = (item.name.lower(), item.title.lower())
            if key not in seen:
                seen.add(key)
                merged.append(item)
    return sorted(merged, key=lambda c: c.score, reverse=True)[:6]


def _email_host(email: str) -> str:
    return email.rsplit("@", 1)[1].lower().strip(".") if "@" in email else ""


def _email_local(email: str) -> str:
    return email.split("@", 1)[0].lower() if "@" in email else ""


def _person_email(email: str) -> bool:
    local = _email_local(email)
    return bool(local and local not in GENERIC_LOCALS and not any(x in local for x in ["noreply", "no-reply", "privacy", "webmaster", "dmca"]))


def _related_domain(company: str, website_domain: str, other: str) -> bool:
    if not other or other in FREE_DOMAINS:
        return False
    if other == website_domain or other.endswith("." + website_domain):
        return True
    a = website_domain.split(".")[0].replace("-", "")
    b = other.split(".")[0].replace("-", "")
    prefix = ""
    for x, y in zip(a, b):
        if x != y:
            break
        prefix += x
    if len(prefix) >= 5:
        return True
    tokens = [t for t in re.findall(r"[a-z0-9]+", _ascii(company).lower()) if len(t) >= 5]
    return any(t in b for t in tokens if t not in {"robotics", "technologies", "technology"})


async def email_evidence(company: str, website: str) -> tuple[list[EmailCandidate], set[str], list[str]]:
    domain = domain_from_url(website)
    try:
        result = await asyncio.wait_for(
            scrape_public_contact_data(website, timeout_seconds=8, max_links_from_page=3, browser_fallback=False),
            timeout=9,
        )
    except Exception:
        return [], ({domain} if domain else set()), []
    approved = {domain} if domain else set()
    emails: list[EmailCandidate] = [
        EmailCandidate(email=e, source_url=(result.pages_checked[0] if result.pages_checked else website), source_type="website-evidence", confidence="public")
        for e in result.emails
    ]
    grouped: dict[str, list[str]] = defaultdict(list)
    for email in result.rejected_external_emails or []:
        grouped[_email_host(email)].append(email)
    for host, values in grouped.items():
        if _related_domain(company, domain, host):
            approved.add(host)
            emails.extend(EmailCandidate(email=e, source_url=website, source_type="related-domain-evidence", confidence="public-related-domain") for e in values)
    return emails, approved, result.pages_checked


def _patterns(name: str, domain: str) -> list[tuple[str, str]]:
    parts = _tokens(name)
    if len(parts) < 2 or not domain:
        return []
    f, l = parts[0], parts[-1]
    vals = [
        f"{f}@{domain}", f"{f}.{l}@{domain}", f"{f}{l}@{domain}", f"{f[0]}{l}@{domain}",
        f"{f[0]}.{l}@{domain}", f"{f}.{l[0]}@{domain}", f"{f}{l[0]}@{domain}",
        f"{l}.{f}@{domain}", f"{l}{f}@{domain}", f"{l}@{domain}", f"{f}_{l}@{domain}", f"{f}-{l}@{domain}",
    ]
    return list(zip(PATTERNS, vals))


def infer_pattern(public_emails: list[EmailCandidate], people: list[ContactCandidate], approved: set[str]) -> tuple[str, int]:
    votes: Counter[str] = Counter()
    for item in public_emails:
        email = item.email.lower()
        if _email_host(email) not in approved or not _person_email(email):
            continue
        for person in people:
            for label, candidate in _patterns(person.name, _email_host(email)):
                if candidate == email:
                    votes[label] += 1
                    break
    if votes:
        return votes.most_common(1)[0]
    # Structural evidence is weaker but still useful for ranking Review candidates.
    for item in public_emails:
        local = _email_local(item.email)
        if _email_host(item.email) in approved and re.fullmatch(r"[a-z]{2,}\.[a-z]{2,}", local):
            return "first.last", 0
    return "", 0


def candidate_emails(person: ContactCandidate, public_emails: list[EmailCandidate], people: list[ContactCandidate], approved: set[str], website_domain: str) -> list[dict]:
    pattern, matched = infer_pattern(public_emails, people, approved)
    ranked: dict[str, dict] = {}
    person_tokens = _tokens(person.name)
    first = person_tokens[0] if person_tokens else ""
    last = person_tokens[-1] if person_tokens else ""
    for item in public_emails:
        email = item.email.lower()
        local = _email_local(email)
        if _email_host(email) in approved and _person_email(email) and ((last and last in local) or (first and len(first) >= 3 and first in local)):
            ranked[email] = {"email": email, "score": 100, "pattern": "public-direct", "evidence": "public email matches the selected person's name", "source_url": item.source_url}
    domains = sorted(approved, key=lambda d: (d != website_domain, d))
    for domain in domains:
        for index, (label, email) in enumerate(_patterns(person.name, domain)):
            if email in ranked:
                continue
            score = 66 - min(index, 10)
            evidence = f"Prospector {label} candidate"
            if pattern and label == pattern:
                score = 93 if matched else 80
                evidence = f"company pattern {label}" + (f" matched {matched} public person/email pair(s)" if matched else " inferred from public email structure")
            ranked[email] = {"email": email, "score": score, "pattern": label, "evidence": evidence, "source_url": person.source_url}
    return sorted(ranked.values(), key=lambda x: x["score"], reverse=True)[:10]


async def enrich_person_fast(company: str, website: str, requested: list[str]) -> dict:
    domain = domain_from_url(website)
    people_task = asyncio.create_task(find_people_fast(company, website, requested))
    evidence_task = asyncio.create_task(email_evidence(company, website))
    people, evidence = await asyncio.gather(people_task, evidence_task)
    public_emails, approved, pages = evidence
    person = people[0] if people else None
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
        "Pages Checked": len(pages),
        "Ready to Email": "NO",
    }
