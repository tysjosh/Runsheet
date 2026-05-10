"""Unit tests for :meth:`PriceProtectionService.compute_settlement_variance`
and the related portfolio aggregation / batch convenience helpers.

Covers Task 4.6 of the Fuel Compliance Backbone spec, which adds
settlement-variance reporting to :class:`PriceProtectionService`:

* :meth:`compute_settlement_variance` — pure static computation of
  ``(market_price_cents - effective_price_cents) * gallons`` rounded
  to integer cents (Req 3.7).
* :meth:`compute_portfolio_variance` — async aggregator that sums
  per-delivery variances for a contract into a total + per-delivery
  breakdown.
* :meth:`iter_contract_invoice_events` — batch-friendly scan of the
  invoice-events index that feeds ``compute_portfolio_variance``.

The async tests use a minimal async fake ES stand-in so the query
shape emitted by the service is exercised end-to-end (tenant filter,
``payload.contract_id`` term, size cap) without a live cluster.

Validates: Requirement 3.7
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from commerce.services.price_protection_service import PriceProtectionService


# ---------------------------------------------------------------------------
# Fake ES service — matches the async surface used by the Task 4.2/4.3 tests
# ---------------------------------------------------------------------------


class _FakeESService:
    """Async ES stand-in that returns canned invoice-event rows.

    Applies the tenant filter and the ``payload.contract_id`` term
    clause the same way a real cluster would, so the query emitted by
    :meth:`iter_contract_invoice_events` is round-tripped end-to-end.
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

        contract_term: Optional[str] = None
        for clause in inner_filter:
            term = clause.get("term") if isinstance(clause, dict) else None
            if isinstance(term, dict) and "payload.contract_id" in term:
                contract_term = term["payload.contract_id"]
                break

        matching: List[Dict[str, Any]] = []
        for row in self._rows:
            if tenant_term is not None and row.get("tenant_id") != tenant_term:
                continue
            payload = row.get("payload") or {}
            if (
                contract_term is not None
                and payload.get("contract_id") != contract_term
            ):
                continue
            matching.append(row)

        return {"hits": {"hits": [{"_source": row} for row in matching]}}


# ---------------------------------------------------------------------------
# compute_settlement_variance — Req 3.7 core cases
# ---------------------------------------------------------------------------


class TestComputeSettlementVariance:
    """Unit-test the core per-delivery variance computation.

    ``compute_settlement_variance`` is a pure static method, so these
    tests exercise it directly on the class without instantiating the
    service.
    """

    def test_positive_variance_customer_saved_money(self):
        """Market above contract → customer saved money → positive variance.

        Spec example: market 410¢, contract 340¢, 1000 gallons →
        +70_000 cents. The customer's bill was locked at 340¢ while
        the market climbed to 410¢.
        """
        variance = PriceProtectionService.compute_settlement_variance(
            market_price_cents=410,
            effective_price_cents=340,
            gallons=1000.0,
        )

        assert variance == 70_000
        assert isinstance(variance, int)

    def test_negative_variance_contract_cost_customer_more(self):
        """Market below fixed contract → customer overpaid → negative variance.

        Spec example: market 200¢, contract 325¢ (fixed_price), 1000
        gallons → -125_000 cents. The contract's locked price cost
        the customer 125¢/gal more than the prevailing market.
        """
        variance = PriceProtectionService.compute_settlement_variance(
            market_price_cents=200,
            effective_price_cents=325,
            gallons=1000.0,
        )

        assert variance == -125_000
        assert isinstance(variance, int)

    def test_zero_gallons_yields_zero_variance(self):
        """Zero gallons short-circuits any spread to zero variance."""
        variance = PriceProtectionService.compute_settlement_variance(
            market_price_cents=410,
            effective_price_cents=340,
            gallons=0.0,
        )

        assert variance == 0

    def test_zero_spread_yields_zero_variance(self):
        """Contract price equal to market → no settlement delta."""
        variance = PriceProtectionService.compute_settlement_variance(
            market_price_cents=350,
            effective_price_cents=350,
            gallons=1_500.0,
        )

        assert variance == 0

    def test_fractional_gallons_are_rounded_to_nearest_cent(self):
        """Fractional gallons × integer spread collapses to nearest int.

        A 70¢ spread × 1337.5 gallons = 93_625 cents exactly (no
        rounding needed). A 75¢ spread × 1337.5 gallons = 100_312.5
        cents → rounds to 100_312 under banker's rounding (half-even),
        which is the documented behaviour.
        """
        exact = PriceProtectionService.compute_settlement_variance(
            market_price_cents=410,
            effective_price_cents=340,  # spread = 70
            gallons=1337.5,
        )
        assert exact == 93_625
        assert isinstance(exact, int)

        rounded = PriceProtectionService.compute_settlement_variance(
            market_price_cents=420,
            effective_price_cents=345,  # spread = 75
            gallons=1337.5,
        )
        # 75 * 1337.5 = 100312.5 → banker's rounding → 100312.
        assert rounded == 100_312
        assert isinstance(rounded, int)

    def test_returns_int_for_float_gallons(self):
        """Even when gallons is a float, the return type is ``int``."""
        variance = PriceProtectionService.compute_settlement_variance(
            market_price_cents=410,
            effective_price_cents=340,
            gallons=12.0,
        )
        assert variance == 840
        assert type(variance) is int  # noqa: E721 — strict identity check

    def test_large_gallons_does_not_overflow(self):
        """Python ints are arbitrary-precision — a million-gallon
        contract still returns the exact cents."""
        variance = PriceProtectionService.compute_settlement_variance(
            market_price_cents=500,
            effective_price_cents=100,
            gallons=1_000_000.0,
        )
        # spread 400¢ × 1_000_000 gallons = 400_000_000 cents
        assert variance == 400_000_000

    # Input validation --------------------------------------------------

    def test_negative_market_price_is_rejected(self):
        with pytest.raises(ValueError):
            PriceProtectionService.compute_settlement_variance(
                market_price_cents=-1,
                effective_price_cents=340,
                gallons=1000.0,
            )

    def test_negative_effective_price_is_rejected(self):
        with pytest.raises(ValueError):
            PriceProtectionService.compute_settlement_variance(
                market_price_cents=410,
                effective_price_cents=-1,
                gallons=1000.0,
            )

    def test_negative_gallons_is_rejected(self):
        with pytest.raises(ValueError):
            PriceProtectionService.compute_settlement_variance(
                market_price_cents=410,
                effective_price_cents=340,
                gallons=-1.0,
            )

    def test_non_int_market_price_is_rejected(self):
        with pytest.raises(ValueError):
            PriceProtectionService.compute_settlement_variance(
                market_price_cents=410.0,  # type: ignore[arg-type]
                effective_price_cents=340,
                gallons=1000.0,
            )

    def test_bool_is_rejected_for_int_fields(self):
        """``bool`` must not slip through as 0/1 cents."""
        with pytest.raises(ValueError):
            PriceProtectionService.compute_settlement_variance(
                market_price_cents=True,  # type: ignore[arg-type]
                effective_price_cents=340,
                gallons=1000.0,
            )
        with pytest.raises(ValueError):
            PriceProtectionService.compute_settlement_variance(
                market_price_cents=410,
                effective_price_cents=False,  # type: ignore[arg-type]
                gallons=1000.0,
            )

    def test_non_finite_gallons_is_rejected(self):
        with pytest.raises(ValueError):
            PriceProtectionService.compute_settlement_variance(
                market_price_cents=410,
                effective_price_cents=340,
                gallons=float("inf"),
            )
        with pytest.raises(ValueError):
            PriceProtectionService.compute_settlement_variance(
                market_price_cents=410,
                effective_price_cents=340,
                gallons=float("nan"),
            )


# ---------------------------------------------------------------------------
# compute_portfolio_variance — aggregation across deliveries
# ---------------------------------------------------------------------------


class TestComputePortfolioVariance:
    """Aggregate per-delivery variances into a contract-level total."""

    def _service(self) -> PriceProtectionService:
        return PriceProtectionService(_FakeESService(), "tenant-1")

    @pytest.mark.asyncio
    async def test_empty_deliveries_produces_zero_totals(self):
        service = self._service()

        report = await service.compute_portfolio_variance(
            contract_id="contract-1",
            deliveries=[],
        )

        assert report["contract_id"] == "contract-1"
        assert report["total_variance_cents"] == 0
        assert report["total_gallons"] == 0.0
        assert report["delivery_count"] == 0
        assert report["breakdown"] == []

    @pytest.mark.asyncio
    async def test_mixed_positive_and_negative_deliveries_net_out(self):
        """A portfolio with both savings and losses reports the net."""
        service = self._service()

        deliveries = [
            {
                "delivery_id": "del-1",
                "market_price_cents": 410,
                "effective_price_cents": 340,
                "gallons": 1000.0,
            },  # +70_000
            {
                "delivery_id": "del-2",
                "market_price_cents": 200,
                "effective_price_cents": 325,
                "gallons": 1000.0,
            },  # -125_000
            {
                "delivery_id": "del-3",
                "market_price_cents": 350,
                "effective_price_cents": 350,
                "gallons": 500.0,
            },  # 0
        ]

        report = await service.compute_portfolio_variance(
            contract_id="contract-1",
            deliveries=deliveries,
        )

        assert report["contract_id"] == "contract-1"
        assert report["total_variance_cents"] == (70_000 - 125_000 + 0)
        assert report["total_gallons"] == 2_500.0
        assert report["delivery_count"] == 3
        assert [row["delivery_id"] for row in report["breakdown"]] == [
            "del-1",
            "del-2",
            "del-3",
        ]
        assert report["breakdown"][0]["variance_cents"] == 70_000
        assert report["breakdown"][1]["variance_cents"] == -125_000
        assert report["breakdown"][2]["variance_cents"] == 0

    @pytest.mark.asyncio
    async def test_malformed_delivery_is_skipped_and_logged(self):
        """Bad rows don't drop the entire report."""
        service = self._service()

        deliveries = [
            {
                "delivery_id": "del-1",
                "market_price_cents": 410,
                "effective_price_cents": 340,
                "gallons": 1000.0,
            },  # valid +70_000
            {
                "delivery_id": "del-bad",
                "market_price_cents": -5,  # rejected by the static helper
                "effective_price_cents": 340,
                "gallons": 1000.0,
            },
            "not-a-dict",  # skipped outright
        ]

        report = await service.compute_portfolio_variance(
            contract_id="contract-1",
            deliveries=deliveries,  # type: ignore[arg-type]
        )

        assert report["total_variance_cents"] == 70_000
        assert report["delivery_count"] == 1
        assert report["breakdown"][0]["delivery_id"] == "del-1"

    @pytest.mark.asyncio
    async def test_empty_contract_id_is_rejected(self):
        service = self._service()

        with pytest.raises(ValueError):
            await service.compute_portfolio_variance(
                contract_id="",
                deliveries=[],
            )
        with pytest.raises(ValueError):
            await service.compute_portfolio_variance(
                contract_id="   ",
                deliveries=[],
            )

    @pytest.mark.asyncio
    async def test_none_deliveries_is_rejected(self):
        service = self._service()

        with pytest.raises(ValueError):
            await service.compute_portfolio_variance(
                contract_id="contract-1",
                deliveries=None,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# iter_contract_invoice_events — batch-friendly convenience
# ---------------------------------------------------------------------------


class TestIterContractInvoiceEvents:
    """Scan the invoice-events index and yield the minimal delivery shape."""

    def _event_row(
        self,
        *,
        tenant_id: str = "tenant-1",
        contract_id: Optional[str] = "contract-1",
        delivery_id: Optional[str] = "del-1",
        market_price_cents: Optional[int] = 410,
        effective_price_cents: Optional[int] = 340,
        gallons: Optional[float] = 1000.0,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if contract_id is not None:
            payload["contract_id"] = contract_id
        if delivery_id is not None:
            payload["delivery_id"] = delivery_id
        if market_price_cents is not None:
            payload["market_price_cents"] = market_price_cents
        if effective_price_cents is not None:
            payload["effective_price_cents"] = effective_price_cents
        if gallons is not None:
            payload["gallons"] = gallons
        return {"tenant_id": tenant_id, "payload": payload}

    @pytest.mark.asyncio
    async def test_yields_matching_delivery_dicts(self):
        rows = [
            self._event_row(delivery_id="del-1"),
            self._event_row(delivery_id="del-2", gallons=500.0),
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        events = [event async for event in
                  service.iter_contract_invoice_events("contract-1")]

        assert len(events) == 2
        assert {event["delivery_id"] for event in events} == {"del-1", "del-2"}
        assert events[0]["market_price_cents"] == 410
        assert events[0]["effective_price_cents"] == 340
        assert events[0]["gallons"] == 1000.0

    @pytest.mark.asyncio
    async def test_tenant_isolation(self):
        """Events for a different tenant must not be yielded."""
        rows = [
            self._event_row(tenant_id="tenant-2", delivery_id="del-other"),
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        events = [event async for event in
                  service.iter_contract_invoice_events("contract-1")]

        assert events == []

    @pytest.mark.asyncio
    async def test_other_contract_events_are_excluded(self):
        rows = [
            self._event_row(contract_id="contract-other"),
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        events = [event async for event in
                  service.iter_contract_invoice_events("contract-1")]

        assert events == []

    @pytest.mark.asyncio
    async def test_events_missing_required_fields_are_skipped(self):
        rows = [
            # Missing delivery_id.
            self._event_row(delivery_id=None),
            # Missing gallons.
            self._event_row(delivery_id="del-no-gallons", gallons=None),
            # Complete row.
            self._event_row(delivery_id="del-good"),
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        events = [event async for event in
                  service.iter_contract_invoice_events("contract-1")]

        assert len(events) == 1
        assert events[0]["delivery_id"] == "del-good"

    @pytest.mark.asyncio
    async def test_feeds_compute_portfolio_variance_end_to_end(self):
        """The iterator and the aggregator compose cleanly."""
        rows = [
            self._event_row(
                delivery_id="del-1",
                market_price_cents=410,
                effective_price_cents=340,
                gallons=1000.0,
            ),
            self._event_row(
                delivery_id="del-2",
                market_price_cents=200,
                effective_price_cents=325,
                gallons=1000.0,
            ),
        ]
        es = _FakeESService(rows)
        service = PriceProtectionService(es, "tenant-1")

        deliveries = [event async for event in
                      service.iter_contract_invoice_events("contract-1")]

        report = await service.compute_portfolio_variance(
            contract_id="contract-1",
            deliveries=deliveries,
        )

        assert report["total_variance_cents"] == (70_000 - 125_000)
        assert report["delivery_count"] == 2

    @pytest.mark.asyncio
    async def test_invalid_inputs_are_rejected(self):
        service = PriceProtectionService(_FakeESService(), "tenant-1")

        with pytest.raises(ValueError):
            async for _ in service.iter_contract_invoice_events(""):
                pass
        with pytest.raises(ValueError):
            async for _ in service.iter_contract_invoice_events(
                "contract-1", batch_size=0
            ):
                pass
        with pytest.raises(ValueError):
            async for _ in service.iter_contract_invoice_events(
                "contract-1", batch_size=-5
            ):
                pass
