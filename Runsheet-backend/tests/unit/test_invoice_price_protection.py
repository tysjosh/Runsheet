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
    """Serves one contract (or none) and one posted-price rule.

    Contract and invoice-projection updates are applied, so volume consumed
    by one invoice is visible to the next (D14c) and void can read back what
    the invoice consumed.
    """

    def __init__(
        self, contract: Optional[Dict[str, Any]] = None, rule: bool = True
    ) -> None:
        self._contract = contract
        self._rule = rule
        self.indexed: List[tuple] = []
        self.invoices: Dict[str, Dict[str, Any]] = {}
        self.fail_invoice_write = False

    async def search_documents(self, index, query, size=10, **kwargs):
        if index == "price_protection_contracts" and self._contract:
            return {"hits": {"hits": [{"_source": dict(self._contract)}]}}
        if index == "pricing_rules" and self._rule:
            return {"hits": {"hits": [{"_source": dict(_RULE)}]}}
        if index == "invoices_current" and self.invoices:
            invoice_id = _term(query, "invoice_id")
            doc = self.invoices.get(invoice_id)
            hits = [{"_source": dict(doc)}] if doc else []
            return {"hits": {"hits": hits, "total": {"value": len(hits)}}}
        return {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {"max_seq": {"value": None}},
        }

    async def index_document(self, index, doc_id, doc, **kwargs):
        if index == "invoices_current":
            if self.fail_invoice_write:
                raise RuntimeError("store down")
            self.invoices[doc_id] = dict(doc)
        self.indexed.append((index, doc_id, doc))
        return {"result": "created"}

    async def update_document(self, index, doc_id, partial, **kwargs):
        if index == "price_protection_contracts" and self._contract:
            self._contract.update(partial)
        if index == "invoices_current" and doc_id in self.invoices:
            self.invoices[doc_id].update(partial)
        return {"result": "updated"}

    async def get_document(self, *args, **kwargs):
        return None


def _term(query: Dict[str, Any], field: str) -> Optional[str]:
    """First ``term`` value for ``field`` anywhere in an ES query body."""
    if isinstance(query, dict):
        term = query.get("term")
        if isinstance(term, dict) and field in term:
            value = term[field]
            return value.get("value") if isinstance(value, dict) else value
        for value in query.values():
            found = _term(value, field)
            if found is not None:
                return found
    elif isinstance(query, list):
        for value in query:
            found = _term(value, field)
            if found is not None:
                return found
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


async def _invoice(
    es: _FakeES, gallons: float, market_cents: int, order_id: str = "QA-ORD-1"
) -> Dict[str, Any]:
    with patch("commerce.services.invoice_service.utcnow", return_value=FIXED_NOW):
        return await _service(es).generate_from_order(
            tenant_id=TENANT,
            order_id=order_id,
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
async def test_partial_contract_splits_into_contract_and_rule_priced_lines():
    # 40.5 contract gallons left for a 100.25 gal delivery. The excess bills
    # at the customer's rule price (333), not bare rack (349) (D14c).
    contract = _contract(remaining_gallons=40.5)
    es = _FakeES(contract)
    result = await _invoice(es, gallons=100.25, market_cents=349)
    contract_line, market_line = result["line_items"]

    assert contract_line["line_id"] == "line_qa_1"
    assert contract_line["quantity_gallons"] == pytest.approx(40.5)
    assert contract_line["unit_price_cents"] == 310
    assert contract_line["subtotal_cents"] == 12_555  # 40.5 × 310

    assert market_line["line_id"].startswith("line_")
    assert market_line["line_id"] != "line_qa_1"
    assert market_line["quantity_gallons"] == pytest.approx(59.75)
    assert market_line["unit_price_cents"] == RULE_PRICE
    assert market_line["subtotal_cents"] == 19_897  # 59.75 × 333 = 19896.75

    # The contract price is applied once, to the contracted gallons only.
    assert (
        contract_line["quantity_gallons"] + market_line["quantity_gallons"]
        == pytest.approx(100.25)
    )
    assert result["subtotal_cents"] == 12_555 + 19_897
    # The contract is now used up.
    assert es._contract["remaining_gallons"] == pytest.approx(0.0)
    assert result["contract_consumption"] == [
        {"contract_id": "ppc-qa", "line_id": "line_qa_1", "gallons": 40.5}
    ]


@pytest.mark.asyncio
async def test_exhausted_contract_bills_everything_at_the_rule_price():
    """Running out of a contract must not bill below a no-contract customer."""
    contract = _contract(remaining_gallons=0.0)
    result = await _invoice(_FakeES(contract), gallons=100.0, market_cents=349)
    [line] = result["line_items"]
    assert line["unit_price_cents"] == RULE_PRICE
    assert line["quantity_gallons"] == pytest.approx(100.0)
    assert result["subtotal_cents"] == 33_300
    assert "contract_consumption" not in result


@pytest.mark.asyncio
async def test_exhausted_contract_without_a_rule_bills_at_market():
    """No price-book rule means no normal price; the excess bills at market."""
    contract = _contract(remaining_gallons=0.0)
    es = _FakeES(contract, rule=False)
    result = await _invoice(es, gallons=100.0, market_cents=349)
    [line] = result["line_items"]
    assert line["unit_price_cents"] == 349
    assert result["subtotal_cents"] == 34_900


@pytest.mark.asyncio
async def test_contract_volume_is_cumulative_across_invoices():
    """D14c: two 800 gal invoices against a 1,000 gal contract. The first
    takes 800 at the contract price; the second gets the last 200 and the
    other 600 bill at the rule price."""
    contract = _contract(contracted_gallons=1_000.0, remaining_gallons=1_000.0)
    es = _FakeES(contract)

    first = await _invoice(es, gallons=800.0, market_cents=350, order_id="QA-ORD-1")
    [line] = first["line_items"]
    assert line["unit_price_cents"] == 310
    assert first["subtotal_cents"] == 248_000  # 800 × 310
    assert es._contract["remaining_gallons"] == pytest.approx(200.0)

    second = await _invoice(es, gallons=800.0, market_cents=350, order_id="QA-ORD-2")
    contract_line, excess_line = second["line_items"]
    assert (contract_line["quantity_gallons"], contract_line["unit_price_cents"]) == (200.0, 310)
    assert contract_line["subtotal_cents"] == 62_000
    assert (excess_line["quantity_gallons"], excess_line["unit_price_cents"]) == (600.0, RULE_PRICE)
    assert excess_line["subtotal_cents"] == 199_800
    assert second["subtotal_cents"] == 261_800
    assert es._contract["remaining_gallons"] == pytest.approx(0.0)

    third = await _invoice(es, gallons=100.0, market_cents=350, order_id="QA-ORD-3")
    [line] = third["line_items"]
    assert line["unit_price_cents"] == RULE_PRICE
    assert third["subtotal_cents"] == 33_300


@pytest.mark.asyncio
async def test_gallons_taken_since_the_quote_bill_at_the_rule_price():
    """The resolver saw 1,000 gal left, but another invoice took all but 30
    before this one consumed: 30 at the contract price, 70 at the rule."""
    from commerce.services.price_protection_service import PriceProtectionService

    async def _grant_30(self, contract_id, gallons):
        return 30.0

    with patch.object(PriceProtectionService, "consume_gallons", _grant_30):
        result = await _invoice(_FakeES(_contract()), gallons=100.0, market_cents=350)
    contract_line, excess_line = result["line_items"]
    assert contract_line["subtotal_cents"] == 9_300  # 30 × 310
    assert excess_line["subtotal_cents"] == 23_310  # 70 × 333
    assert result["subtotal_cents"] == 32_610


@pytest.mark.asyncio
async def test_void_gives_the_contract_gallons_back():
    contract = _contract(contracted_gallons=1_000.0, remaining_gallons=1_000.0)
    es = _FakeES(contract)
    invoice = await _invoice(es, gallons=800.0, market_cents=350)
    assert es._contract["remaining_gallons"] == pytest.approx(200.0)

    await _service(es).void(
        tenant_id=TENANT, invoice_id=invoice["invoice_id"],
        reason="QA test", actor="qa",
    )
    assert es._contract["remaining_gallons"] == pytest.approx(1_000.0)

    # The next invoice gets the full contract again.
    again = await _invoice(es, gallons=800.0, market_cents=350, order_id="QA-ORD-2")
    assert again["subtotal_cents"] == 248_000


@pytest.mark.asyncio
async def test_void_reactivates_an_exhausted_contract():
    contract = _contract(contracted_gallons=100.0, remaining_gallons=100.0)
    es = _FakeES(contract)
    invoice = await _invoice(es, gallons=100.0, market_cents=350)
    es._contract["status"] = "exhausted"  # the lifecycle cron ran
    await _service(es).void(
        tenant_id=TENANT, invoice_id=invoice["invoice_id"],
        reason="QA test", actor="qa",
    )
    assert es._contract["remaining_gallons"] == pytest.approx(100.0)
    assert es._contract["status"] == "active"


@pytest.mark.asyncio
async def test_failed_invoice_write_gives_the_gallons_back():
    contract = _contract(contracted_gallons=1_000.0, remaining_gallons=1_000.0)
    es = _FakeES(contract)
    es.fail_invoice_write = True
    with pytest.raises(RuntimeError, match="store down"):
        await _invoice(es, gallons=800.0, market_cents=350)
    assert es._contract["remaining_gallons"] == pytest.approx(1_000.0)


@pytest.mark.asyncio
async def test_refused_invoice_consumes_nothing():
    """POD gallons mismatch refuses the invoice before any consumption."""
    contract = _contract(contracted_gallons=1_000.0, remaining_gallons=1_000.0)
    es = _FakeES(contract)
    with patch("commerce.services.invoice_service.utcnow", return_value=FIXED_NOW):
        with pytest.raises(Exception, match="POD actual gallons"):
            await _service(es).generate_from_order(
                tenant_id=TENANT, order_id="QA-ORD-1", customer_id=CUSTOMER,
                account_id="QA-ACCT",
                line_items=[{
                    "line_id": "line_qa_1", "product_code": PRODUCT,
                    "quantity_gallons": 800.0, "unit_price_cents": 0,
                    "subtotal_cents": 0, "terminal_id": "QA-TERM",
                    "market_price_cents": 350,
                }],
                tax_cents=0, effective_date=INVOICE_DATE, actor="system",
                delivery_result={"actual_gallons": 700.0, "pod_id": "QA-POD"},
            )
    assert es._contract["remaining_gallons"] == pytest.approx(1_000.0)


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

