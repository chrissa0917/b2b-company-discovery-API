from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from extract_emails import DefaultWorker
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
    "about",
    "contact",
    "press",
    "media",
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
    if LINKEDIN_PROFILE_RE.fullmatch(clean):
        return clean
    return ""


def _collect(pages: list, website_domain: str) -> ScrapedContactData:
    emails: list[str] = []
    external_emails: list[str] = []
    linkedin_urls: list[str] = []
    pages_checked: list[str] = []
    seen_emails: set[str] = set()
    seen_external: set[str] = set()
    seen_linkedin: set[str] = set()

    for page in pages:
        page_url = str(getattr(page, "page_url", "") or "")
        if page_url and page_url not in pages_checked:
            pages_checked.append(page_url)

        data = getattr(page, "data", {}) or {}
        for raw in data.get("email", []) or []:
            email = _clean_email(str(raw))
            if not email or _is_placeholder(email):
                continue
            if not _same_company_domain(email, website_domain):
                if email not in seen_external:
                    seen_external.add(email)
                    external_emails.append(email)
                continue
            if email in seen_emails:
                continue
            seen_emails.add(email)
            emails.append(email)

        for raw in data.get("linkedin", []) or []:
            url = _clean_linkedin_url(str(raw or ""))
            if not url or url in seen_linkedin:
                continue
            seen_linkedin.add(url)
            linkedin_urls.append(url)

    emails.sort()
    return ScrapedContactData(
        emails=emails,
        linkedin_urls=linkedin_urls,
        pages_checked=pages_checked,
        rejected_external_emails=external_emails,
    )


async def _scrape_with_browser(
    website: str,
    browser,
    *,
    depth: int,
    max_links_from_page: int,
) -> list:
    link_filter = ContactInfoLinkFilter(
        website,
        contruct_candidates=CONTACT_LINK_HINTS,
        use_default=False,
    )
    worker = DefaultWorker(
        website,
        browser,
        link_filter=link_filter,
        data_extractors=[AdvancedEmailExtractor(), LinkedinExtractor()],
        depth=depth,
        max_links_from_page=min(max_links_from_page, 6),
    )
    return await worker.aget_data()


async def scrape_public_contact_data(
    website: str,
    *,
    timeout_seconds: int = 25,
    depth: int = 1,
    max_links_from_page: int = 8,
    browser_fallback: bool = True,
) -> ScrapedContactData:
    """Use dmitriiweb/extract-emails as the website email extraction engine.

    Fast path: async HTTP extraction from the open-source project.
    Fallback: Playwright/Chromium only when HTTP finds no same-domain company email.
    The wrapper keeps only same-company-domain emails, validates LinkedIn profile URLs,
    filters placeholders, and limits the browser fallback so a site cannot hold a batch.
    """

    website_domain = _domain_from_url(website)

    async def _http_run():
        async with HttpxBrowser() as browser:
            return await _scrape_with_browser(
                website,
                browser,
                depth=depth,
                max_links_from_page=max_links_from_page,
            )

    http_pages = await asyncio.wait_for(_http_run(), timeout=timeout_seconds)
    result = _collect(http_pages, website_domain)
    if result.emails or not browser_fallback:
        return result

    async def _chromium_run():
        async with ChromiumBrowser(headless=True) as browser:
            return await _scrape_with_browser(
                website,
                browser,
                depth=depth,
                max_links_from_page=max_links_from_page,
            )

    try:
        browser_pages = await asyncio.wait_for(
            _chromium_run(),
            timeout=min(timeout_seconds, 12),
        )
    except Exception:
        return result

    browser_result = _collect(browser_pages, website_domain)
    merged_pages = list(dict.fromkeys(result.pages_checked + browser_result.pages_checked))
    merged_emails = list(dict.fromkeys(result.emails + browser_result.emails))
    merged_linkedin = list(dict.fromkeys(result.linkedin_urls + browser_result.linkedin_urls))
    merged_external = list(
        dict.fromkeys(
            (result.rejected_external_emails or [])
            + (browser_result.rejected_external_emails or [])
        )
    )

    return ScrapedContactData(
        emails=merged_emails,
        linkedin_urls=merged_linkedin,
        pages_checked=merged_pages,
        used_browser_fallback=True,
        rejected_external_emails=merged_external,
    )
