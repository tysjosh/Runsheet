"""Unit tests for ``TaxEngine._compute_federal_excise`` (Task 3.5).

Covers the federal excise tax component of the Tax_Engine per
Requirement 1.1:

    WHEN a delivery is invoiced, THE Tax_Engine SHALL compute the
    federal excise tax rate (currently 18.4¢/gallon for gasoline,
    24.4¢/gallon for diesel) based on the product code and apply it
    to the invoiced gallons.

The helper resolves the rate in two stages:

1. Prefer a federal row from the rate table (fips_code=="00",
   jurisdiction_level=="federal", tax_type=="excise", with the product
   code in its product_codes list). This lets operators schedule
   prospective rate changes without a code deploy (Req 1.5).
2. Fall back to the statutory default for the canonicalized product
   code — 18.4¢ for gasoline/E85, 24.4¢ for diesel/off-road/heating
   oil/kerosene, 18.3¢ for propane, 0¢ for DEF.

Rates are stored in tenths-of-a-cent per gallon (``RATE_SCALE == 10``)
to preserve the sub-cent precision of the statutory rates. The amount
is rendered into integer cents via the formula::

    amount_cents = round(rate_stored * gallons / RATE_SCALE)

so 184 tenths × 1000 gallons / 10 == 18_400 cents ($184.00).

Validates: Requirement 1.1
"""

from __future__ import annotations

from datetime import date
from typing import List

import pytest

from compliance.models.jurisdiction_rate import JurisdictionRate
from compliance.services.tax_engine import (
    FEDERAL_EXCISE_COMPONENT_NAME,
    FEDERAL_EXCISE_DIESEL_RATE,
    FEDERAL_EXCISE_GASOLINE_RATE,
    FEDERAL_EXCISE_PROPANE_RATE,
    FEDERAL_FIPS_SENTINEL,
    RATE_SCALE,
    TaxEngine,
    TaxLineItem,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal ES stand-in — ``_compute_federal_excise`` never calls ES."""


@pytest.fixture
def tax_engine() -> TaxEngine:
    return TaxEngine(es_service=_FakeESService(), tenant_id="tenant-1")


def _make_federal_row(
    *,
    tenant_id: str = "tenant-1",
    product_codes: List[str] | None = None,
    rate_cents_per_gallon: int = FEDERAL_EXCISE_GASOLINE_RATE,
    effective_date: date = date(2024, 1, 1),
    expiry_date: date | None = None,
) -> JurisdictionRate:
    """Build a federal excise ``JurisdictionRate`` row."""
    return JurisdictionRate(
        tenant_id=tenant_id,
        fips_code=FEDERAL_FIPS_SENTINEL,
        jurisdiction_level="federal",
        tax_type="excise",
        product_codes=list(
            product_codes or ["DIESEL_2", "GASOLINE_REG"]
        ),
        rate_cents_per_gallon=rate_cents_per_gallon,
        effective_date=effective_date,
        expiry_date=expiry_date,
    )


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


class TestRateConstants:
    """Statutory rate constants match IRC §4081 / §4041(a)(2)."""

    def test_gasoline_rate_is_184_tenths(self):
        """18.4¢ / gallon → 184 in the RATE_SCALE=10 convention."""
        assert FEDERAL_EXCISE_GASOLINE_RATE == 184
        assert RATE_SCALE == 10
        # Spelled out: 184 / 10 == 18.4¢ per gallon.
        assert FEDERAL_EXCISE_GASOLINE_RATE / RATE_SCALE == pytest.approx(18.4)

    def test_diesel_rate_is_244_tenths(self):
        """24.4¢ / gallon → 244 in the RATE_SCALE=10 convention."""
        assert FEDERAL_EXCISE_DIESEL_RATE == 244
        assert FEDERAL_EXCISE_DIESEL_RATE / RATE_SCALE == pytest.approx(24.4)

    def test_propane_rate_is_183_tenths(self):
        assert FEDERAL_EXCISE_PROPANE_RATE == 183


# ---------------------------------------------------------------------------
# Statutory fallback — amount math & product-code coverage
# ---------------------------------------------------------------------------


class TestStatutoryFallbackAmounts:
    """With no rate-table row, the statutory rate is applied."""

    def test_gasoline_1000_gallons_produces_18400_cents(
        self, tax_engine: TaxEngine
    ):
        """GASOLINE_REG × 1000 gal → 184 × 1000 / 10 = 18_400 cents ($184.00)."""
        amount, line = tax_engine._compute_federal_excise(
            product_code="GASOLINE_REG",
            net_gallons=1000.0,
            jurisdiction_rates=[],
        )

        assert amount == 18_400
        assert isinstance(line, TaxLineItem)
        assert line.tax_component_name == FEDERAL_EXCISE_COMPONENT_NAME
        assert line.jurisdiction_fips == FEDERAL_FIPS_SENTINEL
        assert line.jurisdiction_level == "federal"
        assert line.rate_cents_per_gallon == FEDERAL_EXCISE_GASOLINE_RATE
        assert line.gallons == 1000.0
        assert line.amount_cents == 18_400

    def test_diesel_1000_gallons_produces_24400_cents(
        self, tax_engine: TaxEngine
    ):
        """DIESEL_2 × 1000 gal → 244 × 1000 / 10 = 24_400 cents ($244.00)."""
        amount, line = tax_engine._compute_federal_excise(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=[],
        )

        assert amount == 24_400
        assert line is not None
        assert line.rate_cents_per_gallon == FEDERAL_EXCISE_DIESEL_RATE
        assert line.amount_cents == 24_400

    @pytest.mark.parametrize(
        ("product_code", "expected_rate"),
        [
            ("GASOLINE_REG", FEDERAL_EXCISE_GASOLINE_RATE),
            ("GASOLINE_PREM", FEDERAL_EXCISE_GASOLINE_RATE),
            ("ETHANOL_E85", FEDERAL_EXCISE_GASOLINE_RATE),
            ("DIESEL_2", FEDERAL_EXCISE_DIESEL_RATE),
            ("OFF_ROAD_DIESEL", FEDERAL_EXCISE_DIESEL_RATE),
            ("HEATING_OIL", FEDERAL_EXCISE_DIESEL_RATE),
            ("KEROSENE", FEDERAL_EXCISE_DIESEL_RATE),
            ("PROPANE", FEDERAL_EXCISE_PROPANE_RATE),
            ("DEF", 0),
        ],
    )
    def test_statutory_rates_cover_every_catalog_product(
        self,
        tax_engine: TaxEngine,
        product_code: str,
        expected_rate: int,
    ):
        """Every US-catalog product resolves to a known statutory rate."""
        _, line = tax_engine._compute_federal_excise(
            product_code=product_code,
            net_gallons=100.0,
            jurisdiction_rates=[],
        )
        assert line is not None
        assert line.rate_cents_per_gallon == expected_rate

    def test_def_produces_zero_cents(self, tax_engine: TaxEngine):
        """DEF is a non-fuel additive with no federal excise (Req 1.1 product-code basis)."""
        amount, line = tax_engine._compute_federal_excise(
            product_code="DEF",
            net_gallons=500.0,
            jurisdiction_rates=[],
        )

        assert amount == 0
        assert line is not None
        assert line.rate_cents_per_gallon == 0
        assert line.amount_cents == 0

    def test_legacy_alias_canonicalized(self, tax_engine: TaxEngine):
        """Legacy Nigerian alias ``AGO`` resolves to ``DIESEL_2``'s rate."""
        amount, line = tax_engine._compute_federal_excise(
            product_code="AGO",
            net_gallons=1000.0,
            jurisdiction_rates=[],
        )
        assert amount == 24_400
        assert line is not None
        assert line.rate_cents_per_gallon == FEDERAL_EXCISE_DIESEL_RATE

    def test_case_insensitive_product_code(self, tax_engine: TaxEngine):
        """``canonicalize`` accepts mixed-case input; the helper follows suit."""
        amount, _ = tax_engine._compute_federal_excise(
            product_code="gasoline_reg",
            net_gallons=1000.0,
            jurisdiction_rates=[],
        )
        assert amount == 18_400


# ---------------------------------------------------------------------------
# Amount math — rounding, zero gallons, fractional gallons
# ---------------------------------------------------------------------------


class TestAmountMath:
    """Integer-cents math is stable and monotonic in gallons."""

    def test_zero_gallons_produces_zero_cents(self, tax_engine: TaxEngine):
        amount, line = tax_engine._compute_federal_excise(
            product_code="DIESEL_2",
            net_gallons=0.0,
            jurisdiction_rates=[],
        )
        assert amount == 0
        assert line is not None
        assert line.amount_cents == 0
        assert line.gallons == 0.0

    def test_fractional_gallons_rounded_to_nearest_cent(
        self, tax_engine: TaxEngine
    ):
        """``100.5`` gal × 18.4¢ = $18.492 → 1849 cents after rounding."""
        amount, _ = tax_engine._compute_federal_excise(
            product_code="GASOLINE_REG",
            net_gallons=100.5,
            jurisdiction_rates=[],
        )
        # 184 * 100.5 / 10 == 1849.2 → round to 1849
        assert amount == 1849

    def test_amount_is_integer(self, tax_engine: TaxEngine):
        """amount_cents is always an int, never a float (Constraint C1)."""
        amount, line = tax_engine._compute_federal_excise(
            product_code="DIESEL_2",
            net_gallons=1234.567,
            jurisdiction_rates=[],
        )
        assert isinstance(amount, int)
        assert line is not None
        assert isinstance(line.amount_cents, int)

    def test_amount_is_monotonic_in_gallons(self, tax_engine: TaxEngine):
        """More gallons → more cents (at a positive statutory rate)."""
        small, _ = tax_engine._compute_federal_excise(
            product_code="DIESEL_2",
            net_gallons=100.0,
            jurisdiction_rates=[],
        )
        large, _ = tax_engine._compute_federal_excise(
            product_code="DIESEL_2",
            net_gallons=200.0,
            jurisdiction_rates=[],
        )
        assert large > small
        # 244 * 200 / 10 - 244 * 100 / 10 == 2440
        assert large - small == 2440


# ---------------------------------------------------------------------------
# Rate-table priority
# ---------------------------------------------------------------------------


class TestRateTablePriority:
    """A matching federal row in ``jurisdiction_rates`` wins over the default."""

    def test_rate_table_row_takes_priority(self, tax_engine: TaxEngine):
        """A scheduled prospective rate from the rate table wins."""
        scheduled_row = _make_federal_row(
            product_codes=["GASOLINE_REG"],
            rate_cents_per_gallon=200,  # 20.0¢ hypothetical future rate
        )

        amount, line = tax_engine._compute_federal_excise(
            product_code="GASOLINE_REG",
            net_gallons=1000.0,
            jurisdiction_rates=[scheduled_row],
        )

        # 200 × 1000 / 10 == 20_000 cents ($200.00)
        assert amount == 20_000
        assert line is not None
        assert line.rate_cents_per_gallon == 200

    def test_rate_table_row_ignored_when_product_not_in_list(
        self, tax_engine: TaxEngine
    ):
        """Rows whose ``product_codes`` do not include the delivered
        product fall through to the statutory default."""
        diesel_only_row = _make_federal_row(
            product_codes=["DIESEL_2"],
            rate_cents_per_gallon=300,
        )

        amount, line = tax_engine._compute_federal_excise(
            product_code="GASOLINE_REG",
            net_gallons=1000.0,
            jurisdiction_rates=[diesel_only_row],
        )

        # Should use statutory 184 (18.4¢) since no gasoline row matched.
        assert amount == 18_400
        assert line is not None
        assert line.rate_cents_per_gallon == FEDERAL_EXCISE_GASOLINE_RATE

    def test_non_federal_rows_ignored(self, tax_engine: TaxEngine):
        """State-level / county-level rows never match federal."""
        state_row = JurisdictionRate(
            tenant_id="tenant-1",
            fips_code="06",
            jurisdiction_level="state",
            tax_type="excise",
            product_codes=["GASOLINE_REG"],
            rate_cents_per_gallon=999,
            effective_date=date(2024, 1, 1),
        )

        amount, _ = tax_engine._compute_federal_excise(
            product_code="GASOLINE_REG",
            net_gallons=1000.0,
            jurisdiction_rates=[state_row],
        )
        # Falls back to statutory — state row must not leak into federal.
        assert amount == 18_400

    def test_non_excise_tax_types_ignored(self, tax_engine: TaxEngine):
        """UST / SPCC / environmental rows never match federal excise."""
        ust_row = JurisdictionRate(
            tenant_id="tenant-1",
            fips_code=FEDERAL_FIPS_SENTINEL,
            jurisdiction_level="federal",
            tax_type="ust",
            product_codes=["GASOLINE_REG"],
            rate_cents_per_gallon=999,
            effective_date=date(2024, 1, 1),
        )

        amount, _ = tax_engine._compute_federal_excise(
            product_code="GASOLINE_REG",
            net_gallons=1000.0,
            jurisdiction_rates=[ust_row],
        )
        assert amount == 18_400

    def test_more_specific_row_beats_catch_all(self, tax_engine: TaxEngine):
        """A single-product row wins over a multi-product catch-all.

        Supports operators scheduling a product-specific override
        without deleting the base catch-all row.
        """
        catch_all = _make_federal_row(
            product_codes=["GASOLINE_REG", "DIESEL_2", "HEATING_OIL"],
            rate_cents_per_gallon=200,
        )
        specific = _make_federal_row(
            product_codes=["GASOLINE_REG"],
            rate_cents_per_gallon=220,
        )

        amount, line = tax_engine._compute_federal_excise(
            product_code="GASOLINE_REG",
            net_gallons=1000.0,
            jurisdiction_rates=[catch_all, specific],
        )

        # 220 × 1000 / 10 == 22_000 cents
        assert amount == 22_000
        assert line is not None
        assert line.rate_cents_per_gallon == 220


# ---------------------------------------------------------------------------
# Line-item shape
# ---------------------------------------------------------------------------


class TestLineItemShape:
    """The emitted TaxLineItem records the federal provenance fields."""

    def test_line_item_fields_for_diesel(self, tax_engine: TaxEngine):
        _, line = tax_engine._compute_federal_excise(
            product_code="DIESEL_2",
            net_gallons=2000.0,
            jurisdiction_rates=[],
        )

        assert line is not None
        assert line.tax_component_name == "federal_excise"
        assert line.jurisdiction_fips == "00"
        assert line.jurisdiction_level == "federal"
        assert line.rate_cents_per_gallon == 244
        assert line.gallons == 2000.0
        # 244 × 2000 / 10 == 48_800 cents
        assert line.amount_cents == 48_800


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Invalid inputs raise ``ValueError`` with actionable messages."""

    def test_negative_gallons_rejected(self, tax_engine: TaxEngine):
        with pytest.raises(ValueError, match="net_gallons must be >= 0"):
            tax_engine._compute_federal_excise(
                product_code="DIESEL_2",
                net_gallons=-1.0,
                jurisdiction_rates=[],
            )

    def test_non_string_product_code_rejected(self, tax_engine: TaxEngine):
        with pytest.raises(ValueError, match="product_code must be a string"):
            tax_engine._compute_federal_excise(
                product_code=123,  # type: ignore[arg-type]
                net_gallons=100.0,
                jurisdiction_rates=[],
            )

    def test_unknown_product_code_rejected(self, tax_engine: TaxEngine):
        """Unknown products raise through ``canonicalize`` (UnknownFuelProductError)."""
        from fuel.services.fuel_product_catalog import UnknownFuelProductError

        with pytest.raises(UnknownFuelProductError):
            tax_engine._compute_federal_excise(
                product_code="UNOBTANIUM",
                net_gallons=100.0,
                jurisdiction_rates=[],
            )
