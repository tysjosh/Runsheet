"""Unit tests for priority resolution and posted_price strategy.

Task 5.3 of the Fuel Compliance Backbone spec implements:
- Priority resolution: customer-specific → account-tier → product-default
- Lower priority number wins within the same tier
- posted_price strategy returns the fixed price from the rule
- No rule matched still raises NotImplementedError (Task 5.8 will wire error code)

Validates: Requirement 11.2
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pytest

from commerce.models.pricing_rule import PricingRule
from commerce.services.price_protection_service import PriceResolution
from commerce.services.sales_pricing_engine import SalesPricingEngine


# ---------------------------------------------------------------------------
# Fake ES service
# ---------------------------------------------------------------------------


class _FakeESService:
    """Async ES stand-in that filters rows based on the query structure.

    Handles the query structure produced by inject_tenant_filter wrapping
    the inner bool query.
    """

    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self._rows: List[Dict[str, Any]] = list(rows or [])

    async def search_documents(
        self,
        index: str,
        query: Dict[str, Any],
        size: int = 100,
    ) -> Dict[str, Any]:
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


def _make_rule_row(
    *,
    rule_id: str = "rule-1",
    tenant_id: str = "tenant-1",
    customer_id: Optional[str] = None,
    account_id: Optional[str] = None,
    product_code: str = "HEATING_OIL",
    strategy: str = "posted_price",
    posted_price_cents: Optional[int] = 310,
    margin_cents: Optional[int] = None,
    freight_rate_cents_per_mile: Optional[int] = None,
    terminal_id: Optional[str] = None,
    tier_thresholds: Optional[List[Dict[str, Any]]] = None,
    priority: int = 0,
    effective_date: str = "2026-01-01",
    expiry_date: Optional[str] = "2026-12-31",
    status: str = "active",
) -> Dict[str, Any]:
    """Build a serialized pricing rule row."""
    return {
        "rule_id": rule_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "account_id": account_id,
        "product_code": product_code,
        "strategy": strategy,
        "posted_price_cents": posted_price_cents,
        "margin_cents": margin_cents,
        "freight_rate_cents_per_mile": freight_rate_cents_per_mile,
        "terminal_id": terminal_id,
        "tier_thresholds": tier_thresholds,
        "priority": priority,
        "effective_date": effective_date,
        "expiry_date": expiry_date,
        "status": status,
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Priority resolution tests
# ---------------------------------------------------------------------------


class TestPriorityResolution:
    """Customer-specific rules win over account-tier and product-default."""

    @pytest.mark.asyncio
    async def test_customer_specific_wins_over_product_default(self):
        """A rule with customer_id set beats a product-default rule."""
        product_default = _make_rule_row(
            rule_id="rule-default",
            customer_id=None,
            account_id=None,
            posted_price_cents=250,
            priority=0,
        )
        customer_specific = _make_rule_row(
            rule_id="rule-customer",
            customer_id="cust-1",
            account_id=None,
            posted_price_cents=280,
            priority=0,
        )
        es = _FakeESService(rows=[product_default, customer_specific])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id=None,
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is not None
        assert rule.rule_id == "rule-customer"
        assert rule.posted_price_cents == 280

    @pytest.mark.asyncio
    async def test_customer_plus_account_wins_over_customer_only(self):
        """A rule scoped to customer + account beats customer-only."""
        customer_only = _make_rule_row(
            rule_id="rule-cust-only",
            customer_id="cust-1",
            account_id=None,
            posted_price_cents=280,
            priority=0,
        )
        customer_and_account = _make_rule_row(
            rule_id="rule-cust-acct",
            customer_id="cust-1",
            account_id="acct-1",
            posted_price_cents=270,
            priority=0,
        )
        es = _FakeESService(rows=[customer_only, customer_and_account])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id="acct-1",
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is not None
        assert rule.rule_id == "rule-cust-acct"

    @pytest.mark.asyncio
    async def test_account_tier_wins_over_product_default(self):
        """A rule scoped to account (no customer) beats product-default."""
        product_default = _make_rule_row(
            rule_id="rule-default",
            customer_id=None,
            account_id=None,
            posted_price_cents=250,
            priority=0,
        )
        account_tier = _make_rule_row(
            rule_id="rule-account",
            customer_id=None,
            account_id="acct-1",
            posted_price_cents=260,
            priority=0,
        )
        es = _FakeESService(rows=[product_default, account_tier])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id="acct-1",
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is not None
        assert rule.rule_id == "rule-account"

    @pytest.mark.asyncio
    async def test_lower_priority_number_wins_within_same_tier(self):
        """Within the same tier, lower priority number wins."""
        high_priority = _make_rule_row(
            rule_id="rule-high",
            customer_id="cust-1",
            account_id=None,
            posted_price_cents=290,
            priority=1,
        )
        higher_priority = _make_rule_row(
            rule_id="rule-higher",
            customer_id="cust-1",
            account_id=None,
            posted_price_cents=285,
            priority=5,
        )
        lowest_number = _make_rule_row(
            rule_id="rule-lowest",
            customer_id="cust-1",
            account_id=None,
            posted_price_cents=275,
            priority=0,
        )
        es = _FakeESService(rows=[high_priority, higher_priority, lowest_number])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id=None,
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is not None
        assert rule.rule_id == "rule-lowest"
        assert rule.priority == 0

    @pytest.mark.asyncio
    async def test_mismatched_customer_rule_excluded(self):
        """A rule scoped to a different customer is not selected."""
        wrong_customer = _make_rule_row(
            rule_id="rule-wrong",
            customer_id="cust-other",
            account_id=None,
            posted_price_cents=200,
            priority=0,
        )
        product_default = _make_rule_row(
            rule_id="rule-default",
            customer_id=None,
            account_id=None,
            posted_price_cents=250,
            priority=0,
        )
        es = _FakeESService(rows=[wrong_customer, product_default])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id=None,
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is not None
        assert rule.rule_id == "rule-default"

    @pytest.mark.asyncio
    async def test_only_mismatched_rules_returns_none(self):
        """When all rules are scoped to non-matching customers, return None."""
        wrong_customer = _make_rule_row(
            rule_id="rule-wrong",
            customer_id="cust-other",
            account_id=None,
            posted_price_cents=200,
            priority=0,
        )
        es = _FakeESService(rows=[wrong_customer])
        engine = SalesPricingEngine(es, "tenant-1")

        rule = await engine.resolve_rule(
            customer_id="cust-1",
            account_id=None,
            product_code="HEATING_OIL",
            effective_date=date(2026, 6, 1),
        )

        assert rule is None


# ---------------------------------------------------------------------------
# posted_price strategy tests
# ---------------------------------------------------------------------------


class TestPostedPriceStrategy:
    """posted_price strategy returns the fixed price from the rule."""

    @pytest.mark.asyncio
    async def test_posted_price_returns_fixed_price(self):
        """posted_price strategy returns posted_price_cents as effective price."""
        row = _make_rule_row(
            rule_id="rule-posted",
            customer_id="cust-1",
            account_id=None,
            posted_price_cents=310,
            priority=0,
        )
        es = _FakeESService(rows=[row])
        engine = SalesPricingEngine(es, "tenant-1")

        result = await engine.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            gallons=500.0,
            terminal_id="TERMINAL-A",
            route_miles=42.0,
            effective_date=date(2026, 6, 1),
        )

        assert isinstance(result, PriceResolution)
        assert result.effective_price_cents == 310
        assert result.contract_id is None
        assert result.contract_type is None
        assert result.market_price_cents == 0

    @pytest.mark.asyncio
    async def test_posted_price_with_market_price_provided(self):
        """When market_price_cents is provided, it's echoed back."""
        row = _make_rule_row(
            rule_id="rule-posted",
            customer_id="cust-1",
            account_id=None,
            posted_price_cents=310,
            priority=0,
        )
        es = _FakeESService(rows=[row])
        engine = SalesPricingEngine(es, "tenant-1")

        result = await engine.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            gallons=500.0,
            terminal_id="TERMINAL-A",
            route_miles=42.0,
            effective_date=date(2026, 6, 1),
            market_price_cents=295,
        )

        assert result.effective_price_cents == 310
        assert result.market_price_cents == 295

    @pytest.mark.asyncio
    async def test_posted_price_priority_resolution_end_to_end(self):
        """Full flow: customer-specific posted_price wins over product-default."""
        product_default = _make_rule_row(
            rule_id="rule-default",
            customer_id=None,
            account_id=None,
            posted_price_cents=250,
            priority=0,
        )
        customer_specific = _make_rule_row(
            rule_id="rule-customer",
            customer_id="cust-1",
            account_id=None,
            posted_price_cents=280,
            priority=0,
        )
        es = _FakeESService(rows=[product_default, customer_specific])
        engine = SalesPricingEngine(es, "tenant-1")

        result = await engine.resolve_price(
            customer_id="cust-1",
            product_code="HEATING_OIL",
            gallons=500.0,
            terminal_id="TERMINAL-A",
            route_miles=42.0,
            effective_date=date(2026, 6, 1),
        )

        # Customer-specific rule wins → 280 cents
        assert result.effective_price_cents == 280


# ---------------------------------------------------------------------------
# No rule matched — still raises NotImplementedError (Task 5.8)
# ---------------------------------------------------------------------------


class TestNoRuleMatched:
    """No rule matched raises PricingNoRuleMatchedError (Task 5.8)."""

    @pytest.mark.asyncio
    async def test_no_rule_raises_pricing_no_rule_matched_error(self):
        """Empty index → PricingNoRuleMatchedError (Req 11.7)."""
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
