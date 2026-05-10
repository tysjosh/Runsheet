"""Integration-path unit tests for :meth:`TaxEngine.compute_tax` (Task 3.11).

These complement the existing focused test modules
(``test_tax_engine_skeleton.py``, ``test_tax_engine_jurisdiction_lookup.py``,
``test_tax_engine_federal_excise.py``, ``test_tax_engine_state_local_taxes.py``,
``test_tax_engine_check_exemption.py``, ``test_tax_engine_compute_tax.py``)
by exercising each exemption path, the 2/5/7-digit FIPS resolution
chain, and line-item count correctness **end-to-end through
``compute_tax``**.

Coverage grouped by concern:

* ``TestExemptionPathsEndToEnd`` — one class per exemption type
  (``dyed_diesel``, ``off_road``, ``637M``, ``farm``, ``government``,
  ``resale``). Each validates bucket-level zeroing, line-item removal,
  and ``exemptions_applied`` provenance against a fully-populated
  federal + state + county + city + UST + SPCC + environmental rollup.
* ``TestMultiLevelFipsResolution`` — verifies that a 2-digit state FIPS
  returns federal + state only; a 5-digit county FIPS pulls the county
  level in; a 7-digit city FIPS pulls every level; and a mixed rollup
  with gaps computes cents only from rows that exist.
* ``TestMissingJurisdictionPaths`` — the Req 1.9 gate fires for missing
  state rows across different canonical product codes, but is
  short-circuited by road-use / jurisdictional exemptions.
* ``TestEdgeCases`` — zero gallons, multiple overlapping exemption
  certificates (priority wins), historical-rate lookups with expired
  ``expiry_date``, and future-dated rates that are not yet effective.
* ``TestLineItemCountCorrectness`` — the breakdown carries exactly the
  expected number of line items (7 for a full rollup, 5 after a
  dyed-diesel exemption removes federal + state).

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.7, 1.8, 1.9, 1.10
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

from compliance.services.compliance_es_mappings import (
    TAX_EXEMPTIONS_INDEX,
    TAX_JURISDICTIONS_INDEX,
)
from compliance.services.tax_engine import (
    CITY_EXCISE_COMPONENT_NAME,
    COUNTY_EXCISE_COMPONENT_NAME,
    ENVIRONMENTAL_FEE_COMPONENT_NAME,
    FEDERAL_EXCISE_COMPONENT_NAME,
    FEDERAL_FIPS_SENTINEL,
    SPCC_FEE_COMPONENT_NAME,
    TaxBreakdown,
    TaxEngine,
    TaxJurisdictionNotFoundError,
    UST_FEE_COMPONENT_NAME,
)


# ---------------------------------------------------------------------------
# Fake ES service — mirrors the _FakeESService pattern from
# test_tax_engine_compute_tax.py so queries against both the
# ``tax_jurisdictions`` and ``tax_exemptions`` indices round-trip through
# the same filter logic a real ES cluster would apply.
# ---------------------------------------------------------------------------


class _FakeESService:
    """Async ES stand-in that routes on index name and applies the same
    tenant / customer / effective-date / product-code filters a live ES
    cluster would apply.
    """

    def __init__(
        self,
        *,
        jurisdiction_rows: Optional[List[Dict[str, Any]]] = None,
        exemption_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._jurisdictions: List[Dict[str, Any]] = list(
            jurisdiction_rows or []
        )
        self._exemptions: List[Dict[str, Any]] = list(exemption_rows or [])
        self.calls: List[Dict[str, Any]] = []

    async def search_documents(
        self,
        index: str,
        query: Dict[str, Any],
        size: int = 100,
    ) -> Dict[str, Any]:
        self.calls.append({"index": index, "query": query, "size": size})

        if index == TAX_JURISDICTIONS_INDEX:
            return await self._search_jurisdictions(query)
        if index == TAX_EXEMPTIONS_INDEX:
            return await self._search_exemptions(query)
        return {"hits": {"hits": []}}

    async def _search_jurisdictions(
        self, query: Dict[str, Any]
    ) -> Dict[str, Any]:
        tenant_term = self._extract_tenant(query)
        inner_filter = self._inner_filter_clauses(query)

        candidate_codes: List[str] = []
        effective_lte: Optional[str] = None
        for clause in inner_filter:
            if "terms" in clause and "fips_code" in clause["terms"]:
                candidate_codes = list(clause["terms"]["fips_code"])
            if "range" in clause and "effective_date" in clause["range"]:
                effective_lte = clause["range"]["effective_date"].get("lte")

        matching: List[Dict[str, Any]] = []
        for row in self._jurisdictions:
            if tenant_term is not None and row.get("tenant_id") != tenant_term:
                continue
            if candidate_codes and row.get("fips_code") not in candidate_codes:
                continue
            if effective_lte is not None:
                eff = row.get("effective_date")
                if eff is not None and eff > effective_lte:
                    continue
                exp = row.get("expiry_date")
                if exp is not None and exp < effective_lte:
                    continue
            matching.append(row)

        return {"hits": {"hits": [{"_source": row} for row in matching]}}

    async def _search_exemptions(
        self, query: Dict[str, Any]
    ) -> Dict[str, Any]:
        tenant_term = self._extract_tenant(query)
        inner_filter = self._inner_filter_clauses(query)

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
                        for exists_clause in branch["bool"]["must_not"]:
                            if exists_clause == {
                                "exists": {"field": "product_codes"}
                            }:
                                allow_missing_product_codes = True

        matching: List[Dict[str, Any]] = []
        for row in self._exemptions:
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

    @staticmethod
    def _extract_tenant(query: Dict[str, Any]) -> Optional[str]:
        outer_filter = (
            (((query or {}).get("query") or {}).get("bool") or {})
            .get("filter", [])
        )
        for clause in outer_filter:
            if "term" in clause and "tenant_id" in clause["term"]:
                return clause["term"]["tenant_id"]
        return None

    @staticmethod
    def _inner_filter_clauses(query: Dict[str, Any]) -> List[Dict[str, Any]]:
        musts = (
            (((query or {}).get("query") or {}).get("bool") or {})
            .get("must", [])
        )
        if not musts:
            return []
        inner_bool = (
            musts[0].get("bool", {}) if isinstance(musts[0], dict) else {}
        )
        return list(inner_bool.get("filter", []))


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _make_jurisdiction_row(
    *,
    tenant_id: str = "tenant-1",
    fips_code: str = "00",
    jurisdiction_level: str = "federal",
    jurisdiction_name: Optional[str] = None,
    tax_type: str = "excise",
    product_codes: Optional[List[str]] = None,
    rate_cents_per_gallon: int = 184,
    effective_date: str = "2024-01-01",
    expiry_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a ``tax_jurisdictions`` _source row with sensible defaults."""
    return {
        "jurisdiction_id": f"juris_{uuid4()}",
        "tenant_id": tenant_id,
        "fips_code": fips_code,
        "jurisdiction_level": jurisdiction_level,
        "jurisdiction_name": jurisdiction_name,
        "tax_type": tax_type,
        "product_codes": list(product_codes or ["DIESEL_2"]),
        "rate_cents_per_gallon": rate_cents_per_gallon,
        "effective_date": effective_date,
        "expiry_date": expiry_date,
        "source": None,
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }


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
    """Build a ``tax_exemptions`` _source row with sensible defaults."""
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


def _full_diesel_rollup(product_code: str = "DIESEL_2") -> List[Dict[str, Any]]:
    """Build a 7-row rollup covering every tax component bucket.

    * Federal excise (24.4¢/gal diesel statutory)
    * CA state excise (40.0¢/gal)
    * Los Angeles county excise (5.0¢/gal)
    * Los Angeles city excise (3.0¢/gal)
    * CA state UST fee (2.0¢/gal)
    * LA county SPCC fee (1.0¢/gal)
    * CA state environmental fee (0.5¢/gal)
    """
    return [
        _make_jurisdiction_row(
            fips_code=FEDERAL_FIPS_SENTINEL,
            jurisdiction_level="federal",
            jurisdiction_name="United States",
            tax_type="excise",
            product_codes=[product_code],
            rate_cents_per_gallon=244,
        ),
        _make_jurisdiction_row(
            fips_code="06",
            jurisdiction_level="state",
            jurisdiction_name="California",
            tax_type="excise",
            product_codes=[product_code],
            rate_cents_per_gallon=400,
        ),
        _make_jurisdiction_row(
            fips_code="06037",
            jurisdiction_level="county",
            jurisdiction_name="Los Angeles County",
            tax_type="excise",
            product_codes=[product_code],
            rate_cents_per_gallon=50,
        ),
        _make_jurisdiction_row(
            fips_code="0603744",
            jurisdiction_level="city",
            jurisdiction_name="Los Angeles",
            tax_type="excise",
            product_codes=[product_code],
            rate_cents_per_gallon=30,
        ),
        _make_jurisdiction_row(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="ust",
            product_codes=[product_code],
            rate_cents_per_gallon=20,
        ),
        _make_jurisdiction_row(
            fips_code="06037",
            jurisdiction_level="county",
            tax_type="spcc",
            product_codes=[product_code],
            rate_cents_per_gallon=10,
        ),
        _make_jurisdiction_row(
            fips_code="06",
            jurisdiction_level="state",
            tax_type="environmental",
            product_codes=[product_code],
            rate_cents_per_gallon=5,
        ),
    ]


# ---------------------------------------------------------------------------
# Per-exemption end-to-end paths
# ---------------------------------------------------------------------------


class TestExemptionPathsEndToEnd:
    """Each exemption_type is exercised through compute_tax with a
    full federal+state+county+city+UST+SPCC+environmental rollup.

    Assertions cover bucket-level zeroing, line-item removal, and the
    ``exemptions_applied`` provenance list so downstream invoice
    rendering and IRS audit trails stay in sync with the breakdown.

    Validates: Requirements 1.7, 1.8, 1.10
    """

    # ---- dyed_diesel ------------------------------------------------

    @pytest.mark.asyncio
    async def test_dyed_diesel_exemption_zeros_federal_and_state_retains_county_city_ust_spcc_env(
        self,
    ):
        """dyed_diesel: federal + state zeroed; county/city + all fees retained."""
        rows = _full_diesel_rollup(product_code="OFF_ROAD_DIESEL")
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-dd-1",
                customer_id="cust-1",
                exemption_type="dyed_diesel",
                product_codes=["OFF_ROAD_DIESEL"],
                letter_suffix="M",
                issuing_authority="IRS",
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=rows, exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="OFF_ROAD_DIESEL",
            net_gallons=1000.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        # Federal + state excise zeroed by the road-use exclusion.
        assert breakdown.federal_cents == 0
        assert breakdown.state_cents == 0
        # County, city, UST, SPCC, environmental retained.
        assert breakdown.county_cents == 5_000
        assert breakdown.city_cents == 3_000
        assert breakdown.ust_cents == 2_000
        assert breakdown.spcc_cents == 1_000
        assert breakdown.environmental_cents == 500
        # Provenance captured.
        assert breakdown.exemptions_applied == ["exempt-dd-1"]

        components = {li.tax_component_name for li in breakdown.line_items}
        assert FEDERAL_EXCISE_COMPONENT_NAME not in components
        assert "California_state_excise" not in components
        assert COUNTY_EXCISE_COMPONENT_NAME in components
        assert CITY_EXCISE_COMPONENT_NAME in components
        assert UST_FEE_COMPONENT_NAME in components
        assert SPCC_FEE_COMPONENT_NAME in components
        assert ENVIRONMENTAL_FEE_COMPONENT_NAME in components

    # ---- off_road ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_off_road_exemption_mirrors_dyed_diesel_behavior(self):
        """off_road: federal + state zeroed, same as dyed_diesel."""
        rows = _full_diesel_rollup(product_code="OFF_ROAD_DIESEL")
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-or-1",
                customer_id="cust-1",
                exemption_type="off_road",
                product_codes=["OFF_ROAD_DIESEL"],
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=rows, exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="OFF_ROAD_DIESEL",
            net_gallons=1000.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        assert breakdown.federal_cents == 0
        assert breakdown.state_cents == 0
        assert breakdown.county_cents == 5_000
        assert breakdown.city_cents == 3_000
        assert breakdown.ust_cents == 2_000
        assert breakdown.spcc_cents == 1_000
        assert breakdown.environmental_cents == 500
        assert breakdown.exemptions_applied == ["exempt-or-1"]

    # ---- 637M -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_637m_exemption_mirrors_dyed_diesel_behavior(self):
        """637M: federal + state zeroed, same as dyed_diesel."""
        rows = _full_diesel_rollup(product_code="DIESEL_2")
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-637m-1",
                customer_id="cust-1",
                exemption_type="637M",
                product_codes=None,  # blanket 637M registration
                letter_suffix="M",
                issuing_authority="IRS",
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=rows, exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        assert breakdown.federal_cents == 0
        assert breakdown.state_cents == 0
        assert breakdown.county_cents == 5_000
        assert breakdown.city_cents == 3_000
        assert breakdown.ust_cents == 2_000
        assert breakdown.spcc_cents == 1_000
        assert breakdown.environmental_cents == 500
        assert breakdown.exemptions_applied == ["exempt-637m-1"]

    # ---- farm -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_farm_exemption_does_not_adjust_amounts_but_records_provenance(
        self,
    ):
        """farm is flag-only per Req 1.8 / Task 3.7 design note.

        Every component bucket is preserved and the exemption_id is
        appended to ``exemptions_applied``. Rate reduction is expected
        via a farm-specific jurisdiction row (see companion test below).
        """
        rows = _full_diesel_rollup(product_code="DIESEL_2")
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-farm-1",
                customer_id="cust-1",
                exemption_type="farm",
                product_codes=None,
                issuing_authority="CA_CDTFA",
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=rows, exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        # Every bucket preserved at the standard rate.
        assert breakdown.federal_cents == 24_400
        assert breakdown.state_cents == 40_000
        assert breakdown.county_cents == 5_000
        assert breakdown.city_cents == 3_000
        assert breakdown.ust_cents == 2_000
        assert breakdown.spcc_cents == 1_000
        assert breakdown.environmental_cents == 500
        # Line items untouched.
        assert len(breakdown.line_items) == 7
        # Provenance still captured for IRS audit (Req 6.7).
        assert breakdown.exemptions_applied == ["exempt-farm-1"]

    @pytest.mark.asyncio
    async def test_farm_specific_jurisdiction_row_applies_reduced_rate(self):
        """Farm-specific rate rows attach via ``product_codes`` scoping.

        The Task 3.7 design note documents that farm rate reductions
        are resolved upstream through a farm-scoped ``product_codes``
        row rather than through ``apply_exemption``. Here we use
        ``OFF_ROAD_DIESEL`` as the farm-canonical product and confirm
        that only the row scoped to the delivered product matches —
        the ``DIESEL_2``-scoped standard row contributes nothing.

        Validates: Requirement 1.8
        """
        rows: List[Dict[str, Any]] = [
            # Statutory federal row for OFF_ROAD_DIESEL so the state
            # gate has a matching canonical product.
            _make_jurisdiction_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                product_codes=["OFF_ROAD_DIESEL"],
                rate_cents_per_gallon=244,
            ),
            # Standard state rate — scoped to DIESEL_2 only.
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                jurisdiction_name="California",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=400,  # 40¢/gal standard
            ),
            # Farm-specific state rate — scoped to OFF_ROAD_DIESEL only.
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                jurisdiction_name="California",
                tax_type="excise",
                product_codes=["OFF_ROAD_DIESEL"],
                rate_cents_per_gallon=50,  # 5¢/gal reduced
            ),
        ]
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-farm-2",
                customer_id="cust-1",
                exemption_type="farm",
                product_codes=None,
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=rows, exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="OFF_ROAD_DIESEL",
            net_gallons=1000.0,
            destination_fips="06",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        # Only the OFF_ROAD_DIESEL-scoped state row applied; DIESEL_2 row skipped.
        assert breakdown.state_cents == 5_000
        # Farm provenance still recorded.
        assert breakdown.exemptions_applied == ["exempt-farm-2"]

    # ---- government -------------------------------------------------

    @pytest.mark.asyncio
    async def test_government_exemption_zeros_state_county_city_keeps_federal_and_fees(
        self,
    ):
        """government: state/county/city zeroed; federal + fees retained."""
        rows = _full_diesel_rollup(product_code="DIESEL_2")
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-gov-1",
                customer_id="cust-1",
                exemption_type="government",
                product_codes=None,
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=rows, exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        # Federal retained, jurisdictional excise zeroed.
        assert breakdown.federal_cents == 24_400
        assert breakdown.state_cents == 0
        assert breakdown.county_cents == 0
        assert breakdown.city_cents == 0
        # Fees retained — they are separate from excise.
        assert breakdown.ust_cents == 2_000
        assert breakdown.spcc_cents == 1_000
        assert breakdown.environmental_cents == 500
        assert breakdown.exemptions_applied == ["exempt-gov-1"]

        components = {li.tax_component_name for li in breakdown.line_items}
        assert FEDERAL_EXCISE_COMPONENT_NAME in components
        assert "California_state_excise" not in components
        assert COUNTY_EXCISE_COMPONENT_NAME not in components
        assert CITY_EXCISE_COMPONENT_NAME not in components
        assert UST_FEE_COMPONENT_NAME in components
        assert SPCC_FEE_COMPONENT_NAME in components
        assert ENVIRONMENTAL_FEE_COMPONENT_NAME in components

    # ---- resale -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_resale_exemption_mirrors_government_behavior(self):
        """resale: state/county/city zeroed, same as government."""
        rows = _full_diesel_rollup(product_code="DIESEL_2")
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-resale-1",
                customer_id="cust-1",
                exemption_type="resale",
                product_codes=None,
                issuing_authority="CA_CDTFA",
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=rows, exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        assert breakdown.federal_cents == 24_400
        assert breakdown.state_cents == 0
        assert breakdown.county_cents == 0
        assert breakdown.city_cents == 0
        assert breakdown.ust_cents == 2_000
        assert breakdown.spcc_cents == 1_000
        assert breakdown.environmental_cents == 500
        assert breakdown.exemptions_applied == ["exempt-resale-1"]


# ---------------------------------------------------------------------------
# Multi-level FIPS resolution end-to-end
# ---------------------------------------------------------------------------


class TestMultiLevelFipsResolution:
    """Verify compute_tax rolls the right rows in for each FIPS depth.

    The jurisdiction lookup tests (Task 3.4) cover the query shape; the
    tests here pin the end-to-end translation from destination FIPS →
    component-populated :class:`TaxBreakdown`.

    Validates: Requirements 1.2, 1.3
    """

    @pytest.mark.asyncio
    async def test_2_digit_state_fips_populates_federal_and_state_only(self):
        """State-only delivery: federal + state populate; county / city stay 0."""
        rows = _full_diesel_rollup(product_code="DIESEL_2")
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="06",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        # 2-digit FIPS → federal + state only.
        assert breakdown.federal_cents == 24_400
        assert breakdown.state_cents == 40_000
        # County and city rows exist in the dataset but are outside the
        # state-only rollup, so they contribute nothing.
        assert breakdown.county_cents == 0
        assert breakdown.city_cents == 0
        # State-level fees still apply.
        assert breakdown.ust_cents == 2_000
        # County-level SPCC is scoped to 06037 → excluded by 2-digit
        # rollup.
        assert breakdown.spcc_cents == 0
        assert breakdown.environmental_cents == 500

    @pytest.mark.asyncio
    async def test_5_digit_county_fips_populates_federal_state_county(self):
        """County-scoped delivery: federal + state + county populate; city 0."""
        rows = _full_diesel_rollup(product_code="DIESEL_2")
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="06037",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        assert breakdown.federal_cents == 24_400
        assert breakdown.state_cents == 40_000
        assert breakdown.county_cents == 5_000
        # City row scoped to 0603744 is outside the county rollup.
        assert breakdown.city_cents == 0
        assert breakdown.ust_cents == 2_000
        assert breakdown.spcc_cents == 1_000
        assert breakdown.environmental_cents == 500

    @pytest.mark.asyncio
    async def test_7_digit_city_fips_populates_every_level(self):
        """City-scoped delivery: federal + state + county + city all populate."""
        rows = _full_diesel_rollup(product_code="DIESEL_2")
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        assert breakdown.federal_cents == 24_400
        assert breakdown.state_cents == 40_000
        assert breakdown.county_cents == 5_000
        assert breakdown.city_cents == 3_000
        assert breakdown.ust_cents == 2_000
        assert breakdown.spcc_cents == 1_000
        assert breakdown.environmental_cents == 500
        # Full total: 24_400 + 40_000 + 5_000 + 3_000 + 2_000 + 1_000 + 500
        assert breakdown.total_tax_cents == 75_900

    @pytest.mark.asyncio
    async def test_mixed_rollup_with_missing_levels_reflects_only_existing_rows(
        self,
    ):
        """When county / city rows are absent, city-scoped delivery only
        picks up the levels that exist.

        Req 1.3 scopes county / city surcharges to where they apply —
        a delivery in a jurisdiction without a city fuel tax should
        produce ``city_cents == 0`` rather than synthesising one.
        """
        # Federal + state + county excise ONLY; no city row present.
        rows = [
            _make_jurisdiction_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=244,
            ),
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                jurisdiction_name="California",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=400,
            ),
            _make_jurisdiction_row(
                fips_code="06037",
                jurisdiction_level="county",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=50,
            ),
            # No city row for 0603744, no UST / SPCC / environmental.
        ]
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        # Only the levels with rows contribute.
        assert breakdown.federal_cents == 24_400
        assert breakdown.state_cents == 40_000
        assert breakdown.county_cents == 5_000
        # Missing city / fee rows → zero.
        assert breakdown.city_cents == 0
        assert breakdown.ust_cents == 0
        assert breakdown.spcc_cents == 0
        assert breakdown.environmental_cents == 0
        assert breakdown.total_tax_cents == 69_400


# ---------------------------------------------------------------------------
# Missing jurisdiction gate — Req 1.9 across resolution paths
# ---------------------------------------------------------------------------


class TestMissingJurisdictionPaths:
    """Req 1.9: ``tax.jurisdiction_not_found`` fires for missing state rows.

    Complements ``TestMissingStateRow`` in ``test_tax_engine_compute_tax``
    by exercising the gate across FIPS depths and multiple canonical
    product codes.
    """

    @pytest.mark.asyncio
    async def test_missing_state_row_raises_for_gasoline_delivery(self):
        """A gasoline delivery with no state excise row triggers Req 1.9."""
        rows = [
            _make_jurisdiction_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                product_codes=["GASOLINE_REG"],
                rate_cents_per_gallon=184,
            ),
        ]
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        with pytest.raises(TaxJurisdictionNotFoundError) as excinfo:
            await engine.compute_tax(
                product_code="GASOLINE_REG",
                net_gallons=500.0,
                destination_fips="48",
                customer_id="cust-1",
                effective_date=date(2026, 6, 1),
            )

        exc = excinfo.value
        assert exc.fips_code == "48"
        assert exc.jurisdiction_level == "state"
        assert exc.product_code == "GASOLINE_REG"

    @pytest.mark.asyncio
    async def test_missing_state_row_raises_at_7_digit_resolution(self):
        """The gate fires the same way for 7-digit deliveries."""
        # Federal + county + city present but no state row.
        rows = [
            _make_jurisdiction_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=244,
            ),
            _make_jurisdiction_row(
                fips_code="06037",
                jurisdiction_level="county",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=50,
            ),
            _make_jurisdiction_row(
                fips_code="0603744",
                jurisdiction_level="city",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=30,
            ),
        ]
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        with pytest.raises(TaxJurisdictionNotFoundError) as excinfo:
            await engine.compute_tax(
                product_code="DIESEL_2",
                net_gallons=1000.0,
                destination_fips="0603744",
                customer_id="cust-1",
                effective_date=date(2026, 6, 1),
            )

        # The gate reports the 2-digit state prefix as the missing row.
        assert excinfo.value.fips_code == "06"

    @pytest.mark.asyncio
    async def test_government_exemption_short_circuits_missing_state_gate(self):
        """A government blanket exemption zeros state anyway, so the gate
        does not fire even when no state row exists.
        """
        rows = [
            _make_jurisdiction_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=244,
            ),
        ]
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-gov-2",
                customer_id="cust-1",
                exemption_type="government",
                product_codes=None,
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=rows, exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        # Does NOT raise.
        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="06",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        assert breakdown.federal_cents == 24_400
        assert breakdown.state_cents == 0
        assert "exempt-gov-2" in breakdown.exemptions_applied


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases — zero gallons, overlapping certificates, historical
    rates, and future-dated rates.

    Validates: Requirements 1.5, 1.7, 1.10
    """

    @pytest.mark.asyncio
    async def test_zero_gallons_produces_zero_components_but_records_exemption(
        self,
    ):
        """Zero-gallon invoice: every amount bucket is 0, exemptions still recorded.

        A 0-gallon meter ticket is a legitimate edge case (e.g. a
        customer arrived but pumped nothing). The breakdown must still
        honor exemption provenance so the invoice audit trail reflects
        the customer's status.
        """
        rows = _full_diesel_rollup(product_code="OFF_ROAD_DIESEL")
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-zero",
                customer_id="cust-1",
                exemption_type="dyed_diesel",
                product_codes=["OFF_ROAD_DIESEL"],
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=rows, exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="OFF_ROAD_DIESEL",
            net_gallons=0.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        assert breakdown.federal_cents == 0
        assert breakdown.state_cents == 0
        assert breakdown.county_cents == 0
        assert breakdown.city_cents == 0
        assert breakdown.ust_cents == 0
        assert breakdown.spcc_cents == 0
        assert breakdown.environmental_cents == 0
        assert breakdown.total_tax_cents == 0
        # Exemption still recorded.
        assert breakdown.exemptions_applied == ["exempt-zero"]

    @pytest.mark.asyncio
    async def test_multiple_exemption_certificates_highest_priority_wins(
        self,
    ):
        """Customer with dyed_diesel + farm + government + resale → dyed_diesel wins.

        Priority order (Task 3.7): dyed_diesel > off_road > farm > 637M >
        government > resale. Only the highest-priority certificate is
        honored for a given invoice.
        """
        rows = _full_diesel_rollup(product_code="OFF_ROAD_DIESEL")
        exemptions = [
            # Intentionally out of priority order to confirm the engine
            # does not rely on ES ordering.
            _make_exemption_row(
                exemption_id="exempt-resale",
                customer_id="cust-1",
                exemption_type="resale",
                product_codes=None,
            ),
            _make_exemption_row(
                exemption_id="exempt-gov",
                customer_id="cust-1",
                exemption_type="government",
                product_codes=None,
            ),
            _make_exemption_row(
                exemption_id="exempt-farm",
                customer_id="cust-1",
                exemption_type="farm",
                product_codes=None,
            ),
            _make_exemption_row(
                exemption_id="exempt-dyed",
                customer_id="cust-1",
                exemption_type="dyed_diesel",
                product_codes=["OFF_ROAD_DIESEL"],
                letter_suffix="M",
                issuing_authority="IRS",
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=rows, exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="OFF_ROAD_DIESEL",
            net_gallons=1000.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        # dyed_diesel (highest priority) applied: federal + state zeroed,
        # county / city / fees retained.
        assert breakdown.federal_cents == 0
        assert breakdown.state_cents == 0
        assert breakdown.county_cents == 5_000
        assert breakdown.city_cents == 3_000
        # Only the dyed-diesel exemption_id appears in the provenance —
        # the lower-priority certificates are ignored for this invoice.
        assert breakdown.exemptions_applied == ["exempt-dyed"]

    @pytest.mark.asyncio
    async def test_historical_effective_date_honors_expired_rate(self):
        """An invoice dated inside an expired rate's window picks up the historical rate.

        Req 1.5: ``effective_date`` / ``expiry_date`` on rate rows are
        prospective bounds — historical invoices continue to reference
        the row active at their invoice date.
        """
        rows = [
            _make_jurisdiction_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=244,
                effective_date="2024-01-01",
                expiry_date=None,
            ),
            # Historical state rate active 2020-01-01 through 2023-12-31
            # (now expired).
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                jurisdiction_name="California",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=350,
                effective_date="2020-01-01",
                expiry_date="2023-12-31",
            ),
            # Current state rate active 2024-01-01 onwards.
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                jurisdiction_name="California",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=400,
                effective_date="2024-01-01",
                expiry_date=None,
            ),
        ]
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        # Invoice dated 2022 → the historical 3.50¢/gal row applies even
        # though it has now expired.
        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="06",
            customer_id="cust-1",
            effective_date=date(2022, 6, 1),
        )

        # 350 × 1000 / 10 == 35_000 cents (historical rate).
        assert breakdown.state_cents == 35_000

    @pytest.mark.asyncio
    async def test_future_dated_rates_not_yet_effective_are_excluded(self):
        """Rate rows with ``effective_date`` after the invoice date do not apply.

        Prospective rate changes must not leak into today's invoices
        before their effective date (Req 1.5).
        """
        rows = [
            _make_jurisdiction_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=244,
            ),
            # Current state rate — active today.
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                jurisdiction_name="California",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=400,
                effective_date="2024-01-01",
                expiry_date="2026-12-31",
            ),
            # Future state rate — scheduled for 2027-01-01.
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                jurisdiction_name="California",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=500,
                effective_date="2027-01-01",
                expiry_date=None,
            ),
        ]
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        # Invoice on 2026-06-01 → only the 2024-current row applies.
        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="06",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        # 400 × 1000 / 10 == 40_000 cents — future 500 rate not yet in effect.
        assert breakdown.state_cents == 40_000


# ---------------------------------------------------------------------------
# Line item count correctness
# ---------------------------------------------------------------------------


class TestLineItemCountCorrectness:
    """Req 1.10: the breakdown carries one line item per applied component.

    Federal + state + county + city + UST + SPCC + environmental = 7
    line items on a full rollup. A dyed-diesel exemption drops federal
    + state, leaving 5.
    """

    @pytest.mark.asyncio
    async def test_full_rollup_produces_exactly_seven_line_items(self):
        """7-component rollup → exactly 7 line items on the breakdown."""
        rows = _full_diesel_rollup(product_code="DIESEL_2")
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        assert len(breakdown.line_items) == 7

        components = [li.tax_component_name for li in breakdown.line_items]
        assert FEDERAL_EXCISE_COMPONENT_NAME in components
        assert "California_state_excise" in components
        assert COUNTY_EXCISE_COMPONENT_NAME in components
        assert CITY_EXCISE_COMPONENT_NAME in components
        assert UST_FEE_COMPONENT_NAME in components
        assert SPCC_FEE_COMPONENT_NAME in components
        assert ENVIRONMENTAL_FEE_COMPONENT_NAME in components

    @pytest.mark.asyncio
    async def test_dyed_diesel_exemption_drops_federal_and_state_leaving_five_line_items(
        self,
    ):
        """After dyed_diesel exemption: 5 line items (federal + state removed)."""
        rows = _full_diesel_rollup(product_code="OFF_ROAD_DIESEL")
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-dd-count",
                customer_id="cust-1",
                exemption_type="dyed_diesel",
                product_codes=["OFF_ROAD_DIESEL"],
                letter_suffix="M",
                issuing_authority="IRS",
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=rows, exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="OFF_ROAD_DIESEL",
            net_gallons=1000.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        assert len(breakdown.line_items) == 5

        components = [li.tax_component_name for li in breakdown.line_items]
        # Federal + state removed.
        assert FEDERAL_EXCISE_COMPONENT_NAME not in components
        assert "California_state_excise" not in components
        # County, city, UST, SPCC, environmental retained.
        assert COUNTY_EXCISE_COMPONENT_NAME in components
        assert CITY_EXCISE_COMPONENT_NAME in components
        assert UST_FEE_COMPONENT_NAME in components
        assert SPCC_FEE_COMPONENT_NAME in components
        assert ENVIRONMENTAL_FEE_COMPONENT_NAME in components

    @pytest.mark.asyncio
    async def test_government_exemption_drops_state_county_city_leaving_four_line_items(
        self,
    ):
        """After government exemption: 4 line items (state/county/city removed)."""
        rows = _full_diesel_rollup(product_code="DIESEL_2")
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-gov-count",
                customer_id="cust-1",
                exemption_type="government",
                product_codes=None,
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=rows, exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="0603744",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        # 7 rows - (state + county + city excise) = 4 line items.
        assert len(breakdown.line_items) == 4

        components = [li.tax_component_name for li in breakdown.line_items]
        assert FEDERAL_EXCISE_COMPONENT_NAME in components
        assert "California_state_excise" not in components
        assert COUNTY_EXCISE_COMPONENT_NAME not in components
        assert CITY_EXCISE_COMPONENT_NAME not in components
        assert UST_FEE_COMPONENT_NAME in components
        assert SPCC_FEE_COMPONENT_NAME in components
        assert ENVIRONMENTAL_FEE_COMPONENT_NAME in components
