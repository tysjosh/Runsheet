"""Unit tests for :meth:`TaxEngine.compute_tax` (Task 3.8).

Covers Requirement 1.9 (``tax.jurisdiction_not_found`` raised when a
required jurisdiction row is missing from the ``tax_jurisdictions``
index) and the end-to-end orchestration of Tasks 3.4-3.7 into a
single :class:`TaxBreakdown`:

    IF a tax table entry is missing for a required jurisdiction,
    THEN THE Tax_Engine SHALL reject the invoice with error code
    ``tax.jurisdiction_not_found`` and log the missing jurisdiction
    for operator resolution.

The test suite uses a single fake ES service that returns canned rows
for both the ``tax_jurisdictions`` and ``tax_exemptions`` indices so
``compute_tax`` can exercise the real jurisdiction lookup + exemption
lookup + federal / state-local computation + exemption application
pipeline end-to-end.

Validates: Requirements 1.9, 1.10
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
    ERROR_CODE_JURISDICTION_NOT_FOUND,
    FEDERAL_EXCISE_COMPONENT_NAME,
    FEDERAL_FIPS_SENTINEL,
    SPCC_FEE_COMPONENT_NAME,
    TaxBreakdown,
    TaxEngine,
    TaxJurisdictionNotFoundError,
    UST_FEE_COMPONENT_NAME,
)


# ---------------------------------------------------------------------------
# Fake ES service — routes on index name and applies the same filters a
# real ES cluster would apply for both ``tax_jurisdictions`` and
# ``tax_exemptions``.
# ---------------------------------------------------------------------------


class _FakeESService:
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

    # ---- jurisdictions ------------------------------------------------

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

    # ---- exemptions ---------------------------------------------------

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

    # ---- shared helpers ----------------------------------------------

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
        inner_bool = musts[0].get("bool", {}) if isinstance(musts[0], dict) else {}
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
    return {
        "jurisdiction_id": f"juris_{uuid4()}",
        "tenant_id": tenant_id,
        "fips_code": fips_code,
        "jurisdiction_level": jurisdiction_level,
        "jurisdiction_name": jurisdiction_name,
        "tax_type": tax_type,
        "product_codes": list(product_codes or ["DIESEL_2", "GASOLINE_REG"]),
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


# ---------------------------------------------------------------------------
# End-to-end happy paths
# ---------------------------------------------------------------------------


class TestComputeTaxHappyPath:
    """Diesel delivery with federal + state + UST rows populates every bucket."""

    @pytest.mark.asyncio
    async def test_diesel_delivery_with_full_rollup(self):
        """Federal + state + UST excise all populate component buckets.

        California diesel delivery with the standard federal 24.4¢ row,
        a 40.0¢ state excise, and a 2.0¢ state UST fee. 1000 gallons
        produces $244 federal + $400 state + $20 UST = $664 total.
        """
        rows = [
            _make_jurisdiction_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                jurisdiction_name="United States",
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
                fips_code="06",
                jurisdiction_level="state",
                tax_type="ust",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=20,
            ),
        ]
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=1000.0,
            destination_fips="06",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        assert isinstance(breakdown, TaxBreakdown)
        assert breakdown.federal_cents == 24_400
        assert breakdown.state_cents == 40_000
        assert breakdown.ust_cents == 2_000
        assert breakdown.county_cents == 0
        assert breakdown.city_cents == 0
        assert breakdown.spcc_cents == 0
        assert breakdown.environmental_cents == 0
        assert breakdown.total_tax_cents == 24_400 + 40_000 + 2_000
        assert breakdown.exemptions_applied == []

        component_names = [item.tax_component_name for item in breakdown.line_items]
        assert FEDERAL_EXCISE_COMPONENT_NAME in component_names
        assert "California_state_excise" in component_names
        assert UST_FEE_COMPONENT_NAME in component_names

    @pytest.mark.asyncio
    async def test_gasoline_delivery_uses_statutory_federal_rate(self):
        """Federal row absent → statutory 18.4¢/gal fallback kicks in."""
        rows = [
            # Only a state excise row — forces statutory federal fallback.
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                jurisdiction_name="California",
                tax_type="excise",
                product_codes=["GASOLINE_REG"],
                rate_cents_per_gallon=400,
            ),
        ]
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="GASOLINE_REG",
            net_gallons=1000.0,
            destination_fips="06",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        # 184 × 1000 / 10 == 18_400 cents (statutory)
        assert breakdown.federal_cents == 18_400
        assert breakdown.state_cents == 40_000


# ---------------------------------------------------------------------------
# Dyed diesel exemption — road-use exclusion (Req 1.7)
# ---------------------------------------------------------------------------


class TestDyedDieselExemption:
    """Valid dyed-diesel certificate zeroes federal + state, keeps fees."""

    @pytest.mark.asyncio
    async def test_dyed_diesel_zeros_federal_and_state_retains_county_and_ust(self):
        """Dyed diesel w/ valid exemption: federal+state excise dropped;
        county excise + UST retained.
        """
        rows = [
            _make_jurisdiction_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                product_codes=["OFF_ROAD_DIESEL"],
                rate_cents_per_gallon=244,
            ),
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                jurisdiction_name="California",
                tax_type="excise",
                product_codes=["OFF_ROAD_DIESEL"],
                rate_cents_per_gallon=400,
            ),
            _make_jurisdiction_row(
                fips_code="06037",
                jurisdiction_level="county",
                tax_type="excise",
                product_codes=["OFF_ROAD_DIESEL"],
                rate_cents_per_gallon=50,
            ),
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="ust",
                product_codes=["OFF_ROAD_DIESEL"],
                rate_cents_per_gallon=20,
            ),
        ]
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-dd",
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
            destination_fips="06037",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        # Federal + state excise zeroed by the road-use exemption.
        assert breakdown.federal_cents == 0
        assert breakdown.state_cents == 0
        # County excise and UST fee retained.
        assert breakdown.county_cents == 5_000
        assert breakdown.ust_cents == 2_000
        # Exemption recorded.
        assert "exempt-dd" in breakdown.exemptions_applied

        component_names = [item.tax_component_name for item in breakdown.line_items]
        assert FEDERAL_EXCISE_COMPONENT_NAME not in component_names
        assert "California_state_excise" not in component_names
        assert COUNTY_EXCISE_COMPONENT_NAME in component_names
        assert UST_FEE_COMPONENT_NAME in component_names


# ---------------------------------------------------------------------------
# Missing state row — Req 1.9
# ---------------------------------------------------------------------------


class TestMissingStateRow:
    """Req 1.9: raise ``tax.jurisdiction_not_found`` when a required
    state row is missing and no exemption zeros it out."""

    @pytest.mark.asyncio
    async def test_missing_state_row_raises_jurisdiction_not_found(self):
        """Only a federal row present → state excise required → raises."""
        rows = [
            _make_jurisdiction_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=244,
            ),
        ]
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        with pytest.raises(TaxJurisdictionNotFoundError) as excinfo:
            await engine.compute_tax(
                product_code="DIESEL_2",
                net_gallons=1000.0,
                destination_fips="06037",
                customer_id="cust-1",
                effective_date=date(2026, 6, 1),
            )

        exc = excinfo.value
        assert exc.error_code == ERROR_CODE_JURISDICTION_NOT_FOUND
        assert exc.fips_code == "06"
        assert exc.jurisdiction_level == "state"
        assert exc.tax_type == "excise"
        assert exc.product_code == "DIESEL_2"
        assert exc.effective_date == date(2026, 6, 1)

    @pytest.mark.asyncio
    async def test_missing_state_row_is_also_value_error(self):
        """Subclassing :class:`ValueError` keeps legacy callers working."""
        es = _FakeESService(jurisdiction_rows=[], exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        with pytest.raises(ValueError) as excinfo:
            await engine.compute_tax(
                product_code="DIESEL_2",
                net_gallons=1000.0,
                destination_fips="06",
                customer_id="cust-1",
                effective_date=date(2026, 6, 1),
            )
        assert isinstance(excinfo.value, TaxJurisdictionNotFoundError)

    @pytest.mark.asyncio
    async def test_state_row_for_wrong_product_still_raises(self):
        """A state excise row present for a different product does NOT
        satisfy the gate — the row must match the delivered product."""
        rows = [
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="excise",
                product_codes=["GASOLINE_REG"],  # not DIESEL_2
                rate_cents_per_gallon=400,
            ),
        ]
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        with pytest.raises(TaxJurisdictionNotFoundError):
            await engine.compute_tax(
                product_code="DIESEL_2",
                net_gallons=1000.0,
                destination_fips="06",
                customer_id="cust-1",
                effective_date=date(2026, 6, 1),
            )

    @pytest.mark.asyncio
    async def test_missing_state_row_with_dyed_diesel_exemption_does_not_raise(self):
        """Road-use exemption short-circuits the missing-row gate.

        A dyed-diesel / off-road / 637M certificate zeroes the state
        component anyway, so requiring the row would reject an invoice
        that would compute to $0 state regardless.
        """
        # No state row — only federal (for completeness).
        rows = [
            _make_jurisdiction_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                product_codes=["OFF_ROAD_DIESEL"],
                rate_cents_per_gallon=244,
            ),
        ]
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-dd",
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

        # Must not raise — the dyed_diesel exemption zeroes the state
        # component regardless of whether a row exists.
        breakdown = await engine.compute_tax(
            product_code="OFF_ROAD_DIESEL",
            net_gallons=1000.0,
            destination_fips="06",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        assert breakdown.federal_cents == 0
        assert breakdown.state_cents == 0
        assert "exempt-dd" in breakdown.exemptions_applied

    @pytest.mark.asyncio
    async def test_missing_state_row_with_off_road_exemption_does_not_raise(self):
        """off_road exemption also short-circuits the missing-row gate."""
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-offroad",
                customer_id="cust-1",
                exemption_type="off_road",
                product_codes=["OFF_ROAD_DIESEL"],
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=[], exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="OFF_ROAD_DIESEL",
            net_gallons=1000.0,
            destination_fips="06",
            customer_id="cust-1",
            effective_date=date(2026, 6, 1),
        )

        assert breakdown.federal_cents == 0
        assert breakdown.state_cents == 0
        assert "exempt-offroad" in breakdown.exemptions_applied

    @pytest.mark.asyncio
    async def test_missing_state_row_with_farm_exemption_still_raises(self):
        """Farm exemption does NOT zero the state component — gate still applies.

        Per the Task 3.7 design note, farm certificates are flag-only;
        rate reduction is resolved via a farm-specific jurisdiction
        row. A missing state row with only a farm certificate means
        the invoice genuinely has no state rate to apply.
        """
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-farm",
                customer_id="cust-1",
                exemption_type="farm",
                product_codes=None,
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=[], exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        with pytest.raises(TaxJurisdictionNotFoundError):
            await engine.compute_tax(
                product_code="DIESEL_2",
                net_gallons=1000.0,
                destination_fips="06",
                customer_id="cust-1",
                effective_date=date(2026, 6, 1),
            )


# ---------------------------------------------------------------------------
# Government exemption — jurisdictional blanket (Req 1.7)
# ---------------------------------------------------------------------------


class TestGovernmentExemption:
    """Government blanket exemption zeroes state/county/city, keeps federal."""

    @pytest.mark.asyncio
    async def test_government_exemption_zeros_state_county_city_keeps_federal(self):
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
            _make_jurisdiction_row(
                fips_code="0603744",
                jurisdiction_level="city",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=30,
            ),
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="ust",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=20,
            ),
        ]
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-gov",
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

        # Federal stays, state / county / city are zeroed.
        assert breakdown.federal_cents == 24_400
        assert breakdown.state_cents == 0
        assert breakdown.county_cents == 0
        assert breakdown.city_cents == 0
        # UST fee retained (it's not a jurisdictional excise).
        assert breakdown.ust_cents == 2_000
        assert "exempt-gov" in breakdown.exemptions_applied


# ---------------------------------------------------------------------------
# Tenant isolation (Constraint C3)
# ---------------------------------------------------------------------------


class TestTenantFiltering:
    """Queries from tenant-1 must not surface rows belonging to tenant-2."""

    @pytest.mark.asyncio
    async def test_other_tenant_state_row_does_not_satisfy_gate(self):
        """A state row owned by a different tenant is invisible — raises."""
        rows = [
            _make_jurisdiction_row(
                tenant_id="tenant-2",  # other tenant
                fips_code="06",
                jurisdiction_level="state",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=400,
            ),
        ]
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        with pytest.raises(TaxJurisdictionNotFoundError):
            await engine.compute_tax(
                product_code="DIESEL_2",
                net_gallons=1000.0,
                destination_fips="06",
                customer_id="cust-1",
                effective_date=date(2026, 6, 1),
            )

    @pytest.mark.asyncio
    async def test_other_tenant_exemption_does_not_zero_state(self):
        """Exemption owned by another tenant cannot short-circuit the gate."""
        exemptions = [
            _make_exemption_row(
                exemption_id="exempt-other",
                tenant_id="tenant-2",  # other tenant
                customer_id="cust-1",
                exemption_type="dyed_diesel",
                product_codes=["OFF_ROAD_DIESEL"],
            ),
        ]
        es = _FakeESService(
            jurisdiction_rows=[], exemption_rows=exemptions
        )
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        with pytest.raises(TaxJurisdictionNotFoundError):
            await engine.compute_tax(
                product_code="OFF_ROAD_DIESEL",
                net_gallons=1000.0,
                destination_fips="06",
                customer_id="cust-1",
                effective_date=date(2026, 6, 1),
            )


# ---------------------------------------------------------------------------
# Default effective_date
# ---------------------------------------------------------------------------


class TestDefaultEffectiveDate:
    """When ``effective_date`` is omitted, ``date.today()`` is used."""

    @pytest.mark.asyncio
    async def test_default_effective_date_uses_today(self, monkeypatch):
        """Verify the jurisdiction lookup filters by today's date."""
        rows = [
            _make_jurisdiction_row(
                fips_code="06",
                jurisdiction_level="state",
                jurisdiction_name="California",
                tax_type="excise",
                product_codes=["DIESEL_2"],
                rate_cents_per_gallon=400,
            ),
        ]
        es = _FakeESService(jurisdiction_rows=rows, exemption_rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        breakdown = await engine.compute_tax(
            product_code="DIESEL_2",
            net_gallons=100.0,
            destination_fips="06",
            customer_id="cust-1",
            # effective_date omitted → should default to today
        )

        # Simple sanity: no jurisdiction error and a state row applied.
        assert breakdown.state_cents == 4_000

        # Confirm the emitted ES call used today's ISO date.
        today_iso = date.today().isoformat()
        jurisdiction_calls = [
            c for c in es.calls if c["index"] == TAX_JURISDICTIONS_INDEX
        ]
        assert len(jurisdiction_calls) == 1
        inner = jurisdiction_calls[0]["query"]["query"]["bool"]["must"][0]["bool"]
        eff_range = [
            c for c in inner["filter"]
            if "range" in c and "effective_date" in c["range"]
        ][0]
        assert eff_range["range"]["effective_date"] == {"lte": today_iso}
