from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import Counter, defaultdict
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

from .enricher import (
    ContactCandidate,
    EmailCandidate,
    domain_from_url,
    duckduckgo_decision_makers,
    role_score,
)
from .open_source_email_scraper import scrape_public_contact_data

# Pattern order is adapted from the MIT-licensed Prospector engine vendored at
# third_party/prospector-email-finder/email-engine.js. Keep the upstream license.
PROSPECTOR_PATTERN_LABELS = [
    "first",
    "first.last",
    "firstlast",
    "flast",
    "f.last",
    "first.l",
    "firstl",
    "last.first",
    "lastfirst",
    "last",
    "first_last",
    "first-last",
]

ROLE_LOCALS = {
    "info", "hello", "contact", "admin", "office", "team", "support", "sales",
    "marketing", "press", "media", "pr", "communications", "partnerships", "billing",
    "privacy", "legal", "careers", "jobs", "hr", "webmaster", "dmca", "abuse",
    "security", "service", "customerservice", "customer.service", "noreply", "no-reply",
}
FREE_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com",
    "proton.me", "protonmail.com", "gmx.com", "mail.com", "yandex.com",
}
BAD_NAME_WORDS = {
    "marketing", "communications", "director", "manager", "president", "chief", "officer",
    "founder", "sales", "partnerships", "business", "development", "company", "team",
    "contact", "about", "robotics", "technology", "technologies", "global", "solutions",
}
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


def _ascii(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def _name_tokens(name: str) -> list[str]:
    cleaned = _ascii(name).lower()
    return [re.sub(r"[^a-z]", "", part) for part in cleaned.split() if re.sub(r"[^a-z]", "", part)]


def _valid_human_name(name: str) -> bool:
    name = (name or "").strip(" -|,;:")
    if not name or len(name) > 60 or any(ch in name for ch in ".!?@:/"):
        return False
    words = name.split()
    if not 2 <= len(words) <= 5:
        return False
    tokens = _name_tokens(name)
    if len(tokens) != len(words) or any(len(t) < 2 for t in tokens):
        return False
    if any(t in BAD_NAME_WORDS for t in tokens):
        return False
    return True


def _valid_job_title(title: str, requested_positions: list[str]) -> bool:
    title = re.sub(r"\s+", " ", (title or "")).strip(" -|,;:")
    if not title or len(title) > 120 or len(title.split()) > 16:
        return False
    if any(mark in title for mark in [". ", "!", "?", "http://", "https://"]):
        return False
    return role_score(title, requested_positions) > 0


def _parse_result_title(text: str, requested_positions: list[str]) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"\s*[|]\s*LinkedIn\s*$", "", text, flags=re.I)
    parts = [p.strip() for p in re.split(r"\s+[|–—-]\s+", text) if p.strip()]

    name = ""
    title = ""
    for part in parts[:6]:
        at_parts = re.split(r"\s+at\s+", part, maxsplit=1, flags=re.I)
        candidate = at_parts[0].strip()
        if not name and _valid_human_name(candidate) and role_score(candidate, requested_positions) == 0:
            name = candidate
            continue
        if not title and _valid_job_title(candidate, requested_positions):
            title = candidate

    if name and not title:
        after_name = text[text.lower().find(name.lower()) + len(name):].strip(" -|–—")
        for part in re.split(r"\s+[|–—-]\s+", after_name):
            part = re.split(r"\s+at\s+", part, maxsplit=1, flags=re.I)[0].strip()
            if _valid_job_title(part, requested_positions):
                title = part
                break
    return name, title


def sanitize_contacts(items: list[ContactCandidate], requested_positions: list[str]) -> list[ContactCandidate]:
    clean: list[ContactCandidate] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        result_title = (item.source_snippet or "").split(" — ", 1)[0].strip()
        parsed_name, parsed_title = _parse_result_title(result_title, requested_positions)
        name = item.name.strip() if _valid_human_name(item.name) else parsed_name
        title = item.title.strip() if _valid_job_title(item.title, requested_positions) else parsed_title
        if not name or not title:
            continue
        key = (name.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        clean.append(ContactCandidate(
            name=name,
            title=title,
            linkedin_url=item.linkedin_url,
            source_url=item.source_url,
            source_snippet=item.source_snippet[:350],
            score=item.score + 20,
        ))
    return sorted(clean, key=lambda x: x.score, reverse=True)[:8]


def _prospector_patterns(name: str, domain: str) -> list[tuple[str, str]]:
    parts = _name_tokens(name)
    if len(parts) < 2 or not domain:
        return []
    first, last = parts[0], parts[-1]
    values = [
        f"{first}@{domain}",
        f"{first}.{last}@{domain}",
        f"{first}{last}@{domain}",
        f"{first[0]}{last}@{domain}",
        f"{first[0]}.{last}@{domain}",
        f"{first}.{last[0]}@{domain}",
        f"{first}{last[0]}@{domain}",
        f"{last}.{first}@{domain}",
        f"{last}{first}@{domain}",
        f"{last}@{domain}",
        f"{first}_{last}@{domain}",
        f"{first}-{last}@{domain}",
    ]
    return list(zip(PROSPECTOR_PATTERN_LABELS, values))


def _email_host(email: str) -> str:
    return email.rsplit("@", 1)[1].lower().strip(".") if "@" in email else ""


def _email_local(email: str) -> str:
    return email.split("@", 1)[0].lower() if "@" in email else ""


def _is_person_email(email: str) -> bool:
    local = _email_local(email)
    if not local or local in ROLE_LOCALS:
        return False
    if any(local.startswith(prefix) for prefix in ["noreply", "no-reply", "privacy", "webmaster", "dmca"]):
        return False
    return bool(re.search(r"[a-z]{2,}", local))


def _brand_tokens(company: str, domain: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]{4,}", _ascii(company).lower()))
    label = domain.split(".")[0].replace("-", "")
    if len(label) >= 4:
        tokens.add(label)
    return {t for t in tokens if t not in {"company", "group", "global", "technologies", "technology", "robotics", "robot", "systems", "solutions", "incorporated"}}


def _related_corporate_domain(company: str, website_domain: str, candidate_domain: str) -> bool:
    if not candidate_domain or candidate_domain in FREE_DOMAINS:
        return False
    if candidate_domain == website_domain or candidate_domain.endswith("." + website_domain):
        return True
    a = website_domain.split(".")[0].replace("-", "")
    b = candidate_domain.split(".")[0].replace("-", "")
    prefix = ""
    for left, right in zip(a, b):
        if left != right:
            break
        prefix += left
    if len(prefix) >= 5:
        return True
    tokens = _brand_tokens(company, website_domain)
    return any(len(token) >= 5 and token in b for token in tokens)


async def collect_public_email_evidence(company: str, website: str) -> tuple[list[EmailCandidate], list[str], set[str], list[str]]:
    website_domain = domain_from_url(website)
    result = await scrape_public_contact_data(
        website,
        timeout_seconds=12,
        max_links_from_page=4,
        browser_fallback=False,
    )
    evidence: list[EmailCandidate] = []
    source_url = result.pages_checked[0] if result.pages_checked else website
    approved_domains = {website_domain} if website_domain else set()
    notes: list[str] = []

    for email in result.emails:
        evidence.append(EmailCandidate(email=email, source_url=source_url, source_type="open-source-website", confidence="public-company-email"))

    external_by_domain: dict[str, list[str]] = defaultdict(list)
    for email in result.rejected_external_emails or []:
        external_by_domain[_email_host(email)].append(email)

    for host, emails in external_by_domain.items():
        if _related_corporate_domain(company, website_domain, host):
            approved_domains.add(host)
            notes.append(f"related corporate domain discovered: {host}")
            for email in emails:
                evidence.append(EmailCandidate(email=email, source_url=source_url, source_type="related-corporate-domain", confidence="public-related-company-email"))

    return evidence, result.pages_checked, approved_domains, notes


async def search_public_person_emails(name: str, approved_domains: set[str]) -> list[EmailCandidate]:
    if not name or not approved_domains:
        return []
    domain_hint = sorted(approved_domains)[0]
    query = f'"{name}" "@{domain_hint}"'
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    found: dict[str, EmailCandidate] = {}
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "ChrissaAutomatesContactEnricher/2.0"}, follow_redirects=True, timeout=7) as client:
            response = await client.get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        for result in soup.select(".result")[:6]:
            title = result.select_one(".result__a")
            snippet = result.select_one(".result__snippet")
            text = " ".join([
                title.get_text(" ", strip=True) if title else "",
                snippet.get_text(" ", strip=True) if snippet else "",
            ])
            source = title.get("href", "") if title else url
            for email in EMAIL_RE.findall(text):
                email = email.lower().rstrip(".,;:)")
                if _email_host(email) in approved_domains and _is_person_email(email):
                    found[email] = EmailCandidate(email=email, source_url=source, source_type="person-search", confidence="public-person-email")
    except Exception:
        pass
    return list(found.values())


def learn_company_pattern(public_emails: list[EmailCandidate], contacts: list[ContactCandidate], approved_domains: set[str]) -> tuple[str, float, int]:
    votes: Counter[str] = Counter()
    matched_pairs = 0
    for email_item in public_emails:
        email = email_item.email.lower()
        if _email_host(email) not in approved_domains or not _is_person_email(email):
            continue
        for contact in contacts:
            for label, candidate in _prospector_patterns(contact.name, _email_host(email)):
                if candidate == email:
                    votes[label] += 1
                    matched_pairs += 1
                    break
    if votes:
        label, count = votes.most_common(1)[0]
        confidence = count / max(1, matched_pairs)
        return label, round(confidence, 2), matched_pairs

    # Low-confidence structural fallback when public personal emails exist but cannot
    # be paired to a named person. This never creates Ready-to-Email by itself.
    structural: Counter[str] = Counter()
    for item in public_emails:
        if _email_host(item.email) not in approved_domains or not _is_person_email(item.email):
            continue
        local = _email_local(item.email)
        if re.fullmatch(r"[a-z]{2,}\.[a-z]{2,}", local):
            structural["first.last"] += 1
        elif re.fullmatch(r"[a-z]{2,}_[a-z]{2,}", local):
            structural["first_last"] += 1
    if structural:
        label, count = structural.most_common(1)[0]
        return label, min(0.49, 0.3 + 0.05 * count), 0
    return "", 0.0, 0


def rank_person_email_candidates(
    contact: ContactCandidate,
    public_emails: list[EmailCandidate],
    contacts: list[ContactCandidate],
    approved_domains: set[str],
) -> list[dict]:
    if not contact or not contact.name:
        return []

    ranked: dict[str, dict] = {}
    name_tokens = _name_tokens(contact.name)
    pattern, pattern_confidence, matched_pairs = learn_company_pattern(public_emails, contacts, approved_domains)

    for item in public_emails:
        email = item.email.lower()
        if _email_host(email) not in approved_domains or not _is_person_email(email):
            continue
        local = _email_local(email)
        surname = name_tokens[-1] if name_tokens else ""
        first = name_tokens[0] if name_tokens else ""
        direct_match = (surname and surname in local) or (first and len(first) >= 3 and first in local)
        if direct_match:
            ranked[email] = {
                "email": email,
                "score": 100,
                "evidence": "public email matches this person's name",
                "source_url": item.source_url,
                "pattern": "public-direct",
            }

    domain_order = sorted(approved_domains, key=lambda d: (d != domain_from_url(contact.source_url), d))
    if not domain_order:
        domain_order = sorted(approved_domains)

    for domain in domain_order:
        for index, (label, email) in enumerate(_prospector_patterns(contact.name, domain)):
            if email in ranked:
                continue
            score = 68 - min(index, 10)
            evidence = f"Prospector {label} candidate"
            if pattern and label == pattern:
                score = 86 + int(pattern_confidence * 10)
                if matched_pairs:
                    evidence = f"company pattern {label} matched {matched_pairs} public name/email pair(s)"
                else:
                    evidence = f"company email structure suggests {label} (low confidence)"
            ranked[email] = {
                "email": email,
                "score": score,
                "evidence": evidence,
                "source_url": contact.source_url,
                "pattern": label,
            }

    return sorted(ranked.values(), key=lambda item: item["score"], reverse=True)[:12]


async def build_person_first_inputs(
    company: str,
    website: str,
    requested_positions: list[str],
) -> tuple[list[EmailCandidate], list[ContactCandidate], list[str], dict]:
    # Limit the public people search to the user's first four requested functions so
    # selecting every department does not explode into seven slow web searches.
    requested_for_search = requested_positions[:4]
    email_task = asyncio.create_task(collect_public_email_evidence(company, website))
    people_task = asyncio.create_task(
        duckduckgo_decision_makers(company, website, requested_for_search, max_results=4)
    )

    public_result, raw_contacts = await asyncio.gather(email_task, people_task)
    public_emails, pages_checked, approved_domains, notes = public_result
    contacts = sanitize_contacts(raw_contacts, requested_positions)

    # One focused search for the top two real people can surface addresses from press
    # releases, PDFs and public snippets that a normal website contact crawl misses.
    if contacts:
        person_searches = await asyncio.gather(*[
            search_public_person_emails(contact.name, approved_domains)
            for contact in contacts[:2]
        ])
        seen = {item.email for item in public_emails}
        for group in person_searches:
            for item in group:
                if item.email not in seen:
                    public_emails.append(item)
                    seen.add(item.email)

    best = contacts[0] if contacts else None
    ranked_candidates = rank_person_email_candidates(best, public_emails, contacts, approved_domains) if best else []
    meta = {
        "approved_domains": sorted(approved_domains),
        "notes": notes,
        "ranked_candidates": ranked_candidates,
        "pattern": learn_company_pattern(public_emails, contacts, approved_domains),
    }
    return public_emails, contacts, pages_checked, meta
