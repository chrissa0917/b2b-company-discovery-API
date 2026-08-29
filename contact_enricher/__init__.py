"""Chrissa Automates contact enricher package."""

# Install the person-first Reoon selector before the application imports
# enrich_rows. This leaves basic company-email mode unchanged while targeted
# named-contact searches use the passed Reoon SMTP verification path.
from . import verified_enricher as _verified_enricher
from .reoon_integration import choose_and_verify_email as _reoon_choose_and_verify_email

_verified_enricher.choose_and_verify_email = _reoon_choose_and_verify_email
