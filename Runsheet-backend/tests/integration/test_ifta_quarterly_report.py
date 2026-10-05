"""Integration test: IFTA quarterly report aggregates mileage + fuel from multiple sources.

Verifies that ``IFTAReporter.generate_quarterly_report()`` correctly:
1. Aggregates Geotab mileage per truck per jurisdiction from the ``ifta_mileage`` index
2. Aggregates fuel gallons from terminal BOLs + fuel card transactions
3. Flags trucks with missing Geotab data (``ifta_data_incomplete``)
4. Computes fleet MPG as total_miles / total_gallons
5. Produces per-truck, per-jurisdiction IFTA summaries with correct
   net_taxable_gallons = (taxable_miles / fleet_mpg) - tax_paid_gallons

Test scenario:
- Fleet has 3 trucks: truck_A, truck_B, truck_C
- truck_A: 800 miles in CA, 200 miles in NV (Geotab)
- truck_B: 600 miles in CA, 400 miles in AZ (Geotab)
- truck_C: NO Geotab data → flagged as ifta_data_incomplete
- Terminal BOLs: truck_A purchased 150 gal in CA; truck_B purchased 120 gal in AZ
- Fuel cards: truck_A purchased 50 gal in NV

ES is fully mocked via side_effect to return the correct data for each query.

Validates: Requirements 7.2, 7.3, 7.4, 7.5, 7.6
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from compliance.services.ifta_reporter import IFTAReporter

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant_ifta_integ"
QUARTER = "2026-Q3"

# Fleet trucks
TRUCK_A = "truck_A"
TRUCK_B = "truck_B"
TRUCK_C = "truck_C"  # No Geotab data → flagged

# Mileage data (via Geotab trip segments)
TRUCK_A_CA_MILES = 800.0
TRUCK_A_NV_MILES = 200.0
TRUCK_B_CA_MILES = 600.0
TRUCK_B_AZ_MILES = 400.0

TOTAL_MILES = (
    TRUCK_A_CA_MILES + TRUCK_A_NV_MILES + TRUCK_B_CA_MILES + TRUCK_B_AZ_MILES
)

# Fuel data (terminal BOLs + fuel cards)
TRUCK_A_CA_BOL_GALLONS = 150.0
TRUCK_B_AZ_BOL_GALLONS = 120.0
TRUCK_A_NV_FUELCARD_GALLONS = 50.0

TOTAL_GALLONS = (
    TRUCK_A_CA_BOL_GALLONS + TRUCK_B_AZ_BOL_GALLONS + TRUCK_A_NV_FUELCARD_GALLONS
)

EXPECTED_FLEET_MPG = TOTAL_MILES / TOTAL_GALLONS


# ---------------------------------------------------------------------------
# ES mock responses
# ---------------------------------------------------------------------------


def _make_fleet_trucks_response() -> Dict[str, Any]:
    """Response for _get_fleet_truck_ids: all 3 trucks in the fleet."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {
            "truck_ids": {
                "buckets": [
                    {"key": TRUCK_A, "doc_count": 1},
                    {"key": TRUCK_B, "doc_count": 1},
                    {"key": TRUCK_C, "doc_count": 1},
                ]
            }
        },
    }


def _make_trucks_with_mileage_data_response() -> Dict[str, Any]:
    """Response for _get_trucks_with_mileage_data: only A and B have data."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {
            "truck_ids": {
                "buckets": [
                    {"key": TRUCK_A, "doc_count": 5},
                    {"key": TRUCK_B, "doc_count": 4},
                    # truck_C absent → flagged
                ]
            }
        },
    }


def _make_all_trucks_mileage_response() -> Dict[str, Any]:
    """Response for get_all_trucks_mileage: per-truck, per-state mileage."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {
            "by_truck": {
                "buckets": [
                    {
                        "key": TRUCK_A,
                        "doc_count": 5,
                        "by_jurisdiction": {
                            "buckets": [
                                {
                                    "key": "CA",
                                    "doc_count": 3,
                                    "total_miles": {"value": TRUCK_A_CA_MILES},
                                    "taxable_miles": {"value": TRUCK_A_CA_MILES},
                                },
                                {
                                    "key": "NV",
                                    "doc_count": 2,
                                    "total_miles": {"value": TRUCK_A_NV_MILES},
                                    "taxable_miles": {"value": TRUCK_A_NV_MILES},
                                },
                            ]
                        },
                        "truck_total_miles": {
                            "value": TRUCK_A_CA_MILES + TRUCK_A_NV_MILES
                        },
                    },
                    {
                        "key": TRUCK_B,
                        "doc_count": 4,
                        "by_jurisdiction": {
                            "buckets": [
                                {
                                    "key": "CA",
                                    "doc_count": 2,
                                    "total_miles": {"value": TRUCK_B_CA_MILES},
                                    "taxable_miles": {"value": TRUCK_B_CA_MILES},
                                },
                                {
                                    "key": "AZ",
                                    "doc_count": 2,
                                    "total_miles": {"value": TRUCK_B_AZ_MILES},
                                    "taxable_miles": {"value": TRUCK_B_AZ_MILES},
                                },
                            ]
                        },
                        "truck_total_miles": {
                            "value": TRUCK_B_CA_MILES + TRUCK_B_AZ_MILES
                        },
                    },
                ]
            }
        },
    }



def _make_terminal_bols_response() -> Dict[str, Any]:
    """Response for _aggregate_terminal_bol_gallons."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {
            "by_truck": {
                "buckets": [
                    {
                        "key": TRUCK_A,
                        "doc_count": 2,
                        "by_jurisdiction": {
                            "buckets": [
                                {
                                    "key": "CA",
                                    "doc_count": 2,
                                    "total_gallons": {
                                        "value": TRUCK_A_CA_BOL_GALLONS
                                    },
                                },
                            ]
                        },
                        "truck_total_gallons": {
                            "value": TRUCK_A_CA_BOL_GALLONS
                        },
                    },
                    {
                        "key": TRUCK_B,
                        "doc_count": 1,
                        "by_jurisdiction": {
                            "buckets": [
                                {
                                    "key": "AZ",
                                    "doc_count": 1,
                                    "total_gallons": {
                                        "value": TRUCK_B_AZ_BOL_GALLONS
                                    },
                                },
                            ]
                        },
                        "truck_total_gallons": {
                            "value": TRUCK_B_AZ_BOL_GALLONS
                        },
                    },
                ]
            }
        },
    }


def _make_fuel_card_response() -> Dict[str, Any]:
    """Response for _aggregate_fuel_card_gallons."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {
            "by_truck": {
                "buckets": [
                    {
                        "key": TRUCK_A,
                        "doc_count": 1,
                        "by_jurisdiction": {
                            "buckets": [
                                {
                                    "key": "NV",
                                    "doc_count": 1,
                                    "total_gallons": {
                                        "value": TRUCK_A_NV_FUELCARD_GALLONS
                                    },
                                },
                            ]
                        },
                        "truck_total_gallons": {
                            "value": TRUCK_A_NV_FUELCARD_GALLONS
                        },
                    },
                ]
            }
        },
    }


def _make_tax_jurisdictions_response() -> Dict[str, Any]:
    """State excise rows in the Tax_Engine RATE_SCALE convention (tenths of a cent).

    CA 200 = 20.0 c/gal and NV 270 = 27.0 c/gal are in effect for the quarter.
    The other two CA rows must be ignored: one takes effect after the quarter
    ends, the other expired before it began.
    """
    def row(fips, rate, effective, expiry=None):
        src = {
            "fips_code": fips,
            "jurisdiction_level": "state",
            "tax_type": "excise",
            "rate_cents_per_gallon": rate,
            "effective_date": effective,
            "tenant_id": TENANT_ID,
        }
        if expiry:
            src["expiry_date"] = expiry
        return {"_source": src}

    hits = [
        row("06", 200, "2025-01-01"),
        row("32", 270, "2025-01-01"),
        row("06", 900, "2026-10-01"),  # next quarter
        row("06", 800, "2024-01-01", expiry="2026-06-30"),  # expired before Q3
    ]
    return {"hits": {"hits": hits, "total": {"value": len(hits)}}}


# ---------------------------------------------------------------------------
# ES mock builder with call routing
# ---------------------------------------------------------------------------


def _build_es_mock() -> AsyncMock:
    """Build ES mock that routes search_documents calls by index name."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)

    call_counter: Dict[str, int] = {}

    # ``size`` is positional: the reporter calls
    # ``search_documents(index, query, 200)``. A mock without it raised
    # TypeError there, which the reporter swallowed as "no rates", so the
    # tax-rate path was never exercised (finding C6).
    async def _search_documents(index: str, query: Any, size: int = 100, **kwargs) -> Dict:
        call_counter.setdefault(index, 0)
        call_counter[index] += 1

        if index == "trucks":
            return _make_fleet_trucks_response()
        elif index == "ifta_mileage":
            # First call: _get_trucks_with_mileage_data (terms agg)
            # Second call: get_all_trucks_mileage (nested agg)
            if call_counter[index] == 1:
                return _make_trucks_with_mileage_data_response()
            else:
                return _make_all_trucks_mileage_response()
        elif index == "terminal_bols":
            return _make_terminal_bols_response()
        elif index == "fuel_card_transactions":
            return _make_fuel_card_response()
        elif index == "tax_jurisdictions":
            return _make_tax_jurisdictions_response()
        else:
            return {"hits": {"hits": [], "total": {"value": 0}}}

    es.search_documents = AsyncMock(side_effect=_search_documents)
    return es


# ===========================================================================
# Integration Test
# ===========================================================================


class TestIFTAQuarterlyReport:
    """End-to-end: IFTA quarterly report aggregates Geotab mileage + terminal BOL fuel.

    3 trucks, only 2 have Geotab data. Fleet MPG = total_miles / total_gallons.
    Each truck's per-jurisdiction breakdown includes net_taxable_gallons computation.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.es = _build_es_mock()
        # StateBoundaryDetector is unused by generate_quarterly_report;
        # pass a mock to satisfy the constructor.
        self.sbd = AsyncMock()
        self.reporter = IFTAReporter(self.es, self.sbd)

    @pytest.mark.asyncio
    async def test_quarterly_report_structure(self):
        """Report has the correct top-level structure."""
        report = await self.reporter.generate_quarterly_report(
            tenant_id=TENANT_ID,
            quarter=QUARTER,
        )

        assert report.tenant_id == TENANT_ID
        assert report.quarter == QUARTER

        # 2 qualified trucks (A and B), truck_C is flagged
        assert report.truck_count == 2
        assert len(report.trucks) == 2

        # 1 truck flagged as incomplete
        assert len(report.incomplete_trucks) == 1
        assert report.incomplete_trucks[0].truck_id == TRUCK_C
        assert report.incomplete_trucks[0].flag_type == "ifta_data_incomplete"

    @pytest.mark.asyncio
    async def test_fleet_totals(self):
        """Fleet-level totals match expected values."""
        report = await self.reporter.generate_quarterly_report(
            tenant_id=TENANT_ID,
            quarter=QUARTER,
        )

        assert report.total_miles == TOTAL_MILES  # 2000
        assert report.total_gallons == TOTAL_GALLONS  # 320
        assert report.fleet_mpg == pytest.approx(EXPECTED_FLEET_MPG, rel=1e-4)

    @pytest.mark.asyncio
    async def test_truck_a_jurisdictions(self):
        """Truck A has CA and NV jurisdiction entries with correct values."""
        report = await self.reporter.generate_quarterly_report(
            tenant_id=TENANT_ID,
            quarter=QUARTER,
        )

        truck_a = next(t for t in report.trucks if t.truck_id == TRUCK_A)
        assert truck_a.total_miles == TRUCK_A_CA_MILES + TRUCK_A_NV_MILES
        assert truck_a.total_gallons == pytest.approx(
            TRUCK_A_CA_BOL_GALLONS + TRUCK_A_NV_FUELCARD_GALLONS, rel=1e-4
        )

        # Get CA entry
        ca_entry = next(
            j for j in truck_a.jurisdictions if j.jurisdiction == "CA"
        )
        assert ca_entry.total_miles == TRUCK_A_CA_MILES
        assert ca_entry.taxable_miles == TRUCK_A_CA_MILES
        assert ca_entry.tax_paid_gallons == TRUCK_A_CA_BOL_GALLONS

        # net_taxable_gallons = (taxable_miles / fleet_mpg) - tax_paid_gallons
        expected_ca_consumed = TRUCK_A_CA_MILES / EXPECTED_FLEET_MPG
        expected_ca_net = expected_ca_consumed - TRUCK_A_CA_BOL_GALLONS
        assert ca_entry.net_taxable_gallons == pytest.approx(
            expected_ca_net, rel=1e-4
        )

        # Get NV entry
        nv_entry = next(
            j for j in truck_a.jurisdictions if j.jurisdiction == "NV"
        )
        assert nv_entry.total_miles == TRUCK_A_NV_MILES
        assert nv_entry.tax_paid_gallons == TRUCK_A_NV_FUELCARD_GALLONS

        expected_nv_consumed = TRUCK_A_NV_MILES / EXPECTED_FLEET_MPG
        expected_nv_net = expected_nv_consumed - TRUCK_A_NV_FUELCARD_GALLONS
        assert nv_entry.net_taxable_gallons == pytest.approx(
            expected_nv_net, rel=1e-4
        )

    @pytest.mark.asyncio
    async def test_truck_b_jurisdictions(self):
        """Truck B has CA and AZ jurisdiction entries."""
        report = await self.reporter.generate_quarterly_report(
            tenant_id=TENANT_ID,
            quarter=QUARTER,
        )

        truck_b = next(t for t in report.trucks if t.truck_id == TRUCK_B)
        assert truck_b.total_miles == TRUCK_B_CA_MILES + TRUCK_B_AZ_MILES

        # AZ entry: fuel purchased in AZ via terminal BOL
        az_entry = next(
            j for j in truck_b.jurisdictions if j.jurisdiction == "AZ"
        )
        assert az_entry.total_miles == TRUCK_B_AZ_MILES
        assert az_entry.tax_paid_gallons == TRUCK_B_AZ_BOL_GALLONS

        expected_az_consumed = TRUCK_B_AZ_MILES / EXPECTED_FLEET_MPG
        expected_az_net = expected_az_consumed - TRUCK_B_AZ_BOL_GALLONS
        assert az_entry.net_taxable_gallons == pytest.approx(
            expected_az_net, rel=1e-4
        )

        # CA entry: no fuel purchased in CA for truck B
        ca_entry = next(
            j for j in truck_b.jurisdictions if j.jurisdiction == "CA"
        )
        assert ca_entry.tax_paid_gallons == 0.0

        expected_ca_consumed = TRUCK_B_CA_MILES / EXPECTED_FLEET_MPG
        assert ca_entry.net_taxable_gallons == pytest.approx(
            expected_ca_consumed, rel=1e-4
        )

    @pytest.mark.asyncio
    async def test_truck_c_excluded_from_report(self):
        """Truck C (no Geotab data) is flagged and excluded from truck summaries."""
        report = await self.reporter.generate_quarterly_report(
            tenant_id=TENANT_ID,
            quarter=QUARTER,
        )

        truck_ids_in_report = [t.truck_id for t in report.trucks]
        assert TRUCK_C not in truck_ids_in_report

        flagged_ids = [f.truck_id for f in report.incomplete_trucks]
        assert TRUCK_C in flagged_ids

    @pytest.mark.asyncio
    async def test_fleet_jurisdiction_aggregation(self):
        """Fleet-level jurisdictions aggregate mileage across all qualified trucks."""
        report = await self.reporter.generate_quarterly_report(
            tenant_id=TENANT_ID,
            quarter=QUARTER,
        )

        jur_map = {j.jurisdiction: j for j in report.jurisdictions}

        # CA: truck_A(800) + truck_B(600)
        assert "CA" in jur_map
        assert jur_map["CA"].total_miles == TRUCK_A_CA_MILES + TRUCK_B_CA_MILES

        # NV: truck_A(200)
        assert "NV" in jur_map
        assert jur_map["NV"].total_miles == TRUCK_A_NV_MILES

        # AZ: truck_B(400)
        assert "AZ" in jur_map
        assert jur_map["AZ"].total_miles == TRUCK_B_AZ_MILES


class TestIFTATaxRates:
    """Finding C6: rates are scaled from tenths of a cent and dated to the quarter."""

    @pytest.mark.asyncio
    async def test_tax_rates_are_scaled_and_dated(self, caplog):
        es = _build_es_mock()
        reporter = IFTAReporter(es, AsyncMock())

        with caplog.at_level(logging.WARNING, logger="compliance.services.ifta_reporter"):
            report = await reporter.generate_quarterly_report(TENANT_ID, QUARTER)

        assert any(c.args[0] == "tax_jurisdictions" for c in es.search_documents.call_args_list)
        assert not [r for r in caplog.records if "failed to load" in r.getMessage()]

        entries = [j for t in report.trucks for j in t.jurisdictions]
        ca = [j for j in entries if j.jurisdiction == "CA"]
        nv = [j for j in entries if j.jurisdiction == "NV"]
        assert ca and nv
        for j in ca:
            assert j.tax_rate == 20.0
            assert j.tax_due == round(j.net_taxable_gallons * 20.0, 2)
        for j in nv:
            assert j.tax_rate == 27.0
