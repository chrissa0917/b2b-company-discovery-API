from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlparse

import httpx
from ddgs import DDGS

from .enricher import ContactCandidate, EmailCandidate, domain_from_url
from .reoon_integration import FREE_MAIL_DOMAINS, candidate_mail_domains

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)

GENERIC_PRIORITY = [
    "marketing", "partnerships", "businessdevelopment", "business-development",
    "sales", "press", "media", "communications", "pr", "hello", "contact",
    "info", "support", "team", "office", "enquiries", "inquiries",
]
BAD_GENERIC_LOCALS = {
    "noreply", "no-reply", "privacy", "abuse", "postmaster", "webmaster",
    "mailer-daemon", "newsletter", "updates", "notifications", "notification",
    "security", "legal", "billing", "careers", "jobs",
}

_GITHUB_CACHE: dict[str, tuple[float, tuple[list[EmailCandidate], list[ContactCandidate]]]] = {}
_GITHUB_DISABLED_UNTIL = 0.0
_GITHUB_LOCK = asyncio.Lock()


def _host(url: str) -> str:
    try:
        value = (urlparse(url).hostname or "").lower().strip(".")
    except Exception:
        return ""
    return value[4:] if value.startswith("www.") else value


def _same_site(source_url: str, website_domain: str) -> bool:
    host = _host(source_url)
    return bool(host and website_domain and (host == website_domain or host.endswith("." + website_domain)))


def _clean_email(value: str) -> str:
    return (value or "").strip().lower().rstrip(".,;:)>]")


def _email_host(email: str) -> str:
    return email.split("@", 1)[1].lower().strip(".") if "@" in email else ""


def _email_local(email: str) -> str:
    return email.split("@", 1)[0].lower().strip() if "@" in email else ""


def _looks_technical_or_artifact(email: str) -> bool:
    local = _email_local(email)
    host = _email_host(email)
    if not local or not host or len(local) > 64 or len(host) > 253:
        return True
    if local in BAD_GENERIC_LOCALS or any(local.startswith(x) for x in ("noreply", "no-reply", "mailer", "notification")):
        return True
    if re.fullmatch(r"[a-f0-9]{20,}", local):
        return True
    if any(token in email for token in (".png@", ".jpg@", ".jpeg@", ".gif@", ".svg@", "git@")):
        return True
    return False


def _name_tokens(name: str) -> list[str]:
    return [token for token in re.findall(r"[a-z]{2,}", (name or "").lower())]


def _name_affinity(email: str, name: str) -> int:
    local = re.sub(r"[^a-z]", "", _email_local(email))
    tokens = _name_tokens(name)
    if not local or len(tokens) < 2:
        return 0
    first, last = tokens[0], tokens[-1]
    score = 0
    if first in local:
        score += 55
    elif local.startswith(first[:1]) and last in local:
        score += 40
    if last in local:
        score += 45
    elif local.endswith(last[:1]) and first in local:
        score += 20
    return min(score, 100)


def _company_tokens(company: str, website: str) -> list[str]:
    skip = {
        "inc", "incorporated", "corp", "corporation", "company", "group", "global",
        "robot", "robots", "robotics", "technology", "technologies", "solutions",
        "systems", "international", "limited", "ltd", "llc",
    }
    tokens = [t for t in re.findall(r"[a-z0-9]+", (company or "").lower()) if len(t) >= 3 and t not in skip]
    domain = domain_from_url(website)
    for label in domain.split("."):
        clean = re.sub(r"[^a-z0-9]", "", label.lower())
        if len(clean) >= 3 and clean not in {"www", "com", "net", "org", "io", "ai", "co", "global"}:
            tokens.append(clean)
    return list(dict.fromkeys(tokens))


def company_match_score(company: str, website: str, text: str, source_url: str = "") -> int:
    haystack = (text or "").lower()
    compact = re.sub(r"[^a-z0-9]", "", haystack)
    score = 0
    for token in _company_tokens(company, website):
        if len(token) <= 3:
            if re.search(rf"\b{re.escape(token)}\b", haystack):
                score = max(score, 40)
        elif re.sub(r"[^a-z0-9]", "", token) in compact:
            score = max(score, 50)
    domain = domain_from_url(website)
    source_host = _host(source_url)
    if domain and source_host and (source_host == domain or source_host.endswith("." + domain)):
        score += 50
    return min(score, 100)


def _search_sync(query: str, max_results: int = 8) -> list[dict]:
    try:
        rows = DDGS(timeout=6).text(
            query,
            region="us-en",
            safesearch="off",
            max_results=max_results,
            backend="auto",
        )
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


async def _search(query: str, max_results: int = 8) -> list[dict]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_search_sync, query, max_results), timeout=9)
    except Exception:
        return []


async def discover_person_email_snippets(
    company: str,
    website: str,
    contact: ContactCandidate | None,
    public_emails: list[EmailCandidate],
) -> tuple[list[EmailCandidate], list[ContactCandidate]]:
    """Find exact public person-email evidence in search snippets without opening LinkedIn."""
    if not contact or not contact.name:
        return [], []
    website_domain = domain_from_url(website)
    domains = candidate_mail_domains(public_emails, website_domain) or ([website_domain] if website_domain else [])
    if not domains:
        return [], []

    queries: list[str] = []
    for domain in domains[:2]:
        queries.append(f'"{contact.name}" "@{domain}"')
        queries.append(f'"{contact.name}" "{domain}" email')

    groups = await asyncio.gather(*[asyncio.create_task(_search(q, 8)) for q in queries[:3]])
    found_emails: dict[str, EmailCandidate] = {}
    found_contacts: list[ContactCandidate] = []
    allowed_domains = set(domains)

    for rows in groups:
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "")
            body = str(row.get("body") or row.get("description") or "")
            href = str(row.get("href") or row.get("url") or "")
            text = f"{title} {body}"
            company_score = company_match_score(company, website, text, href)
            if company_score < 40:
                continue
            for raw in EMAIL_RE.findall(text):
                email = _clean_email(raw)
                if _email_host(email) not in allowed_domains or _looks_technical_or_artifact(email):
                    continue
                if _name_affinity(email, contact.name) < 70:
                    continue
                key = email.lower()
                if key in found_emails:
                    continue
                source = href or f"search:{company}"
                found_emails[key] = EmailCandidate(
                    email=email,
                    source_url=source,
                    source_type="public-search-snippet",
                    mx_valid=True,
                    confidence="public-person-email-snippet",
                )
                found_contacts.append(ContactCandidate(
                    name=contact.name,
                    title=contact.title,
                    linkedin_url=contact.linkedin_url,
                    source_url=source,
                    source_snippet=text[:350],
                    score=max(contact.score, 145),
                ))
    return list(found_emails.values()), found_contacts


async def discover_github_pattern_evidence(domain: str) -> tuple[list[EmailCandidate], list[ContactCandidate]]:
    """Harvest public Git commit author emails for one corporate domain.

    This is optional evidence. If GitHub rate-limits anonymous search, the pipeline simply skips it.
    """
    global _GITHUB_DISABLED_UNTIL
    domain = (domain or "").lower().strip(".")
    if not domain or domain in FREE_MAIL_DOMAINS:
        return [], []
    now = time.time()
    cached = _GITHUB_CACHE.get(domain)
    if cached and now - cached[0] < 3600:
        return cached[1]
    if now < _GITHUB_DISABLED_UNTIL:
        return [], []

    async with _GITHUB_LOCK:
        now = time.time()
        cached = _GITHUB_CACHE.get(domain)
        if cached and now - cached[0] < 3600:
            return cached[1]
        if now < _GITHUB_DISABLED_UNTIL:
            return [], []

        url = "https://api.github.com/search/commits"
        params = {"q": f"author-email:@{domain}", "sort": "author-date", "per_page": 25}
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ChrissaAutomatesContactEnricher/3.0",
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=4.0)) as client:
                response = await client.get(url, params=params, headers=headers)
        except Exception:
            return [], []
        if response.status_code in {403, 429}:
            reset = response.headers.get("x-ratelimit-reset")
            try:
                _GITHUB_DISABLED_UNTIL = max(time.time() + 120, float(reset or 0))
            except Exception:
                _GITHUB_DISABLED_UNTIL = time.time() + 600
            return [], []
        if response.status_code >= 400:
            return [], []
        try:
            payload = response.json()
        except Exception:
            return [], []

        emails: dict[str, EmailCandidate] = {}
        contacts: list[ContactCandidate] = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
            author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
            email = _clean_email(str(author.get("email") or ""))
            name = str(author.get("name") or "").strip()
            if _email_host(email) != domain or _looks_technical_or_artifact(email):
                continue
            local = _email_local(email)
            if local in GENERIC_PRIORITY or "noreply" in email or email.endswith("@users.noreply.github.com"):
                continue
            if len(_name_tokens(name)) < 2 or _name_affinity(email, name) < 60:
                continue
            source = str(item.get("html_url") or item.get("url") or "https://github.com")
            if email not in emails:
                emails[email] = EmailCandidate(
                    email=email,
                    source_url=source,
                    source_type="github-public-commit",
                    mx_valid=True,
                    confidence="public-github-employee-email",
                )
                contacts.append(ContactCandidate(
                    name=name,
                    title="",
                    source_url=source,
                    source_snippet="Public Git commit author email on the corporate domain.",
                    score=25,
                ))
        result = (list(emails.values()), contacts)
        _GITHUB_CACHE[domain] = (time.time(), result)
        return result


async def augment_email_evidence(
    company: str,
    website: str,
    contact: ContactCandidate | None,
    public_emails: list[EmailCandidate],
    site_contacts: list[ContactCandidate],
) -> tuple[list[EmailCandidate], list[ContactCandidate]]:
    """Add free live public evidence used for exact lookup and pattern learning."""
    website_domain = domain_from_url(website)
    mail_domains = candidate_mail_domains(public_emails, website_domain) or ([website_domain] if website_domain else [])

    snippet_emails, snippet_contacts = await discover_person_email_snippets(
        company, website, contact, public_emails
    )

    personal_hosts = [
        _email_host(item.email) for item in public_emails
        if item.email and _email_local(item.email) not in GENERIC_PRIORITY and not _looks_technical_or_artifact(item.email)
    ]
    github_emails: list[EmailCandidate] = []
    github_contacts: list[ContactCandidate] = []
    if len(personal_hosts) < 2 and mail_domains:
        github_emails, github_contacts = await discover_github_pattern_evidence(mail_domains[0])

    merged: dict[str, EmailCandidate] = {}
    for item in [*public_emails, *snippet_emails, *github_emails]:
        email = _clean_email(item.email)
        if not email or "@" not in email:
            continue
        existing = merged.get(email)
        if existing is None or (item.source_type == "public-search-snippet" and existing.source_type != "public-search-snippet"):
            merged[email] = item

    return list(merged.values()), [*site_contacts, *snippet_contacts, *github_contacts]


def select_generic_company_email(
    public_emails: list[EmailCandidate],
    website: str,
    requested_positions: list[str] | None = None,
) -> tuple[str, str, str]:
    """Choose a clean public company inbox without attributing it to a person."""
    website_domain = domain_from_url(website)
    corporate_domains = set(candidate_mail_domains(public_emails, website_domain))
    if website_domain:
        corporate_domains.add(website_domain)

    role_bias: list[str] = []
    joined = " ".join(requested_positions or []).lower()
    if "marketing" in joined:
        role_bias += ["marketing", "communications", "press", "media"]
    if "partnership" in joined or "business development" in joined:
        role_bias += ["partnerships", "businessdevelopment", "business-development", "sales"]
    if "sales" in joined:
        role_bias += ["sales"]
    priority = list(dict.fromkeys([*role_bias, *GENERIC_PRIORITY]))
    priority_index = {name: idx for idx, name in enumerate(priority)}

    ranked: list[tuple[int, EmailCandidate]] = []
    for item in public_emails:
        email = _clean_email(item.email)
        if not email or "@" not in email or _looks_technical_or_artifact(email):
            continue
        local, host = email.split("@", 1)
        official_source = _same_site(item.source_url, website_domain)
        corporate_host = host in corporate_domains or (website_domain and host.endswith("." + website_domain))
        free_mail = host in FREE_MAIL_DOMAINS

        if free_mail and not official_source:
            continue
        if not free_mail and not corporate_host:
            continue
        if item.mx_valid is False:
            continue

        score = 0
        if official_source:
            score += 60
        if corporate_host:
            score += 35
        if item.mx_valid is True:
            score += 15
        if local in priority_index:
            score += max(5, 40 - priority_index[local])
        elif local in BAD_GENERIC_LOCALS:
            continue
        else:
            continue
        ranked.append((score, item))

    if not ranked:
        return "", "", ""
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    best = ranked[0][1]
    note = "Public company inbox on the official site" if _same_site(best.source_url, website_domain) else "Public company inbox"
    return best.email, best.source_url, note
