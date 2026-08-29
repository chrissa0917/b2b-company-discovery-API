from __future__ import annotations

import asyncio
import os
import re
import time
import unicodedata
from collections import Counter, defaultdict
from urllib.parse import urlparse

from . import verified_enricher as _base
from .enricher import ContactCandidate, EmailCandidate, email_rank
from .reoon_verifier import check_reoon_balance, verify_email_reoon

_ORIGINAL_CHOOSE_AND_VERIFY = _base.choose_and_verify_email

FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
}
GENERIC_LOCALPARTS = {
    "info", "contact", "hello", "sales", "support", "marketing", "press", "media",
    "pr", "communications", "partnerships", "team", "careers", "jobs", "privacy",
    "abuse", "admin", "office", "enquiries", "inquiries", "service", "customerservice",
    "businessdevelopment", "business-development", "billing", "legal", "security",
}

PATTERN_ORDER = [
    "first.last", "firstlast", "flast", "first", "f.last", "firstl", "first.l",
    "first_last", "first-last", "last.first", "lastfirst", "lastf", "last.f",
    "last", "fl", "f_last",
]

_BALANCE_LOCK = asyncio.Lock()
_BALANCE_CHECKED_AT = 0.0
_BALANCE_AVAILABLE: int | None = None
_BALANCE_RESERVED_CALLS = 0


def _reserve_floor() -> int:
    try:
        return max(0, int(os.getenv("REOON_MIN_RESERVE", "20")))
    except Exception:
        return 20


async def _reserve_reoon_credit() -> tuple[bool, dict]:
    global _BALANCE_CHECKED_AT, _BALANCE_AVAILABLE, _BALANCE_RESERVED_CALLS
    async with _BALANCE_LOCK:
        now = time.time()
        if _BALANCE_AVAILABLE is None or now - _BALANCE_CHECKED_AT > 30:
            balance = await check_reoon_balance()
            if balance.get("configured") is False and balance.get("error"):
                return False, {"verdict": "not_run", "provider": "reoon", "error": "reoon_balance_unavailable"}
            daily = int(balance.get("remaining_daily_credits") or 0)
            instant = int(balance.get("remaining_instant_credits") or 0)
            _BALANCE_AVAILABLE = daily + instant
            _BALANCE_RESERVED_CALLS = 0
            _BALANCE_CHECKED_AT = now

        remaining = int(_BALANCE_AVAILABLE or 0) - _BALANCE_RESERVED_CALLS
        if remaining <= _reserve_floor():
            return False, {
                "verdict": "not_run",
                "provider": "reoon",
                "error": "reoon_reserve_reached",
                "available_before_reserve": remaining,
            }
        _BALANCE_RESERVED_CALLS += 1
        return True, {}


async def _verify_person_email(email: str) -> dict:
    allowed, blocked = await _reserve_reoon_credit()
    if not allowed:
        return {"email": email, **blocked}

    data = await verify_email_reoon(email, mode="power")
    verdict = str(data.get("verdict") or "unknown").lower()
    if verdict in {"valid", "invalid", "catch_all", "risky"}:
        return data

    if str(data.get("error") or "").startswith("reoon_reserve"):
        return data

    fallback = await _base.verify_email_address(email)
    fallback_verdict = str(fallback.get("verdict") or "unknown").lower()
    if fallback_verdict in {"valid", "invalid", "catch_all"}:
        return fallback
    return data


def _ascii_token(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z]", "", value.lower())


def _name_parts(name: str) -> tuple[str, str]:
    raw = [part for part in re.split(r"\s+", (name or "").strip()) if part]
    cleaned = [_ascii_token(part) for part in raw]
    cleaned = [part for part in cleaned if part and part not in {"dr", "mr", "mrs", "ms", "prof"}]
    while cleaned and cleaned[-1] in {"jr", "sr", "ii", "iii", "iv"}:
        cleaned.pop()
    if len(cleaned) < 2:
        return "", ""
    return cleaned[0], cleaned[-1]


def _format_local(pattern: str, first: str, last: str) -> str:
    mapping = {
        "first.last": f"{first}.{last}",
        "firstlast": f"{first}{last}",
        "flast": f"{first[:1]}{last}",
        "first": first,
        "f.last": f"{first[:1]}.{last}",
        "firstl": f"{first}{last[:1]}",
        "first.l": f"{first}.{last[:1]}",
        "first_last": f"{first}_{last}",
        "first-last": f"{first}-{last}",
        "last.first": f"{last}.{first}",
        "lastfirst": f"{last}{first}",
        "lastf": f"{last}{first[:1]}",
        "last.f": f"{last}.{first[:1]}",
        "last": last,
        "fl": f"{first[:1]}{last[:1]}",
        "f_last": f"{first[:1]}_{last}",
    }
    return mapping.get(pattern, "")


def _pattern_for_email(name: str, email: str, domain: str) -> str:
    first, last = _name_parts(name)
    if not first or not last or not email or "@" not in email:
        return ""
    local, host = email.lower().split("@", 1)
    if domain and host != domain and not host.endswith("." + domain):
        return ""
    for pattern in PATTERN_ORDER:
        if local == _format_local(pattern, first, last):
            return pattern
    return ""


def _is_personal_company_email(email: str) -> bool:
    if not email or "@" not in email:
        return False
    local, host = email.lower().split("@", 1)
    if host in FREE_MAIL_DOMAINS or local in GENERIC_LOCALPARTS:
        return False
    if any(local.startswith(prefix) for prefix in ("noreply", "no-reply", "newsletter", "updates", "notification")):
        return False
    if re.fullmatch(r"[a-f0-9]{20,}", local):
        return False
    return bool(re.fullmatch(r"[a-z0-9._+-]+", local))


def _source_host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _domain_brand_tokens(domain: str) -> set[str]:
    skip = {
        "www", "global", "corp", "corporate", "company", "group", "mail", "email",
        "com", "net", "org", "co", "io", "ai", "tech", "app", "cloud",
    }
    return {
        token.replace("-", "")
        for token in re.findall(r"[a-z0-9-]+", (domain or "").lower())
        if len(token.replace("-", "")) >= 3 and token.replace("-", "") not in skip
    }


def candidate_mail_domains(public_emails: list[EmailCandidate], website_domain: str) -> list[str]:
    website_domain = (website_domain or "").lower().strip(".")
    scores: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    website_tokens = _domain_brand_tokens(website_domain)

    if website_domain:
        scores[website_domain] += 3

    pending: list[tuple[str, bool]] = []
    for item in public_emails:
        email = (getattr(item, "email", "") or "").lower().strip()
        if "@" not in email:
            continue
        host = email.split("@", 1)[1].strip(".")
        if not host or host in FREE_MAIL_DOMAINS or getattr(item, "mx_valid", None) is False:
            continue
        source_host = _source_host(getattr(item, "source_url", "") or "")
        published_on_company_site = (
            source_host == website_domain
            or (website_domain and source_host.endswith("." + website_domain))
        )
        pending.append((host, published_on_company_site))
        counts[host] += 1

    for host, published_on_company_site in pending:
        host_tokens = _domain_brand_tokens(host)
        brand_related = bool(website_tokens & host_tokens)
        same_or_subdomain = host == website_domain or (website_domain and host.endswith("." + website_domain))
        if not same_or_subdomain and not (published_on_company_site and (brand_related or counts[host] >= 2)):
            continue
        scores[host] += 2 + min(counts[host], 3)
        if published_on_company_site:
            scores[host] += 5
        if brand_related:
            scores[host] += 4
        if same_or_subdomain:
            scores[host] += 3

    ranked = [
        host for host, _ in sorted(
            scores.items(),
            key=lambda kv: (kv[1], counts[kv[0]], kv[0] == website_domain),
            reverse=True,
        )
    ]
    return ranked[:2] or ([website_domain] if website_domain else [])


def learn_domain_patterns(
    public_emails: list[EmailCandidate],
    site_contacts: list[ContactCandidate],
    website_domain: str,
) -> list[tuple[str, str, int, int]]:
    scores: defaultdict[tuple[str, str], int] = defaultdict(int)
    examples: Counter[tuple[str, str]] = Counter()
    mail_domains = set(candidate_mail_domains(public_emails, website_domain))

    for item in public_emails:
        email = (getattr(item, "email", "") or "").lower().strip()
        if not _is_personal_company_email(email) or "@" not in email:
            continue
        host = email.split("@", 1)[1]
        if mail_domains and host not in mail_domains:
            continue

        same_source = [
            contact for contact in site_contacts
            if contact.name and getattr(contact, "source_url", "") == getattr(item, "source_url", "")
        ]
        matched = False
        for contact in same_source:
            pattern = _pattern_for_email(contact.name, email, host)
            if pattern:
                scores[(host, pattern)] += 12
                examples[(host, pattern)] += 1
                matched = True
                break
        if matched:
            continue

        for contact in site_contacts:
            if not contact.name or not _base.contact_name_match(email, contact):
                continue
            pattern = _pattern_for_email(contact.name, email, host)
            if pattern:
                scores[(host, pattern)] += 5
                examples[(host, pattern)] += 1
                break

    ranked = sorted(
        [
            (host, pattern, score, examples[(host, pattern)])
            for (host, pattern), score in scores.items()
        ],
        key=lambda row: (row[2], row[3], -PATTERN_ORDER.index(row[1])),
        reverse=True,
    )
    return ranked


def ranked_person_candidates(
    name: str,
    website_domain: str,
    public_emails: list[EmailCandidate],
    site_contacts: list[ContactCandidate],
    max_candidates: int = 2,
) -> tuple[list[tuple[str, str, str]], dict]:
    first, last = _name_parts(name)
    if not first or not last:
        return [], {"mail_domains": [], "learned_patterns": []}

    mail_domains = candidate_mail_domains(public_emails, website_domain)
    learned = learn_domain_patterns(public_emails, site_contacts, website_domain)
    candidates: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for host, pattern, score, support in learned:
        if support < 1 or score < 5:
            continue
        local = _format_local(pattern, first, last)
        email = f"{local}@{host}" if local and host else ""
        if not email or email in seen:
            continue
        seen.add(email)
        candidates.append((
            email,
            f"learned-pattern:{pattern};score={score};examples={support}",
            "domain-pattern",
        ))
        if len(candidates) >= max_candidates:
            break

    return candidates, {
        "mail_domains": mail_domains,
        "learned_patterns": [
            {"domain": host, "pattern": pattern, "score": score, "examples": support}
            for host, pattern, score, support in learned[:5]
        ],
        "blind_fallback_disabled": True,
    }


async def choose_and_verify_person_email(
    public_emails: list[EmailCandidate],
    site_contacts: list[ContactCandidate],
    contact: ContactCandidate | None,
    website_domain: str,
    deep_verify: bool,
    max_verifications: int = 2,
) -> tuple[str, str, str, dict, list[str], dict]:
    named_contact = bool(contact and contact.name and len(contact.name.split()) >= 2)
    strategy = {
        "mail_domains": [],
        "learned_patterns": [],
        "verification_budget": max_verifications,
        "blind_fallback_disabled": True,
    }
    if not named_contact or not deep_verify:
        return "", "", "", {"verdict": "not_run"}, [], strategy

    attempts: list[str] = []
    review_candidate: tuple[str, str, str, dict] | None = None
    allowed_domains = set(candidate_mail_domains(public_emails, website_domain))

    direct_public = [
        item for item in public_emails
        if _base.contact_name_match(item.email, contact)
        and _is_personal_company_email(item.email)
        and (not allowed_domains or item.email.lower().split("@", 1)[1] in allowed_domains)
    ]
    direct_public = sorted(
        direct_public,
        key=lambda item: email_rank(item.email, website_domain),
        reverse=True,
    )
    for item in direct_public[:1]:
        data = await _verify_person_email(item.email)
        verdict = str(data.get("verdict") or "unknown").lower()
        attempts.append(f"{item.email}={verdict}")
        if verdict == "valid":
            strategy["selected_reason"] = "public-exact-name-match"
            return item.email, "verified public named contact", item.source_url, data, attempts, strategy
        if verdict in {"catch_all", "risky", "unknown"}:
            review_candidate = (item.email, item.confidence or "review candidate", item.source_url, data)
        if str(data.get("error") or "") == "reoon_reserve_reached":
            strategy["selected_reason"] = "verification-reserve"
            return "", "", "", data, attempts, strategy

    remaining_budget = max(0, max_verifications - len(attempts))
    ranked, learned_strategy = ranked_person_candidates(
        contact.name,
        website_domain,
        public_emails,
        site_contacts,
        max_candidates=remaining_budget,
    )
    strategy.update(learned_strategy)

    existing_direct = {item.email.lower() for item in direct_public}
    for candidate, reason, _source_type in ranked[:remaining_budget]:
        if candidate.lower() in existing_direct:
            continue
        data = await _verify_person_email(candidate)
        verdict = str(data.get("verdict") or "unknown").lower()
        attempts.append(f"{candidate}={verdict}")
        if verdict == "valid":
            strategy["selected_reason"] = reason
            return candidate, f"verified contact email · {reason}", contact.source_url, data, attempts, strategy
        if verdict in {"catch_all", "risky", "unknown"} and review_candidate is None:
            review_candidate = (candidate, f"review candidate · {reason}", contact.source_url, data)
        if str(data.get("error") or "") == "reoon_reserve_reached":
            strategy["selected_reason"] = "verification-reserve"
            break

    if review_candidate:
        strategy["selected_reason"] = "review-only"
        return review_candidate[0], review_candidate[1], review_candidate[2], review_candidate[3], attempts, strategy

    strategy["selected_reason"] = "no-evidence-backed-person-email"
    return "", "", "", {"verdict": "invalid" if attempts else "not_run"}, attempts, strategy


async def choose_and_verify_email(
    public_emails: list[EmailCandidate],
    contact: ContactCandidate | None,
    domain: str,
    deep_verify: bool,
) -> tuple[str, str, str, dict, list[str]]:
    if not contact or not contact.name:
        return await _ORIGINAL_CHOOSE_AND_VERIFY(public_emails, contact, domain, deep_verify)

    email, confidence, source, verification, attempts, _ = await choose_and_verify_person_email(
        public_emails,
        [],
        contact,
        domain,
        deep_verify,
        max_verifications=2,
    )
    return email, confidence, source, verification, attempts
