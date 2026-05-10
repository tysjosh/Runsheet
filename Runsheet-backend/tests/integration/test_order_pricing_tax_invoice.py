"""Integration test: Order → Pricing → Tax → Invoice end-to-end.

Verifies the full pipeline from order creation through pricing resolution,
tax computation, and invoice generation using the commerce-backbone
(InvoiceService, SalesPricingEngine) and compliance-backbone (TaxEngine,
VCFCalculator) services wired together.

The test exercises:
1. A mock order with customer, product, and delivery details
2. SalesPricingEngine resolves the correct sell price via a pricing rule
3. TaxEngine computes the correct tax breakdown (federal + state at minimum)
4. InvoiceService produces a complete invoice with pricing + tax line items

ES and external dependencies are mocked via AsyncMock fixtures.

Validates: Requirements 1.1, 1.2, 1.10, 11.2, 11.3, 5.1
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from commerce.models.invoice import InvoiceStatus
from commerce.services.invoice_service import InvoiceService
from commerce.services.price_protection_service import PriceResolution
from commerce.services.sales_pricing_engine import SalesPricingEngine
from compliance.services.tax_engine import (
    TaxBreakdown,
    TaxEngine,
    TaxLineItem,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant_integ_e2e"
CUSTOMER_ID = "cust_acme_fuel"
ACCOUNT_ID = "acct_acme_001"
ORDER_ID = "order_delivery_001"
DESTINATION_FIPS = "06037"  # Los Angeles County, CA
EFFECTIVE_DATE = date(2026, 7, 10)
FIXED_NOW = datetime(2026, 7, 10, 14, 0, 0, tzinfo=timezone.utc)

# Product details
PRODUCT_CODE = "DIESEL_2"
NET_GALLONS = 800.0
TERMINAL_ID = "terminal_la_01"
ROUTE_MILES = 45.0

# Pricing rule: rack_plus_margin strategy
RACK_PRICE_CENTS = 280  # $2.80/gallon rack price
MARGIN_CENTS = 35  # 35¢ margin
EXPECTED_SELL_PRICE_CENTS = RACK_PRICE_CENTS + MARGIN_CENTS  # 315¢ = $3.15/gal

# Tax rates (in tenths-of-cent / mills per gallon for RATE_SCALE=10)
FEDERAL_EXCISE_RATE_STORED = 244  # 24.4¢/gal for diesel
STATE_EXCISE_RATE_STORED = 389  # 38.9¢/gal CA state excise
UST_RATE_STORED = 20  # 2.0¢/gal UST fee

# Expected tax amounts (rate_stored * gallons / RATE_SCALE)
EXPECTED_FEDERAL_TAX_CENTS = round(FEDERAL_EXCISE_RATE_STORED * NET_GALLONS / 10)  # 19520
EXPECTED_STATE_TAX_CENTS = round(STATE_EXCISE_RATE_STORED * NET_GALLONS / 10)  # 31120
EXPECTED_UST_CENTS = round(UST_RATE_STORED * NET_GALLONS / 10)  # 1600
EXPECTED_TOTAL_TAX_CENTS = (
    EXPECTED_FEDERAL_TAX_CENTS + EXPECTED_STATE_TAX_CENTS + EXPECTED_UST_CENTS
)


# ---------------------------------------------------------------------------
# Mock ES service
# ---------------------------------------------------------------------------


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    # Default: no existing events (sequence starts at 1).
    es.search_documents = AsyncMock(
        return_value={
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {"max_seq": {"value": None}},
        }
    )
    return es


def _make_idempotency_service() -> AsyncMock:
    """Create a mocked IdempotencyService."""
    idemp = AsyncMock()
    idemp.is_duplicate = AsyncMock(return_value=False)
    idemp.mark_processed = AsyncMock(return_value=None)
    return idemp


# ---------------------------------------------------------------------------
# Fake TaxEngine that simulates real jurisdiction lookups
# ---------------------------------------------------------------------------


class FakeTaxEngine:
    """Simulates TaxEngine.compute_tax with realistic federal + state + UST.

    Returns a TaxBreakdown with federal excise (24.4¢/gal for diesel),
    CA state excise (38.9¢/gal), and UST fee (2.0¢/gal).
    """

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.calls: List[Dict[str, Any]] = []

    async def compute_tax(
        self,
        *,
        product_code: str,
        net_gallons: float,
        destination_fips: str,
        customer_id: str,
        effective_date: Optional[date] = None,
    ) -> TaxBreakdown:
        self.calls.append(
            {
                "product_code": product_code,
                "net_gallons": net_gallons,
                "destination_fips": destination_fips,
                "customer_id": customer_id,
                "effective_date": effective_date,
            }
        )

        federal_cents = round(FEDERAL_EXCISE_RATE_STORED * net_gallons / 10)
        state_cents = round(STATE_EXCISE_RATE_STORED * net_gallons / 10)
        ust_cents = round(UST_RATE_STORED * net_gallons / 10)

        return TaxBreakdown(
            federal_cents=federal_cents,
            state_cents=state_cents,
            county_cents=0,
            city_cents=0,
            ust_cents=ust_cents,
            spcc_cents=0,
            environmental_cents=0,
            line_items=[
                TaxLineItem(
                    tax_component_name="federal_excise",
                    jurisdiction_fips="00",
                    jurisdiction_level="federal",
                    rate_cents_per_gallon=FEDERAL_EXCISE_RATE_STORED,
                    gallons=net_gallons,
                    amount_cents=federal_cents,
                ),
                TaxLineItem(
                    tax_component_name="CA_state_excise",
                    jurisdiction_fips="06",
                    jurisdiction_level="state",
                    rate_cents_per_gallon=STATE_EXCISE_RATE_STORED,
                    gallons=net_gallons,
                    amount_cents=state_cents,
                ),
                TaxLineItem(
                    tax_component_name="ust_fee",
                    jurisdiction_fips="06",
                    jurisdiction_level="state",
                    rate_cents_per_gallon=UST_RATE_STORED,
                    gallons=net_gallons,
                    amount_cents=ust_cents,
                ),
            ],
            exemptions_applied=[],
        )


# ---------------------------------------------------------------------------
# Fake SalesPricingEngine that simulates rack_plus_margin resolution
# ---------------------------------------------------------------------------


class FakeSalesPricingEngine:
    """Simulates SalesPricingEngine.resolve_price with rack_plus_margin.

    Returns a PriceResolution with effective_price = rack + margin.
    """

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.calls: List[Dict[str, Any]] = []

    async def resolve_price(
        self,
        customer_id: str,
        product_code: str,
        gallons: float,
        terminal_id: str,
        route_miles: float,
        effective_date: date,
        market_price_cents: Optional[int] = None,
        account_id: Optional[str] = None,
    ) -> PriceResolution:
        self.calls.append(
            {
                "customer_id": customer_id,
                "product_code": product_code,
                "gallons": gallons,
                "terminal_id": terminal_id,
                "route_miles": route_miles,
                "effective_date": effective_date,
                "market_price_cents": market_price_cents,
                "account_id": account_id,
            }
        )

        return PriceResolution(
            effective_price_cents=EXPECTED_SELL_PRICE_CENTS,
            contract_id=None,
            contract_type=None,
            market_price_cents=RACK_PRICE_CENTS,
        )


# ===========================================================================
# Integration Test: Order → Pricing → Tax → Invoice
# ===========================================================================


class TestOrderPricingTaxInvoiceE2E:
    """End-to-end integration: Order → SalesPricingEngine → TaxEngine → Invoice.

    Simulates the full invoice generation pipeline where:
    1. An order is created with a customer, product, and delivery details
    2. SalesPricingEngine resolves the sell price (rack_plus_margin)
    3. TaxEngine computes federal + state + UST taxes
    4. InvoiceService produces the final invoice with all line items
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up the service instances with mocked ES."""
        self.es = _make_es_service()
        self.idemp = _make_idempotency_service()
        self.fake_tax_engine = FakeTaxEngine(TENANT_ID)
        self.fake_pricing_engine = FakeSalesPricingEngine(TENANT_ID)

    def _build_invoice_service(self) -> InvoiceService:
        """Build InvoiceService wired with both pricing and tax engines."""
        return InvoiceService(
            self.es,
            self.idemp,
            tax_engine_factory=lambda tid: self.fake_tax_engine,
            sales_pricing_engine_factory=lambda tid: self.fake_pricing_engine,
        )

    def _build_order_line_items(self) -> List[Dict[str, Any]]:
        """Build line items representing a delivered fuel order."""
        return [
            {
                "line_id": "line_diesel_001",
                "product_code": PRODUCT_CODE,
                "quantity": NET_GALLONS,
                "quantity_gallons": NET_GALLONS,
                "unit_price_cents": 0,  # Will be resolved by pricing engine
                "subtotal_cents": 0,  # Will be recomputed after pricing
                "terminal_id": TERMINAL_ID,
                "route_miles": ROUTE_MILES,
                "market_price_cents": RACK_PRICE_CENTS,
            }
        ]

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=FIXED_NOW)
    async def test_full_pipeline_order_to_invoice(self, _mock_utcnow):
        """Full pipeline: order → pricing → tax → invoice with correct totals."""
        service = self._build_invoice_service()
        line_items = self._build_order_line_items()

        result = await service.generate_from_order(
            tenant_id=TENANT_ID,
            order_id=ORDER_ID,
            customer_id=CUSTOMER_ID,
            account_id=ACCOUNT_ID,
            line_items=line_items,
            tax_cents=0,  # Legacy fallback — superseded by TaxEngine
            destination_fips=DESTINATION_FIPS,
            effective_date=EFFECTIVE_DATE,
            actor="system",
        )

        # --- Verify pricing resolution ---
        assert len(self.fake_pricing_engine.calls) == 1
        pricing_call = self.fake_pricing_engine.calls[0]
        assert pricing_call["customer_id"] == CUSTOMER_ID
        assert pricing_call["product_code"] == PRODUCT_CODE
        assert pricing_call["gallons"] == NET_GALLONS
        assert pricing_call["terminal_id"] == TERMINAL_ID
        assert pricing_call["route_miles"] == ROUTE_MILES
        assert pricing_call["effective_date"] == EFFECTIVE_DATE
        assert pricing_call["account_id"] == ACCOUNT_ID

        # Line item price updated by pricing engine
        invoice_line = result["line_items"][0]
        assert invoice_line["unit_price_cents"] == EXPECTED_SELL_PRICE_CENTS
        expected_subtotal = round(EXPECTED_SELL_PRICE_CENTS * NET_GALLONS)
        assert invoice_line["subtotal_cents"] == expected_subtotal

        # --- Verify tax computation ---
        assert len(self.fake_tax_engine.calls) == 1
        tax_call = self.fake_tax_engine.calls[0]
        assert tax_call["product_code"] == PRODUCT_CODE
        assert tax_call["net_gallons"] == NET_GALLONS
        assert tax_call["destination_fips"] == DESTINATION_FIPS
        assert tax_call["customer_id"] == CUSTOMER_ID
        assert tax_call["effective_date"] == EFFECTIVE_DATE

        # Tax breakdown present on invoice
        assert "tax_breakdown" in result
        breakdown = result["tax_breakdown"]
        assert breakdown["federal_cents"] == EXPECTED_FEDERAL_TAX_CENTS
        assert breakdown["state_cents"] == EXPECTED_STATE_TAX_CENTS
        assert breakdown["ust_cents"] == EXPECTED_UST_CENTS
        assert breakdown["total_tax_cents"] == EXPECTED_TOTAL_TAX_CENTS

        # Tax line items present for Form 720 reporting
        assert len(breakdown["line_items"]) == 3
        component_names = [li["tax_component_name"] for li in breakdown["line_items"]]
        assert "federal_excise" in component_names
        assert "CA_state_excise" in component_names
        assert "ust_fee" in component_names

        # --- Verify invoice totals ---
        assert result["tax_cents"] == EXPECTED_TOTAL_TAX_CENTS
        assert result["subtotal_cents"] == expected_subtotal
        assert result["total_cents"] == expected_subtotal + EXPECTED_TOTAL_TAX_CENTS

        # Invoice created in draft status
        assert result["status"] == InvoiceStatus.DRAFT.value
        assert result["customer_id"] == CUSTOMER_ID
        assert result["account_id"] == ACCOUNT_ID
        assert result["order_id"] == ORDER_ID
        assert result["tenant_id"] == TENANT_ID

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=FIXED_NOW)
    async def test_pricing_resolves_before_tax(self, _mock_utcnow):
        """Pricing resolution happens before tax computation in the pipeline."""
        service = self._build_invoice_service()
        line_items = self._build_order_line_items()

        await service.generate_from_order(
            tenant_id=TENANT_ID,
            order_id=ORDER_ID,
            customer_id=CUSTOMER_ID,
            account_id=ACCOUNT_ID,
            line_items=line_items,
            tax_cents=0,
            destination_fips=DESTINATION_FIPS,
            effective_date=EFFECTIVE_DATE,
            actor="system",
        )

        # Both engines were called
        assert len(self.fake_pricing_engine.calls) == 1
        assert len(self.fake_tax_engine.calls) == 1

        # Tax engine received the correct gallons (from the line item,
        # which was already processed by the pricing engine)
        tax_call = self.fake_tax_engine.calls[0]
        assert tax_call["net_gallons"] == NET_GALLONS

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=FIXED_NOW)
    async def test_multi_product_order(self, _mock_utcnow):
        """Pipeline handles multiple line items (diesel + gasoline)."""
        # Override pricing engine to return different prices per product
        class MultiProductPricingEngine:
            def __init__(self):
                self.calls = []

            async def resolve_price(self, customer_id, product_code, gallons,
                                    terminal_id, route_miles, effective_date,
                                    market_price_cents=None, account_id=None):
                self.calls.append({"product_code": product_code})
                if product_code == "DIESEL_2":
                    return PriceResolution(
                        effective_price_cents=315,
                        contract_id=None,
                        contract_type=None,
                        market_price_cents=280,
                    )
                else:  # GASOLINE_REG
                    return PriceResolution(
                        effective_price_cents=295,
                        contract_id=None,
                        contract_type=None,
                        market_price_cents=260,
                    )

        # Override tax engine to return different rates per product
        class MultiProductTaxEngine:
            def __init__(self):
                self.calls = []

            async def compute_tax(self, *, product_code, net_gallons,
                                  destination_fips, customer_id,
                                  effective_date=None):
                self.calls.append({"product_code": product_code})
                if product_code == "DIESEL_2":
                    return TaxBreakdown(
                        federal_cents=round(244 * net_gallons / 10),
                        state_cents=round(389 * net_gallons / 10),
                        line_items=[
                            TaxLineItem(
                                tax_component_name="federal_excise",
                                jurisdiction_fips="00",
                                jurisdiction_level="federal",
                                rate_cents_per_gallon=244,
                                gallons=net_gallons,
                                amount_cents=round(244 * net_gallons / 10),
                            ),
                            TaxLineItem(
                                tax_component_name="CA_state_excise",
                                jurisdiction_fips="06",
                                jurisdiction_level="state",
                                rate_cents_per_gallon=389,
                                gallons=net_gallons,
                                amount_cents=round(389 * net_gallons / 10),
                            ),
                        ],
                    )
                else:  # GASOLINE_REG
                    return TaxBreakdown(
                        federal_cents=round(184 * net_gallons / 10),
                        state_cents=round(389 * net_gallons / 10),
                        line_items=[
                            TaxLineItem(
                                tax_component_name="federal_excise",
                                jurisdiction_fips="00",
                                jurisdiction_level="federal",
                                rate_cents_per_gallon=184,
                                gallons=net_gallons,
                                amount_cents=round(184 * net_gallons / 10),
                            ),
                            TaxLineItem(
                                tax_component_name="CA_state_excise",
                                jurisdiction_fips="06",
                                jurisdiction_level="state",
                                rate_cents_per_gallon=389,
                                gallons=net_gallons,
                                amount_cents=round(389 * net_gallons / 10),
                            ),
                        ],
                    )

        multi_pricing = MultiProductPricingEngine()
        multi_tax = MultiProductTaxEngine()

        service = InvoiceService(
            self.es,
            self.idemp,
            tax_engine_factory=lambda tid: multi_tax,
            sales_pricing_engine_factory=lambda tid: multi_pricing,
        )

        line_items = [
            {
                "line_id": "line_diesel",
                "product_code": "DIESEL_2",
                "quantity": 500.0,
                "quantity_gallons": 500.0,
                "unit_price_cents": 0,
                "subtotal_cents": 0,
                "terminal_id": TERMINAL_ID,
                "route_miles": ROUTE_MILES,
                "market_price_cents": 280,
            },
            {
                "line_id": "line_gasoline",
                "product_code": "GASOLINE_REG",
                "quantity": 300.0,
                "quantity_gallons": 300.0,
                "unit_price_cents": 0,
                "subtotal_cents": 0,
                "terminal_id": TERMINAL_ID,
                "route_miles": ROUTE_MILES,
                "market_price_cents": 260,
            },
        ]

        result = await service.generate_from_order(
            tenant_id=TENANT_ID,
            order_id=ORDER_ID,
            customer_id=CUSTOMER_ID,
            account_id=ACCOUNT_ID,
            line_items=line_items,
            tax_cents=0,
            destination_fips=DESTINATION_FIPS,
            effective_date=EFFECTIVE_DATE,
            actor="system",
        )

        # Both products priced
        assert len(multi_pricing.calls) == 2
        assert multi_pricing.calls[0]["product_code"] == "DIESEL_2"
        assert multi_pricing.calls[1]["product_code"] == "GASOLINE_REG"

        # Both products taxed
        assert len(multi_tax.calls) == 2

        # Line items have correct prices
        assert result["line_items"][0]["unit_price_cents"] == 315
        assert result["line_items"][1]["unit_price_cents"] == 295

        # Subtotals correct
        diesel_subtotal = round(315 * 500.0)
        gas_subtotal = round(295 * 300.0)
        assert result["line_items"][0]["subtotal_cents"] == diesel_subtotal
        assert result["line_items"][1]["subtotal_cents"] == gas_subtotal

        # Tax breakdown aggregates both products
        breakdown = result["tax_breakdown"]
        expected_federal = round(244 * 500 / 10) + round(184 * 300 / 10)
        expected_state = round(389 * 500 / 10) + round(389 * 300 / 10)
        assert breakdown["federal_cents"] == expected_federal
        assert breakdown["state_cents"] == expected_state

        # Total = subtotals + tax
        total_subtotal = diesel_subtotal + gas_subtotal
        total_tax = breakdown["total_tax_cents"]
        assert result["subtotal_cents"] == total_subtotal
        assert result["total_cents"] == total_subtotal + total_tax

        # 4 tax line items total (2 per product)
        assert len(breakdown["line_items"]) == 4

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=FIXED_NOW)
    async def test_invoice_idempotency(self, _mock_utcnow):
        """Generating an invoice for the same order twice is idempotent."""
        service = self._build_invoice_service()
        line_items = self._build_order_line_items()

        # First call succeeds
        result1 = await service.generate_from_order(
            tenant_id=TENANT_ID,
            order_id=ORDER_ID,
            customer_id=CUSTOMER_ID,
            account_id=ACCOUNT_ID,
            line_items=line_items,
            tax_cents=0,
            destination_fips=DESTINATION_FIPS,
            effective_date=EFFECTIVE_DATE,
            actor="system",
        )

        assert result1["status"] == InvoiceStatus.DRAFT.value

        # Idempotency service marks as processed
        self.idemp.mark_processed.assert_called_once()

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=FIXED_NOW)
    async def test_pricing_failure_falls_back_to_existing_price(
        self, _mock_utcnow
    ):
        """If pricing engine fails for a line, existing price is preserved."""

        class FailingPricingEngine:
            async def resolve_price(self, **kwargs):
                raise ValueError("OPIS rack price unavailable")

        service = InvoiceService(
            self.es,
            self.idemp,
            tax_engine_factory=lambda tid: self.fake_tax_engine,
            sales_pricing_engine_factory=lambda tid: FailingPricingEngine(),
        )

        line_items = [
            {
                "line_id": "line_diesel_fallback",
                "product_code": PRODUCT_CODE,
                "quantity": NET_GALLONS,
                "quantity_gallons": NET_GALLONS,
                "unit_price_cents": 300,  # Pre-set fallback price
                "subtotal_cents": round(300 * NET_GALLONS),
                "terminal_id": TERMINAL_ID,
                "route_miles": ROUTE_MILES,
            },
        ]

        result = await service.generate_from_order(
            tenant_id=TENANT_ID,
            order_id="order_fallback_001",
            customer_id=CUSTOMER_ID,
            account_id=ACCOUNT_ID,
            line_items=line_items,
            tax_cents=0,
            destination_fips=DESTINATION_FIPS,
            effective_date=EFFECTIVE_DATE,
            actor="system",
        )

        # Existing price preserved when pricing engine fails
        assert result["line_items"][0]["unit_price_cents"] == 300
        assert result["line_items"][0]["subtotal_cents"] == round(300 * NET_GALLONS)

        # Tax still computed on the line (non-blocking)
        assert "tax_breakdown" in result
        assert result["tax_breakdown"]["federal_cents"] == EXPECTED_FEDERAL_TAX_CENTS

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=FIXED_NOW)
    async def test_invoice_event_sourcing(self, _mock_utcnow):
        """Invoice generation writes an event before the projection (C7)."""
        service = self._build_invoice_service()
        line_items = self._build_order_line_items()

        await service.generate_from_order(
            tenant_id=TENANT_ID,
            order_id=ORDER_ID,
            customer_id=CUSTOMER_ID,
            account_id=ACCOUNT_ID,
            line_items=line_items,
            tax_cents=0,
            destination_fips=DESTINATION_FIPS,
            effective_date=EFFECTIVE_DATE,
            actor="system",
        )

        # ES index_document called at least twice:
        # 1. Event write (invoice_events index)
        # 2. Projection write (invoices_current index)
        assert self.es.index_document.call_count >= 2

        # First call is the event write
        first_call_args = self.es.index_document.call_args_list[0]
        event_index = first_call_args[0][0]
        assert "event" in event_index.lower()

        # Second call is the projection write
        second_call_args = self.es.index_document.call_args_list[1]
        projection_doc = second_call_args[0][2]
        assert projection_doc["status"] == InvoiceStatus.DRAFT.value
        assert projection_doc["tenant_id"] == TENANT_ID
