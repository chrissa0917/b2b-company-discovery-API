from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from extract_emails.browsers import ChromiumBrowser, HttpxBrowser
from extract_emails.data_extractors import LinkedinExtractor
from extract_emails.data_extractors.advanced_email_extractor import AdvancedEmailExtractor
from extract_emails.link_filters import ContactInfoLinkFilter


PLACEHOLDER_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "yourdomain.com",
    "youremail.com",
    "email.com",
    "domain.com",
}
PLACEHOLDER_LOCALS = {
    "you",
    "yourname",
    "your-name",
    "name",
    "test",
    "example",
    "email",
}
CONTACT_LINK_HINTS = [
    "contact",
    "press",
    "media",
    "about",
    "team",
    "people",
    "leadership",
    "investor",
]
LINKEDIN_PROFILE_RE = re.compile(
    r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9%._-]+/?$",
    re.IGNORECASE,
)


@dataclass
class ScrapedContactData:
    emails: list[str]
    linkedin_urls: list[str]
    pages_checked: list[str]
    used_browser_fallback: bool = False
    rejected_external_emails: list[str] | None = None


def _domain_from_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    host = (urlparse(value).hostname or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def _clean_email(value: str) -> str:
    return (value or "").strip().lower().strip(".,;:<>[](){}\"'")


def _is_placeholder(email: str) -> bool:
    if "@" not in email:
        return True
    local, domain = email.rsplit("@", 1)
    if domain in PLACEHOLDER_DOMAINS:
        return True
    if local in PLACEHOLDER_LOCALS:
        return True
    if re.search(r"(?:your|sample|dummy|fake)[-_\. ]?(?:email|name)?", local):
        return True
    return False


def _same_company_domain(email: str, website_domain: str) -> bool:
    if "@" not in email or not website_domain:
        return False
    email_domain = email.rsplit("@", 1)[1].lower().strip(".")
    return email_domain == website_domain or email_domain.endswith("." + website_domain)


def _clean_linkedin_url(value: str) -> str:
    value = (value or "").strip().strip("\"'\\,;<>[](){}")
    if not value:
        return ""
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    return clean if LINKEDIN_PROFILE_RE.fullmatch(clean) else ""


def _extract_from_source(
    source: str,
    website_domain: str,
    email_extractor: AdvancedEmailExtractor,
    linkedin_extractor: LinkedinExtractor,
) -> tuple[list[str], list[str], list[str]]:
    if not source:
        return [], [], []

    same_domain: list[str] = []
    external: list[str] = []
    linkedins: list[str] = []

    try:
        raw_emails = email_extractor.get_data(source)
    except Exception:
        raw_emails = set()

    for raw in raw_emails:
        email = _clean_email(str(raw))
        if not email or _is_placeholder(email):
            continue
        if _same_company_domain(email, website_domain):
            same_domain.append(email)
        else:
            external.append(email)

    try:
        raw_linkedins = linkedin_extractor.get_data(source)
    except Exception:
        raw_linkedins = set()

    for raw in raw_linkedins:
        url = _clean_linkedin_url(str(raw or ""))
        if url:
            linkedins.append(url)

    return (
        list(dict.fromkeys(same_domain)),
        list(dict.fromkeys(external)),
        list(dict.fromkeys(linkedins)),
    )


async def _focused_scrape(
    website: str,
    browser,
    *,
    max_contact_pages: int,
) -> ScrapedContactData:
    """Use extract-emails components but stop as soon as a good company email is found."""

    website_domain = _domain_from_url(website)
    email_extractor = AdvancedEmailExtractor()
    linkedin_extractor = LinkedinExtractor()
    link_filter = ContactInfoLinkFilter(
        website,
        contruct_candidates=CONTACT_LINK_HINTS,
        use_default=False,
    )

    queue = [website]
    queued = {website}
    pages_checked: list[str] = []
    emails: list[str] = []
    external_emails: list[str] = []
    linkedin_urls: list[str] = []

    while queue and len(pages_checked) < max_contact_pages + 1:
        url = queue.pop(0)
        source = await browser.aget_page_source(url)
        if not source:
            continue

        pages_checked.append(url)
        page_emails, page_external, page_linkedins = _extract_from_source(
            source,
            website_domain,
            email_extractor,
            linkedin_extractor,
        )
        emails.extend(x for x in page_emails if x not in emails)
        external_emails.extend(x for x in page_external if x not in external_emails)
        linkedin_urls.extend(x for x in page_linkedins if x not in linkedin_urls)

        # A same-company-domain email is the success condition. Do not waste time
        # crawling more pages after finding one.
        if emails:
            return ScrapedContactData(
                emails=emails,
                linkedin_urls=linkedin_urls,
                pages_checked=pages_checked,
                rejected_external_emails=external_emails,
            )

        try:
            discovered = link_filter.get_links(source)
            contact_links = link_filter.filter(discovered)
        except Exception:
            contact_links = []

        for candidate in contact_links:
            if candidate not in queued and len(queued) < max_contact_pages + 1:
                queued.add(candidate)
                queue.append(candidate)

    return ScrapedContactData(
        emails=emails,
        linkedin_urls=linkedin_urls,
        pages_checked=pages_checked,
        rejected_external_emails=external_emails,
    )


async def scrape_public_contact_data(
    website: str,
    *,
    timeout_seconds: int = 25,
    depth: int = 1,
    max_links_from_page: int = 8,
    browser_fallback: bool = True,
) -> ScrapedContactData:
    """Website email extraction powered by the pinned dmitriiweb/extract-emails project.

    Fast path uses its HttpxBrowser, AdvancedEmailExtractor, LinkedinExtractor and
    ContactInfoLinkFilter. If no same-domain email is found, its ChromiumBrowser is
    used as a short JavaScript fallback. Our wrapper adds strict company-domain and
    placeholder filters, validates LinkedIn profile URLs, and stops immediately on
    the first useful company email.
    """

    max_contact_pages = min(max(max_links_from_page, 1), 6)

    async def _http_run():
        async with HttpxBrowser() as browser:
            return await _focused_scrape(
                website,
                browser,
                max_contact_pages=max_contact_pages,
            )

    try:
        result = await asyncio.wait_for(_http_run(), timeout=min(timeout_seconds, 12))
    except Exception:
        result = ScrapedContactData([], [], [], rejected_external_emails=[])

    if result.emails or not browser_fallback:
        return result

    async def _chromium_run():
        async with ChromiumBrowser(headless=True) as browser:
            return await _focused_scrape(
                website,
                browser,
                max_contact_pages=min(max_contact_pages, 4),
            )

    try:
        browser_result = await asyncio.wait_for(_chromium_run(), timeout=min(timeout_seconds, 12))
    except Exception:
        return result

    return ScrapedContactData(
        emails=list(dict.fromkeys(result.emails + browser_result.emails)),
        linkedin_urls=list(dict.fromkeys(result.linkedin_urls + browser_result.linkedin_urls)),
        pages_checked=list(dict.fromkeys(result.pages_checked + browser_result.pages_checked)),
        used_browser_fallback=True,
        rejected_external_emails=list(
            dict.fromkeys(
                (result.rejected_external_emails or [])
                + (browser_result.rejected_external_emails or [])
            )
        ),
    )
