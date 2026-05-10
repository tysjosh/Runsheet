"""Unit tests for :meth:`TaxEngine.get_jurisdiction_rates`.

Covers Task 3.4 of the Fuel Compliance Backbone spec, which implements
the jurisdiction rollup from a destination FIPS code + effective date to
the full set of federal / state / county / city rates active on that
date (Requirements 1.2 and 1.3, with effective-window semantics from
Req 1.5).

The test suite uses an async fake ES service that returns canned rows
so we can:

* Confirm the correct ``terms`` filter is built for 2 / 5 / 7 digit
  FIPS codes (federal + state / county / city rollup).
* Confirm expired and future-dated rows are excluded by the
  ``effective_date`` / ``expiry_date`` range clause.
* Confirm the tenant filter is applied via ``inject_tenant_filter``
  (Constraint C3).
* Confirm invalid FIPS codes raise ``ValueError`` before any ES call.

Validates: Requirements 1.2, 1.3
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

from compliance.models.jurisdiction_rate import JurisdictionRate
from compliance.services.compliance_es_mappings import TAX_JURISDICTIONS_INDEX
from compliance.services.tax_engine import (
    FEDERAL_FIPS_SENTINEL,
    TaxEngine,
)


# ---------------------------------------------------------------------------
# Fake ES service
# ---------------------------------------------------------------------------


class _FakeESService:
    """Async ES stand-in that returns canned rows and records calls.

    Stores every (index, query, size) tuple so tests can assert on the
    query shape that ``TaxEngine.get_jurisdiction_rates`` emits.
    Applies the same ``effective_date`` / ``expiry_date`` / ``terms`` /
    ``tenant_id`` filters that a real ES cluster would so the client-
    side filter logic stays honest.
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

        # Apply the filters a real ES cluster would apply so the test
        # doubles as an end-to-end round trip of the query shape.
        filters = (
            (((query or {}).get("query") or {}).get("bool") or {})
            .get("must", [])
        )
        inner_bool = (
            filters[0].get("bool", {}) if filters else {}
        )
        inner_filter = inner_bool.get("filter", []) if inner_bool else []

        tenant_filter = (
            (((query or {}).get("query") or {}).get("bool") or {})
            .get("filter", [])
        )
        tenant_term = None
        for clause in tenant_filter:
            if "term" in clause and "tenant_id" in clause["term"]:
                tenant_term = clause["term"]["tenant_id"]
                break

        candidate_codes: List[str] = []
        effective_lte: Optional[str] = None

        for clause in inner_filter:
            if "terms" in clause and "fips_code" in clause["terms"]:
                candidate_codes = list(clause["terms"]["fips_code"])
            if "range" in clause and "effective_date" in clause["range"]:
                effective_lte = clause["range"]["effective_date"].get("lte")

        matching: List[Dict[str, Any]] = []
        for row in self._rows:
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


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _make_row(
    *,
    tenant_id: str = "tenant-1",
    fips_code: str = "00",
    jurisdiction_level: str = "federal",
    tax_type: str = "excise",
    product_codes: Optional[List[str]] = None,
    rate_cents_per_gallon: int = 184,
    effective_date: str = "2024-01-01",
    expiry_date: Optional[str] = None,
    jurisdiction_name: Optional[str] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a ``tax_jurisdictions`` _source row with sensible defaults."""
    row: Dict[str, Any] = {
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
        "source": source,
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }
    return row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _levels(rates: List[JurisdictionRate]) -> List[str]:
    return sorted(rate.jurisdiction_level for rate in rates)


def _fips(rates: List[JurisdictionRate]) -> List[str]:
    return sorted(rate.fips_code for rate in rates)


# ---------------------------------------------------------------------------
# Candidate FIPS resolution (pure logic)
# ---------------------------------------------------------------------------


class TestComputeCandidateFipsCodes:
    """Unit-test the pure rollup helper directly for speed and clarity."""

    def test_2_digit_state_includes_federal_and_state(self):
        codes = TaxEngine._compute_candidate_fips_codes("06")
        assert codes == [FEDERAL_FIPS_SENTINEL, "06"]

    def test_5_digit_county_includes_federal_state_county(self):
        codes = TaxEngine._compute_candidate_fips_codes("06037")
        assert codes == [FEDERAL_FIPS_SENTINEL, "06", "06037"]

    def test_7_digit_city_includes_full_hierarchy(self):
        codes = TaxEngine._compute_candidate_fips_codes("0603744")
        assert codes == [FEDERAL_FIPS_SENTINEL, "06", "06037", "0603744"]

    def test_federal_sentinel_is_not_duplicated(self):
        """A 2-digit input equal to the sentinel should appear once."""
        codes = TaxEngine._compute_candidate_fips_codes(
            FEDERAL_FIPS_SENTINEL
        )
        assert codes == [FEDERAL_FIPS_SENTINEL]

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "CA",
            "06A",
            "abcdefg",
            "123",  # length 3 — not allowed
            "123456",  # length 6 — not allowed
            "12345678",  # length 8 — not allowed
        ],
    )
    def test_invalid_fips_raises_value_error(self, bad: str):
        with pytest.raises(ValueError):
            TaxEngine._compute_candidate_fips_codes(bad)

    def test_non_string_fips_raises_value_error(self):
        with pytest.raises(ValueError):
            TaxEngine._compute_candidate_fips_codes(6037)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# get_jurisdiction_rates — async lookup behavior
# ---------------------------------------------------------------------------


class TestGetJurisdictionRates:
    """Round-trip the async lookup through the fake ES.

    The fake ES mirrors the filters a real cluster would apply so each
    test exercises the real query shape end-to-end.
    """

    # ---- dataset -----------------------------------------------------

    def _default_rows(self) -> List[Dict[str, Any]]:
        return [
            # Federal row — active 2024-01-01 onwards.
            _make_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                rate_cents_per_gallon=184,
                effective_date="2024-01-01",
                expiry_date=None,
                jurisdiction_name="United States",
            ),
            # California state excise.
            _make_row(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="excise",
                rate_cents_per_gallon=51,
                effective_date="2024-01-01",
                expiry_date=None,
                jurisdiction_name="California",
            ),
            # Los Angeles county UST fee.
            _make_row(
                fips_code="06037",
                jurisdiction_level="county",
                tax_type="ust",
                rate_cents_per_gallon=2,
                effective_date="2024-01-01",
                expiry_date=None,
                jurisdiction_name="Los Angeles County",
            ),
            # Los Angeles city environmental fee.
            _make_row(
                fips_code="0603744",
                jurisdiction_level="city",
                tax_type="environmental",
                rate_cents_per_gallon=1,
                effective_date="2024-01-01",
                expiry_date=None,
                jurisdiction_name="Los Angeles",
            ),
        ]

    # ---- happy path by FIPS depth ------------------------------------

    @pytest.mark.asyncio
    async def test_2_digit_fips_returns_federal_and_state(self):
        es = _FakeESService(rows=self._default_rows())
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        rates = await engine.get_jurisdiction_rates(
            fips_code="06",
            effective_date=date(2026, 6, 1),
        )

        assert _levels(rates) == ["federal", "state"]
        assert _fips(rates) == [FEDERAL_FIPS_SENTINEL, "06"]

    @pytest.mark.asyncio
    async def test_5_digit_fips_returns_federal_state_county(self):
        es = _FakeESService(rows=self._default_rows())
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        rates = await engine.get_jurisdiction_rates(
            fips_code="06037",
            effective_date=date(2026, 6, 1),
        )

        assert _levels(rates) == ["county", "federal", "state"]
        assert _fips(rates) == [FEDERAL_FIPS_SENTINEL, "06", "06037"]

    @pytest.mark.asyncio
    async def test_7_digit_fips_returns_full_hierarchy(self):
        es = _FakeESService(rows=self._default_rows())
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        rates = await engine.get_jurisdiction_rates(
            fips_code="0603744",
            effective_date=date(2026, 6, 1),
        )

        assert _levels(rates) == ["city", "county", "federal", "state"]
        assert _fips(rates) == [
            FEDERAL_FIPS_SENTINEL,
            "06",
            "06037",
            "0603744",
        ]

    # ---- effective-window filtering (Req 1.5) ------------------------

    @pytest.mark.asyncio
    async def test_expired_rates_are_excluded(self):
        """Rows whose ``expiry_date`` is before the invoice date drop out."""
        rows = self._default_rows()
        # Add an expired state excise row that should NOT appear.
        rows.append(
            _make_row(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="excise",
                rate_cents_per_gallon=40,  # Old rate
                effective_date="2020-01-01",
                expiry_date="2023-12-31",
                jurisdiction_name="California (legacy)",
            )
        )
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        rates = await engine.get_jurisdiction_rates(
            fips_code="06",
            effective_date=date(2026, 6, 1),
        )

        # Only the current rate survives; the legacy expired row is gone.
        state_rates = [r for r in rates if r.jurisdiction_level == "state"]
        assert len(state_rates) == 1
        assert state_rates[0].rate_cents_per_gallon == 51

    @pytest.mark.asyncio
    async def test_future_dated_rates_are_excluded(self):
        """Rows whose ``effective_date`` is after the invoice date drop out."""
        rows = self._default_rows()
        # Add a future-dated state excise row that should NOT appear yet.
        rows.append(
            _make_row(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="excise",
                rate_cents_per_gallon=60,  # Future-scheduled rate
                effective_date="2027-01-01",
                expiry_date=None,
                jurisdiction_name="California (scheduled)",
            )
        )
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        rates = await engine.get_jurisdiction_rates(
            fips_code="06",
            effective_date=date(2026, 6, 1),
        )

        state_rates = [r for r in rates if r.jurisdiction_level == "state"]
        assert len(state_rates) == 1
        assert state_rates[0].rate_cents_per_gallon == 51

    @pytest.mark.asyncio
    async def test_open_ended_rate_is_included(self):
        """Rows with ``expiry_date=None`` are active indefinitely (Req 1.5)."""
        rows = [
            _make_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                rate_cents_per_gallon=184,
                effective_date="2024-01-01",
                expiry_date=None,
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        rates = await engine.get_jurisdiction_rates(
            fips_code="06",
            effective_date=date(2030, 1, 1),  # Many years later
        )

        assert len(rates) == 1
        assert rates[0].expiry_date is None

    @pytest.mark.asyncio
    async def test_rate_active_on_effective_date_boundary(self):
        """Rows whose window exactly brackets the invoice date are included.

        Both ``effective_date`` (lte) and ``expiry_date`` (gte) are
        inclusive per Req 1.5.
        """
        rows = [
            _make_row(
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                rate_cents_per_gallon=184,
                effective_date="2026-06-01",  # Exactly == invoice date
                expiry_date="2026-06-01",  # Also exactly the invoice date
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        rates = await engine.get_jurisdiction_rates(
            fips_code="06",
            effective_date=date(2026, 6, 1),
        )

        assert len(rates) == 1

    # ---- tenant isolation (Constraint C3) ----------------------------

    @pytest.mark.asyncio
    async def test_tenant_filter_excludes_other_tenants(self):
        rows = [
            _make_row(
                tenant_id="tenant-1",
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                rate_cents_per_gallon=184,
            ),
            _make_row(
                tenant_id="tenant-2",
                fips_code=FEDERAL_FIPS_SENTINEL,
                jurisdiction_level="federal",
                tax_type="excise",
                rate_cents_per_gallon=999,  # would be wrong if returned
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        rates = await engine.get_jurisdiction_rates(
            fips_code="06",
            effective_date=date(2026, 6, 1),
        )

        assert len(rates) == 1
        assert rates[0].rate_cents_per_gallon == 184

    @pytest.mark.asyncio
    async def test_query_carries_tenant_filter_clause(self):
        """The emitted query must wrap the bool with a tenant_id filter."""
        es = _FakeESService(rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-xyz")

        await engine.get_jurisdiction_rates(
            fips_code="06",
            effective_date=date(2026, 6, 1),
        )

        assert len(es.calls) == 1
        sent = es.calls[0]
        assert sent["index"] == TAX_JURISDICTIONS_INDEX

        filter_clauses = sent["query"]["query"]["bool"]["filter"]
        tenant_terms = [
            clause["term"]["tenant_id"]
            for clause in filter_clauses
            if "term" in clause and "tenant_id" in clause["term"]
        ]
        assert tenant_terms == ["tenant-xyz"]

    @pytest.mark.asyncio
    async def test_query_uses_terms_filter_on_fips_rollup(self):
        """The query filters on the full candidate FIPS rollup."""
        es = _FakeESService(rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        await engine.get_jurisdiction_rates(
            fips_code="0603744",
            effective_date=date(2026, 6, 1),
        )

        inner = es.calls[0]["query"]["query"]["bool"]["must"][0]["bool"]
        terms_clauses = [
            clause
            for clause in inner["filter"]
            if "terms" in clause and "fips_code" in clause["terms"]
        ]
        assert len(terms_clauses) == 1
        assert terms_clauses[0]["terms"]["fips_code"] == [
            FEDERAL_FIPS_SENTINEL,
            "06",
            "06037",
            "0603744",
        ]

    @pytest.mark.asyncio
    async def test_query_includes_effective_date_range_and_missing_expiry(self):
        """The query filters by effective_date <= date and (expiry_date >=
        date OR expiry_date is missing).
        """
        es = _FakeESService(rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        await engine.get_jurisdiction_rates(
            fips_code="06",
            effective_date=date(2026, 6, 1),
        )

        inner = es.calls[0]["query"]["query"]["bool"]["must"][0]["bool"]
        filter_clauses = inner["filter"]

        # effective_date <= invoice_date
        eff_clauses = [
            clause
            for clause in filter_clauses
            if "range" in clause and "effective_date" in clause["range"]
        ]
        assert len(eff_clauses) == 1
        assert eff_clauses[0]["range"]["effective_date"] == {"lte": "2026-06-01"}

        # expiry_date >= invoice_date OR expiry_date missing
        expiry_bool_clauses = [
            clause
            for clause in filter_clauses
            if "bool" in clause and "should" in clause["bool"]
        ]
        assert len(expiry_bool_clauses) == 1
        should = expiry_bool_clauses[0]["bool"]["should"]
        # One branch: range expiry_date >= invoice_date
        gte_branches = [
            b
            for b in should
            if "range" in b and "expiry_date" in b["range"]
        ]
        assert gte_branches[0]["range"]["expiry_date"] == {"gte": "2026-06-01"}
        # Other branch: must_not exists expiry_date
        missing_branches = [
            b
            for b in should
            if "bool" in b and "must_not" in b["bool"]
        ]
        assert missing_branches[0]["bool"]["must_not"] == [
            {"exists": {"field": "expiry_date"}}
        ]

    # ---- empty result set --------------------------------------------

    @pytest.mark.asyncio
    async def test_empty_rowset_returns_empty_list(self):
        es = _FakeESService(rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        rates = await engine.get_jurisdiction_rates(
            fips_code="06037",
            effective_date=date(2026, 6, 1),
        )

        assert rates == []

    # ---- invalid input (pre-query) -----------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_fips",
        ["", "   ", "CA", "06A", "123", "123456", "12345678"],
    )
    async def test_invalid_fips_raises_value_error_without_es_call(
        self, bad_fips: str
    ):
        es = _FakeESService(rows=[])
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        with pytest.raises(ValueError):
            await engine.get_jurisdiction_rates(
                fips_code=bad_fips,
                effective_date=date(2026, 6, 1),
            )

        # No ES call should have been made for an invalid FIPS code.
        assert es.calls == []

    # ---- parsing: multiple rates per jurisdiction --------------------

    @pytest.mark.asyncio
    async def test_multiple_tax_types_at_one_level_all_returned(self):
        """Product filtering is NOT done here — all tax_types come back.

        Callers are responsible for filtering by ``product_codes`` /
        ``tax_type``. This test pins that contract so future changes
        cannot silently narrow the rollup.
        """
        rows = [
            _make_row(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="excise",
                rate_cents_per_gallon=51,
            ),
            _make_row(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="ust",
                rate_cents_per_gallon=3,
            ),
            _make_row(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="environmental",
                rate_cents_per_gallon=2,
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        rates = await engine.get_jurisdiction_rates(
            fips_code="06",
            effective_date=date(2026, 6, 1),
        )

        tax_types = sorted(r.tax_type for r in rates)
        assert tax_types == ["environmental", "excise", "ust"]

    @pytest.mark.asyncio
    async def test_malformed_row_is_skipped_not_raised(self):
        """A malformed _source should be skipped with a warning, not raise.

        The TaxEngine surfaces missing rows via
        ``TaxJurisdictionNotFoundError`` in Task 3.8; a malformed single
        row must not poison the whole lookup.
        """
        rows = [
            # Malformed: product_codes empty — JurisdictionRate validator rejects.
            {
                "jurisdiction_id": "juris_bad",
                "tenant_id": "tenant-1",
                "fips_code": FEDERAL_FIPS_SENTINEL,
                "jurisdiction_level": "federal",
                "tax_type": "excise",
                "product_codes": [],  # <-- will fail validation
                "rate_cents_per_gallon": 184,
                "effective_date": "2024-01-01",
                "expiry_date": None,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            },
            # Well-formed state row — should still come through.
            _make_row(
                fips_code="06",
                jurisdiction_level="state",
                tax_type="excise",
                rate_cents_per_gallon=51,
            ),
        ]
        es = _FakeESService(rows=rows)
        engine = TaxEngine(es_service=es, tenant_id="tenant-1")

        rates = await engine.get_jurisdiction_rates(
            fips_code="06",
            effective_date=date(2026, 6, 1),
        )

        assert len(rates) == 1
        assert rates[0].fips_code == "06"
