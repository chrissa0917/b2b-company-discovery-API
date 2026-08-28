from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import dns.resolver
import httpx
from bs4 import BeautifulSoup

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
OBFUSCATED_RE = re.compile(
    r"([A-Z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\sat\s)\s*([A-Z0-9.-]+)\s*(?:\[dot\]|\(dot\)|\sdot\s)\s*([A-Z]{2,})",
    re.I,
)
ROLE_TERMS = [
    ("head of marketing", 100), ("vp marketing", 98), ("vice president marketing", 98),
    ("marketing director", 96), ("director of marketing", 96), ("marketing manager", 92),
    ("head of communications", 91), ("communications director", 90), ("public relations", 89),
    ("pr manager", 88), ("media relations", 87), ("content marketing", 86),
    ("content manager", 85), ("editor", 84), ("partnerships", 83),
    ("business development", 78), ("seo manager", 77), ("digital marketing", 76),
    ("founder", 70), ("chief executive officer", 69), ("ceo", 68), ("owner", 67),
]
GENERIC_PRIORITY = [
    "marketing", "press", "media", "pr", "communications", "partnerships", "businessdevelopment",
    "business-development", "hello", "contact", "info", "sales"
]
PAGE_HINTS = [
    "contact", "about", "team", "people", "leadership", "press", "media", "news", "partners",
    "partnership", "marketing", "company"
]
USER_AGENT = "BuyAndRentRobotsContactEnricher/1.0 (+https://buyandrentrobots.com)"

@dataclass
class ContactCandidate:
    name: str = ""
    title: str = ""
    linkedin_url: str = ""
    source_url: str = ""
    source_snippet: str = ""
    score: int = 0

@dataclass
class EmailCandidate:
    email: str
    source_url: str = ""
    source_type: str = "public"
    mx_valid: bool | None = None
    confidence: str = ""


def normalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def domain_from_url(value: str) -> str:
    try:
        host = urlparse(normalize_url(value)).hostname or ""
    except Exception:
        return ""
    host = host.lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def decode_cfemail(encoded: str) -> str:
    try:
        key = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i+2], 16) ^ key) for i in range(2, len(encoded), 2))
    except Exception:
        return ""


def extract_emails(text: str, soup: BeautifulSoup | None = None) -> set[str]:
    found = {m.group(0).lower().rstrip(".,;:)>") for m in EMAIL_RE.finditer(text or "")}
    for m in OBFUSCATED_RE.finditer(text or ""):
        found.add(f"{m.group(1)}@{m.group(2)}.{m.group(3)}".lower())
    if soup:
        for tag in soup.select("[data-cfemail]"):
            value = decode_cfemail(tag.get("data-cfemail", ""))
            if value:
                found.add(value.lower())
        for a in soup.select('a[href^="mailto:"]'):
            addr = a.get("href", "")[7:].split("?")[0].strip()
            if EMAIL_RE.fullmatch(addr):
                found.add(addr.lower())
    return {e for e in found if not e.endswith(("@example.com", "@domain.com"))}


def email_rank(email: str, domain: str) -> int:
    local, _, host = email.partition("@")
    score = 10
    if host == domain or host.endswith("." + domain):
        score += 40
    for idx, role in enumerate(GENERIC_PRIORITY):
        if local == role:
            score += 50 - idx
            break
    if any(token in local for token in ["noreply", "no-reply", "privacy", "abuse", "support"]):
        score -= 30
    return score


def role_score(text: str) -> int:
    t = (text or "").lower()
    return max((score for term, score in ROLE_TERMS if term in t), default=0)


def infer_name_title(text: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", html.unescape(text or "")).strip(" -|•")
    if not role_score(cleaned):
        return "", ""
    parts = re.split(r"\s+[|–—-]\s+|\s+at\s+", cleaned, maxsplit=2, flags=re.I)
    possible_name = ""
    possible_title = cleaned
    for part in parts:
        words = part.strip().split()
        if 2 <= len(words) <= 5 and all(re.match(r"^[A-Za-zÀ-ÖØ-öø-ÿ'`.]+$", w) for w in words):
            if role_score(part) == 0:
                possible_name = part.strip()
        if role_score(part) > 0:
            possible_title = part.strip()
    return possible_name, possible_title


async def mx_valid(domain: str) -> bool:
    if not domain:
        return False
    loop = asyncio.get_running_loop()
    def _check() -> bool:
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=5)
            return bool(list(answers))
        except Exception:
            return False
    return await loop.run_in_executor(None, _check)


async def allowed_by_robots(client: httpx.AsyncClient, base_url: str, target_url: str) -> bool:
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        r = await client.get(robots_url, timeout=8)
        if r.status_code >= 400:
            return True
        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.parse(r.text.splitlines())
        return rp.can_fetch(USER_AGENT, target_url)
    except Exception:
        return True


async def crawl_company(website: str, max_pages: int = 12) -> tuple[list[EmailCandidate], list[ContactCandidate], list[str]]:
    website = normalize_url(website)
    domain = domain_from_url(website)
    if not website or not domain:
        return [], [], []

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    limits = httpx.Limits(max_connections=6, max_keepalive_connections=3)
    emails: dict[str, EmailCandidate] = {}
    contacts: list[ContactCandidate] = []
    visited: list[str] = []

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, limits=limits) as client:
        queue = [website]
        queue += [urljoin(website.rstrip("/") + "/", p) for p in ["contact", "about", "team", "press", "media", "partners"]]
        seen = set()

        while queue and len(visited) < max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if domain_from_url(url) != domain:
                continue
            if not await allowed_by_robots(client, website, url):
                continue
            try:
                r = await client.get(url, timeout=12)
                if r.status_code >= 400 or "text/html" not in r.headers.get("content-type", ""):
                    continue
                visited.append(str(r.url))
                soup = BeautifulSoup(r.text, "html.parser")
                text = soup.get_text(" ", strip=True)
                for email_addr in extract_emails(text + " " + r.text, soup):
                    if email_addr not in emails:
                        emails[email_addr] = EmailCandidate(email=email_addr, source_url=str(r.url), source_type="public")

                for node in soup.find_all(["p", "li", "div", "article", "section", "h2", "h3", "h4"]):
                    snippet = node.get_text(" ", strip=True)
                    score = role_score(snippet)
                    if score <= 0 or len(snippet) > 500:
                        continue
                    name, title = infer_name_title(snippet)
                    contacts.append(ContactCandidate(name=name, title=title, source_url=str(r.url), source_snippet=snippet[:350], score=score))

                links = []
                for a in soup.find_all("a", href=True):
                    href = urljoin(str(r.url), a["href"])
                    parsed = urlparse(href)
                    href = parsed._replace(fragment="").geturl()
                    if domain_from_url(href) != domain:
                        continue
                    anchor = (a.get_text(" ", strip=True) + " " + parsed.path).lower()
                    if any(h in anchor for h in PAGE_HINTS):
                        links.append(href)
                for href in links[:20]:
                    if href not in seen and href not in queue:
                        queue.append(href)
                await asyncio.sleep(0.15)
            except Exception:
                continue

    mx_cache: dict[str, bool] = {}
    for item in emails.values():
        host = item.email.split("@")[-1]
        if host not in mx_cache:
            mx_cache[host] = await mx_valid(host)
        item.mx_valid = mx_cache[host]
        same_domain = host == domain or host.endswith("." + domain)
        if same_domain and item.mx_valid:
            item.confidence = "A-public-domain-mx"
        elif same_domain:
            item.confidence = "A-public-domain"
        elif item.mx_valid:
            item.confidence = "B-public-external"
        else:
            item.confidence = "C-public-unverified"

    contacts = dedupe_contacts(contacts)
    ranked_emails = sorted(emails.values(), key=lambda e: email_rank(e.email, domain), reverse=True)
    return ranked_emails, contacts, visited


def dedupe_contacts(items: Iterable[ContactCandidate]) -> list[ContactCandidate]:
    out: list[ContactCandidate] = []
    seen = set()
    for item in sorted(items, key=lambda x: x.score, reverse=True):
        key = (item.name.lower(), item.title.lower(), item.source_snippet[:100].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:10]


async def duckduckgo_decision_makers(company: str, website: str, max_results: int = 8) -> list[ContactCandidate]:
    """Use DuckDuckGo HTML results only. Does not crawl LinkedIn pages."""
    domain = domain_from_url(website)
    queries = [
        f'"{company}" marketing director LinkedIn',
        f'"{company}" communications PR partnerships LinkedIn',
        f'"{company}" content marketing manager',
    ]
    headers = {"User-Agent": USER_AGENT}
    found: list[ContactCandidate] = []
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        for query in queries:
            url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
            try:
                r = await client.get(url, timeout=15)
                soup = BeautifulSoup(r.text, "html.parser")
                for result in soup.select(".result")[:max_results]:
                    a = result.select_one(".result__a")
                    snippet_el = result.select_one(".result__snippet")
                    if not a:
                        continue
                    title_text = a.get_text(" ", strip=True)
                    snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
                    score = role_score(title_text + " " + snippet)
                    if score <= 0:
                        continue
                    href = a.get("href", "")
                    linkedin = href if "linkedin.com" in href else ""
                    name, title = infer_name_title(title_text + " | " + snippet)
                    found.append(ContactCandidate(
                        name=name,
                        title=title,
                        linkedin_url=linkedin,
                        source_url=url,
                        source_snippet=(title_text + " — " + snippet)[:350],
                        score=score + (5 if domain and domain in snippet.lower() else 0),
                    ))
                await asyncio.sleep(0.8)
            except Exception:
                continue
    return dedupe_contacts(found)


def generate_email_patterns(name: str, domain: str) -> list[str]:
    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in (name or "").split()]
    parts = [p for p in parts if p]
    if len(parts) < 2 or not domain:
        return []
    first, last = parts[0], parts[-1]
    vals = [
        f"{first}.{last}@{domain}", f"{first}{last}@{domain}", f"{first[0]}{last}@{domain}",
        f"{first}@{domain}", f"{first[0]}.{last}@{domain}", f"{last}@{domain}"
    ]
    return list(dict.fromkeys(vals))


def choose_primary_email(public_emails: list[EmailCandidate], contact: ContactCandidate | None, domain: str) -> tuple[str, str, str]:
    if contact and contact.name:
        tokens = [re.sub(r"[^a-z]", "", p.lower()) for p in contact.name.split()]
        tokens = [t for t in tokens if t]
        for item in public_emails:
            local = item.email.split("@")[0].lower()
            if any(len(t) >= 3 and t in local for t in tokens):
                return item.email, item.confidence, item.source_url
    if public_emails:
        return public_emails[0].email, public_emails[0].confidence, public_emails[0].source_url
    if contact and contact.name:
        patterns = generate_email_patterns(contact.name, domain)
        if patterns:
            return patterns[0], "D-pattern-only-do-not-auto-send", contact.source_url
    return "", "", ""


async def enrich_record(record: dict, use_search: bool = True, max_pages: int = 12) -> dict:
    company = str(record.get("Company") or record.get("Company Name") or record.get("company") or record.get("name") or "").strip()
    website = str(record.get("Website") or record.get("website") or record.get("Website URL") or record.get("website_url") or "").strip()
    listing = str(record.get("BuyAndRentRobots Listing URL") or record.get("Listing URL") or record.get("listing_url") or "").strip()
    domain = domain_from_url(website)

    public_emails, site_contacts, visited = await crawl_company(website, max_pages=max_pages)
    search_contacts = await duckduckgo_decision_makers(company, website) if use_search and company else []
    contacts = dedupe_contacts(site_contacts + search_contacts)
    best_contact = contacts[0] if contacts else None
    primary_email, confidence, email_source = choose_primary_email(public_emails, best_contact, domain)

    general = next((e.email for e in public_emails if e.email.split("@")[0] in {"info", "contact", "hello", "sales"}), "")
    marketing = next((e.email for e in public_emails if e.email.split("@")[0] in {"marketing", "press", "media", "pr", "communications", "partnerships"}), "")

    result = dict(record)
    result.update({
        "Company": company,
        "Website": website,
        "BuyAndRentRobots Listing URL": listing,
        "Contact Name": best_contact.name if best_contact else "",
        "Job Title": best_contact.title if best_contact else "",
        "LinkedIn URL": best_contact.linkedin_url if best_contact else "",
        "Best Email": primary_email,
        "Email Confidence": confidence,
        "Email Source URL": email_source,
        "Marketing/PR Email": marketing,
        "General Email": general,
        "All Public Emails": "; ".join(e.email for e in public_emails[:12]),
        "Contact Source URL": best_contact.source_url if best_contact else "",
        "Contact Evidence": best_contact.source_snippet if best_contact else "",
        "Pages Crawled": len(visited),
        "Ready to Email": "YES" if confidence.startswith(("A-", "B-")) else "REVIEW",
    })
    return result


async def enrich_rows(rows: list[dict], concurrency: int = 4, use_search: bool = True, max_pages: int = 12, progress_cb=None) -> list[dict]:
    sem = asyncio.Semaphore(max(1, min(concurrency, 10)))
    total = len(rows)
    completed = 0
    lock = asyncio.Lock()

    async def one(row: dict):
        nonlocal completed
        async with sem:
            try:
                result = await enrich_record(row, use_search=use_search, max_pages=max_pages)
            except Exception as exc:
                result = dict(row)
                result["Error"] = str(exc)[:250]
            async with lock:
                completed += 1
                if progress_cb:
                    progress_cb(completed, total)
            return result

    return await asyncio.gather(*(one(r) for r in rows))
