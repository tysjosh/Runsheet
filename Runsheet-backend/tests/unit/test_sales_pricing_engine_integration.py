"""Integration tests for SalesPricingEngine — full flow.

Exercises the complete resolution path: priority resolution → strategy
dispatch → PriceResolution returned. Covers all 4 strategies in one
file and validates the priority ordering across tiers.

This complements the per-strategy tests in
``test_sales_pricing_engine_strategies.py`` and the priority tests in
``test_sales_pricing_engine_priority.py`` by exercising the full
end-to-end flow including:
- PricingNoRuleMatchedError (Task 5.8 / Req 11.7)
- Resolution logging (Task 5.9 / Req 11.8)
- All 4 strategies in a single test suite

Validates: Requirements 11.1, 11.2, 11.3, 11.4, 11.5, 11.7, 11.8
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pytest

from commerce.models.pricing_rule import PricingRule
from commerce.services.price_protection_service import PriceResolution
from commerce.services.sales_pricing_engine import (
    ERROR_CODE_PRICING_NO_RULE_MATCHED,
    PRICING_RESOLUTION_LOG_INDEX,
    PricingNoRuleMatchedError,
    SalesPricingEngine,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

TENANT_ID = "tenant_integration"
CUSTOMER_ID = "cust_int_001"
ACCOUNT_ID = "acct_int_001"
PRODUCT_CODE = "HEATING_OIL"
TERMINAL_ID = "terminal_northeast_01"
EFFECTIVE_DATE = date(2025, 7, 1)


class FakeESService:
    """ES service stub that returns pre-configured pricing rules and
    records index_document calls for resolution log verification."""

    def __init__(self, rules: List[Dict[str, Any]]) -> None:
        self._rules = rules
        self.indexed_documents: List[Dict[str, Any]] = []

    async def search_documents(
        self, index: str, query: dict, size: int = 100
    ) -> dict:
        """Return all configured rules as ES hits."""
        hits = [{"_source": rule} for rule in self._rules]
        return {"hits": {"hits": hits}}

    async def index_document(
        self, index: str, doc_id: str, document: dict
    ) -> None:
        """Record indexed documents for verification."""
        self.indexed_documents.append(
            {"index": index, "doc_id": doc_id, "document": document}
        )


def _make_rule(
    strategy: str,
    *,
    rule_id: str = "rule_int_001",
    customer_id: Optional[str] = CUSTOMER_ID,
    account_id: Optional[str] = None,
    product_code: str = PRODUCT_CODE,
    margin_cents: Optional[int] = None,
    posted_price_cents: Optional[int] = None,
    freight_rate_cents_per_mile: Optional[int] = None,
    terminal_id: Optional[str] = TERMINAL_ID,
    tier_thresholds: Optional[list] = None,
    priority: int = 0,
) -> Dict[str, Any]:
    """Build a pricing rule dict."""
    return {
        "rule_id": rule_id,
        "tenant_id": TENANT_ID,
        "customer_id": customer_id,
        "account_id": account_id,
        "product_code": product_code,
        "strategy": strategy,
        "margin_cents": margin_cents,
        "posted_price_cents": posted_price_cents,
        "freight_rate_cents_per_mile": freight_rate_cents_per_mile,
        "terminal_id": terminal_id,
        "tier_thresholds": tier_thresholds,
        "priority": priority,
        "effective_date": "2025-01-01",
        "expiry_date": None,
        "status": "active",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Full flow: posted_price strategy
# ---------------------------------------------------------------------------


class TestPostedPriceFullFlow:
    """End-to-end: priority resolution → posted_price → PriceResolution."""

    @pytest.mark.asyncio
    async def test_posted_price_resolves_fixed_price(self):
        """posted_price returns the fixed price from the rule.

        Validates: Requirements 11.1, 11.2
        """
        rule = _make_rule("posted_price", posted_price_cents=350)
        es = FakeESService([rule])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=200.0,
            terminal_id=TERMINAL_ID,
            route_miles=0.0,
            effective_date=EFFECTIVE_DATE,
        )

        assert result.effective_price_cents == 350
        assert result.contract_id is None
        assert result.contract_type is None

    @pytest.mark.asyncio
    async def test_posted_price_logs_resolution(self):
        """posted_price writes to pricing_resolution_log index.

        Validates: Requirement 11.8
        """
        rule = _make_rule("posted_price", posted_price_cents=350)
        es = FakeESService([rule])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=200.0,
            terminal_id=TERMINAL_ID,
            route_miles=0.0,
            effective_date=EFFECTIVE_DATE,
        )

        # Verify resolution was logged
        log_writes = [
            d
            for d in es.indexed_documents
            if d["index"] == PRICING_RESOLUTION_LOG_INDEX
        ]
        assert len(log_writes) == 1
        log_doc = log_writes[0]["document"]
        assert log_doc["customer_id"] == CUSTOMER_ID
        assert log_doc["product_code"] == PRODUCT_CODE
        assert log_doc["strategy"] == "posted_price"
        assert log_doc["resolved_price_cents"] == 350
        assert log_doc["tenant_id"] == TENANT_ID


# ---------------------------------------------------------------------------
# Full flow: rack_plus_margin strategy
# ---------------------------------------------------------------------------


class TestRackPlusMarginFullFlow:
    """End-to-end: priority resolution → rack_plus_margin → PriceResolution."""

    @pytest.mark.asyncio
    async def test_rack_plus_margin_adds_margin_to_rack(self):
        """rack_plus_margin = rack_price + margin_cents.

        Validates: Requirements 11.2, 11.3
        """
        rule = _make_rule("rack_plus_margin", margin_cents=20)
        es = FakeESService([rule])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=500.0,
            terminal_id=TERMINAL_ID,
            route_miles=25.0,
            effective_date=EFFECTIVE_DATE,
            market_price_cents=280,
        )

        assert result.effective_price_cents == 300  # 280 + 20
        assert result.market_price_cents == 280

    @pytest.mark.asyncio
    async def test_rack_plus_margin_logs_resolution(self):
        """rack_plus_margin writes to pricing_resolution_log.

        Validates: Requirement 11.8
        """
        rule = _make_rule("rack_plus_margin", margin_cents=15)
        es = FakeESService([rule])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=100.0,
            terminal_id=TERMINAL_ID,
            route_miles=10.0,
            effective_date=EFFECTIVE_DATE,
            market_price_cents=250,
        )

        log_writes = [
            d
            for d in es.indexed_documents
            if d["index"] == PRICING_RESOLUTION_LOG_INDEX
        ]
        assert len(log_writes) == 1
        assert log_writes[0]["document"]["strategy"] == "rack_plus_margin"
        assert log_writes[0]["document"]["resolved_price_cents"] == 265


# ---------------------------------------------------------------------------
# Full flow: tiered_volume strategy
# ---------------------------------------------------------------------------


class TestTieredVolumeFullFlow:
    """End-to-end: priority resolution → tiered_volume → PriceResolution."""

    @pytest.fixture
    def tier_thresholds(self):
        return [
            {"min_gallons": 0, "max_gallons": 500, "unit_price_cents": 320},
            {"min_gallons": 500, "max_gallons": 2000, "unit_price_cents": 290},
            {"min_gallons": 2000, "max_gallons": None, "unit_price_cents": 260},
        ]

    @pytest.mark.asyncio
    async def test_tiered_volume_selects_correct_tier(self, tier_thresholds):
        """tiered_volume evaluates gallons against tier thresholds.

        Validates: Requirements 11.2, 11.4
        """
        rule = _make_rule(
            "tiered_volume", tier_thresholds=tier_thresholds
        )
        es = FakeESService([rule])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        # First tier: 0-500 gallons → 320¢
        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=250.0,
            terminal_id=TERMINAL_ID,
            route_miles=0.0,
            effective_date=EFFECTIVE_DATE,
        )
        assert result.effective_price_cents == 320

    @pytest.mark.asyncio
    async def test_tiered_volume_top_tier(self, tier_thresholds):
        """tiered_volume unbounded top tier matches large volumes.

        Validates: Requirement 11.4
        """
        rule = _make_rule(
            "tiered_volume", tier_thresholds=tier_thresholds
        )
        es = FakeESService([rule])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=5000.0,
            terminal_id=TERMINAL_ID,
            route_miles=0.0,
            effective_date=EFFECTIVE_DATE,
        )
        assert result.effective_price_cents == 260


# ---------------------------------------------------------------------------
# Full flow: cost_plus strategy
# ---------------------------------------------------------------------------


class TestCostPlusFullFlow:
    """End-to-end: priority resolution → cost_plus → PriceResolution."""

    @pytest.mark.asyncio
    async def test_cost_plus_computes_rack_freight_margin(self):
        """cost_plus = rack + (freight_rate × miles) + margin.

        Validates: Requirements 11.2, 11.5
        """
        rule = _make_rule(
            "cost_plus",
            margin_cents=12,
            freight_rate_cents_per_mile=3,
        )
        es = FakeESService([rule])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=300.0,
            terminal_id=TERMINAL_ID,
            route_miles=40.0,
            effective_date=EFFECTIVE_DATE,
            market_price_cents=250,
        )

        # 250 + (3 × 40) + 12 = 250 + 120 + 12 = 382
        assert result.effective_price_cents == 382
        assert result.market_price_cents == 250

    @pytest.mark.asyncio
    async def test_cost_plus_logs_resolution(self):
        """cost_plus writes to pricing_resolution_log.

        Validates: Requirement 11.8
        """
        rule = _make_rule(
            "cost_plus",
            margin_cents=10,
            freight_rate_cents_per_mile=2,
        )
        es = FakeESService([rule])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=100.0,
            terminal_id=TERMINAL_ID,
            route_miles=20.0,
            effective_date=EFFECTIVE_DATE,
            market_price_cents=200,
        )

        log_writes = [
            d
            for d in es.indexed_documents
            if d["index"] == PRICING_RESOLUTION_LOG_INDEX
        ]
        assert len(log_writes) == 1
        assert log_writes[0]["document"]["strategy"] == "cost_plus"
        # 200 + (2 × 20) + 10 = 250
        assert log_writes[0]["document"]["resolved_price_cents"] == 250


# ---------------------------------------------------------------------------
# No rule matched → PricingNoRuleMatchedError (Task 5.8 / Req 11.7)
# ---------------------------------------------------------------------------


class TestNoRuleMatchedError:
    """When no rule matches, PricingNoRuleMatchedError is raised."""

    @pytest.mark.asyncio
    async def test_no_rule_raises_pricing_no_rule_matched_error(self):
        """Empty rule set raises PricingNoRuleMatchedError.

        Validates: Requirement 11.7
        """
        es = FakeESService([])  # No rules
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        with pytest.raises(PricingNoRuleMatchedError) as exc_info:
            await engine.resolve_price(
                customer_id=CUSTOMER_ID,
                product_code=PRODUCT_CODE,
                gallons=100.0,
                terminal_id=TERMINAL_ID,
                route_miles=10.0,
                effective_date=EFFECTIVE_DATE,
            )

        err = exc_info.value
        assert err.error_code == ERROR_CODE_PRICING_NO_RULE_MATCHED
        assert err.tenant_id == TENANT_ID
        assert err.customer_id == CUSTOMER_ID
        assert err.product_code == PRODUCT_CODE
        assert err.effective_date == EFFECTIVE_DATE

    @pytest.mark.asyncio
    async def test_no_rule_error_message_contains_details(self):
        """Error message includes tenant, customer, product, date.

        Validates: Requirement 11.7
        """
        es = FakeESService([])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        with pytest.raises(PricingNoRuleMatchedError) as exc_info:
            await engine.resolve_price(
                customer_id="cust_xyz",
                product_code="PROPANE",
                gallons=50.0,
                terminal_id=TERMINAL_ID,
                route_miles=5.0,
                effective_date=date(2025, 8, 15),
            )

        msg = str(exc_info.value)
        assert "cust_xyz" in msg
        assert "PROPANE" in msg
        assert "2025-08-15" in msg
        assert ERROR_CODE_PRICING_NO_RULE_MATCHED in msg


# ---------------------------------------------------------------------------
# Priority ordering across tiers
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    """Customer-specific rule wins over product-default."""

    @pytest.mark.asyncio
    async def test_customer_specific_wins_over_product_default(self):
        """Customer-specific rule (tier 1) beats product-default (tier 3).

        Validates: Requirement 11.2
        """
        customer_rule = _make_rule(
            "posted_price",
            rule_id="rule_customer",
            customer_id=CUSTOMER_ID,
            posted_price_cents=300,
            priority=5,
        )
        default_rule = _make_rule(
            "posted_price",
            rule_id="rule_default",
            customer_id=None,
            posted_price_cents=350,
            priority=0,  # Lower priority number but less specific
        )
        es = FakeESService([default_rule, customer_rule])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=100.0,
            terminal_id=TERMINAL_ID,
            route_miles=0.0,
            effective_date=EFFECTIVE_DATE,
        )

        # Customer-specific wins even though default has lower priority number
        assert result.effective_price_cents == 300

    @pytest.mark.asyncio
    async def test_customer_account_wins_over_customer_only(self):
        """Customer+account rule (tier 0) beats customer-only (tier 1).

        Validates: Requirement 11.2
        """
        customer_account_rule = _make_rule(
            "posted_price",
            rule_id="rule_cust_acct",
            customer_id=CUSTOMER_ID,
            account_id=ACCOUNT_ID,
            posted_price_cents=280,
            priority=10,
        )
        customer_only_rule = _make_rule(
            "posted_price",
            rule_id="rule_cust_only",
            customer_id=CUSTOMER_ID,
            account_id=None,
            posted_price_cents=310,
            priority=0,
        )
        es = FakeESService([customer_only_rule, customer_account_rule])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=100.0,
            terminal_id=TERMINAL_ID,
            route_miles=0.0,
            effective_date=EFFECTIVE_DATE,
            account_id=ACCOUNT_ID,
        )

        # Customer+account (tier 0) wins over customer-only (tier 1)
        assert result.effective_price_cents == 280

    @pytest.mark.asyncio
    async def test_within_same_tier_lower_priority_wins(self):
        """Within the same tier, lower priority number wins.

        Validates: Requirement 11.2
        """
        rule_high_priority = _make_rule(
            "posted_price",
            rule_id="rule_high",
            customer_id=CUSTOMER_ID,
            posted_price_cents=290,
            priority=1,
        )
        rule_low_priority = _make_rule(
            "posted_price",
            rule_id="rule_low",
            customer_id=CUSTOMER_ID,
            posted_price_cents=320,
            priority=5,
        )
        es = FakeESService([rule_low_priority, rule_high_priority])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=100.0,
            terminal_id=TERMINAL_ID,
            route_miles=0.0,
            effective_date=EFFECTIVE_DATE,
        )

        # Priority 1 wins over priority 5
        assert result.effective_price_cents == 290


# ---------------------------------------------------------------------------
# Resolution logging failure is fire-and-forget (Task 5.9)
# ---------------------------------------------------------------------------


class TestResolutionLoggingFireAndForget:
    """Resolution logging failures don't block the resolution."""

    @pytest.mark.asyncio
    async def test_logging_failure_does_not_block_resolution(self):
        """If index_document fails, resolve_price still returns.

        Validates: Requirement 11.8
        """

        class FailingESService:
            """ES service that fails on index_document but works for search."""

            def __init__(self, rules: List[Dict[str, Any]]) -> None:
                self._rules = rules

            async def search_documents(
                self, index: str, query: dict, size: int = 100
            ) -> dict:
                return {"hits": {"hits": [{"_source": r} for r in self._rules]}}

            async def index_document(
                self, index: str, doc_id: str, document: dict
            ) -> None:
                raise RuntimeError("ES write failed!")

        rule = _make_rule("posted_price", posted_price_cents=400)
        es = FailingESService([rule])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        # Should NOT raise despite index_document failure
        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=100.0,
            terminal_id=TERMINAL_ID,
            route_miles=0.0,
            effective_date=EFFECTIVE_DATE,
        )

        assert result.effective_price_cents == 400
