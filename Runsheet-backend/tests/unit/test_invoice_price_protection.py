"""Invoice lines honour price-protection contracts (OI-14).

The invoice pricing factory used to build ``SalesPricingEngine`` without the
``PriceProtectionService``, so an active fixed/cap contract never reached an
invoice while ``POST /pricing/resolve`` quoted it. These tests drive a real
engine from :func:`build_sales_pricing_engine` (the builder both paths use)
through ``InvoiceService.generate_from_order`` with a fake ES.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commerce.api import pricing_endpoints
from commerce.services.invoice_service import InvoiceService
from commerce.services.sales_pricing_engine import (
    SalesPricingEngine,
    build_sales_pricing_engine,
)
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "demo-tenant"
CUSTOMER = "QA-CUST"
PRODUCT = "DIESEL_2"
INVOICE_DATE = date(2026, 10, 6)
FIXED_NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
RULE_PRICE = 333


def _contract(**overrides: Any) -> Dict[str, Any]:
    doc = {
        "contract_id": "ppc-qa",
        "tenant_id": TENANT,
        "customer_id": CUSTOMER,
        "account_id": "QA-ACCT",
        "product_code": PRODUCT,
        "contract_type": "fixed_price",
        "start_date": "2026-01-01",
        "end_date": "2027-12-31",
        "contracted_gallons": 10_000.0,
        "remaining_gallons": 10_000.0,
        "fixed_price_cents": 310,
        "status": "active",
        "version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    doc.update(overrides)
    return doc


_RULE = {
    "rule_id": "rule-qa-posted",
    "tenant_id": TENANT,
    "product_code": PRODUCT,
    "strategy": "posted_price",
    "posted_price_cents": RULE_PRICE,
    "priority": 0,
    "effective_date": "2026-01-01",
    "status": "active",
    "created_at": "2026-01-01T00:00:00+00:00",
    "updated_at": "2026-01-01T00:00:00+00:00",
}


class _FakeES:
    """Serves one contract (or none) and one posted-price rule."""

    def __init__(self, contract: Optional[Dict[str, Any]] = None) -> None:
        self._contract = contract
        self.indexed: List[tuple] = []

    async def search_documents(self, index, query, size=10, **kwargs):
        if index == "price_protection_contracts" and self._contract:
            return {"hits": {"hits": [{"_source": dict(self._contract)}]}}
        if index == "pricing_rules":
            return {"hits": {"hits": [{"_source": dict(_RULE)}]}}
        return {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {"max_seq": {"value": None}},
        }

    async def index_document(self, index, doc_id, doc, **kwargs):
        self.indexed.append((index, doc_id, doc))
        return {"result": "created"}

    async def update_document(self, *args, **kwargs):
        return {"result": "updated"}

    async def get_document(self, *args, **kwargs):
        return None


class _Idempotency:
    async def is_duplicate(self, *args, **kwargs):
        return False

    async def mark_processed(self, *args, **kwargs):
        return None


def _service(es: _FakeES) -> InvoiceService:
    return InvoiceService(
        es,
        _Idempotency(),
        sales_pricing_engine_factory=lambda tid: build_sales_pricing_engine(
            es, tid
        ),
    )


async def _invoice(es: _FakeES, gallons: float, market_cents: int) -> Dict[str, Any]:
    with patch("commerce.services.invoice_service.utcnow", return_value=FIXED_NOW):
        return await _service(es).generate_from_order(
            tenant_id=TENANT,
            order_id="QA-ORD-1",
            customer_id=CUSTOMER,
            account_id="QA-ACCT",
            line_items=[
                {
                    "line_id": "line_qa_1",
                    "product_code": PRODUCT,
                    "quantity_gallons": gallons,
                    "unit_price_cents": 0,
                    "subtotal_cents": 0,
                    "terminal_id": "QA-TERM",
                    "market_price_cents": market_cents,
                }
            ],
            tax_cents=0,
            effective_date=INVOICE_DATE,
            actor="system",
        )


def test_builder_wires_the_contract_resolver():
    engine = build_sales_pricing_engine(_FakeES(), TENANT)
    assert isinstance(engine, SalesPricingEngine)
    assert engine._price_protection_service is not None


@pytest.mark.asyncio
async def test_fixed_price_contract_prices_the_line():
    result = await _invoice(_FakeES(_contract()), gallons=100.0, market_cents=350)
    [line] = result["line_items"]
    assert line["unit_price_cents"] == 310
    assert line["subtotal_cents"] == 31_000
    assert result["subtotal_cents"] == 31_000


@pytest.mark.asyncio
async def test_cap_contract_caps_a_higher_market():
    contract = _contract(
        contract_type="cap_price", fixed_price_cents=None, price_cap_cents=320
    )
    result = await _invoice(_FakeES(contract), gallons=100.0, market_cents=350)
    [line] = result["line_items"]
    assert line["unit_price_cents"] == 320
    assert result["subtotal_cents"] == 32_000


@pytest.mark.asyncio
async def test_cap_contract_passes_a_lower_market_through():
    contract = _contract(
        contract_type="cap_price", fixed_price_cents=None, price_cap_cents=320
    )
    result = await _invoice(_FakeES(contract), gallons=100.0, market_cents=301)
    [line] = result["line_items"]
    assert line["unit_price_cents"] == 301
    assert result["subtotal_cents"] == 30_100


@pytest.mark.asyncio
async def test_no_contract_uses_the_pricing_rule():
    result = await _invoice(_FakeES(None), gallons=100.0, market_cents=350)
    [line] = result["line_items"]
    assert line["unit_price_cents"] == RULE_PRICE
    assert result["subtotal_cents"] == 33_300


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"end_date": "2026-09-30"},  # window ended before the invoice date
        {"status": "expired"},
    ],
    ids=["end_date_passed", "status_expired"],
)
async def test_expired_contract_uses_the_pricing_rule(overrides):
    result = await _invoice(
        _FakeES(_contract(**overrides)), gallons=100.0, market_cents=350
    )
    [line] = result["line_items"]
    assert line["unit_price_cents"] == RULE_PRICE
    assert result["subtotal_cents"] == 33_300


@pytest.mark.asyncio
async def test_partial_contract_splits_into_contract_and_market_lines():
    # 40.5 contract gallons left for a 100.25 gal delivery.
    contract = _contract(remaining_gallons=40.5)
    result = await _invoice(_FakeES(contract), gallons=100.25, market_cents=349)
    contract_line, market_line = result["line_items"]

    assert contract_line["line_id"] == "line_qa_1"
    assert contract_line["quantity_gallons"] == pytest.approx(40.5)
    assert contract_line["unit_price_cents"] == 310
    assert contract_line["subtotal_cents"] == 12_555  # 40.5 × 310

    assert market_line["line_id"].startswith("line_")
    assert market_line["line_id"] != "line_qa_1"
    assert market_line["quantity_gallons"] == pytest.approx(59.75)
    assert market_line["unit_price_cents"] == 349
    assert market_line["subtotal_cents"] == 20_853  # 59.75 × 349 = 20852.75

    # The contract price is applied once, to the contracted gallons only.
    assert (
        contract_line["quantity_gallons"] + market_line["quantity_gallons"]
        == pytest.approx(100.25)
    )
    assert result["subtotal_cents"] == 12_555 + 20_853


@pytest.mark.asyncio
async def test_exhausted_contract_bills_everything_at_market():
    contract = _contract(remaining_gallons=0.0)
    result = await _invoice(_FakeES(contract), gallons=100.0, market_cents=349)
    [line] = result["line_items"]
    assert line["unit_price_cents"] == 349
    assert line["quantity_gallons"] == pytest.approx(100.0)
    assert result["subtotal_cents"] == 34_900


@pytest.mark.asyncio
async def test_invoice_factory_and_resolve_endpoint_agree():
    """The bootstrap factory and POST /pricing/resolve quote the same price."""
    contract = _contract(
        contract_type="cap_price", fixed_price_cents=None, price_cap_cents=320
    )
    es = _FakeES(contract)

    result = await _invoice(es, gallons=100.0, market_cents=350)
    invoice_price = result["line_items"][0]["unit_price_cents"]

    pricing_endpoints.configure_pricing_api(es_service=es)
    try:
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(pricing_endpoints.router)
        install_test_auth(app)
        client = TestClient(app, raise_server_exceptions=False)
        settings = SimpleNamespace(
            commerce_backbone_enabled=True, commerce_pricing_engine_enabled=True
        )
        with patch(
            "commerce.api.price_book_endpoints.get_settings", return_value=settings
        ):
            resp = client.post(
                "/api/commerce/pricing/resolve",
                json={
                    "customer_id": CUSTOMER,
                    "product_code": PRODUCT,
                    "gallons": 100,
                    "terminal_id": "QA-TERM",
                    "effective_date": INVOICE_DATE.isoformat(),
                    "market_price_cents": 350,
                },
                headers=auth_headers(TENANT, roles=["platform_admin", "admin"]),
            )
    finally:
        pricing_endpoints._es_service = None
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["effective_price_cents"] == invoice_price == 320

