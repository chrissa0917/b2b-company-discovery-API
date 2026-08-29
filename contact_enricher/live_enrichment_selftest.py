from __future__ import annotations

from .enricher import ContactCandidate, EmailCandidate
from .live_sources import select_generic_company_email
from .reoon_integration import candidate_mail_domains, ranked_person_candidates


def run() -> None:
    source = "https://acme.example/team"
    public = [
        EmailCandidate(email="jane.doe@acme.example", source_url=source, source_type="public", mx_valid=True),
        EmailCandidate(email="sales@acme.example", source_url="https://acme.example/contact", source_type="public", mx_valid=True),
        EmailCandidate(email="info@acme.example", source_url="https://acme.example/contact", source_type="public", mx_valid=True),
    ]
    contacts = [
        ContactCandidate(name="Jane Doe", title="Marketing Director", source_url=source, source_snippet="Jane Doe Marketing Director", score=150),
    ]

    candidates, strategy = ranked_person_candidates(
        "John Smith", "acme.example", public, contacts, max_candidates=2
    )
    assert candidates, "expected learned pattern candidate"
    assert candidates[0][0] == "john.smith@acme.example", candidates
    assert strategy["learned_patterns"][0]["pattern"] == "first.last"

    no_evidence, empty_strategy = ranked_person_candidates(
        "John Smith", "empty.example", [], [], max_candidates=2
    )
    assert no_evidence == [], no_evidence
    assert empty_strategy.get("blind_fallback_disabled") is True

    generic, generic_source, _ = select_generic_company_email(
        public, "https://acme.example", ["Marketing"]
    )
    assert generic == "sales@acme.example", generic
    assert generic_source == "https://acme.example/contact"

    domains = candidate_mail_domains(public, "acme.example")
    assert domains and domains[0] == "acme.example", domains

    print("LIVE_ENRICHMENT_SELFTEST_OK", flush=True)


if __name__ == "__main__":
    run()
