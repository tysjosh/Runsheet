"""Unit tests for :class:`compliance.models.jurisdiction_rate.JurisdictionRate`.

Covers Task 3.1 of the Fuel Compliance Backbone spec, which validates
Requirement 1.5 (rate tables carry ``effective_date`` and ``expiry_date``
so rate changes apply prospectively without altering historical invoices).

The tests assert:
- Happy-path construction for each jurisdiction level.
- FIPS code digit-only and length-by-level validation.
- Non-negative integer cents rate (Constraint C1).
- Non-empty ``product_codes`` list.
- ``expiry_date >= effective_date`` cross-field validation.
- Optional text fields are stripped / normalized.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from compliance.models.jurisdiction_rate import JurisdictionRate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_payload(**overrides) -> dict:
    payload = {
        "tenant_id": "tenant-1",
        "fips_code": "06",
        "jurisdiction_level": "state",
        "tax_type": "excise",
        "product_codes": ["GAS_87", "DIESEL_LSD"],
        "rate_cents_per_gallon": 184,
        "effective_date": date(2026, 1, 1),
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Each jurisdiction level accepts a correctly-shaped FIPS code."""

    def test_federal_rate_with_sentinel_fips(self):
        rate = JurisdictionRate(
            **_base_payload(
                fips_code="00",
                jurisdiction_level="federal",
                jurisdiction_name="United States",
                source="irs_form_720",
            )
        )

        assert rate.jurisdiction_level == "federal"
        assert rate.fips_code == "00"
        assert rate.jurisdiction_name == "United States"
        assert rate.source == "irs_form_720"
        assert rate.expiry_date is None
        assert rate.jurisdiction_id.startswith("juris_")

    def test_state_rate_2_digit_fips(self):
        rate = JurisdictionRate(**_base_payload(fips_code="48"))

        assert rate.fips_code == "48"
        assert rate.jurisdiction_level == "state"

    def test_county_rate_5_digit_fips(self):
        rate = JurisdictionRate(
            **_base_payload(
                fips_code="06037",
                jurisdiction_level="county",
                jurisdiction_name="Los Angeles County",
            )
        )

        assert rate.fips_code == "06037"
        assert rate.jurisdiction_level == "county"

    def test_city_rate_7_digit_fips(self):
        rate = JurisdictionRate(
            **_base_payload(
                fips_code="0644000",
                jurisdiction_level="city",
                jurisdiction_name="Los Angeles",
                tax_type="ust",
            )
        )

        assert rate.fips_code == "0644000"
        assert rate.jurisdiction_level == "city"
        assert rate.tax_type == "ust"

    def test_expiry_after_effective_is_accepted(self):
        rate = JurisdictionRate(
            **_base_payload(
                effective_date=date(2026, 1, 1),
                expiry_date=date(2026, 12, 31),
            )
        )

        assert rate.effective_date == date(2026, 1, 1)
        assert rate.expiry_date == date(2026, 12, 31)

    def test_expiry_equal_to_effective_is_accepted(self):
        """Same-day rate windows are legitimate (single-day overrides)."""
        rate = JurisdictionRate(
            **_base_payload(
                effective_date=date(2026, 7, 1),
                expiry_date=date(2026, 7, 1),
            )
        )

        assert rate.expiry_date == rate.effective_date


# ---------------------------------------------------------------------------
# FIPS code validation
# ---------------------------------------------------------------------------


class TestFipsCodeValidation:
    """FIPS code must be digits only and length-aligned with the level."""

    def test_non_digit_fips_code_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            JurisdictionRate(**_base_payload(fips_code="CA"))

        assert "digits" in str(exc_info.value)

    def test_empty_fips_code_rejected(self):
        with pytest.raises(ValidationError):
            JurisdictionRate(**_base_payload(fips_code=""))

    def test_wrong_length_fips_code_rejected(self):
        # 3 digits is never a valid FIPS width.
        with pytest.raises(ValidationError) as exc_info:
            JurisdictionRate(**_base_payload(fips_code="123"))

        assert "2 digits" in str(exc_info.value) or "length" in str(exc_info.value)

    def test_county_level_rejects_2_digit_fips(self):
        with pytest.raises(ValidationError) as exc_info:
            JurisdictionRate(
                **_base_payload(fips_code="06", jurisdiction_level="county")
            )

        assert "county" in str(exc_info.value)

    def test_city_level_rejects_5_digit_fips(self):
        with pytest.raises(ValidationError) as exc_info:
            JurisdictionRate(
                **_base_payload(fips_code="06037", jurisdiction_level="city")
            )

        assert "city" in str(exc_info.value)

    def test_state_level_rejects_7_digit_fips(self):
        with pytest.raises(ValidationError):
            JurisdictionRate(
                **_base_payload(fips_code="0644000", jurisdiction_level="state")
            )


# ---------------------------------------------------------------------------
# Rate / product codes validation
# ---------------------------------------------------------------------------


class TestRateAndProductCodes:
    """Rate is non-negative integer cents; product_codes must be non-empty."""

    def test_negative_rate_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            JurisdictionRate(**_base_payload(rate_cents_per_gallon=-1))

        assert ">= 0" in str(exc_info.value) or "greater" in str(exc_info.value)

    def test_zero_rate_allowed(self):
        """A zero-cent row is legitimate for exempt product overrides."""
        rate = JurisdictionRate(**_base_payload(rate_cents_per_gallon=0))
        assert rate.rate_cents_per_gallon == 0

    def test_empty_product_codes_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            JurisdictionRate(**_base_payload(product_codes=[]))

        assert "product_codes" in str(exc_info.value)

    def test_whitespace_product_code_rejected(self):
        with pytest.raises(ValidationError):
            JurisdictionRate(**_base_payload(product_codes=["GAS_87", "  "]))

    def test_product_codes_are_stripped(self):
        rate = JurisdictionRate(
            **_base_payload(product_codes=["  GAS_87 ", "DIESEL_LSD"])
        )
        assert rate.product_codes == ["GAS_87", "DIESEL_LSD"]


# ---------------------------------------------------------------------------
# Effective window validation (Req 1.5)
# ---------------------------------------------------------------------------


class TestEffectiveWindow:
    """``expiry_date`` must not precede ``effective_date`` when provided."""

    def test_expiry_before_effective_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            JurisdictionRate(
                **_base_payload(
                    effective_date=date(2026, 6, 1),
                    expiry_date=date(2026, 5, 31),
                )
            )

        assert "expiry_date" in str(exc_info.value)

    def test_missing_expiry_date_is_allowed(self):
        """``None`` expiry means the rate is active indefinitely."""
        rate = JurisdictionRate(**_base_payload(expiry_date=None))
        assert rate.expiry_date is None


# ---------------------------------------------------------------------------
# Optional text normalization
# ---------------------------------------------------------------------------


class TestOptionalTextNormalization:
    """Optional text fields are stripped; all-whitespace collapses to ``None``."""

    def test_jurisdiction_name_is_stripped(self):
        rate = JurisdictionRate(
            **_base_payload(jurisdiction_name="  California  ")
        )
        assert rate.jurisdiction_name == "California"

    def test_whitespace_only_source_becomes_none(self):
        rate = JurisdictionRate(**_base_payload(source="   "))
        assert rate.source is None


# ---------------------------------------------------------------------------
# Schema hygiene
# ---------------------------------------------------------------------------


class TestSchemaHygiene:
    """The model forbids unknown fields so ES writes stay schema-aligned."""

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            JurisdictionRate(**_base_payload(unexpected_field="value"))

    def test_invalid_jurisdiction_level_rejected(self):
        with pytest.raises(ValidationError):
            JurisdictionRate(
                **_base_payload(jurisdiction_level="regional")
            )

    def test_invalid_tax_type_rejected(self):
        with pytest.raises(ValidationError):
            JurisdictionRate(**_base_payload(tax_type="sales"))
