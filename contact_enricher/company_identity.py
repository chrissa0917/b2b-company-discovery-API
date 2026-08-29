from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .enricher import domain_from_url

COMMON_DOMAIN = {"www", "com", "net", "org", "io", "ai", "co", "app", "tech", "global", "news", "investors", "corporate"}
LEGAL = {"inc", "incorporated", "corp", "corporation", "company", "co", "group", "llc", "ltd", "limited", "plc", "pvt", "private"}


def _ascii(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _ascii(value).lower())


def _company_forms(company: str) -> tuple[str, list[str]]:
    words = [w for w in re.findall(r"[a-z0-9]+", _ascii(company).lower()) if w not in LEGAL]
    compact = "".join(words)
    meaningful = [w for w in words if len(w) >= 3]
    return compact, meaningful


def _domain_brands(website: str) -> list[str]:
    domain = domain_from_url(website)
    labels = [re.sub(r"[^a-z0-9]", "", label.lower()) for label in domain.split(".")]
    return [label for label in labels if len(label) >= 3 and label not in COMMON_DOMAIN]


def company_website_alignment(company: str, website: str) -> tuple[bool, int, str]:
    """Conservative gate that prevents publisher/aggregator URLs being treated as the company site."""
    company_compact, company_words = _company_forms(company)
    brands = _domain_brands(website)
    if not company_compact or not brands:
        return True, 50, "insufficient identity tokens"

    best = 0.0
    reason = "no company/domain identity overlap"
    for brand in brands:
        if brand in company_compact or company_compact in brand:
            return True, 100, f"company/domain direct match: {brand}"
        for word in company_words:
            if word == brand or (len(word) >= 4 and (word in brand or brand in word)):
                return True, 95, f"company/domain token match: {word}~{brand}"
            ratio = SequenceMatcher(None, word, brand).ratio()
            if ratio > best:
                best = ratio
                reason = f"closest token {word}~{brand}"
        ratio = SequenceMatcher(None, company_compact, brand).ratio()
        if ratio > best:
            best = ratio
            reason = f"closest company/domain {company_compact}~{brand}"

    # Short acronyms such as IFR are valid when they are an explicit company token and domain label.
    for word in company_words:
        for brand in brands:
            if len(word) == 3 and word == brand:
                return True, 90, f"short brand match: {word}"

    score = round(best * 100)
    return (score >= 68), score, reason
