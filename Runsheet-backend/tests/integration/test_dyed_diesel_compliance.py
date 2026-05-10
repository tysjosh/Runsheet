"""Integration test: Dyed diesel order → 637M validation → compartment load → invoice with exemption.

Verifies the full dyed diesel compliance chain:
1. Customer orders dyed diesel (OFF_ROAD_DIESEL product code)
2. DyedDieselEnforcer validates the customer has a valid IRS 637M certificate
3. Load plan assigns dyed diesel to a dyed-compatible compartment
4. DyedDieselEnforcer validates the compartment is dyed-compatible
5. Invoice is generated with road-use excise tax exemption applied
6. DyedDieselEnforcer validates the invoice excludes federal+state road-use excise

Also tests failure paths:
- Customer without valid 637M certificate → order rejected with `dyed.no_valid_exemption`
- Dyed diesel assigned to clear-only compartment → rejected with `dyed.compartment_incompatible`
- Expired 637M certificate → future orders blocked

ES and external dependencies are mocked via AsyncMock fixtures.

Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from commerce.services.invoice_service import InvoiceService
from commerce.services.price_protection_service import PriceResolution
from compliance.services.dyed_diesel_enforcer import (
    DyedDieselEnforcer,
    ValidationResult,
)
from compliance.services.tax_engine import (
    TaxBreakdown,
    TaxLineItem,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant_dyed_integ"
CUSTOMER_ID = "cust_farm_ranch_01"
CUSTOMER_NO_CERT = "cust_no_cert_02"
CUSTOMER_EXPIRED_CERT = "cust_expired_03"
ACCOUNT_ID = "acct_farm_001"
ORDER_ID = "order_dyed_diesel_001"
COMPARTMENT_ID_DYED = "comp_truck42_dyed_01"
COMPARTMENT_ID_CLEAR = "comp_truck42_clear_02"
DESTINATION_FIPS = "48201"  # Harris County, TX
EFFECTIVE_DATE = date(2026, 7, 15)
FIXED_NOW = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)

# Product details
PRODUCT_CODE = "OFF_ROAD_DIESEL"
NET_GALLONS = 500.0
TERMINAL_ID = "terminal_houston_01"
ROUTE_MILES = 30.0

# Pricing
SELL_PRICE_CENTS = 290  # $2.90/gallon for off-road diesel

# Tax rates — for dyed diesel, road-use excise should be EXCLUDED
# Only UST and environmental fees apply
UST_RATE_STORED = 20  # 2.0¢/gal UST fee
ENVIRONMENTAL_RATE_STORED = 10  # 1.0¢/gal environmental fee

# Expected tax amounts (with exemption: no federal, no state excise)
EXPECTED_UST_CENTS = round(UST_RATE_STORED * NET_GALLONS / 10)  # 1000
EXPECTED_ENVIRONMENTAL_CENTS = round(ENVIRONMENTAL_RATE_STORED * NET_GALLONS / 10)  # 500
EXPECTED_TOTAL_TAX_CENTS = EXPECTED_UST_CENTS + EXPECTED_ENVIRONMENTAL_CENTS

# Certificate data
VALID_CERT_NUMBER = "637M-2026-FARM-001"
VALID_CERT_EXPIRY = "2027-12-31"
EXPIRED_CERT_EXPIRY = "2025-06-30"


# ---------------------------------------------------------------------------
# Mock ES service
# ---------------------------------------------------------------------------


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService with configurable responses."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
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
# ES response builders for DyedDieselEnforcer queries
# ---------------------------------------------------------------------------


def _build_valid_cert_response(customer_id: str) -> Dict[str, Any]:
    """Build an ES response containing a valid 637M certificate."""
    return {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "exemption_id": "exempt_637m_001",
                        "customer_id": customer_id,
                        "exemption_type": "637M",
                        "certificate_number": VALID_CERT_NUMBER,
                        "expiry_date": VALID_CERT_EXPIRY,
                        "status": "valid",
                        "tenant_id": TENANT_ID,
                    }
                }
            ],
            "total": {"value": 1},
        }
    }


def _build_no_cert_response() -> Dict[str, Any]:
    """Build an ES response with no matching certificates."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
    }


def _build_expired_cert_response(customer_id: str) -> Dict[str, Any]:
    """Build an ES response with an expired certificate (won't match date filter)."""
    # The ES query filters by expiry_date >= today, so an expired cert
    # won't appear in results. Return empty to simulate this.
    return {
        "hits": {"hits": [], "total": {"value": 0}},
    }


def _build_dyed_compartment_response() -> Dict[str, Any]:
    """Build an ES response for a dyed-compatible compartment."""
    return {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "compartment_id": COMPARTMENT_ID_DYED,
                        "truck_id": "truck_42",
                        "capacity_gallons": 3000,
                        "dyed_compatible": True,
                        "product_type": "diesel",
                        "tenant_id": TENANT_ID,
                    }
                }
            ],
            "total": {"value": 1},
        }
    }


def _build_clear_only_compartment_response() -> Dict[str, Any]:
    """Build an ES response for a clear-only compartment."""
    return {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "compartment_id": COMPARTMENT_ID_CLEAR,
                        "truck_id": "truck_42",
                        "capacity_gallons": 3000,
                        "dyed_compatible": False,
                        "product_type": "diesel",
                        "tenant_id": TENANT_ID,
                    }
                }
            ],
            "total": {"value": 1},
        }
    }


def _build_dyed_invoice_with_exemption() -> Dict[str, Any]:
    """Build an ES response for a dyed-diesel invoice with correct exemption."""
    return {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "invoice_id": "inv_dyed_001",
                        "tenant_id": TENANT_ID,
                        "customer_id": CUSTOMER_ID,
                        "line_items": [
                            {
                                "product_code": "OFF_ROAD_DIESEL",
                                "quantity_gallons": NET_GALLONS,
                                "unit_price_cents": SELL_PRICE_CENTS,
                            }
                        ],
                        "tax_breakdown": {
                            "federal_cents": 0,
                            "state_cents": 0,
                            "county_cents": 0,
                            "city_cents": 0,
                            "ust_cents": EXPECTED_UST_CENTS,
                            "spcc_cents": 0,
                            "environmental_cents": EXPECTED_ENVIRONMENTAL_CENTS,
                            "total_tax_cents": EXPECTED_TOTAL_TAX_CENTS,
                            "line_items": [
                                {
                                    "tax_component_name": "ust_fee",
                                    "jurisdiction_fips": "48",
                                    "jurisdiction_level": "state",
                                    "rate_cents_per_gallon": UST_RATE_STORED,
                                    "gallons": NET_GALLONS,
                                    "amount_cents": EXPECTED_UST_CENTS,
                                },
                                {
                                    "tax_component_name": "environmental_fee",
                                    "jurisdiction_fips": "48",
                                    "jurisdiction_level": "state",
                                    "rate_cents_per_gallon": ENVIRONMENTAL_RATE_STORED,
                                    "gallons": NET_GALLONS,
                                    "amount_cents": EXPECTED_ENVIRONMENTAL_CENTS,
                                },
                            ],
                            "exemptions_applied": ["637M"],
                        },
                    }
                }
            ],
            "total": {"value": 1},
        }
    }


# ---------------------------------------------------------------------------
# Fake TaxEngine that simulates dyed-diesel exemption behavior
# ---------------------------------------------------------------------------


class FakeTaxEngineWithExemption:
    """Simulates TaxEngine.compute_tax with dyed-diesel road-use exemption.

    For OFF_ROAD_DIESEL products with a valid 637M exemption, returns a
    TaxBreakdown with federal_cents=0 and state_cents=0 (road-use excise
    excluded), but UST and environmental fees still applied.
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

        ust_cents = round(UST_RATE_STORED * net_gallons / 10)
        env_cents = round(ENVIRONMENTAL_RATE_STORED * net_gallons / 10)

        # Road-use excise excluded for dyed diesel with 637M exemption
        return TaxBreakdown(
            federal_cents=0,
            state_cents=0,
            county_cents=0,
            city_cents=0,
            ust_cents=ust_cents,
            spcc_cents=0,
            environmental_cents=env_cents,
            line_items=[
                TaxLineItem(
                    tax_component_name="ust_fee",
                    jurisdiction_fips="48",
                    jurisdiction_level="state",
                    rate_cents_per_gallon=UST_RATE_STORED,
                    gallons=net_gallons,
                    amount_cents=ust_cents,
                ),
                TaxLineItem(
                    tax_component_name="environmental_fee",
                    jurisdiction_fips="48",
                    jurisdiction_level="state",
                    rate_cents_per_gallon=ENVIRONMENTAL_RATE_STORED,
                    gallons=net_gallons,
                    amount_cents=env_cents,
                ),
            ],
            exemptions_applied=["637M"],
        )


# ---------------------------------------------------------------------------
# Fake SalesPricingEngine
# ---------------------------------------------------------------------------


class FakePricingEngine:
    """Simulates SalesPricingEngine.resolve_price for off-road diesel."""

    def __init__(self) -> None:
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
        self.calls.append({"customer_id": customer_id, "product_code": product_code})
        return PriceResolution(
            effective_price_cents=SELL_PRICE_CENTS,
            contract_id=None,
            contract_type=None,
            market_price_cents=SELL_PRICE_CENTS,
        )


# ===========================================================================
# Integration Test: Dyed Diesel Compliance Chain (Happy Path)
# ===========================================================================


class TestDyedDieselComplianceHappyPath:
    """End-to-end: dyed diesel order → 637M validation → compartment → invoice.

    Verifies the full compliance chain when a customer with a valid IRS 637M
    certificate orders dyed diesel, loads into a dyed-compatible compartment,
    and receives an invoice with road-use excise tax exemption applied.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up service instances with mocked ES."""
        self.es = _make_es_service()
        self.idemp = _make_idempotency_service()
        self.enforcer = DyedDieselEnforcer(es_service=self.es, signal_bus=None)
        self.fake_tax_engine = FakeTaxEngineWithExemption(TENANT_ID)
        self.fake_pricing_engine = FakePricingEngine()

    @pytest.mark.asyncio
    async def test_step1_order_validated_with_valid_637m_cert(self):
        """Step 1-2: Customer orders dyed diesel, 637M certificate is validated."""
        # Configure ES to return a valid 637M certificate
        self.es.search_documents = AsyncMock(
            return_value=_build_valid_cert_response(CUSTOMER_ID)
        )

        result = await self.enforcer.validate_order(
            tenant_id=TENANT_ID,
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
        )

        assert result.valid is True
        assert result.error_code is None

    @pytest.mark.asyncio
    async def test_step2_compartment_validated_dyed_compatible(self):
        """Step 3-4: Load plan assigns dyed diesel to dyed-compatible compartment."""
        # Configure ES to return a dyed-compatible compartment
        self.es.search_documents = AsyncMock(
            return_value=_build_dyed_compartment_response()
        )

        result = await self.enforcer.validate_load_plan(
            tenant_id=TENANT_ID,
            compartment_id=COMPARTMENT_ID_DYED,
            product_code=PRODUCT_CODE,
        )

        assert result.valid is True
        assert result.error_code is None

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=FIXED_NOW)
    async def test_step3_invoice_generated_with_tax_exemption(self, _mock_utcnow):
        """Step 5: Invoice generated with road-use excise tax exemption."""
        service = InvoiceService(
            self.es,
            self.idemp,
            tax_engine_factory=lambda tid: self.fake_tax_engine,
            sales_pricing_engine_factory=lambda tid: self.fake_pricing_engine,
        )

        line_items = [
            {
                "line_id": "line_dyed_diesel_001",
                "product_code": PRODUCT_CODE,
                "quantity": NET_GALLONS,
                "quantity_gallons": NET_GALLONS,
                "unit_price_cents": 0,
                "subtotal_cents": 0,
                "terminal_id": TERMINAL_ID,
                "route_miles": ROUTE_MILES,
                "market_price_cents": SELL_PRICE_CENTS,
            }
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

        # Tax engine was called with correct parameters
        assert len(self.fake_tax_engine.calls) == 1
        tax_call = self.fake_tax_engine.calls[0]
        assert tax_call["product_code"] == PRODUCT_CODE
        assert tax_call["customer_id"] == CUSTOMER_ID

        # Tax breakdown shows exemption applied
        breakdown = result["tax_breakdown"]
        assert breakdown["federal_cents"] == 0, "Federal excise should be 0 for dyed diesel"
        assert breakdown["state_cents"] == 0, "State excise should be 0 for dyed diesel"
        assert breakdown["ust_cents"] == EXPECTED_UST_CENTS, "UST fee still applies"
        assert breakdown["environmental_cents"] == EXPECTED_ENVIRONMENTAL_CENTS
        assert breakdown["total_tax_cents"] == EXPECTED_TOTAL_TAX_CENTS
        assert "637M" in breakdown["exemptions_applied"]

        # No federal_excise or state_excise line items
        component_names = [li["tax_component_name"] for li in breakdown["line_items"]]
        assert "federal_excise" not in component_names
        assert "state_excise" not in component_names
        # UST and environmental fees are present
        assert "ust_fee" in component_names
        assert "environmental_fee" in component_names

    @pytest.mark.asyncio
    async def test_step4_invoice_validation_passes(self):
        """Step 6: DyedDieselEnforcer validates invoice excludes road-use excise."""
        # Configure ES to return the invoice with correct exemption
        self.es.search_documents = AsyncMock(
            return_value=_build_dyed_invoice_with_exemption()
        )

        result = await self.enforcer.validate_invoice(
            tenant_id=TENANT_ID,
            invoice_id="inv_dyed_001",
        )

        assert result.valid is True
        assert result.error_code is None

    @pytest.mark.asyncio
    async def test_full_chain_end_to_end(self):
        """Full chain: order → compartment → invoice generation → invoice validation."""
        # Step 1: Validate order (valid 637M cert)
        self.es.search_documents = AsyncMock(
            return_value=_build_valid_cert_response(CUSTOMER_ID)
        )
        order_result = await self.enforcer.validate_order(
            tenant_id=TENANT_ID,
            customer_id=CUSTOMER_ID,
            product_code=PRODUCT_CODE,
        )
        assert order_result.valid is True

        # Step 2: Validate compartment (dyed-compatible)
        self.es.search_documents = AsyncMock(
            return_value=_build_dyed_compartment_response()
        )
        load_result = await self.enforcer.validate_load_plan(
            tenant_id=TENANT_ID,
            compartment_id=COMPARTMENT_ID_DYED,
            product_code=PRODUCT_CODE,
        )
        assert load_result.valid is True

        # Step 3: Validate invoice (exemption applied correctly)
        self.es.search_documents = AsyncMock(
            return_value=_build_dyed_invoice_with_exemption()
        )
        invoice_result = await self.enforcer.validate_invoice(
            tenant_id=TENANT_ID,
            invoice_id="inv_dyed_001",
        )
        assert invoice_result.valid is True


# ===========================================================================
# Integration Test: Failure Paths
# ===========================================================================


class TestDyedDieselComplianceFailurePaths:
    """Failure paths: no cert, clear-only compartment, expired cert.

    Verifies that the DyedDieselEnforcer correctly rejects orders and load
    plans when compliance requirements are not met.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up service instances with mocked ES."""
        self.es = _make_es_service()
        self.enforcer = DyedDieselEnforcer(es_service=self.es, signal_bus=None)

    @pytest.mark.asyncio
    async def test_order_rejected_without_637m_certificate(self):
        """Customer without valid 637M certificate → order rejected."""
        # Configure ES to return no matching certificates
        self.es.search_documents = AsyncMock(
            return_value=_build_no_cert_response()
        )

        result = await self.enforcer.validate_order(
            tenant_id=TENANT_ID,
            customer_id=CUSTOMER_NO_CERT,
            product_code=PRODUCT_CODE,
        )

        assert result.valid is False
        assert result.error_code == "dyed.no_valid_exemption"
        assert "637M" in result.message
        assert CUSTOMER_NO_CERT in result.message

    @pytest.mark.asyncio
    async def test_load_plan_rejected_clear_only_compartment(self):
        """Dyed diesel assigned to clear-only compartment → rejected."""
        # Configure ES to return a clear-only compartment
        self.es.search_documents = AsyncMock(
            return_value=_build_clear_only_compartment_response()
        )

        result = await self.enforcer.validate_load_plan(
            tenant_id=TENANT_ID,
            compartment_id=COMPARTMENT_ID_CLEAR,
            product_code=PRODUCT_CODE,
        )

        assert result.valid is False
        assert result.error_code == "dyed.compartment_incompatible"
        assert "clear-only" in result.message

    @pytest.mark.asyncio
    async def test_expired_certificate_blocks_future_orders(self):
        """Expired 637M certificate → future dyed diesel orders blocked.

        When a customer's 637M certificate has expired, the ES query
        filters by expiry_date >= today, so the expired cert won't appear
        in results. This effectively blocks the order (Req 6.6).
        """
        # Configure ES to return no results (expired cert filtered out by date)
        self.es.search_documents = AsyncMock(
            return_value=_build_expired_cert_response(CUSTOMER_EXPIRED_CERT)
        )

        result = await self.enforcer.validate_order(
            tenant_id=TENANT_ID,
            customer_id=CUSTOMER_EXPIRED_CERT,
            product_code=PRODUCT_CODE,
        )

        assert result.valid is False
        assert result.error_code == "dyed.no_valid_exemption"
        assert CUSTOMER_EXPIRED_CERT in result.message

    @pytest.mark.asyncio
    async def test_non_dyed_product_bypasses_all_checks(self):
        """Non-dyed diesel product (DIESEL_2) bypasses all enforcer checks."""
        # ES should NOT be called for non-dyed products
        result = await self.enforcer.validate_order(
            tenant_id=TENANT_ID,
            customer_id=CUSTOMER_NO_CERT,
            product_code="DIESEL_2",
        )

        assert result.valid is True
        # ES was not queried
        self.es.search_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_dyed_product_bypasses_compartment_check(self):
        """Non-dyed product bypasses compartment dyed-compatibility check."""
        result = await self.enforcer.validate_load_plan(
            tenant_id=TENANT_ID,
            compartment_id=COMPARTMENT_ID_CLEAR,
            product_code="DIESEL_2",
        )

        assert result.valid is True
        self.es.search_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_invoice_with_road_use_excise_fails_validation(self):
        """Invoice with road-use excise on dyed diesel → validation fails."""
        # Build an invoice that incorrectly has federal/state excise
        bad_invoice_response = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "invoice_id": "inv_bad_001",
                            "tenant_id": TENANT_ID,
                            "customer_id": CUSTOMER_ID,
                            "line_items": [
                                {
                                    "product_code": "OFF_ROAD_DIESEL",
                                    "quantity_gallons": NET_GALLONS,
                                    "unit_price_cents": SELL_PRICE_CENTS,
                                }
                            ],
                            "tax_breakdown": {
                                "federal_cents": 12200,  # Should be 0!
                                "state_cents": 10000,  # Should be 0!
                                "county_cents": 0,
                                "city_cents": 0,
                                "ust_cents": EXPECTED_UST_CENTS,
                                "spcc_cents": 0,
                                "environmental_cents": EXPECTED_ENVIRONMENTAL_CENTS,
                                "total_tax_cents": 12200 + 10000 + EXPECTED_UST_CENTS + EXPECTED_ENVIRONMENTAL_CENTS,
                                "line_items": [
                                    {
                                        "tax_component_name": "federal_excise",
                                        "jurisdiction_fips": "00",
                                        "jurisdiction_level": "federal",
                                        "rate_cents_per_gallon": 244,
                                        "gallons": NET_GALLONS,
                                        "amount_cents": 12200,
                                    },
                                    {
                                        "tax_component_name": "state_excise",
                                        "jurisdiction_fips": "48",
                                        "jurisdiction_level": "state",
                                        "rate_cents_per_gallon": 200,
                                        "gallons": NET_GALLONS,
                                        "amount_cents": 10000,
                                    },
                                ],
                                "exemptions_applied": [],
                            },
                        }
                    }
                ],
                "total": {"value": 1},
            }
        }

        self.es.search_documents = AsyncMock(return_value=bad_invoice_response)

        result = await self.enforcer.validate_invoice(
            tenant_id=TENANT_ID,
            invoice_id="inv_bad_001",
        )

        assert result.valid is False
        assert result.error_code == "dyed.tax_exemption_not_applied"


# ===========================================================================
# Integration Test: Audit Log
# ===========================================================================


class TestDyedDieselAuditLog:
    """Verifies that dyed-diesel sales are logged for IRS audit readiness (Req 6.7)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up service instances with mocked ES."""
        self.es = _make_es_service()
        self.enforcer = DyedDieselEnforcer(es_service=self.es, signal_bus=None)

    @pytest.mark.asyncio
    async def test_dyed_sale_logged_to_audit_index(self):
        """Every dyed-diesel sale is persisted to the audit log."""
        await self.enforcer.log_dyed_sale(
            tenant_id=TENANT_ID,
            customer_id=CUSTOMER_ID,
            certificate_id=VALID_CERT_NUMBER,
            certificate_expiry=VALID_CERT_EXPIRY,
            gallons=NET_GALLONS,
            invoice_id="inv_dyed_001",
            product_code=PRODUCT_CODE,
        )

        # Verify ES index_document was called with the audit log
        self.es.index_document.assert_called_once()
        call_args = self.es.index_document.call_args[0]

        # First arg is the index name
        assert call_args[0] == "dyed_diesel_audit_log"

        # Third arg is the document body
        doc = call_args[2]
        assert doc["tenant_id"] == TENANT_ID
        assert doc["customer_id"] == CUSTOMER_ID
        assert doc["certificate_id"] == VALID_CERT_NUMBER
        assert doc["certificate_expiry"] == VALID_CERT_EXPIRY
        assert doc["gallons"] == NET_GALLONS
        assert doc["invoice_id"] == "inv_dyed_001"
        assert doc["product_code"] == PRODUCT_CODE
        assert "timestamp" in doc
