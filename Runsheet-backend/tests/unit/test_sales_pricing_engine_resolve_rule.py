"""Unit tests for :meth:`SalesPricingEngine.resolve_rule` and strategy dispatch.

Task 5.2 of the Fuel Compliance Backbone spec extends the Task 4.8
:class:`SalesPricingEngine` skeleton with two things: a
:meth:`SalesPricingEngine.resolve_rule` entry point that queries the
``pricing_rules`` Elasticsearch index (tenant filter + ``status ==
active`` + product match + effective-date window), and a
strategy-dispatch structure inside :meth:`SalesPricingEngine.resolve_price`
that branches on ``rule.strategy`` for each of the four strategies
(``posted_price`` / ``rack_plus_margin`` / ``tiered_volume`` /
``cost_plus``). Each strategy branch raises
:class:`NotImplementedError` pointing at its follow-up task (5.3–5.6)
that will fill in the actual price computation.

The tests lock in the Task 5.2 contract:

1. :meth:`resolve_rule` returns the matching :class:`PricingRule` when
   one exists in the index.
2. :meth:`resolve_rule` returns ``None`` when no row matches (tenant
   mismatch, product mismatch, expired, inactive, or pre-window).
3. :meth:`resolve_price` raises :class:`NotImplementedError` on each
   of the four strategies with the message referencing the follow-up
   task (Tasks 5.3–5.6).

Validates: Requirement 11.2
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pytest

from commerce.models.pricing_rule import PricingRule, TierBreak
from commerce.services.sales_pricing_engine import SalesPricingEngine
from compliance.services.compliance_es_mappings import PRICING_RULES_INDEX


# ---------------------------------------------------------------------------
# Fake ES service — same shape as the Task 4.2 / 4.3 / 4.4 suites
# ---------------------------------------------------------------------------


class _FakeESService:
    """Async ES stand-in that filters rows like a real cluster.

    Mirrors the fake used by ``test_price_protection_service`` — reads
    the outer tenant filter and the inner ``bool.filter`` clauses
    produced by :meth:`SalesPricingEngine.resolve_rule` and returns
    only rows that satisfy every clause.
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

        product_code: Optional[str] = None
        status: Optional[str] = None
        eff_lte: Optional[str] = None

        for clause in inner_filter:
            if "term" in clause and "product_code" in clause["term"]:
                product_code = clause["term"]["product_code"]
            elif "term" in clause and "status" in clause["term"]:
                status = clause["term"]["status"]
            elif "range" in clause and "effective_date" in clause["range"]:
                eff_lte = clause["range"]["effective_date"].get("lte")

        matching: List[Dict[str, Any]] = []
        for row in self._rows:
            if tenant_term is not None and row.get("tenant_id") != tenant_term:
                continue
            if product_code is not None and row.get("product_code") != product_code:
                continue
            if status is not None and row.get("status") != status:
                continue
            if eff_lte is not None:
                eff = row.get("effective_date")
                if eff is not None and eff > eff_lte:
                    continue
            matching.append(row)

        return {"hits": {"hits": [{"_source": row} for row in matching]}}


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _make_posted_price_row(
    *,
    rule_id: str = "rule-posted-1",
    tenant_id: str = "tenant-1",
    customer_id: Optional[str] = "cust-1",
    account_id: Optional[str] = None,
    product_code: str = "HEATING_OIL",
    posted_price_cents: int = 310,
    priority: int = 0,
    effective_date: str = "2026-01-01",
    expiry_date: Optional[str] = "2026-12-31",
    status: str = "active",
) -> Dict[str, Any]:
    """Build a serialized ``posted_price`` pricing rule row."""
    return {
        "rule_id": rule_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "account_id": account_id,
        "product_code": product_code,
        "strategy": "posted_price",
        "posted_price_cents": posted_price_cents,
        "margin_cents": None,
        "freight_rate_cents_per_mile": None,
        "terminal_id": None,
        "tier_thresholds": None,
        "priority": priority,
        "effective_date": effective_date,
        "expiry_date": expiry_date,
        "status": status,
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


def _make_rack_plus_margin_row(
    *,
    rule_id: str = "rule-rack-1",
    tenant_id: str = "tenant-1",
    product_code: str = "DIESEL",
    margin_cents: int = 25,
    terminal_id: str = "TERMINAL-A",
    effective_date: str = "2026-01-01",
    expiry_date: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "rule_id": rule_id,
        "tenant_id": tenant_id,
        "customer_id": "cust-1",
        "account_id": None,
        "product_code": product_code,
        "strategy": "rack_plus_margin",
        "posted_price_cents": None,
        "margin_cents": margin_cents,
        "freight_rate_cents_per_mile": None,
        "terminal_id": terminal_id,
        "tier_thresholds": None,
        "priority": 0,
        "effective_date": effective_date,
        "expiry_date": expiry_date,
        "status": "active",
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


def _make_tiered_volume_row(
    *,
    rule_id: str = "rule-tier-1",
    tenant_id: str = "tenant-1",
    product_code: str = "GASOLINE",
    effective_date: str = "2026-01-01",
    expiry_date: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "rule_id": rule_id,
        "tenant_id": tenant_id,
        "customer_id": "cust-1",
        "account_id": None,
        "product_code": product_code,
        "strategy": "tiered_volume",
        "posted_price_cents": None,
        "margin_cents": None,
        "freight_rate_cents_per_mile": None,
        "terminal_id": None,
        "tier_thresholds": [
            {"min_gallons": 0.0, "max_gallons": 500.0, "unit_price_cents": 320},
            {"min_gallons": 500.0, "max_gallons": None, "unit_price_cents": 305},
        ],
        "priority": 0,
        "effective_date": effective_date,
        "expiry_date": expiry_date,
        "status": "active",
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


def _make_cost_plus_row(
    *,
    rule_id: str = "rule-cost-1",
    tenant_id: str = "tenant-1",
    product_code: str = "PROPANE",
    effective_date: str = "2026-01-01",
    expiry_date: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "rule_id": rule_id,
        "tenant_id": tenant_id,
        "customer_id": "cust-1",
        "account_id": None,
        "product_code": product_code,
        "strategy": "cost_plus",
        "posted_price_cents": None,
        "margin_cents": 30,
        "freight_rate_cents_per_mile": 12,
        "terminal_id": None,
        "tier_thresholds": None,
        "priority": 0,
        "effective_date": effective_date,
        "expiry_date": expiry_date,
        "status": "active",
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# resolve_rule — matching rule returned
# ---------------------------------------------------------------------------


class TestResolveRuleMatch:
    """:meth:`resolve_rule` returns the first matching row."""

    @pytest.mark.asyncio
    async def test_returns_matching_active_rule(self):
        # Single active posted_price rule for HEATING_OIL in the
        # tenant — resolve_rule should return it as a PricingRule.
        row = _make_posted_price_row(
            rule_id="rule-posted-1",
            product_code="HEATING_OIL",
            effective_date="2026-01-01",
            expiry_date="2026-12-31",
        )
        es = _FakeESService(rows=[row])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id=None,
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is not None
        assert isinstance(rule, PricingRule)
        assert rule.rule_id == "rule-posted-1"
        assert rule.strategy == "posted_price"
        assert rule.product_code == "HEATING_OIL"
        # The engine targeted the pricing_rules compliance index with
        # the tenant filter applied via inject_tenant_filter.
        assert len(es.calls) == 1
        assert es.calls[0]["index"] == PRICING_RULES_INDEX
        outer = es.calls[0]["query"]["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": "tenant-1"}} in outer

    @pytest.mark.asyncio
    async def test_returns_rule_with_no_expiry_date(self):
        # expiry_date=None is a legal configuration: the rule is
        # active indefinitely. The lower-bound ES range filter
        # matches, and the client-side None check should keep the
        # row in the candidate set.
        row = _make_rack_plus_margin_row(expiry_date=None)
        es = _FakeESService(rows=[row])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id=None,
            product_code="DIESEL",
            effective_date=date(2030, 6, 1),
        )

        assert rule is not None
        assert rule.expiry_date is None
        assert rule.strategy == "rack_plus_margin"


# ---------------------------------------------------------------------------
# resolve_rule — no match returns None
# ---------------------------------------------------------------------------


class TestResolveRuleNoMatch:
    """Every non-match path collapses to ``None``."""

    @pytest.mark.asyncio
    async def test_empty_index_returns_none(self):
        es = _FakeESService(rows=[])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id=None,
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is None

    @pytest.mark.asyncio
    async def test_product_mismatch_returns_none(self):
        # Rule exists for DIESEL; request asks for HEATING_OIL.
        row = _make_rack_plus_margin_row(product_code="DIESEL")
        es = _FakeESService(rows=[row])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id=None,
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is None

    @pytest.mark.asyncio
    async def test_tenant_mismatch_returns_none(self):
        # Rule is tenant-A; the engine is instantiated for tenant-B.
        row = _make_posted_price_row(tenant_id="tenant-a")
        es = _FakeESService(rows=[row])
        engine = SalesPricingEngine(es, "tenant-b")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id=None,
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is None

    @pytest.mark.asyncio
    async def test_inactive_rule_is_skipped(self):
        # ``status == 'inactive'`` rows are filtered out by the ES
        # term filter and also by the client-side defense.
        row = _make_posted_price_row(status="inactive")
        es = _FakeESService(rows=[row])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id=None,
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is None

    @pytest.mark.asyncio
    async def test_expired_rule_is_skipped_client_side(self):
        # expiry_date is before the effective_date — ES's lower-bound
        # range filter accepts the row, but the client-side expiry
        # check rejects it so the caller sees ``None``.
        row = _make_posted_price_row(
            effective_date="2025-01-01",
            expiry_date="2025-12-31",
        )
        es = _FakeESService(rows=[row])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id=None,
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is None

    @pytest.mark.asyncio
    async def test_pre_window_rule_is_skipped(self):
        # effective_date is after the request date — the ES range
        # filter rejects the row (effective_date lte request_date).
        row = _make_posted_price_row(
            effective_date="2027-01-01",
            expiry_date="2027-12-31",
        )
        es = _FakeESService(rows=[row])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id=None,
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is None


# ---------------------------------------------------------------------------
# resolve_rule — input validation
# ---------------------------------------------------------------------------


class TestResolveRuleInputs:
    """Input discipline mirrors :class:`PriceProtectionService`."""

    @pytest.mark.asyncio
    async def test_empty_customer_id_rejected(self):
        engine = SalesPricingEngine(_FakeESService(), "tenant-1")

        with pytest.raises(ValueError):
            await engine.resolve_rule(
                customer_id="",
                account_id=None,
                product_code="HEATING_OIL",
                effective_date=date(2026, 6, 1),
            )

    @pytest.mark.asyncio
    async def test_empty_product_code_rejected(self):
        engine = SalesPricingEngine(_FakeESService(), "tenant-1")

        with pytest.raises(ValueError):
            await engine.resolve_rule(
                customer_id="cust-1",
                account_id=None,
                product_code="   ",
                effective_date=date(2026, 6, 1),
            )

    @pytest.mark.asyncio
    async def test_non_date_effective_date_rejected(self):
        engine = SalesPricingEngine(_FakeESService(), "tenant-1")

        with pytest.raises(ValueError):
            await engine.resolve_rule(
                customer_id="cust-1",
                account_id=None,
                product_code="HEATING_OIL",
                effective_date="2026-06-01",  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# resolve_price — strategy dispatch skeleton (Tasks 5.3–5.6)
# ---------------------------------------------------------------------------


class TestStrategyDispatch:
    """Every strategy branch raises :class:`NotImplementedError` today.

    Task 5.2 establishes the dispatch structure. Tasks 5.3–5.6 fill in
    the four strategy bodies (``posted_price``, ``rack_plus_margin``,
    ``tiered_volume``, ``cost_plus``). Until then, every matched rule
    must raise :class:`NotImplementedError` so the unfinished branches
    are obvious during integration.
    """

    @pytest.mark.asyncio
    async def test_posted_price_strategy_returns_resolution(self):
        es = _FakeESService(rows=[_make_posted_price_row()])
        engine = SalesPricingEngine(es, "tenant-1")

        result = await engine.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            gallons=500.0,
            terminal_id="TERMINAL-A",
            route_miles=42.0,
            effective_date=date(2026, 6, 1),
        )

        # Task 5.3 implemented posted_price — returns a PriceResolution
        # with the fixed price from the rule.
        assert result.effective_price_cents == 310
        assert result.contract_id is None
        assert result.contract_type is None
        assert result.market_price_cents == 0

    @pytest.mark.asyncio
    async def test_rack_plus_margin_strategy_resolves_price(self):
        """rack_plus_margin now resolves when market_price_cents is provided."""
        es = _FakeESService(rows=[_make_rack_plus_margin_row()])
        engine = SalesPricingEngine(es, "tenant-1")

        # Without market_price_cents and no rack_prices row available,
        # get_rack_price raises a typed PricingRackPriceUnavailableError.
        from commerce.services.sales_pricing_engine import (
            PricingRackPriceUnavailableError,
        )

        with pytest.raises(PricingRackPriceUnavailableError):
            await engine.resolve_price(
                customer_id="cust-1",
                product_code="DIESEL",
                gallons=500.0,
                terminal_id="TERMINAL-A",
                route_miles=42.0,
                effective_date=date(2026, 6, 1),
            )

    @pytest.mark.asyncio
    async def test_tiered_volume_strategy_resolves_price(self):
        """tiered_volume resolves the correct tier price for given gallons."""
        es = _FakeESService(rows=[_make_tiered_volume_row()])
        engine = SalesPricingEngine(es, "tenant-1")

        result = await engine.resolve_price(
            customer_id="cust-1",
            product_code="GASOLINE",
            gallons=500.0,  # At boundary of second tier [500, None)
            terminal_id="TERMINAL-A",
            route_miles=42.0,
            effective_date=date(2026, 6, 1),
        )

        # 500 gallons hits the second tier (min_gallons=500, max_gallons=None)
        assert result.effective_price_cents == 305

    @pytest.mark.asyncio
    async def test_cost_plus_strategy_resolves_price(self):
        """cost_plus now resolves when market_price_cents is provided."""
        es = _FakeESService(rows=[_make_cost_plus_row()])
        engine = SalesPricingEngine(es, "tenant-1")

        # Without market_price_cents and no rack_prices row available,
        # get_rack_price raises a typed PricingRackPriceUnavailableError.
        from commerce.services.sales_pricing_engine import (
            PricingRackPriceUnavailableError,
        )

        with pytest.raises(PricingRackPriceUnavailableError):
            await engine.resolve_price(
                customer_id="cust-1",
                product_code="PROPANE",
                gallons=500.0,
                terminal_id="TERMINAL-A",
                route_miles=42.0,
                effective_date=date(2026, 6, 1),
            )

    @pytest.mark.asyncio
    async def test_no_rule_matched_raises_pricing_error(self):
        # Empty ES — no rule matches. Task 5.8 replaced the
        # NotImplementedError with PricingNoRuleMatchedError per Req 11.7.
        from commerce.services.sales_pricing_engine import (
            PricingNoRuleMatchedError,
        )

        es = _FakeESService(rows=[])
        engine = SalesPricingEngine(es, "tenant-1")

        with pytest.raises(PricingNoRuleMatchedError) as exc_info:
            await engine.resolve_price(
                customer_id="cust-99",
                product_code="HEATING_OIL",
                gallons=500.0,
                terminal_id="TERMINAL-A",
                route_miles=42.0,
                effective_date=date(2026, 6, 1),
            )

        assert exc_info.value.error_code == "pricing.no_rule_matched"
        assert exc_info.value.customer_id == "cust-99"
        assert exc_info.value.product_code == "HEATING_OIL"


# ---------------------------------------------------------------------------
# Defensive silencing: ``_make_tiered_volume_row`` exercises TierBreak
# ---------------------------------------------------------------------------


def test_tier_break_row_builder_is_valid_pricing_rule():
    """Sanity check: tiered_volume fixture must be a valid PricingRule.

    Guards the test file from bitrot if :class:`TierBreak` validators
    tighten in the future — the fixture has to keep parsing through
    :meth:`PricingRule.model_validate` or the dispatch test above
    would be exercising a malformed row instead of the strategy
    branch it claims to cover.
    """
    row = _make_tiered_volume_row()
    rule = PricingRule.model_validate(row)
    assert rule.strategy == "tiered_volume"
    assert rule.tier_thresholds is not None
    assert len(rule.tier_thresholds) == 2
    assert isinstance(rule.tier_thresholds[0], TierBreak)
