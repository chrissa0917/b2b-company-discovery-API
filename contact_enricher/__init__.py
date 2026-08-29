"""Chrissa Automates contact enricher package."""

# Install the Reoon selector first, then route targeted searches through the
# tested person-first discovery engine. Basic company-email mode stays on the
# existing public-email path and does not consume Reoon verification credits.
from . import verified_enricher as _verified_enricher
from .reoon_integration import choose_and_verify_email as _reoon_choose_and_verify_email

_verified_enricher.choose_and_verify_email = _reoon_choose_and_verify_email

from .targeted_person_enricher import enrich_record as _targeted_enrich_record

_verified_enricher.enrich_record = _targeted_enrich_record
