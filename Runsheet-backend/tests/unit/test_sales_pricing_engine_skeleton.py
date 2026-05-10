"""Unit tests for the :class:`SalesPricingEngine` skeleton (Task 4.8).

Task 4.8 of the Fuel Compliance Backbone spec is narrowly scoped: wire
:class:`PriceProtectionService` as the *first-priority* resolver inside
:class:`SalesPricingEngine`. The strategy dispatch
(posted_price / rack_plus_margin / tiered_volume / cost_plus) is tracked
by Tasks 5.2–5.9. These tests lock in the three guarantees the skeleton
owns:

1. The constructor rejects empty / whitespace / non-string tenant IDs
   (mirrors :class:`PriceProtectionService` input discipline).
2. When a :class:`PriceProtectionService` is injected *and* returns a
   matched contract, :meth:`SalesPricingEngine.resolve_price` returns
   that :class:`PriceResolution` verbatim (contract price wins over any
   future strategy dispatch).
3. When no contract matches (``contract_id is None`` on the resolver
   output), or when no resolver is wired, :meth:`resolve_price` raises
   :class:`NotImplementedError` so the unfinished Phase 5 strategy
   branch stays loud.

Validates: Requirement 3.8
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

import pytest

from commerce.services.price_protection_service import PriceResolution
from commerce.services.sales_pricing_engine import SalesPricingEngine


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubPriceProtectionService:
    """Minimal async stand-in for :class:`PriceProtectionService`.

    Records every ``resolve_price`` call so assertions can verify the
    engine forwarded the full customer / product / market / gallons /
    date payload unchanged, and returns a canned
    :class:`PriceResolution` so the engine's dispatch behavior is
    exercised without touching Elasticsearch.
    """

    def __init__(self, result: PriceResolution) -> None:
        self._result = result
        self.calls: list[dict] = []

    async def resolve_price(
        self,
        customer_id: str,
        product_code: str,
        market_price_cents: int,
        gallons: float,
        effective_date: date,
    ) -> PriceResolution:
        self.calls.append(
            {
                "customer_id": customer_id,
                "product_code": product_code,
                "market_price_cents": market_price_cents,
                "gallons": gallons,
                "effective_date": effective_date,
            }
        )
        return self._result


class _SentinelESService:
    """Placeholder ES service returning an empty hit set.

    Task 4.8's original skeleton never touched the ES handle. Task 5.2
    added the ``pricing_rules`` lookup in
    :meth:`SalesPricingEngine.resolve_rule`, so the fall-through paths
    these tests exercise now call ``search_documents`` on their way
    to the "no rule matched" branch. Returning an empty hit set keeps
    the fall-through tests valid: the engine still raises
    :class:`NotImplementedError`, it just now raises from the "no
    rule matched" branch (Task 5.8 will wire
    ``pricing.no_rule_matched``) instead of from the Task 4.8 strategy
    stub that Task 5.2 replaced.
    """

    async def search_documents(
        self,
        index: str,
        query: dict,
        size: int = 100,
    ) -> dict:
        return {"hits": {"hits": []}}


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    """Tenant ID discipline mirrors :class:`PriceProtectionService`."""

    def test_non_empty_tenant_id_is_required(self):
        with pytest.raises(ValueError):
            SalesPricingEngine(_SentinelESService(), "")

        with pytest.raises(ValueError):
            SalesPricingEngine(_SentinelESService(), "   ")

    def test_non_string_tenant_id_is_rejected(self):
        with pytest.raises(ValueError):
            SalesPricingEngine(_SentinelESService(), 42)  # type: ignore[arg-type]

    def test_price_protection_service_is_optional(self):
        engine = SalesPricingEngine(_SentinelESService(), "tenant-1")
        # Accessing the private attribute is acceptable in unit tests
        # and documents the contract: the field defaults to None so
        # the constructor does not force callers that have not yet
        # reached Task 4.8 wiring to build a stub.
        assert engine._price_protection_service is None


# ---------------------------------------------------------------------------
# First-priority dispatch (Req 3.8)
# ---------------------------------------------------------------------------


class TestPriceProtectionFirstPriority:
    """When a contract matches, its price wins unconditionally."""

    @pytest.mark.asyncio
    async def test_matched_contract_resolution_is_returned_verbatim(self):
        # Contract resolved to 325¢ with a valid contract_id —
        # simulating the Price_Protection_Service's "fixed_price"
        # dispatch (Req 3.3). The skeleton must hand this back unmodified
        # and MUST NOT fall through to the Phase 5 strategy dispatch.
        canned = PriceResolution(
            effective_price_cents=325,
            contract_id="contract-abc",
            contract_type="fixed_price",
            market_price_cents=340,
        )
        stub = _StubPriceProtectionService(canned)
        engine = SalesPricingEngine(
            _SentinelESService(),
            "tenant-1",
            price_protection_service=stub,
        )

        resolution = await engine.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            gallons=500.0,
            terminal_id="TERMINAL-A",
            route_miles=42.0,
            effective_date=date(2026, 6, 1),
            market_price_cents=340,
        )

        assert resolution is canned
        # Forwarded the exact inputs to the Price_Protection_Service:
        # tasks 5.x are going to depend on this payload being intact.
        assert len(stub.calls) == 1
        call = stub.calls[0]
        assert call == {
            "customer_id": "cust-1",
            "product_code": "HEATING_OIL",
            "market_price_cents": 340,
            "gallons": 500.0,
            "effective_date": date(2026, 6, 1),
        }

    @pytest.mark.asyncio
    async def test_contract_wins_even_when_market_is_cheaper(self):
        # Collar clamp kept the effective price above the market —
        # the contract still wins. This is the scenario that settlement
        # variance (Task 4.6) flags as a customer loss, but the engine
        # still honors the contract.
        canned = PriceResolution(
            effective_price_cents=290,
            contract_id="contract-collar",
            contract_type="collar",
            market_price_cents=275,
        )
        stub = _StubPriceProtectionService(canned)
        engine = SalesPricingEngine(
            _SentinelESService(),
            "tenant-1",
            price_protection_service=stub,
        )

        resolution = await engine.resolve_price(
            customer_id="cust-2",
            product_code="DIESEL",
            gallons=1000.0,
            terminal_id="TERMINAL-B",
            route_miles=15.0,
            effective_date=date(2026, 6, 1),
            market_price_cents=275,
        )

        assert resolution.effective_price_cents == 290
        assert resolution.contract_id == "contract-collar"


# ---------------------------------------------------------------------------
# Fall-through paths → strategy dispatch stub (Tasks 5.2–5.9)
# ---------------------------------------------------------------------------


class TestFallThrough:
    """Everything that is not a matched contract falls through to rule lookup."""

    @pytest.mark.asyncio
    async def test_no_contract_match_raises_pricing_error(self):
        # Resolver signalled "no contract" per Req 3.8 by returning
        # contract_id=None and echoing market_price_cents. The engine
        # falls through to rule lookup which finds nothing and raises
        # PricingNoRuleMatchedError (Task 5.8 / Req 11.7).
        from commerce.services.sales_pricing_engine import (
            PricingNoRuleMatchedError,
        )

        fall_through = PriceResolution(
            effective_price_cents=340,
            contract_id=None,
            contract_type=None,
            market_price_cents=340,
        )
        stub = _StubPriceProtectionService(fall_through)
        engine = SalesPricingEngine(
            _SentinelESService(),
            "tenant-1",
            price_protection_service=stub,
        )

        with pytest.raises(PricingNoRuleMatchedError):
            await engine.resolve_price(
                customer_id="cust-3",
                product_code="DIESEL",
                gallons=100.0,
                terminal_id="TERMINAL-C",
                route_miles=25.0,
                effective_date=date(2026, 6, 1),
                market_price_cents=340,
            )

        # The resolver was still consulted (first-priority contract check
        # must run before the strategy dispatch) — we just had no match.
        assert len(stub.calls) == 1

    @pytest.mark.asyncio
    async def test_no_resolver_wired_raises_pricing_error(self):
        # Without a price-protection resolver the engine falls through
        # to rule lookup which finds nothing and raises
        # PricingNoRuleMatchedError.
        from commerce.services.sales_pricing_engine import (
            PricingNoRuleMatchedError,
        )

        engine = SalesPricingEngine(_SentinelESService(), "tenant-1")

        with pytest.raises(PricingNoRuleMatchedError):
            await engine.resolve_price(
                customer_id="cust-4",
                product_code="GASOLINE",
                gallons=250.0,
                terminal_id="TERMINAL-D",
                route_miles=12.0,
                effective_date=date(2026, 6, 1),
            )

    @pytest.mark.asyncio
    async def test_missing_market_price_with_resolver_raises_not_implemented(
        self,
    ):
        # The resolver needs a market price to dispatch on
        # ``contract_type`` and to build split-line outputs. Task 5.4
        # will supply it from the OPIS rack lookup; for now the skeleton
        # surfaces the gap rather than silently passing zero.
        canned = PriceResolution(
            effective_price_cents=325,
            contract_id="contract-abc",
            contract_type="fixed_price",
            market_price_cents=0,
        )
        stub = _StubPriceProtectionService(canned)
        engine = SalesPricingEngine(
            _SentinelESService(),
            "tenant-1",
            price_protection_service=stub,
        )

        with pytest.raises(NotImplementedError):
            await engine.resolve_price(
                customer_id="cust-5",
                product_code="HEATING_OIL",
                gallons=300.0,
                terminal_id="TERMINAL-E",
                route_miles=8.0,
                effective_date=date(2026, 6, 1),
            )

        # The resolver must not be consulted if we had to bail out
        # before the dispatch.
        assert stub.calls == []
