"""Unit tests for :meth:`PriceProtectionService.resolve_price` split-line logic.

Covers Task 4.4 of the Fuel Compliance Backbone spec: when a delivery's
``gallons`` exceed a contract's ``remaining_gallons``, ``resolve_price``
populates the ``split_gallons_at_contract_price`` and
``split_gallons_at_market_price`` fields on :class:`PriceResolution`
so the caller can emit a pair of invoice lines — contracted portion at
the contract price, excess at the market price.

The tests use the same async fake ES pattern as
``test_price_protection_service.py`` so the query shape emitted by
``find_active_contract`` is exercised end-to-end.

Validates: Requirement 3.5
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pytest

from commerce.services.price_protection_service import (
    PriceProtectionService,
    PriceResolution,
)


# ---------------------------------------------------------------------------
# Fake ES service — same shape as the Task 4.2 / 4.3 suites
# ---------------------------------------------------------------------------


class _FakeESService:
    """Async ES stand-in that applies the same filters a real ES cluster would.

    Only ``search_documents`` is needed for the split-line tests —
    ``resolve_price`` does not call ``update_document``; the caller
    (``SalesPricingEngine`` in Task 4.8) is responsible for calling
    :meth:`PriceProtectionService.decrement_gallons` after the invoice
    is finalized.
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

        # Tenant filter lives on the outer bool.filter.
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
# Row builder
# ---------------------------------------------------------------------------


def _make_contract_row(
    *,
    contract_id: str = "contract-split",
    tenant_id: str = "tenant-1",
    customer_id: str = "cust-1",
    account_id: str = "acct-1",
    product_code: str = "HEATING_OIL",
    contract_type: str = "fixed_price",
    start_date: str = "2026-01-01",
    end_date: str = "2026-12-31",
    contracted_gallons: float = 1_000.0,
    remaining_gallons: Optional[float] = None,
    price_cap_cents: Optional[int] = None,
    price_floor_cents: Optional[int] = None,
    fixed_price_cents: Optional[int] = None,
    status: str = "active",
    version: int = 0,
) -> Dict[str, Any]:
    """Build a ``price_protection_contracts`` ``_source`` row.

    Defaults produce a valid ``fixed_price`` contract with
    ``remaining_gallons == contracted_gallons``. Tests override
    ``remaining_gallons`` / ``contract_type`` / price fields to
    exercise the split-line boundary for each contract type.
    """
    if remaining_gallons is None:
        remaining_gallons = contracted_gallons

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
        "notes": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Delivery fits entirely within the remainder → no split
# ---------------------------------------------------------------------------


class TestDeliveryFitsWithinRemaining:
    """Single-price path: delivery <= remaining_gallons → no split."""

    @pytest.mark.asyncio
    async def test_fits_well_within_remaining_no_split(self):
        """``gallons`` strictly below ``remaining_gallons`` → no split."""
        row = _make_contract_row(
            contracted_gallons=1_000.0,
            remaining_gallons=500.0,
            contract_type="fixed_price",
            fixed_price_cents=325,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=200.0,
            effective_date=date(2026, 6, 1),
        )

        assert isinstance(resolution, PriceResolution)
        assert resolution.effective_price_cents == 325
        assert resolution.contract_id == "contract-split"
        assert resolution.contract_type == "fixed_price"
        # No split: both fields must stay None so the caller bills the
        # whole delivery at the contract price.
        assert resolution.split_gallons_at_contract_price is None
        assert resolution.split_gallons_at_market_price is None
        assert resolution.market_price_cents == 410

    @pytest.mark.asyncio
    async def test_exact_match_is_not_split(self):
        """``gallons == remaining_gallons`` → single-price path (edge case).

        The whole delivery fits within the contract remainder, so the
        caller bills at the contract price and the daily lifecycle cron
        (Task 4.5) later transitions the contract to ``exhausted``.
        """
        row = _make_contract_row(
            contracted_gallons=1_000.0,
            remaining_gallons=500.0,
            contract_type="fixed_price",
            fixed_price_cents=325,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        assert resolution.effective_price_cents == 325
        assert resolution.split_gallons_at_contract_price is None
        assert resolution.split_gallons_at_market_price is None


# ---------------------------------------------------------------------------
# Delivery exceeds remaining → split populated
# ---------------------------------------------------------------------------


class TestDeliveryExceedsRemainingFixedPrice:
    """Split-line path for ``fixed_price`` contracts (Req 3.5)."""

    @pytest.mark.asyncio
    async def test_split_gallons_populated_correctly(self):
        """Exceeds remainder → split fields carry the partition."""
        row = _make_contract_row(
            contracted_gallons=1_000.0,
            remaining_gallons=300.0,
            contract_type="fixed_price",
            fixed_price_cents=325,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        # Contracted portion at contract price; excess bills at market.
        assert resolution.effective_price_cents == 325
        assert resolution.contract_id == "contract-split"
        assert resolution.contract_type == "fixed_price"
        assert resolution.market_price_cents == 410
        assert resolution.split_gallons_at_contract_price == pytest.approx(300.0)
        assert resolution.split_gallons_at_market_price == pytest.approx(200.0)
        # Partition must sum back to the requested volume.
        assert (
            resolution.split_gallons_at_contract_price
            + resolution.split_gallons_at_market_price
            == pytest.approx(500.0)
        )

    @pytest.mark.asyncio
    async def test_exhausted_contract_with_zero_remaining(self):
        """``remaining_gallons == 0`` on active contract → all excess at market.

        A contract can sit at ``remaining_gallons == 0`` after a same-day
        decrement that hasn't yet been picked up by the Task 4.5
        lifecycle cron. The entire delivery bills at market with the
        contract-portion split carrying zero gallons.
        """
        row = _make_contract_row(
            contracted_gallons=1_000.0,
            remaining_gallons=0.0,
            contract_type="fixed_price",
            fixed_price_cents=325,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        assert resolution.contract_id == "contract-split"
        assert resolution.split_gallons_at_contract_price == pytest.approx(0.0)
        assert resolution.split_gallons_at_market_price == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Split for cap_price and collar contracts
# ---------------------------------------------------------------------------


class TestSplitForCapPrice:
    """Split-line path for ``cap_price`` contracts."""

    @pytest.mark.asyncio
    async def test_cap_price_split_market_above_cap(self):
        """Excess above cap: contracted portion at cap, excess at market."""
        row = _make_contract_row(
            contracted_gallons=1_000.0,
            remaining_gallons=300.0,
            contract_type="cap_price",
            price_cap_cents=340,
            fixed_price_cents=None,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        # min(410, 340) → 340 on the contracted portion.
        assert resolution.effective_price_cents == 340
        assert resolution.contract_type == "cap_price"
        assert resolution.split_gallons_at_contract_price == pytest.approx(300.0)
        assert resolution.split_gallons_at_market_price == pytest.approx(200.0)

    @pytest.mark.asyncio
    async def test_cap_price_split_market_below_cap(self):
        """Market below cap: contract price == market; split still produced."""
        row = _make_contract_row(
            contracted_gallons=1_000.0,
            remaining_gallons=300.0,
            contract_type="cap_price",
            price_cap_cents=340,
            fixed_price_cents=None,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=300,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        # Cap gives customer the downside — contracted portion bills at
        # market since market < cap. Split fields must still be
        # populated so the caller emits the pair of lines (the
        # contract-provenance trail is preserved even when both lines
        # end up at the same price).
        assert resolution.effective_price_cents == 300
        assert resolution.split_gallons_at_contract_price == pytest.approx(300.0)
        assert resolution.split_gallons_at_market_price == pytest.approx(200.0)


class TestSplitForCollar:
    """Split-line path for ``collar`` contracts."""

    @pytest.mark.asyncio
    async def test_collar_split_market_below_floor(self):
        """Market below floor: contracted portion at floor, excess at market."""
        row = _make_contract_row(
            contracted_gallons=1_000.0,
            remaining_gallons=300.0,
            contract_type="collar",
            price_cap_cents=340,
            price_floor_cents=290,
            fixed_price_cents=None,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=250,
            gallons=500.0,
            effective_date=date(2026, 6, 1),
        )

        # Floor wins for the contracted portion; excess bills at raw
        # market (which is below the floor).
        assert resolution.effective_price_cents == 290
        assert resolution.contract_type == "collar"
        assert resolution.market_price_cents == 250
        assert resolution.split_gallons_at_contract_price == pytest.approx(300.0)
        assert resolution.split_gallons_at_market_price == pytest.approx(200.0)

    @pytest.mark.asyncio
    async def test_collar_split_market_above_cap(self):
        """Market above cap: contracted portion at cap, excess at market."""
        row = _make_contract_row(
            contracted_gallons=1_000.0,
            remaining_gallons=400.0,
            contract_type="collar",
            price_cap_cents=340,
            price_floor_cents=290,
            fixed_price_cents=None,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=600.0,
            effective_date=date(2026, 6, 1),
        )

        assert resolution.effective_price_cents == 340
        assert resolution.split_gallons_at_contract_price == pytest.approx(400.0)
        assert resolution.split_gallons_at_market_price == pytest.approx(200.0)

    @pytest.mark.asyncio
    async def test_collar_split_market_within_band(self):
        """Market within band: effective == market; split still produced."""
        row = _make_contract_row(
            contracted_gallons=1_000.0,
            remaining_gallons=400.0,
            contract_type="collar",
            price_cap_cents=340,
            price_floor_cents=290,
            fixed_price_cents=None,
        )
        es = _FakeESService([row])
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=310,
            gallons=600.0,
            effective_date=date(2026, 6, 1),
        )

        assert resolution.effective_price_cents == 310
        # Split fields still populated so the invoice carries the
        # contract provenance for the contracted portion.
        assert resolution.split_gallons_at_contract_price == pytest.approx(400.0)
        assert resolution.split_gallons_at_market_price == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# No contract → no split regardless of gallons
# ---------------------------------------------------------------------------


class TestNoContractNoSplit:
    """When no active contract matches, split fields stay ``None``."""

    @pytest.mark.asyncio
    async def test_no_contract_no_split_regardless_of_gallons(self):
        es = _FakeESService(rows=[])
        service = PriceProtectionService(es, "tenant-1")

        resolution = await service.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            market_price_cents=410,
            gallons=5_000.0,  # arbitrary large volume
            effective_date=date(2026, 6, 1),
        )

        assert resolution.effective_price_cents == 410
        assert resolution.contract_id is None
        assert resolution.contract_type is None
        assert resolution.split_gallons_at_contract_price is None
        assert resolution.split_gallons_at_market_price is None
