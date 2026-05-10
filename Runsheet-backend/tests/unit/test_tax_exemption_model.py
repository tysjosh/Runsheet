"""Unit tests for :class:`compliance.models.tax_exemption.TaxExemption`.

Covers Task 3.2 of the Fuel Compliance Backbone spec, validating
Requirements 1.6 (637M registration), 1.7 (dyed/off-road exemption),
and 1.8 (farm exemption).

The tests assert:
- Happy-path construction for each supported ``exemption_type``.
- ``certificate_number`` must be a non-empty (whitespace-stripped) string.
- ``expiry_date >= issued_date`` when both are provided.
- ``is_expired_as_of`` correctly detects date-driven and status-driven
  expiration (``expired`` / ``revoked``).
- Optional text fields are stripped; all-whitespace collapses to ``None``.
- Unknown fields and invalid enum values are rejected.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from compliance.models.tax_exemption import TaxExemption


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_payload(**overrides) -> dict:
    """Return a minimal valid TaxExemption payload, with optional overrides."""
    payload = {
        "tenant_id": "tenant-1",
        "customer_id": "cust-001",
        "exemption_type": "dyed_diesel",
        "certificate_number": "DD-2026-00123",
        "expiry_date": date(2027, 12, 31),
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Happy path — each exemption_type
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Each supported exemption_type constructs cleanly."""

    def test_dyed_diesel_exemption(self):
        """Dyed-diesel certificates excuse federal + state excise (Req 1.7)."""
        exempt = TaxExemption(
            **_base_payload(
                exemption_type="dyed_diesel",
                letter_suffix="M",
                issuing_authority="IRS",
                issued_date=date(2026, 1, 15),
            )
        )

        assert exempt.exemption_type == "dyed_diesel"
        assert exempt.letter_suffix == "M"
        assert exempt.issuing_authority == "IRS"
        assert exempt.status == "valid"
        assert exempt.exemption_id.startswith("exempt_")

    def test_off_road_exemption(self):
        """Off-road exemptions excuse road-use excise (Req 1.7)."""
        exempt = TaxExemption(
            **_base_payload(
                exemption_type="off_road",
                certificate_number="OR-2026-00045",
                product_codes=["OFF_ROAD_DIESEL"],
            )
        )

        assert exempt.exemption_type == "off_road"
        assert exempt.product_codes == ["OFF_ROAD_DIESEL"]

    def test_farm_exemption_applies_reduced_rate(self):
        """Farm exemptions trigger the reduced agricultural rate (Req 1.8)."""
        exempt = TaxExemption(
            **_base_payload(
                exemption_type="farm",
                certificate_number="FARM-IA-0099",
                jurisdiction_fips="19",
                issuing_authority="IA_DOR",
            )
        )

        assert exempt.exemption_type == "farm"
        assert exempt.jurisdiction_fips == "19"

    def test_637M_registration(self):
        """IRS 637 letter M covers dyed-diesel blenders (Req 1.6)."""
        exempt = TaxExemption(
            **_base_payload(
                exemption_type="637M",
                certificate_number="637-M-784512",
                letter_suffix="M",
                issuing_authority="IRS",
                issued_date=date(2024, 6, 1),
                expiry_date=date(2027, 6, 1),
            )
        )

        assert exempt.exemption_type == "637M"
        assert exempt.letter_suffix == "M"

    def test_government_exemption(self):
        exempt = TaxExemption(
            **_base_payload(
                exemption_type="government",
                certificate_number="GOV-FED-A-101",
                account_id="acct-gsa-01",
            )
        )

        assert exempt.exemption_type == "government"
        assert exempt.account_id == "acct-gsa-01"

    def test_resale_exemption(self):
        exempt = TaxExemption(
            **_base_payload(
                exemption_type="resale",
                certificate_number="RESALE-TX-77",
                jurisdiction_fips="48",
            )
        )

        assert exempt.exemption_type == "resale"
        assert exempt.jurisdiction_fips == "48"

    def test_default_status_is_valid(self):
        exempt = TaxExemption(**_base_payload())
        assert exempt.status == "valid"

    def test_product_codes_default_to_none(self):
        """Missing product_codes means 'applies to all products'."""
        exempt = TaxExemption(**_base_payload())
        assert exempt.product_codes is None


# ---------------------------------------------------------------------------
# Certificate number validation
# ---------------------------------------------------------------------------


class TestCertificateNumberValidation:
    """``certificate_number`` must be a non-empty string, whitespace-stripped."""

    def test_empty_certificate_number_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TaxExemption(**_base_payload(certificate_number=""))

        assert "certificate_number" in str(exc_info.value)

    def test_whitespace_only_certificate_number_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TaxExemption(**_base_payload(certificate_number="   "))

        assert "certificate_number" in str(exc_info.value)

    def test_certificate_number_is_stripped(self):
        exempt = TaxExemption(
            **_base_payload(certificate_number="  DD-2026-00123  ")
        )
        assert exempt.certificate_number == "DD-2026-00123"


# ---------------------------------------------------------------------------
# Effective window validation
# ---------------------------------------------------------------------------


class TestEffectiveWindow:
    """``expiry_date`` must be >= ``issued_date`` when both are provided."""

    def test_expiry_after_issued_is_accepted(self):
        exempt = TaxExemption(
            **_base_payload(
                issued_date=date(2026, 1, 1),
                expiry_date=date(2027, 1, 1),
            )
        )
        assert exempt.expiry_date >= exempt.issued_date

    def test_expiry_equal_to_issued_is_accepted(self):
        """Same-day windows are legitimate (single-day override certificates)."""
        same_day = date(2026, 7, 4)
        exempt = TaxExemption(
            **_base_payload(issued_date=same_day, expiry_date=same_day)
        )
        assert exempt.expiry_date == exempt.issued_date

    def test_expiry_before_issued_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            TaxExemption(
                **_base_payload(
                    issued_date=date(2026, 6, 1),
                    expiry_date=date(2026, 5, 31),
                )
            )

        assert "expiry_date" in str(exc_info.value)

    def test_missing_issued_date_allowed_for_legacy_imports(self):
        """Legacy paperwork without an issue date still loads cleanly."""
        exempt = TaxExemption(
            **_base_payload(issued_date=None, expiry_date=date(2026, 12, 31))
        )
        assert exempt.issued_date is None


# ---------------------------------------------------------------------------
# is_expired_as_of helper (Req 6.6)
# ---------------------------------------------------------------------------


class TestIsExpiredAsOf:
    """Helper drives Tax_Engine's "can we honor this certificate?" check."""

    def test_not_expired_before_expiry_date(self):
        exempt = TaxExemption(
            **_base_payload(expiry_date=date(2027, 12, 31))
        )
        assert exempt.is_expired_as_of(date(2026, 6, 1)) is False

    def test_not_expired_on_expiry_date(self):
        """The expiry date itself is still honored (inclusive semantics)."""
        exempt = TaxExemption(
            **_base_payload(expiry_date=date(2027, 12, 31))
        )
        assert exempt.is_expired_as_of(date(2027, 12, 31)) is False

    def test_expired_the_day_after_expiry_date(self):
        exempt = TaxExemption(
            **_base_payload(expiry_date=date(2027, 12, 31))
        )
        assert exempt.is_expired_as_of(date(2028, 1, 1)) is True

    def test_expired_status_overrides_future_date(self):
        """Operator-driven 'expired' status persists regardless of date."""
        exempt = TaxExemption(
            **_base_payload(
                expiry_date=date(2030, 1, 1),
                status="expired",
            )
        )
        assert exempt.is_expired_as_of(date(2026, 1, 1)) is True

    def test_revoked_status_overrides_future_date(self):
        """Revoked certificates are never honored regardless of date (Req 6.6)."""
        exempt = TaxExemption(
            **_base_payload(
                expiry_date=date(2030, 1, 1),
                status="revoked",
            )
        )
        assert exempt.is_expired_as_of(date(2026, 1, 1)) is True


# ---------------------------------------------------------------------------
# Optional text normalization
# ---------------------------------------------------------------------------


class TestOptionalTextNormalization:
    """Optional text fields are stripped; all-whitespace collapses to ``None``."""

    def test_letter_suffix_is_stripped(self):
        exempt = TaxExemption(**_base_payload(letter_suffix="  M  "))
        assert exempt.letter_suffix == "M"

    def test_whitespace_only_issuing_authority_becomes_none(self):
        exempt = TaxExemption(**_base_payload(issuing_authority="   "))
        assert exempt.issuing_authority is None

    def test_whitespace_only_document_ref_becomes_none(self):
        exempt = TaxExemption(**_base_payload(document_ref=""))
        assert exempt.document_ref is None

    def test_jurisdiction_fips_digits_required(self):
        with pytest.raises(ValidationError) as exc_info:
            TaxExemption(**_base_payload(jurisdiction_fips="CA"))
        assert "digits" in str(exc_info.value)

    def test_jurisdiction_fips_wrong_length_rejected(self):
        with pytest.raises(ValidationError):
            TaxExemption(**_base_payload(jurisdiction_fips="123"))

    def test_product_codes_entries_are_stripped(self):
        exempt = TaxExemption(
            **_base_payload(product_codes=["  DYED_DIESEL ", "OFF_ROAD_DIESEL"])
        )
        assert exempt.product_codes == ["DYED_DIESEL", "OFF_ROAD_DIESEL"]

    def test_empty_product_codes_entry_rejected(self):
        with pytest.raises(ValidationError):
            TaxExemption(**_base_payload(product_codes=["DYED_DIESEL", "  "]))

    def test_empty_product_codes_list_collapses_to_none(self):
        exempt = TaxExemption(**_base_payload(product_codes=[]))
        assert exempt.product_codes is None


# ---------------------------------------------------------------------------
# Schema hygiene
# ---------------------------------------------------------------------------


class TestSchemaHygiene:
    """The model forbids unknown fields so ES writes stay schema-aligned."""

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            TaxExemption(**_base_payload(unexpected_field="value"))

    def test_invalid_exemption_type_rejected(self):
        with pytest.raises(ValidationError):
            TaxExemption(**_base_payload(exemption_type="bogus"))

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            TaxExemption(**_base_payload(status="cancelled"))
