"""Unit tests for :class:`commerce.services.price_protection_service.PriceProtectionService`.

Covers Task 4.2 of the Fuel Compliance Backbone spec, which implements
``find_active_contract`` and ``resolve_price`` on the
:class:`PriceProtectionService`.

The test suite uses an async fake ES service that applies the same
``term`` / ``range`` / ``tenant_id`` filters that a real Elasticsearch
cluster would apply, so the query shape emitted by the service is
exercised end-to-end.

Validates: Requirement 3.3
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pytest

from commerce.models.price_protection_contract import PriceProtectionContract
from commerce.services.price_protection_service import (
    PriceProtectionService,
    PriceResolution,
)
from compliance.services.compliance_es_mappings import (
    PRICE_PROTECTION_CONTRACTS_INDEX,
)


# ---------------------------------------------------------------------------
# Fake ES service
# ---------------------------------------------------------------------------


class _FakeESService:
    """Async ES stand-in that returns canned contract rows.

    Mirrors the same filters a real ES cluster would apply (``tenant_id``,
    ``customer_id``, ``product_code``, ``status``, and the
    ``start_date``/``end_date`` range clauses) so the test doubles as a
    round-trip of the query shape emitted by ``find_active_contract``.
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

        # Tenant filter lives on the outer bool.filter produced by
        # inject_tenant_filter.
        tenant_filter = (
            (((query or {}).get("query") or {}).get("bool") or {})
            .get("filter", [])
        )
        tenant_term: Optional[str] = None
        for clause in tenant_filter:
            if "term" in clause and "tenant_id" in clause["term"]:
                tenant_term = clause["term"]["tenant_id"]
                break

        # Inner filters live on bool.must[0].bool.filter.
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
        product_code: Optional[str] = None
        status: Optional[str] = None
        start_lte: Optional[str] = None
        end_gte: Optional[str] = None

        for clause in inner_filter:
            if "term" in clause and "customer_id" in clause["term"]:
                customer_id = clause["term"]["customer_id"]
            elif "term" in clause and "product_code" in clause["term"]:
                product_code = clause["term"]["product_code"]
            elif "term" in clause and "status" in clause["term"]:
                status = clause["term"]["status"]
            elif "range" in clause and "start_date" in clause["range"]:
                start_lte = clause["range"]["start_date"].get("lte")
            elif "range" in clause and "end_date" in clause["range"]:
                end_gte = clause["range"]["end_date"].get("gte")

        matching: List[Dict[str, Any]] = []
        for row in self._rows:
            if tenant_term is not None and row.get("tenant_id") != tenant_term:
                continue
            if customer_id is not None and row.get("customer_id") != customer_id:
                continue
            if product_code is not None and row.get("product_code") != product_code:
                continue
            if status is not None and row.get("status") != status:
                continue
            if start_lte is not None:
                start = row.get("start_date")
                if start is not None and start > start_lte:
                    continue
            if end_gte is not None:
                end = row.get("end_date")
                if end is not None and end < end_gte:
                    continue
            matching.append(row)

        return {"hits": {"hits": [{"_source": row} for row in matching]}}


# ---------------------------------------------------------------------------
# Row / contract builders
# ---------------------------------------------------------------------------


def _make_contract_row(
    *,
    contract_id: str = "contract-1",
    tenant_id: str = "tenant-1",
    customer_id: str = "cust-1",
    account_id: str = "acct-1",
    product_code: str = "HEATING_OIL",
    contract_type: str = "fixed_price",
    start_date: str = "2026-01-01",
    end_date: str = "2026-12-31",
    contracted_gallons: float = 10_000.0,
    remaining_gallons: Optional[float] = None,
    price_cap_cents: Optional[int] = None,
    price_floor_cents: Optional[int] = None,
    fixed_price_cents: Optional[int] = None,
    status: str = "active",
    version: int = 0,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a ``price_protection_contracts`` ``_source`` row.

    Defaults produce a valid ``fixed_price`` contract; override per-test
    to exercise ``cap_price`` / ``collar`` and edge conditions.
    """
    if remaining_gallons is None:
        remaining_gallons = contracted_gallons

    # Sensible price defaults by contract_type so tests only need to
    # override when they want to.
    if contract_type == "fixed_price" and fixed_price_cents is None:
        fixed_price_cents = 325
    if contract_type == "cap_price" and price_cap_cents is None:
        price_cap_cents = 340
    if contract_type == "collar":
        if price_cap_cents is None:
            price_cap_cents = 340
        if price_floor_cents is None:
            price_floor_cents = 290

    return {
        "contract_id": contract_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "account_id": account_id,
        "product_code": product_code,
        "contract_type": contract_type,
        "start_date": start_date,
        "end_date": end_date,
        "contracted_gallons": contracted_gallons,
        "remaining_gallons": remaining_gallons,
        "price_cap_cents": price_cap_cents,
        "price_floor_cents": price_floor_cents,
        "fixed_price_cents": fixed_price_cents,
        "status": status,
        "version": version,
        "notes": notes,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_non_empty_tenant_id_is_required(self):
        with pytest.raises(ValueError):
            PriceProtectionService(_FakeESService(), "")

        with pytest.raises(ValueError):
            PriceProtectionService(_FakeESService(), "   ")

    def test_non_string_tenant_id_is_rejected(self):
        with pytest.raises(ValueError):
            PriceProtectionService(_FakeESService(), 42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# find_active_contract — query shape + filtering
# ---------------------------------------------------------------------------


class TestFindActiveContract:
    """Round-trip the async lookup through the fake ES."""

    @pytest.mark.asyncio
    async def test_returns_single_matching_contract(self):
        rows = [
            _make_contract_row(
                contract_id="contract-1",
                start_date="2026-01-01",
                end_date="2026-12-31",
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        contract = await service.find_active_contract(
            "cust-1", "HEATING_OIL", date(2026, 6, 1)
        )

        assert contract is not None
        assert contract.contract_id == "contract-1"
        # Verify the query went to the correct index.
        assert es.calls
        assert es.calls[0]["index"] == PRICE_PROTECTION_CONTRACTS_INDEX

    @pytest.mark.asyncio
    async def test_returns_none_when_no_contract_matches(self):
        es = _FakeESService(rows=[])
        service = PriceProtectionService(es, "tenant-1")

        contract = await service.find_active_contract(
            "cust-1", "HEATING_OIL", date(2026, 6, 1)
        )

        assert contract is None

    @pytest.mark.asyncio
    async def test_expired_contract_is_excluded(self):
        """end_date < effective_date → filtered out by the range clause."""
        rows = [
            _make_contract_row(
                contract_id="contract-old",
                start_date="2025-01-01",
                end_date="2025-12-31",
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        contract = await service.find_active_contract(
            "cust-1", "HEATING_OIL", date(2026, 6, 1)
        )

        assert contract is None

    @pytest.mark.asyncio
    async def test_future_dated_contract_is_excluded(self):
        """start_date > effective_date → filtered out by the range clause."""
        rows = [
            _make_contract_row(
                contract_id="contract-future",
                start_date="2027-01-01",
                end_date="2027-12-31",
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        contract = await service.find_active_contract(
            "cust-1", "HEATING_OIL", date(2026, 6, 1)
        )

        assert contract is None

    @pytest.mark.asyncio
    async def test_non_active_status_is_excluded(self):
        """status != 'active' → filtered out by the ES status term."""
        rows = [
            _make_contract_row(
                contract_id="contract-exhausted",
                status="exhausted",
            ),
            _make_contract_row(
                contract_id="contract-expired",
                status="expired",
            ),
            _make_contract_row(
                contract_id="contract-cancelled",
                status="cancelled",
            ),
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        contract = await service.find_active_contract(
            "cust-1", "HEATING_OIL", date(2026, 6, 1)
        )

        assert contract is None

    @pytest.mark.asyncio
    async def test_tenant_isolation(self):
        """A contract for a different tenant must not be returned."""
        rows = [
            _make_contract_row(
                contract_id="contract-other",
                tenant_id="tenant-2",
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        contract = await service.find_active_contract(
            "cust-1", "HEATING_OIL", date(2026, 6, 1)
        )

        assert contract is None

    @pytest.mark.asyncio
    async def test_product_code_scoping(self):
        """Only contracts for the requested product are returned."""
        rows = [
            _make_contract_row(
                contract_id="contract-diesel",
                product_code="DIESEL_2",
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        contract = await service.find_active_contract(
            "cust-1", "HEATING_OIL", date(2026, 6, 1)
        )

        assert contract is None

    @pytest.mark.asyncio
    async def test_customer_scoping(self):
        """Only contracts for the requested customer are returned."""
        rows = [
            _make_contract_row(
                contract_id="contract-other-cust",
                customer_id="cust-2",
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        contract = await service.find_active_contract(
            "cust-1", "HEATING_OIL", date(2026, 6, 1)
        )

        assert contract is None

    @pytest.mark.asyncio
    async def test_latest_start_date_wins_when_multiple_match(self):
        """When overlapping contracts match, the most recent start wins."""
        rows = [
            _make_contract_row(
                contract_id="contract-old",
                start_date="2026-01-01",
                end_date="2026-12-31",
            ),
            _make_contract_row(
                contract_id="contract-new",
                start_date="2026-05-01",
                end_date="2026-12-31",
            ),
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        contract = await service.find_active_contract(
            "cust-1", "HEATING_OIL", date(2026, 6, 1)
        )

        assert contract is not None
        assert contract.contract_id == "contract-new"

    @pytest.mark.asyncio
    async def test_invalid_inputs_raise_value_error(self):
        service = PriceProtectionService(_FakeESService(), "tenant-1")

        with pytest.raises(ValueError):
            await service.find_active_contract(
                "", "HEATING_OIL", date(2026, 6, 1)
            )
        with pytest.raises(ValueError):
            await service.find_active_contract(
                "cust-1", "", date(2026, 6, 1)
            )
        with pytest.raises(ValueError):
            await service.find_active_contract(
                "cust-1", "HEATING_OIL", "2026-06-01"  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# resolve_price — contract_type dispatch (Req 3.3)
# ---------------------------------------------------------------------------


class TestResolvePriceDispatch:
    """Each contract_type returns the documented effective price."""

    @pytest.mark.asyncio
    async def test_fixed_price_returns_fixed_price_cents(self):
        rows = [
            _make_contract_row(
                contract_id="contract-fixed",
                contract_type="fixed_price",
                fixed_price_cents=325,
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        assert isinstance(resolution, PriceResolution)
        assert resolution.effective_price_cents == 325
        assert resolution.contract_id == "contract-fixed"
        assert resolution.contract_type == "fixed_price"
        assert resolution.market_price_cents == 410
        # Split-line fields are not populated by Task 4.2.
        assert resolution.split_gallons_at_contract_price is None
        assert resolution.split_gallons_at_market_price is None

    @pytest.mark.asyncio
    async def test_fixed_price_ignores_market_even_when_market_is_lower(self):
        """The fixed_price contract locks the price both ways."""
        rows = [
            _make_contract_row(
                contract_id="contract-fixed",
                contract_type="fixed_price",
                fixed_price_cents=325,
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=200,  # below the fixed price
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        assert resolution.effective_price_cents == 325

    @pytest.mark.asyncio
    async def test_cap_price_returns_min_of_market_and_cap(self):
        rows = [
            _make_contract_row(
                contract_id="contract-cap",
                contract_type="cap_price",
                price_cap_cents=340,
                fixed_price_cents=None,
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        # Market above the cap → cap wins.
        above = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )
        assert above.effective_price_cents == 340
        assert above.contract_type == "cap_price"

        # Market below the cap → market wins (customer enjoys downside).
        below = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=300,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )
        assert below.effective_price_cents == 300

    @pytest.mark.asyncio
    async def test_cap_price_equal_to_market_returns_that_value(self):
        rows = [
            _make_contract_row(
                contract_id="contract-cap",
                contract_type="cap_price",
                price_cap_cents=340,
                fixed_price_cents=None,
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=340,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        assert resolution.effective_price_cents == 340

    @pytest.mark.asyncio
    async def test_collar_clamps_between_floor_and_cap(self):
        rows = [
            _make_contract_row(
                contract_id="contract-collar",
                contract_type="collar",
                price_cap_cents=340,
                price_floor_cents=290,
                fixed_price_cents=None,
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        # Below floor → floor wins.
        below = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=250,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )
        assert below.effective_price_cents == 290
        assert below.contract_type == "collar"

        # Above cap → cap wins.
        above = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )
        assert above.effective_price_cents == 340

        # Within window → market wins.
        inside = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=310,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )
        assert inside.effective_price_cents == 310

    @pytest.mark.asyncio
    async def test_collar_at_exact_floor_and_cap_bounds(self):
        rows = [
            _make_contract_row(
                contract_id="contract-collar",
                contract_type="collar",
                price_cap_cents=340,
                price_floor_cents=290,
                fixed_price_cents=None,
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        at_floor = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=290,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )
        assert at_floor.effective_price_cents == 290

        at_cap = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=340,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )
        assert at_cap.effective_price_cents == 340


# ---------------------------------------------------------------------------
# resolve_price — no contract fall-through (Req 3.8)
# ---------------------------------------------------------------------------


class TestResolvePriceFallThrough:
    """When no active contract exists, resolve_price echoes the market price."""

    @pytest.mark.asyncio
    async def test_no_contract_returns_market_price_with_null_provenance(self):
        es = _FakeESService(rows=[])
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        assert resolution.effective_price_cents == 410
        assert resolution.contract_id is None
        assert resolution.contract_type is None
        assert resolution.market_price_cents == 410

    @pytest.mark.asyncio
    async def test_expired_contract_falls_through_to_market(self):
        rows = [
            _make_contract_row(
                contract_id="contract-old",
                start_date="2025-01-01",
                end_date="2025-12-31",
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        assert resolution.effective_price_cents == 410
        assert resolution.contract_id is None

    @pytest.mark.asyncio
    async def test_non_active_status_falls_through_to_market(self):
        rows = [
            _make_contract_row(
                contract_id="contract-exhausted",
                status="exhausted",
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        assert resolution.effective_price_cents == 410
        assert resolution.contract_id is None

    @pytest.mark.asyncio
    async def test_tenant_isolation_in_resolve_price(self):
        """A contract for another tenant must not drive the resolution."""
        rows = [
            _make_contract_row(
                contract_id="contract-other",
                tenant_id="tenant-2",
                contract_type="fixed_price",
                fixed_price_cents=325,
            )
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        # Tenant-1 sees no contract → market price echoed back.
        assert resolution.effective_price_cents == 410
        assert resolution.contract_id is None


# ---------------------------------------------------------------------------
# resolve_price — input validation
# ---------------------------------------------------------------------------


class TestResolvePriceInputValidation:
    @pytest.mark.asyncio
    async def test_negative_market_price_is_rejected(self):
        service = PriceProtectionService(_FakeESService(), "tenant-1")

        with pytest.raises(ValueError):
            await service.resolve_price(
                customer_id="cust-1",
                product_code="HEATING_OIL",
                market_price_cents=-1,
                gallons=500.0,
                effective_date=date(2026, 6, 1),
            )

    @pytest.mark.asyncio
    async def test_negative_gallons_is_rejected(self):
        service = PriceProtectionService(_FakeESService(), "tenant-1")

        with pytest.raises(ValueError):
            await service.resolve_price(
                customer_id="cust-1",
                product_code="HEATING_OIL",
                market_price_cents=410,
                gallons=-1.0,
                effective_date=date(2026, 6, 1),
            )

    @pytest.mark.asyncio
    async def test_non_int_market_price_is_rejected(self):
        service = PriceProtectionService(_FakeESService(), "tenant-1")

        with pytest.raises(ValueError):
            await service.resolve_price(
                customer_id="cust-1",
                product_code="HEATING_OIL",
                market_price_cents=410.5,  # type: ignore[arg-type]
                gallons=500.0,
                effective_date=date(2026, 6, 1),
            )


# ---------------------------------------------------------------------------
# _dispatch_contract_price — pure helper
# ---------------------------------------------------------------------------


class TestDispatchContractPrice:
    """Exercise the pure dispatch helper directly for clarity."""

    def _make_contract(
        self,
        *,
        contract_type: str = "fixed_price",
        fixed_price_cents: Optional[int] = None,
        price_cap_cents: Optional[int] = None,
        price_floor_cents: Optional[int] = None,
    ) -> PriceProtectionContract:
        payload = {
            "contract_id": "contract-dispatch",
            "tenant_id": "tenant-1",
            "customer_id": "cust-1",
            "account_id": "acct-1",
            "product_code": "HEATING_OIL",
            "contract_type": contract_type,
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 12, 31),
            "contracted_gallons": 10_000.0,
            "remaining_gallons": 10_000.0,
            "price_cap_cents": price_cap_cents,
            "price_floor_cents": price_floor_cents,
            "fixed_price_cents": fixed_price_cents,
            "status": "active",
            "version": 0,
        }
        return PriceProtectionContract(**payload)

    def test_fixed_price_returns_fixed_price_cents(self):
        contract = self._make_contract(
            contract_type="fixed_price", fixed_price_cents=325
        )

        assert (
            PriceProtectionService._dispatch_contract_price(contract, 410)
            == 325
        )
        assert (
            PriceProtectionService._dispatch_contract_price(contract, 200)
            == 325
        )

    def test_cap_price_min_of_market_and_cap(self):
        contract = self._make_contract(
            contract_type="cap_price", price_cap_cents=340
        )

        assert (
            PriceProtectionService._dispatch_contract_price(contract, 410)
            == 340
        )
        assert (
            PriceProtectionService._dispatch_contract_price(contract, 300)
            == 300
        )

    def test_collar_clamps_between_floor_and_cap(self):
        contract = self._make_contract(
            contract_type="collar",
            price_cap_cents=340,
            price_floor_cents=290,
        )

        assert (
            PriceProtectionService._dispatch_contract_price(contract, 250)
            == 290
        )
        assert (
            PriceProtectionService._dispatch_contract_price(contract, 410)
            == 340
        )
        assert (
            PriceProtectionService._dispatch_contract_price(contract, 310)
            == 310
        )
