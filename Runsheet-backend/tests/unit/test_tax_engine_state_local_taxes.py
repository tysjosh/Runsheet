"""Unit tests for ``TaxEngine._compute_state_local_taxes_and_fees`` (Task 3.6).

Covers the state / county / city excise and UST / SPCC / environmental
fee components of the Tax_Engine per Requirements 1.2, 1.3, and 1.4.

Requirement 1.4 states:

    WHEN a delivery is invoiced, THE Tax_Engine SHALL compute UST,
    SPCC, and environmental fees from the jurisdictional tax table and
    add them as separate line items on the invoice.

The helper under test iterates a pre-fetched ``List[JurisdictionRate]``
rollup (as returned by :meth:`TaxEngine.get_jurisdiction_rates`, Task
3.4) and produces a structured dict with per-bucket cents and a
per-row :class:`TaxLineItem` trail. Federal rows are *not* handled
here (Task 3.5 owns them) so the two helpers stay orthogonal for the
Task 3.10 composition.

Rates are stored in the ``RATE_SCALE == 10`` convention (tenths of a
cent per gallon) so every amount rendered in these tests follows::

    amount_cents = round(rate_stored * gallons / RATE_SCALE)

Validates: Requirement 1.4
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

import pytest

from compliance.models.jurisdiction_rate import JurisdictionRate
from compliance.services.tax_engine import (
    CITY_EXCISE_COMPONENT_NAME,
    COUNTY_EXCISE_COMPONENT_NAME,
    ENVIRONMENTAL_FEE_COMPONENT_NAME,
    FEDERAL_FIPS_SENTINEL,
    SPCC_FEE_COMPONENT_NAME,
    STATE_EXCISE_FALLBACK_COMPONENT_NAME,
    TaxEngine,
    TaxLineItem,
    UST_FEE_COMPONENT_NAME,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal ES stand-in — the helper under test never calls ES."""


@pytest.fixture
def tax_engine() -> TaxEngine:
    return TaxEngine(es_service=_FakeESService(), tenant_id="tenant-1")


def _make_rate(
    *,
    fips_code: str,
    jurisdiction_level: str,
    tax_type: str,
    rate_cents_per_gallon: int,
    product_codes: Optional[List[str]] = None,
    jurisdiction_name: Optional[str] = None,
    tenant_id: str = "tenant-1",
) -> JurisdictionRate:
    """Build a JurisdictionRate with sensible defaults."""
    return JurisdictionRate(
        tenant_id=tenant_id,
        fips_code=fips_code,
        jurisdiction_level=jurisdiction_level,  # type: ignore[arg-type]
        jurisdiction_name=jurisdiction_name,
        tax_type=tax_type,  # type: ignore[arg-type]
        product_codes=list(
            product_codes or ["DIESEL_2", "GASOLINE_REG"]
        ),
        rate_cents_per_gallon=rate_cents_per_gallon,
        effective_date=date(2024, 1, 1),
    )


# ---------------------------------------------------------------------------
# Return-shape contract
# ---------------------------------------------------------------------------


class TestReturnShape:
    """The helper returns the documented dict shape."""

    def test_empty_rates_produces_zero_buckets(self, tax_engine: TaxEngine):
        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=[],
            destination_fips="06075",
        )

        assert result == {
            "state_cents": 0,
            "county_cents": 0,
            "city_cents": 0,
            "ust_cents": 0,
            "spcc_cents": 0,
            "environmental_cents": 0,
            "line_items": [],
        }

    def test_result_keys_are_exhaustive(self, tax_engine: TaxEngine):
        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=100.0,
            jurisdiction_rates=[],
            destination_fips="06",
        )
        assert set(result.keys()) == {
            "state_cents",
            "county_cents",
            "city_cents",
            "ust_cents",
            "spcc_cents",
            "environmental_cents",
            "line_items",
        }


# ---------------------------------------------------------------------------
# State excise
# ---------------------------------------------------------------------------


class TestStateExcise:
    """State-level excise routing and component-name shape."""

    def test_state_excise_applied_to_matching_product(
        self, tax_engine: TaxEngine
    ):
        """A state excise row for DIESEL_2 applies to a DIESEL_2 delivery.

        California levies roughly 40.0¢/gal on diesel; the RATE_SCALE=10
        convention stores this as 400 tenths. 400 × 1000 / 10 = 40_000
        cents = $400.00.
        """
        ca_diesel = _make_rate(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="excise",
            rate_cents_per_gallon=400,
            product_codes=["DIESEL_2"],
            jurisdiction_name="CA",
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=[ca_diesel],
            destination_fips="06075",
        )

        assert result["state_cents"] == 40_000
        assert result["county_cents"] == 0
        assert result["city_cents"] == 0
        assert len(result["line_items"]) == 1
        line = result["line_items"][0]
        assert isinstance(line, TaxLineItem)
        assert line.tax_component_name == "CA_state_excise"
        assert line.jurisdiction_fips == "06"
        assert line.jurisdiction_level == "state"
        assert line.rate_cents_per_gallon == 400
        assert line.gallons == 1000.0
        assert line.amount_cents == 40_000

    def test_state_excise_component_falls_back_without_jurisdiction_name(
        self, tax_engine: TaxEngine
    ):
        """When jurisdiction_name is None, fallback to ``state_excise``."""
        unnamed = _make_rate(
            fips_code="48",
            jurisdiction_level="state",
            tax_type="excise",
            rate_cents_per_gallon=200,
            product_codes=["DIESEL_2"],
            jurisdiction_name=None,
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=500.0,
            jurisdiction_rates=[unnamed],
            destination_fips="48",
        )

        line = result["line_items"][0]
        assert line.tax_component_name == STATE_EXCISE_FALLBACK_COMPONENT_NAME
        assert line.tax_component_name == "state_excise"

    def test_state_excise_not_applied_when_product_not_in_list(
        self, tax_engine: TaxEngine
    ):
        """Rows whose product_codes exclude the delivered product
        contribute zero and emit no line item.
        """
        diesel_only = _make_rate(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="excise",
            rate_cents_per_gallon=400,
            product_codes=["DIESEL_2"],
            jurisdiction_name="CA",
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="GASOLINE_REG",
            net_gallons=1000.0,
            jurisdiction_rates=[diesel_only],
            destination_fips="06075",
        )

        assert result["state_cents"] == 0
        assert result["line_items"] == []


# ---------------------------------------------------------------------------
# County excise
# ---------------------------------------------------------------------------


class TestCountyExcise:
    """County-level excise routing keeps county_cents separate from state_cents."""

    def test_county_excise_summed_separately(self, tax_engine: TaxEngine):
        """Even when a state row is also present, county excise stays in its bucket."""
        ca_state = _make_rate(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="excise",
            rate_cents_per_gallon=400,
            product_codes=["DIESEL_2"],
            jurisdiction_name="CA",
        )
        la_county = _make_rate(
            fips_code="06037",
            jurisdiction_level="county",
            tax_type="excise",
            rate_cents_per_gallon=50,
            product_codes=["DIESEL_2"],
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=[ca_state, la_county],
            destination_fips="0603700",
        )

        # 400 × 1000 / 10 == 40_000 (state)
        assert result["state_cents"] == 40_000
        # 50 × 1000 / 10 == 5_000 (county)
        assert result["county_cents"] == 5_000
        # State and county are separate buckets — not summed into each other.
        assert result["state_cents"] != result["county_cents"]

        components = {li.tax_component_name for li in result["line_items"]}
        assert components == {"CA_state_excise", COUNTY_EXCISE_COMPONENT_NAME}

    def test_city_excise_routes_to_city_bucket(self, tax_engine: TaxEngine):
        """City-level excise lands in city_cents, not state or county."""
        city = _make_rate(
            fips_code="0603700",
            jurisdiction_level="city",
            tax_type="excise",
            rate_cents_per_gallon=30,
            product_codes=["DIESEL_2"],
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=[city],
            destination_fips="0603700",
        )

        assert result["state_cents"] == 0
        assert result["county_cents"] == 0
        # 30 × 1000 / 10 == 3_000 cents
        assert result["city_cents"] == 3_000
        line = result["line_items"][0]
        assert line.tax_component_name == CITY_EXCISE_COMPONENT_NAME
        assert line.jurisdiction_level == "city"


# ---------------------------------------------------------------------------
# UST / SPCC / environmental
# ---------------------------------------------------------------------------


class TestEnvironmentalFees:
    """UST, SPCC, and environmental fees land in their own line items (Req 1.4)."""

    def test_ust_spcc_environmental_as_separate_line_items(
        self, tax_engine: TaxEngine
    ):
        """All three fee types produce distinct line items on the breakdown."""
        ust = _make_rate(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="ust",
            rate_cents_per_gallon=20,  # 2.0¢ / gal
            product_codes=["DIESEL_2"],
        )
        spcc = _make_rate(
            fips_code="06037",
            jurisdiction_level="county",
            tax_type="spcc",
            rate_cents_per_gallon=10,  # 1.0¢ / gal
            product_codes=["DIESEL_2"],
        )
        env = _make_rate(
            fips_code="0603700",
            jurisdiction_level="city",
            tax_type="environmental",
            rate_cents_per_gallon=5,  # 0.5¢ / gal
            product_codes=["DIESEL_2"],
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=[ust, spcc, env],
            destination_fips="0603700",
        )

        # 20 × 1000 / 10 == 2_000 cents ($20.00)
        assert result["ust_cents"] == 2_000
        # 10 × 1000 / 10 == 1_000 cents ($10.00)
        assert result["spcc_cents"] == 1_000
        # 5 × 1000 / 10 == 500 cents ($5.00)
        assert result["environmental_cents"] == 500

        # Excise buckets stay at zero — these fees do not route through excise.
        assert result["state_cents"] == 0
        assert result["county_cents"] == 0
        assert result["city_cents"] == 0

        components = {li.tax_component_name for li in result["line_items"]}
        assert components == {
            UST_FEE_COMPONENT_NAME,
            SPCC_FEE_COMPONENT_NAME,
            ENVIRONMENTAL_FEE_COMPONENT_NAME,
        }

    def test_ust_at_any_level_goes_to_ust_bucket(self, tax_engine: TaxEngine):
        """UST tax_type routes to ust_cents regardless of jurisdiction level."""
        state_ust = _make_rate(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="ust",
            rate_cents_per_gallon=10,
            product_codes=["DIESEL_2"],
        )
        city_ust = _make_rate(
            fips_code="0603700",
            jurisdiction_level="city",
            tax_type="ust",
            rate_cents_per_gallon=5,
            product_codes=["DIESEL_2"],
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=[state_ust, city_ust],
            destination_fips="0603700",
        )

        # Both UST rows → ust_cents regardless of level
        # (10 + 5) × 1000 / 10 == 1_500 cents
        assert result["ust_cents"] == 1_500
        assert result["state_cents"] == 0
        assert result["city_cents"] == 0
        # Two line items, both tagged ust_fee
        assert len(result["line_items"]) == 2
        assert all(
            li.tax_component_name == UST_FEE_COMPONENT_NAME
            for li in result["line_items"]
        )


# ---------------------------------------------------------------------------
# Multiple rates at the same level
# ---------------------------------------------------------------------------


class TestMultipleRatesSameLevel:
    """Two rows at the same level sum correctly into one bucket."""

    def test_multiple_state_excise_rows_sum_correctly(
        self, tax_engine: TaxEngine
    ):
        """Stacked state-level excise rows (e.g. base + surcharge) sum."""
        base = _make_rate(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="excise",
            rate_cents_per_gallon=300,
            product_codes=["DIESEL_2"],
            jurisdiction_name="CA",
        )
        surcharge = _make_rate(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="excise",
            rate_cents_per_gallon=100,
            product_codes=["DIESEL_2"],
            jurisdiction_name="CA",
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=[base, surcharge],
            destination_fips="06",
        )

        # (300 + 100) × 1000 / 10 == 40_000 cents
        assert result["state_cents"] == 40_000
        assert len(result["line_items"]) == 2
        # Both line items roll up under the named state component.
        assert all(
            li.tax_component_name == "CA_state_excise"
            for li in result["line_items"]
        )

    def test_multiple_ust_rows_sum_correctly(self, tax_engine: TaxEngine):
        """Multiple UST rows sum into one ust_cents bucket with separate line items."""
        ust_a = _make_rate(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="ust",
            rate_cents_per_gallon=15,
            product_codes=["DIESEL_2"],
        )
        ust_b = _make_rate(
            fips_code="06037",
            jurisdiction_level="county",
            tax_type="ust",
            rate_cents_per_gallon=25,
            product_codes=["DIESEL_2"],
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=[ust_a, ust_b],
            destination_fips="0603700",
        )

        # (15 + 25) × 1000 / 10 == 4_000 cents
        assert result["ust_cents"] == 4_000
        assert len(result["line_items"]) == 2


# ---------------------------------------------------------------------------
# Zero rates still emit line items
# ---------------------------------------------------------------------------


class TestZeroRateAuditability:
    """Zero-rate rows still emit line items so auditors can see the row
    applied to the delivery (Req 1.10 — per-component rate / gallons /
    amount captured for every applicable jurisdiction).
    """

    def test_zero_rate_row_still_produces_line_item(self, tax_engine: TaxEngine):
        """A zero-rate excise row contributes 0 cents but a line item."""
        zero_rate = _make_rate(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="excise",
            rate_cents_per_gallon=0,
            product_codes=["DIESEL_2"],
            jurisdiction_name="CA",
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=[zero_rate],
            destination_fips="06",
        )

        assert result["state_cents"] == 0
        assert len(result["line_items"]) == 1
        line = result["line_items"][0]
        assert line.rate_cents_per_gallon == 0
        assert line.amount_cents == 0
        assert line.gallons == 1000.0
        assert line.tax_component_name == "CA_state_excise"

    def test_zero_rate_ust_row_still_produces_line_item(
        self, tax_engine: TaxEngine
    ):
        """A zero-rate UST row emits the audit row (e.g. scheduled suspension)."""
        suspended_ust = _make_rate(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="ust",
            rate_cents_per_gallon=0,
            product_codes=["DIESEL_2"],
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=[suspended_ust],
            destination_fips="06",
        )

        assert result["ust_cents"] == 0
        assert len(result["line_items"]) == 1
        assert result["line_items"][0].tax_component_name == UST_FEE_COMPONENT_NAME
        assert result["line_items"][0].amount_cents == 0


# ---------------------------------------------------------------------------
# Federal rows are ignored
# ---------------------------------------------------------------------------


class TestFederalRowsIgnored:
    """Federal rows are owned by ``_compute_federal_excise`` (Task 3.5) —
    the state/local helper must not double-count them.
    """

    def test_federal_excise_row_ignored(self, tax_engine: TaxEngine):
        """A federal excise row does not land in any state-local bucket."""
        federal = _make_rate(
            fips_code=FEDERAL_FIPS_SENTINEL,
            jurisdiction_level="federal",
            tax_type="excise",
            rate_cents_per_gallon=244,
            product_codes=["DIESEL_2"],
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=[federal],
            destination_fips="06",
        )

        assert result == {
            "state_cents": 0,
            "county_cents": 0,
            "city_cents": 0,
            "ust_cents": 0,
            "spcc_cents": 0,
            "environmental_cents": 0,
            "line_items": [],
        }


# ---------------------------------------------------------------------------
# Product canonicalization + mixed rollup
# ---------------------------------------------------------------------------


class TestProductCanonicalization:
    """Legacy aliases canonicalize to the same bucket as their canonical form."""

    def test_legacy_alias_matches_canonical_product(
        self, tax_engine: TaxEngine
    ):
        """``AGO`` canonicalizes to ``DIESEL_2`` so a DIESEL_2 rate applies."""
        diesel_rate = _make_rate(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="excise",
            rate_cents_per_gallon=400,
            product_codes=["DIESEL_2"],
            jurisdiction_name="CA",
        )

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="AGO",  # Nigerian alias for DIESEL_2
            net_gallons=1000.0,
            jurisdiction_rates=[diesel_rate],
            destination_fips="06",
        )

        assert result["state_cents"] == 40_000
        assert len(result["line_items"]) == 1


class TestMixedRollupIntegration:
    """A realistic mixed rollup exercises every bucket at once."""

    def test_full_rollup_state_county_city_ust_spcc_environmental(
        self, tax_engine: TaxEngine
    ):
        """All six buckets populate correctly from a mixed rollup.

        Scenario: California / Los Angeles County / Los Angeles city
        diesel delivery with state excise + county excise + city
        excise + state UST + county SPCC + state environmental.
        """
        rows = [
            _make_rate(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="excise",
                rate_cents_per_gallon=400,
                product_codes=["DIESEL_2"],
                jurisdiction_name="CA",
            ),
            _make_rate(
                fips_code="06037",
                jurisdiction_level="county",
                tax_type="excise",
                rate_cents_per_gallon=50,
                product_codes=["DIESEL_2"],
            ),
            _make_rate(
                fips_code="0603700",
                jurisdiction_level="city",
                tax_type="excise",
                rate_cents_per_gallon=30,
                product_codes=["DIESEL_2"],
            ),
            _make_rate(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="ust",
                rate_cents_per_gallon=20,
                product_codes=["DIESEL_2"],
            ),
            _make_rate(
                fips_code="06037",
                jurisdiction_level="county",
                tax_type="spcc",
                rate_cents_per_gallon=10,
                product_codes=["DIESEL_2"],
            ),
            _make_rate(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="environmental",
                rate_cents_per_gallon=5,
                product_codes=["DIESEL_2"],
            ),
        ]

        result = tax_engine._compute_state_local_taxes_and_fees(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            jurisdiction_rates=rows,
            destination_fips="0603700",
        )

        # Each row: rate × 1000 / 10 == rate × 100
        assert result["state_cents"] == 40_000
        assert result["county_cents"] == 5_000
        assert result["city_cents"] == 3_000
        assert result["ust_cents"] == 2_000
        assert result["spcc_cents"] == 1_000
        assert result["environmental_cents"] == 500
        # Six rows → six line items.
        assert len(result["line_items"]) == 6


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Invalid inputs raise ``ValueError`` with actionable messages."""

    def test_negative_gallons_rejected(self, tax_engine: TaxEngine):
        with pytest.raises(ValueError, match="net_gallons must be >= 0"):
            tax_engine._compute_state_local_taxes_and_fees(
                product_code="DIESEL_2",
                net_gallons=-1.0,
                jurisdiction_rates=[],
                destination_fips="06",
            )

    def test_non_string_product_code_rejected(self, tax_engine: TaxEngine):
        with pytest.raises(ValueError, match="product_code must be a string"):
            tax_engine._compute_state_local_taxes_and_fees(
                product_code=123,  # type: ignore[arg-type]
                net_gallons=100.0,
                jurisdiction_rates=[],
                destination_fips="06",
            )

    def test_invalid_destination_fips_rejected(self, tax_engine: TaxEngine):
        """Destination FIPS validation happens before iteration — short-circuits bad inputs."""
        with pytest.raises(ValueError):
            tax_engine._compute_state_local_taxes_and_fees(
                product_code="DIESEL_2",
                net_gallons=100.0,
                jurisdiction_rates=[],
                destination_fips="abc",
            )
