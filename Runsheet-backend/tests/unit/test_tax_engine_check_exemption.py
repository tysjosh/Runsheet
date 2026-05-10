"""Unit tests for :meth:`TaxEngine.check_exemption` and
:meth:`TaxEngine.apply_exemption` (Task 3.7).

Covers Requirements 1.7 (road-use exemption: dyed / off-road / 637M
excludes federal + state excise) and 1.8 (farm exemption flagged
without amount adjustment, with rate reduction resolved upstream via
the farm-specific jurisdiction row).

The test suite uses an async fake ES service that applies the same
``term`` / ``range`` / ``tenant_id`` / ``product_codes`` filters that a
real Elasticsearch cluster would apply, so the query shape emitted by
``check_exemption`` is exercised end-to-end.

Validates: Requirements 1.7, 1.8
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pytest

from compliance.models.tax_exemption import TaxExemption
from compliance.services.compliance_es_mappings import TAX_EXEMPTIONS_INDEX
from compliance.services.tax_engine import (
    CITY_EXCISE_COMPONENT_NAME,
    COUNTY_EXCISE_COMPONENT_NAME,
    ENVIRONMENTAL_FEE_COMPONENT_NAME,
    FEDERAL_EXCISE_COMPONENT_NAME,
    FEDERAL_FIPS_SENTINEL,
    SPCC_FEE_COMPONENT_NAME,
    STATE_EXCISE_FALLBACK_COMPONENT_NAME,
    TaxBreakdown,
    TaxEngine,
    TaxLineItem,
    UST_FEE_COMPONENT_NAME,
)


# ---------------------------------------------------------------------------
# Fake ES service
# ---------------------------------------------------------------------------


class _FakeESService:
    """Async ES stand-in that returns canned exemption rows.

    Mirrors the same filters a real ES cluster would apply
    (``tenant_id``, ``customer_id``, ``status``, ``expiry_date``, and
    ``product_codes`` match-or-missing) so the test doubles as a
    round-trip of the query shape emitted by ``check_exemption``.
    """

    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self._rows: List[Dict[str, Any]] = list(rows or [])
        self.calls: List[Dict[str, Any]] = []

    async def search_documents(
        self,
        index: str,
        query: Dict[str, Any],
        size: int = 100,
    ) -> Dict[str, Any]:
        self.calls.append({"index": index, "query": query, "size": size})

        tenant_filter = (
            (((query or {}).get("query") or {}).get("bool") or {})
            .get("filter", [])
        )
        tenant_term: Optional[str] = None
        for clause in tenant_filter:
            if "term" in clause and "tenant_id" in clause["term"]:
                tenant_term = clause["term"]["tenant_id"]
                break

        inner_filters = (
            (
                (((query or {}).get("query") or {}).get("bool") or {})
                .get("must", [])
            )
            or []
        )
        inner_bool = inner_filters[0].get("bool", {}) if inner_filters else {}
        inner_filter = inner_bool.get("filter", []) if inner_bool else []

        customer_id: Optional[str] = None
        status: Optional[str] = None
        expiry_gte: Optional[str] = None
        product_code_term: Optional[str] = None
        allow_missing_product_codes = False

        for clause in inner_filter:
            if "term" in clause and "customer_id" in clause["term"]:
                customer_id = clause["term"]["customer_id"]
            elif "term" in clause and "status" in clause["term"]:
                status = clause["term"]["status"]
            elif "range" in clause and "expiry_date" in clause["range"]:
                expiry_gte = clause["range"]["expiry_date"].get("gte")
            elif "bool" in clause and "should" in clause["bool"]:
                for branch in clause["bool"]["should"]:
                    if (
                        "term" in branch
                        and "product_codes" in branch["term"]
                    ):
                        product_code_term = branch["term"]["product_codes"]
                    elif "bool" in branch and "must_not" in branch["bool"]:
                        mn = branch["bool"]["must_not"]
                        for exists_clause in mn:
                            if exists_clause == {
                                "exists": {"field": "product_codes"}
                            }:
                                allow_missing_product_codes = True

        matching: List[Dict[str, Any]] = []
        for row in self._rows:
            if tenant_term is not None and row.get("tenant_id") != tenant_term:
                continue
            if customer_id is not None and row.get("customer_id") != customer_id:
                continue
            if status is not None and row.get("status") != status:
                continue
            if expiry_gte is not None:
                exp = row.get("expiry_date")
                if exp is not None and exp < expiry_gte:
                    continue
            if product_code_term is not None:
                row_codes = row.get("product_codes")
                has_codes = row_codes is not None and len(row_codes) > 0
                if has_codes:
                    if product_code_term not in row_codes:
                        continue
                else:
                    if not allow_missing_product_codes:
                        continue
            matching.append(row)

        return {"hits": {"hits": [{"_source": row} for row in matching]}}


# ---------------------------------------------------------------------------
# Row / breakdown builders
# ---------------------------------------------------------------------------


def _make_exemption_row(
    *,
    exemption_id: str = "exempt-1",
    tenant_id: str = "tenant-1",
    customer_id: str = "cust-1",
    exemption_type: str = "dyed_diesel",
    certificate_number: str = "CERT-001",
    product_codes: Optional[List[str]] = None,
    expiry_date: str = "2030-12-31",
    status: str = "valid",
    letter_suffix: Optional[str] = None,
    issuing_authority: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "exemption_id": exemption_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "account_id": None,
        "exemption_type": exemption_type,
        "certificate_number": certificate_number,
        "letter_suffix": letter_suffix,
        "issuing_authority": issuing_authority,
        "product_codes": product_codes,
        "jurisdiction_fips": None,
        "issued_date": None,
        "expiry_date": expiry_date,
        "status": status,
        "document_ref": None,
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }


def _make_full_breakdown(
    *,
    invoice_id: str = "inv-001",
    net_gallons: float = 1000.0,
) -> TaxBreakdown:
    """Build a pre-exemption breakdown with every component bucket used.

    Mirrors the output of ``compute_tax`` for a diesel delivery where
    the jurisdiction rollup yields a federal excise, a state excise, a
    county excise, a city excise, and the three fee types.
    """
    line_items = [
        TaxLineItem(
            tax_component_name=FEDERAL_EXCISE_COMPONENT_NAME,
            jurisdiction_fips=FEDERAL_FIPS_SENTINEL,
            jurisdiction_level="federal",
            rate_cents_per_gallon=244,
            gallons=net_gallons,
            amount_cents=24_400,
        ),
        TaxLineItem(
            tax_component_name="California_state_excise",
            jurisdiction_fips="06",
            jurisdiction_level="state",
            rate_cents_per_gallon=51,
            gallons=net_gallons,
            amount_cents=5_100,
        ),
        TaxLineItem(
            tax_component_name=COUNTY_EXCISE_COMPONENT_NAME,
            jurisdiction_fips="06037",
            jurisdiction_level="county",
            rate_cents_per_gallon=10,
            gallons=net_gallons,
            amount_cents=1_000,
        ),
        TaxLineItem(
            tax_component_name=CITY_EXCISE_COMPONENT_NAME,
            jurisdiction_fips="0603744",
            jurisdiction_level="city",
            rate_cents_per_gallon=5,
            gallons=net_gallons,
            amount_cents=500,
        ),
        TaxLineItem(
            tax_component_name=UST_FEE_COMPONENT_NAME,
            jurisdiction_fips="06",
            jurisdiction_level="state",
            rate_cents_per_gallon=2,
            gallons=net_gallons,
            amount_cents=200,
        ),
        TaxLineItem(
            tax_component_name=SPCC_FEE_COMPONENT_NAME,
            jurisdiction_fips="06",
            jurisdiction_level="state",
            rate_cents_per_gallon=1,
            gallons=net_gallons,
            amount_cents=100,
        ),
        TaxLineItem(
            tax_component_name=ENVIRONMENTAL_FEE_COMPONENT_NAME,
            jurisdiction_fips="06",
            jurisdiction_level="state",
            rate_cents_per_gallon=1,
            gallons=net_gallons,
            amount_cents=100,
        ),
    ]
    return TaxBreakdown(
        invoice_id=invoice_id,
        federal_cents=24_400,
        state_cents=5_100,
        county_cents=1_000,
        city_cents=500,
        ust_cents=200,
        spcc_cents=100,
        environmental_cents=100,
        line_items=line_items,
    )


# ---------------------------------------------------------------------------
# check_exemption — matching / filtering behavior
# ---------------------------------------------------------------------------


class TestCheckExemption:
    """End-to-end exercises of the exemption lookup."""

    @pytest.mark.asyncio
    async def test_returns_dyed_diesel_for_matching_customer(self):
        """A valid dyed-diesel certificate is returned for the customer."""
        rows = [
            _make_exemption_row(
                exemption_id="exempt-dd",
                customer_id="cust-1",
                exemption_type="dyed_diesel",
                product_codes=["OFF_ROAD_DIESEL"],
                letter_suffix="M",
                issuing_authority="IRS",
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        result = await engine.check_exemption(
            customer_id="cust-1",
            product_code="OFF_ROAD_DIESEL",
            effective_date=date(2026, 6, 1),
        )

        assert result is not None
        assert isinstance(result, TaxExemption)
        assert result.exemption_id == "exempt-dd"
        assert result.exemption_type == "dyed_diesel"

    @pytest.mark.asyncio
    async def test_priority_order_when_multiple_valid_certs(self):
        """dyed_diesel > off_road > farm > 637M > government > resale.

        A customer holding dyed_diesel + off_road + farm + 637M +
        government + resale certificates — all valid for the delivered
        product on the invoice date — resolves to the dyed_diesel
        certificate per :data:`_EXEMPTION_PRIORITY_ORDER`.
        """
        rows = [
            _make_exemption_row(
                exemption_id="exempt-resale",
                exemption_type="resale",
                product_codes=None,
            ),
            _make_exemption_row(
                exemption_id="exempt-gov",
                exemption_type="government",
                product_codes=None,
            ),
            _make_exemption_row(
                exemption_id="exempt-637M",
                exemption_type="637M",
                product_codes=None,
                letter_suffix="M",
                issuing_authority="IRS",
            ),
            _make_exemption_row(
                exemption_id="exempt-farm",
                exemption_type="farm",
                product_codes=None,
            ),
            _make_exemption_row(
                exemption_id="exempt-off-road",
                exemption_type="off_road",
                product_codes=["OFF_ROAD_DIESEL"],
            ),
            _make_exemption_row(
                exemption_id="exempt-dyed",
                exemption_type="dyed_diesel",
                product_codes=["OFF_ROAD_DIESEL"],
                letter_suffix="M",
                issuing_authority="IRS",
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        result = await engine.check_exemption(
            customer_id="cust-1",
            product_code="OFF_ROAD_DIESEL",
            effective_date=date(2026, 6, 1),
        )

        assert result is not None
        assert result.exemption_id == "exempt-dyed"
        assert result.exemption_type == "dyed_diesel"

    @pytest.mark.asyncio
    async def test_priority_falls_through_when_higher_priority_missing(self):
        """With no dyed/off-road certificates present, farm wins over 637M."""
        rows = [
            _make_exemption_row(
                exemption_id="exempt-637M",
                exemption_type="637M",
                product_codes=None,
                letter_suffix="M",
                issuing_authority="IRS",
            ),
            _make_exemption_row(
                exemption_id="exempt-farm",
                exemption_type="farm",
                product_codes=None,
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        result = await engine.check_exemption(
            customer_id="cust-1",
            product_code="DIESEL_2",
            effective_date=date(2026, 6, 1),
        )

        assert result is not None
        assert result.exemption_id == "exempt-farm"

    @pytest.mark.asyncio
    async def test_expired_cert_not_returned(self):
        """A certificate whose expiry_date is in the past is not honored."""
        rows = [
            _make_exemption_row(
                exemption_id="exempt-old",
                exemption_type="dyed_diesel",
                product_codes=["OFF_ROAD_DIESEL"],
                expiry_date="2025-12-31",
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        result = await engine.check_exemption(
            customer_id="cust-1",
            product_code="OFF_ROAD_DIESEL",
            effective_date=date(2026, 6, 1),
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_revoked_status_not_returned(self):
        """A revoked certificate is not honored even if the date is valid."""
        rows = [
            _make_exemption_row(
                exemption_id="exempt-revoked",
                exemption_type="dyed_diesel",
                product_codes=["OFF_ROAD_DIESEL"],
                status="revoked",
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        result = await engine.check_exemption(
            customer_id="cust-1",
            product_code="OFF_ROAD_DIESEL",
            effective_date=date(2026, 6, 1),
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_no_cert_returns_none(self):
        """When no certificate exists, check_exemption returns None."""
        es = _FakeESService(rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        result = await engine.check_exemption(
            customer_id="cust-1",
            product_code="DIESEL_2",
            effective_date=date(2026, 6, 1),
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_tenant_isolation(self):
        """Tenant filter excludes certificates belonging to other tenants."""
        rows = [
            _make_exemption_row(
                exemption_id="exempt-other-tenant",
                tenant_id="tenant-2",
                exemption_type="dyed_diesel",
                product_codes=["OFF_ROAD_DIESEL"],
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        result = await engine.check_exemption(
            customer_id="cust-1",
            product_code="OFF_ROAD_DIESEL",
            effective_date=date(2026, 6, 1),
        )

        assert result is None

        # Confirm the emitted query carries the tenant filter.
        assert len(es.calls) == 1
        tenant_terms = [
            clause["term"]["tenant_id"]
            for clause in es.calls[0]["query"]["query"]["bool"]["filter"]
            if "term" in clause and "tenant_id" in clause["term"]
        ]
        assert tenant_terms == ["tenant-1"]

    @pytest.mark.asyncio
    async def test_empty_product_codes_matches_all_products(self):
        """A certificate with no product_codes applies to every product."""
        rows = [
            _make_exemption_row(
                exemption_id="exempt-blanket-farm",
                exemption_type="farm",
                product_codes=None,  # blanket — applies to all products
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        # The blanket certificate matches any canonical product code.
        result1 = await engine.check_exemption(
            customer_id="cust-1",
            product_code="DIESEL_2",
            effective_date=date(2026, 6, 1),
        )
        result2 = await engine.check_exemption(
            customer_id="cust-1",
            product_code="GASOLINE_REG",
            effective_date=date(2026, 6, 1),
        )

        assert result1 is not None
        assert result1.exemption_id == "exempt-blanket-farm"
        assert result2 is not None
        assert result2.exemption_id == "exempt-blanket-farm"

    @pytest.mark.asyncio
    async def test_product_code_mismatch_is_skipped(self):
        """A product-scoped certificate does not match a different product."""
        rows = [
            _make_exemption_row(
                exemption_id="exempt-diesel-only",
                exemption_type="dyed_diesel",
                product_codes=["OFF_ROAD_DIESEL"],
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        result = await engine.check_exemption(
            customer_id="cust-1",
            product_code="GASOLINE_REG",
            effective_date=date(2026, 6, 1),
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_query_targets_tax_exemptions_index(self):
        es = _FakeESService(rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        await engine.check_exemption(
            customer_id="cust-1",
            product_code="DIESEL_2",
            effective_date=date(2026, 6, 1),
        )

        assert len(es.calls) == 1
        assert es.calls[0]["index"] == TAX_EXEMPTIONS_INDEX


# ---------------------------------------------------------------------------
# apply_exemption — breakdown adjustment behavior
# ---------------------------------------------------------------------------


class TestApplyExemption:
    """Pure-function exercises over the breakdown adjustment helper."""

    def _make_engine(self) -> TaxEngine:
        class _ES:
            pass

        return TaxEngine(es_service=_ES(), tenant_id="tenant-1")

    def test_dyed_diesel_zeros_federal_and_state(self):
        """Dyed-diesel exemption zeros federal + state excise."""
        engine = self._make_engine()
        breakdown = _make_full_breakdown()
        exemption = TaxExemption(
            exemption_id="exempt-dd",
            tenant_id="tenant-1",
            customer_id="cust-1",
            exemption_type="dyed_diesel",
            certificate_number="DD-001",
            letter_suffix="M",
            issuing_authority="IRS",
            expiry_date=date(2030, 12, 31),
        )

        result = engine.apply_exemption(breakdown, exemption)

        # Federal and state excise are zero; other buckets are untouched.
        assert result.federal_cents == 0
        assert result.state_cents == 0
        assert result.county_cents == 1_000
        assert result.city_cents == 500
        assert result.ust_cents == 200
        assert result.spcc_cents == 100
        assert result.environmental_cents == 100

    def test_dyed_diesel_removes_federal_and_state_line_items(self):
        """Matching line items are removed from the breakdown."""
        engine = self._make_engine()
        breakdown = _make_full_breakdown()
        exemption = TaxExemption(
            exemption_id="exempt-dd",
            tenant_id="tenant-1",
            customer_id="cust-1",
            exemption_type="dyed_diesel",
            certificate_number="DD-001",
            expiry_date=date(2030, 12, 31),
        )

        result = engine.apply_exemption(breakdown, exemption)

        component_names = [item.tax_component_name for item in result.line_items]
        assert FEDERAL_EXCISE_COMPONENT_NAME not in component_names
        assert "California_state_excise" not in component_names
        # County / city excise and UST / SPCC / environmental fees stay.
        assert COUNTY_EXCISE_COMPONENT_NAME in component_names
        assert CITY_EXCISE_COMPONENT_NAME in component_names
        assert UST_FEE_COMPONENT_NAME in component_names
        assert SPCC_FEE_COMPONENT_NAME in component_names
        assert ENVIRONMENTAL_FEE_COMPONENT_NAME in component_names

    def test_off_road_matches_dyed_diesel_behavior(self):
        """off_road follows the same road-use exclusion as dyed_diesel."""
        engine = self._make_engine()
        breakdown = _make_full_breakdown()
        exemption = TaxExemption(
            exemption_id="exempt-offroad",
            tenant_id="tenant-1",
            customer_id="cust-1",
            exemption_type="off_road",
            certificate_number="OR-001",
            expiry_date=date(2030, 12, 31),
        )

        result = engine.apply_exemption(breakdown, exemption)

        assert result.federal_cents == 0
        assert result.state_cents == 0
        assert result.county_cents == 1_000

    def test_637M_matches_dyed_diesel_behavior(self):
        """637M follows the same road-use exclusion as dyed_diesel."""
        engine = self._make_engine()
        breakdown = _make_full_breakdown()
        exemption = TaxExemption(
            exemption_id="exempt-637m",
            tenant_id="tenant-1",
            customer_id="cust-1",
            exemption_type="637M",
            certificate_number="637-001",
            letter_suffix="M",
            issuing_authority="IRS",
            expiry_date=date(2030, 12, 31),
        )

        result = engine.apply_exemption(breakdown, exemption)

        assert result.federal_cents == 0
        assert result.state_cents == 0

    def test_farm_does_not_zero_amounts(self):
        """Farm exemption is flag-only per Task 3.7 design note (Req 1.8).

        The caller is expected to resolve the reduced rate via a
        farm-specific row in the jurisdiction table, so apply_exemption
        leaves amounts unchanged and only records the exemption_id.
        """
        engine = self._make_engine()
        breakdown = _make_full_breakdown()
        exemption = TaxExemption(
            exemption_id="exempt-farm",
            tenant_id="tenant-1",
            customer_id="cust-1",
            exemption_type="farm",
            certificate_number="FARM-001",
            expiry_date=date(2030, 12, 31),
        )

        result = engine.apply_exemption(breakdown, exemption)

        # Every bucket is preserved.
        assert result.federal_cents == breakdown.federal_cents
        assert result.state_cents == breakdown.state_cents
        assert result.county_cents == breakdown.county_cents
        assert result.city_cents == breakdown.city_cents
        assert result.ust_cents == breakdown.ust_cents
        assert result.spcc_cents == breakdown.spcc_cents
        assert result.environmental_cents == breakdown.environmental_cents
        # Line items unchanged.
        assert len(result.line_items) == len(breakdown.line_items)
        # Provenance recorded.
        assert "exempt-farm" in result.exemptions_applied

    def test_government_zeros_state_county_city_keeps_federal(self):
        """Government blanket zeros state+county+city excise, keeps federal."""
        engine = self._make_engine()
        breakdown = _make_full_breakdown()
        exemption = TaxExemption(
            exemption_id="exempt-gov",
            tenant_id="tenant-1",
            customer_id="cust-1",
            exemption_type="government",
            certificate_number="GOV-001",
            expiry_date=date(2030, 12, 31),
        )

        result = engine.apply_exemption(breakdown, exemption)

        assert result.federal_cents == breakdown.federal_cents
        assert result.state_cents == 0
        assert result.county_cents == 0
        assert result.city_cents == 0
        # UST / SPCC / environmental fees are retained.
        assert result.ust_cents == breakdown.ust_cents
        assert result.spcc_cents == breakdown.spcc_cents
        assert result.environmental_cents == breakdown.environmental_cents

    def test_resale_matches_government_behavior(self):
        """resale follows the same jurisdictional blanket rules as government."""
        engine = self._make_engine()
        breakdown = _make_full_breakdown()
        exemption = TaxExemption(
            exemption_id="exempt-resale",
            tenant_id="tenant-1",
            customer_id="cust-1",
            exemption_type="resale",
            certificate_number="RS-001",
            expiry_date=date(2030, 12, 31),
        )

        result = engine.apply_exemption(breakdown, exemption)

        assert result.federal_cents == breakdown.federal_cents
        assert result.state_cents == 0
        assert result.county_cents == 0
        assert result.city_cents == 0

    def test_appends_exemption_id_to_exemptions_applied(self):
        """apply_exemption always appends the exemption_id for provenance."""
        engine = self._make_engine()
        breakdown = _make_full_breakdown()
        exemption = TaxExemption(
            exemption_id="exempt-dd-42",
            tenant_id="tenant-1",
            customer_id="cust-1",
            exemption_type="dyed_diesel",
            certificate_number="DD-042",
            expiry_date=date(2030, 12, 31),
        )

        result = engine.apply_exemption(breakdown, exemption)

        assert "exempt-dd-42" in result.exemptions_applied
        # And preserves any pre-existing exemptions on the breakdown.
        assert len(result.exemptions_applied) == len(
            breakdown.exemptions_applied
        ) + 1

    def test_does_not_mutate_input_breakdown(self):
        """apply_exemption returns a new instance; the input is untouched."""
        engine = self._make_engine()
        breakdown = _make_full_breakdown()
        original_federal = breakdown.federal_cents
        original_line_items = list(breakdown.line_items)
        original_exemptions = list(breakdown.exemptions_applied)

        exemption = TaxExemption(
            exemption_id="exempt-dd",
            tenant_id="tenant-1",
            customer_id="cust-1",
            exemption_type="dyed_diesel",
            certificate_number="DD-001",
            expiry_date=date(2030, 12, 31),
        )

        engine.apply_exemption(breakdown, exemption)

        assert breakdown.federal_cents == original_federal
        assert breakdown.line_items == original_line_items
        assert breakdown.exemptions_applied == original_exemptions
