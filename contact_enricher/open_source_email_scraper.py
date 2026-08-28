from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from extract_emails import DefaultWorker
from extract_emails.browsers import HttpxBrowser
from extract_emails.data_extractors.advanced_email_extractor import AdvancedEmailExtractor
from extract_emails.data_extractors import LinkedinExtractor


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


@dataclass
class ScrapedContactData:
    emails: list[str]
    linkedin_urls: list[str]
    pages_checked: list[str]


def _domain_from_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    host = (urlparse(value).hostname or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def _clean_email(value: str) -> str:
    value = (value or "").strip().lower().strip(".,;:<>[](){}\"'")
    return value


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


async def scrape_public_contact_data(
    website: str,
    *,
    timeout_seconds: int = 25,
    depth: int = 1,
    max_links_from_page: int = 8,
) -> ScrapedContactData:
    """Use dmitriiweb/extract-emails as the primary website email extractor.

    The upstream project handles contact/about link discovery, ordinary emails,
    common obfuscation, Cloudflare-protected addresses, and LinkedIn URLs.
    This adapter adds strict placeholder filtering and company-domain ranking.
    """

    website_domain = _domain_from_url(website)

    async def _run():
        async with HttpxBrowser() as browser:
            worker = DefaultWorker(
                website,
                browser,
                data_extractors=[AdvancedEmailExtractor(), LinkedinExtractor()],
                depth=depth,
                max_links_from_page=max_links_from_page,
            )
            return await worker.aget_data()

    pages = await asyncio.wait_for(_run(), timeout=timeout_seconds)

    emails: list[str] = []
    linkedin_urls: list[str] = []
    pages_checked: list[str] = []
    seen_emails: set[str] = set()
    seen_linkedin: set[str] = set()

    for page in pages:
        page_url = str(getattr(page, "page_url", "") or "")
        if page_url and page_url not in pages_checked:
            pages_checked.append(page_url)

        data = getattr(page, "data", {}) or {}
        for raw in data.get("email", []) or []:
            email = _clean_email(str(raw))
            if not email or _is_placeholder(email) or email in seen_emails:
                continue
            seen_emails.add(email)
            emails.append(email)

        for raw in data.get("linkedin", []) or []:
            url = str(raw or "").strip()
            if not url or url in seen_linkedin:
                continue
            seen_linkedin.add(url)
            linkedin_urls.append(url)

    emails.sort(key=lambda item: (1 if _same_company_domain(item, website_domain) else 0, item), reverse=True)
    return ScrapedContactData(
        emails=emails,
        linkedin_urls=linkedin_urls,
        pages_checked=pages_checked,
    )
