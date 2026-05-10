"""Unit tests for SalesPricingEngine strategy implementations.

Tests cover the three strategies implemented in Tasks 5.4–5.6:
- rack_plus_margin (Task 5.4 / Req 11.3)
- tiered_volume (Task 5.5 / Req 11.4)
- cost_plus (Task 5.6 / Req 11.5)

Validates: Requirements 11.3, 11.4, 11.5
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

import pytest

from commerce.models.pricing_rule import PricingRule, TierBreak
from commerce.services.price_protection_service import PriceResolution
from commerce.services.sales_pricing_engine import SalesPricingEngine


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

TENANT_ID = "tenant_test_strategies"
CUSTOMER_ID = "cust_001"
PRODUCT_CODE = "DIESEL_LSD"
TERMINAL_ID = "terminal_houston_01"
EFFECTIVE_DATE = date(2025, 6, 1)


class FakeESService:
    """Minimal ES service stub that returns pre-configured pricing rules."""

    def __init__(self, rules: list[Dict[str, Any]]) -> None:
        self._rules = rules

    async def search_documents(
        self, index: str, query: dict, size: int = 100
    ) -> dict:
        """Return all configured rules as ES hits."""
        hits = [{"_source": rule} for rule in self._rules]
        return {"hits": {"hits": hits}}


def _make_rule_dict(
    strategy: str,
    *,
    rule_id: str = "rule_test_001",
    customer_id: Optional[str] = CUSTOMER_ID,
    product_code: str = PRODUCT_CODE,
    margin_cents: Optional[int] = None,
    posted_price_cents: Optional[int] = None,
    freight_rate_cents_per_mile: Optional[int] = None,
    terminal_id: Optional[str] = TERMINAL_ID,
    tier_thresholds: Optional[list] = None,
    priority: int = 0,
) -> Dict[str, Any]:
    """Build a pricing rule dict suitable for the FakeESService."""
    rule = {
        "rule_id": rule_id,
        "tenant_id": TENANT_ID,
        "customer_id": customer_id,
        "account_id": None,
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
    return rule


# ---------------------------------------------------------------------------
# rack_plus_margin tests (Task 5.4 / Req 11.3)
# ---------------------------------------------------------------------------


class TestRackPlusMargin:
    """Tests for the rack_plus_margin strategy."""

    @pytest.mark.asyncio
    async def test_rack_plus_margin_computes_effective_price(self):
        """rack_price + margin_cents = effective_price_cents.

        Validates: Requirements 11.3
        """
        rack_price = 250  # 250 cents/gallon
        margin = 15  # 15 cents margin
        rule_dict = _make_rule_dict(
            "rack_plus_margin",
            margin_cents=margin,
            terminal_id=TERMINAL_ID,
        )
        es = FakeESService([rule_dict])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=100.0,
            terminal_id=TERMINAL_ID,
            route_miles=50.0,
            effective_date=EFFECTIVE_DATE,
            market_price_cents=rack_price,
        )

        assert result.effective_price_cents == rack_price + margin
        assert result.market_price_cents == rack_price
        assert result.contract_id is None

    @pytest.mark.asyncio
    async def test_rack_plus_margin_without_market_price_raises(self):
        """Without market_price_cents, rack_plus_margin raises NotImplementedError.

        Validates: Requirements 11.3
        """
        rule_dict = _make_rule_dict(
            "rack_plus_margin",
            margin_cents=10,
            terminal_id=TERMINAL_ID,
        )
        es = FakeESService([rule_dict])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        with pytest.raises(NotImplementedError, match="OPIS rack-price"):
            await engine.resolve_price(
                customer_id=CUSTOMER_ID,
                product_code=PRODUCT_CODE,
                gallons=100.0,
                terminal_id=TERMINAL_ID,
                route_miles=50.0,
                effective_date=EFFECTIVE_DATE,
                market_price_cents=None,
            )


# ---------------------------------------------------------------------------
# tiered_volume tests (Task 5.5 / Req 11.4)
# ---------------------------------------------------------------------------


class TestTieredVolume:
    """Tests for the tiered_volume strategy."""

    @pytest.fixture
    def tier_thresholds(self):
        """Three-tier pricing structure."""
        return [
            {"min_gallons": 0, "max_gallons": 500, "unit_price_cents": 300},
            {"min_gallons": 500, "max_gallons": 1000, "unit_price_cents": 280},
            {"min_gallons": 1000, "max_gallons": None, "unit_price_cents": 260},
        ]

    @pytest.mark.asyncio
    async def test_tiered_volume_first_tier(self, tier_thresholds):
        """Gallons in first tier returns first tier price.

        Validates: Requirements 11.4
        """
        rule_dict = _make_rule_dict(
            "tiered_volume",
            tier_thresholds=tier_thresholds,
            terminal_id=None,
        )
        es = FakeESService([rule_dict])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=250.0,  # In first tier [0, 500)
            terminal_id=TERMINAL_ID,
            route_miles=0.0,
            effective_date=EFFECTIVE_DATE,
        )

        assert result.effective_price_cents == 300
        assert result.contract_id is None

    @pytest.mark.asyncio
    async def test_tiered_volume_second_tier(self, tier_thresholds):
        """Gallons in second tier returns second tier price.

        Validates: Requirements 11.4
        """
        rule_dict = _make_rule_dict(
            "tiered_volume",
            tier_thresholds=tier_thresholds,
            terminal_id=None,
        )
        es = FakeESService([rule_dict])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=750.0,  # In second tier [500, 1000)
            terminal_id=TERMINAL_ID,
            route_miles=0.0,
            effective_date=EFFECTIVE_DATE,
        )

        assert result.effective_price_cents == 280
        assert result.contract_id is None

    @pytest.mark.asyncio
    async def test_tiered_volume_unbounded_top_tier(self, tier_thresholds):
        """Gallons in unbounded top tier returns top tier price.

        Validates: Requirements 11.4
        """
        rule_dict = _make_rule_dict(
            "tiered_volume",
            tier_thresholds=tier_thresholds,
            terminal_id=None,
        )
        es = FakeESService([rule_dict])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=5000.0,  # In top tier [1000, ∞)
            terminal_id=TERMINAL_ID,
            route_miles=0.0,
            effective_date=EFFECTIVE_DATE,
        )

        assert result.effective_price_cents == 260
        assert result.contract_id is None


# ---------------------------------------------------------------------------
# cost_plus tests (Task 5.6 / Req 11.5)
# ---------------------------------------------------------------------------


class TestCostPlus:
    """Tests for the cost_plus strategy."""

    @pytest.mark.asyncio
    async def test_cost_plus_computes_effective_price(self):
        """rack_price + (freight_rate × miles) + margin = effective_price.

        Validates: Requirements 11.5
        """
        rack_price = 250  # 250 cents/gallon
        margin = 10  # 10 cents margin
        freight_rate = 2  # 2 cents per mile
        route_miles = 30.0  # 30 miles

        rule_dict = _make_rule_dict(
            "cost_plus",
            margin_cents=margin,
            freight_rate_cents_per_mile=freight_rate,
            terminal_id=TERMINAL_ID,
        )
        es = FakeESService([rule_dict])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        result = await engine.resolve_price(
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
            gallons=100.0,
            terminal_id=TERMINAL_ID,
            route_miles=route_miles,
            effective_date=EFFECTIVE_DATE,
            market_price_cents=rack_price,
        )

        expected = rack_price + (freight_rate * route_miles) + margin
        assert result.effective_price_cents == round(expected)
        assert result.market_price_cents == rack_price
        assert result.contract_id is None

    @pytest.mark.asyncio
    async def test_cost_plus_without_market_price_raises(self):
        """Without market_price_cents, cost_plus raises NotImplementedError.

        Validates: Requirements 11.5
        """
        rule_dict = _make_rule_dict(
            "cost_plus",
            margin_cents=10,
            freight_rate_cents_per_mile=2,
            terminal_id=TERMINAL_ID,
        )
        es = FakeESService([rule_dict])
        engine = SalesPricingEngine(es_service=es, tenant_id=TENANT_ID)

        with pytest.raises(NotImplementedError, match="OPIS rack-price"):
            await engine.resolve_price(
                customer_id=CUSTOMER_ID,
                product_code=PRODUCT_CODE,
                gallons=100.0,
                terminal_id=TERMINAL_ID,
                route_miles=50.0,
                effective_date=EFFECTIVE_DATE,
                market_price_cents=None,
            )
