"""Commerce 4xx errors use the standard envelope (C12/C-2).

These handlers raised ``HTTPException(detail={...})``, which nests the code
under ``detail`` and carries no ``request_id``. As ``AppException`` they
render top-level ``error_code`` + ``request_id``. The code strings are
unchanged, and ``commerce/api`` now raises no ``HTTPException`` at all.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commerce.api import (
    account_endpoints,
    invoice_endpoints,
    payment_endpoints,
    price_book_endpoints,
    price_protection_endpoints,
    pricing_endpoints,
)
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "demo-tenant"
HEADERS = auth_headers(TENANT, roles=["platform_admin", "admin"])
_SETTINGS = SimpleNamespace(
    commerce_backbone_enabled=True,
    commerce_customers_enabled=True,
    commerce_pricing_engine_enabled=True,
    commerce_invoicing_enabled=True,
)
_MODULES = (
    account_endpoints,
    invoice_endpoints,
    payment_endpoints,
    price_book_endpoints,
)


@pytest.fixture
def client():
    es = MagicMock()
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}, "aggregations": {}}
    )
    es.index_document = AsyncMock(return_value={"result": "created"})

    account_endpoints.configure_account_api(
        account_service=MagicMock(), credit_service=MagicMock()
    )
    invoice_endpoints.configure_invoice_api(invoice_service=MagicMock())
    payment_endpoints.configure_payment_api(payment_service=MagicMock())
    price_book_endpoints.configure_price_book_api(
        price_book_service=MagicMock(), pricing_engine=MagicMock()
    )
    price_protection_endpoints.configure_price_protection_api(es_service=es)
    pricing_endpoints.configure_pricing_api(es_service=es)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(account_endpoints.router)
    app.include_router(invoice_endpoints.router)
    app.include_router(payment_endpoints.router)
    app.include_router(price_book_endpoints.pricing_router)
    app.include_router(price_protection_endpoints.router)
    app.include_router(pricing_endpoints.router)
    install_test_auth(app)

    with ExitStack() as stack:
        for mod in _MODULES:
            stack.enter_context(patch.object(mod, "get_settings", return_value=_SETTINGS))
        yield TestClient(app, raise_server_exceptions=False)

    account_endpoints._account_service = None
    account_endpoints._credit_service = None
    invoice_endpoints._invoice_service = None
    payment_endpoints._payment_service = None
    price_book_endpoints._price_book_service = None
    price_book_endpoints._pricing_engine = None
    price_protection_endpoints._es_service = None
    pricing_endpoints._es_service = None


def _assert_envelope(resp, status: int, code: str) -> Dict[str, Any]:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error_code"] == code
    assert body.get("request_id")
    assert "detail" not in body
    return body


def test_price_protection_unknown_id_is_404(client):
    resp = client.get(
        "/api/commerce/price-protection-contracts/QA-PPC-NOPE", headers=HEADERS
    )
    _assert_envelope(resp, 404, "price_protection_contract.not_found")


def test_price_protection_fixed_price_without_cents_is_422(client):
    resp = client.post(
        "/api/commerce/price-protection-contracts",
        json={
            "customer_id": "QA-CUST",
            "account_id": "QA-ACCT",
            "product_code": "DIESEL_2",
            "contract_type": "fixed_price",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "contracted_gallons": 1000.0,
        },
        headers=HEADERS,
    )
    _assert_envelope(resp, 422, "price_protection_contract.invalid_payload")


def test_pricing_no_rule_matched_is_422(client):
    resp = client.post(
        "/api/commerce/pricing/resolve",
        json={
            "customer_id": "QA-CUST",
            "product_code": "DIESEL_2",
            "gallons": 100,
            "terminal_id": "QA-TERM",
            "effective_date": "2026-10-06",
        },
        headers=HEADERS,
    )
    _assert_envelope(resp, 422, "pricing.no_rule_matched")


def test_account_invalid_expires_at_is_422(client):
    resp = client.post(
        "/api/commerce/accounts/QA-ACCT/credit-override",
        json={"reason": "QA", "authorized_by": "qa", "expires_at": "not-a-date"},
        headers=HEADERS,
    )
    _assert_envelope(resp, 422, "INVALID_EXPIRES_AT")


def test_invoice_force_void_without_authorized_by_is_422(client):
    resp = client.post(
        "/api/commerce/invoices/QA-INV/void",
        json={"reason": "QA", "force": True},
        headers=HEADERS,
    )
    _assert_envelope(resp, 422, "MISSING_AUTHORIZED_BY")


def test_payment_invalid_method_is_422(client):
    resp = client.post(
        "/api/commerce/payments",
        json={"invoice_id": "QA-INV", "amount_cents": 100, "method": "barter"},
        headers=HEADERS,
    )
    _assert_envelope(resp, 422, "INVALID_PAYMENT_METHOD")


def test_price_book_invalid_moment_is_422(client):
    resp = client.post(
        "/api/commerce/price-books/resolve",
        json={
            "account_id": "QA-ACCT",
            "product_code": "DIESEL_2",
            "quantity_gallons": 10,
            "moment": "not-a-moment",
        },
        headers=HEADERS,
    )
    _assert_envelope(resp, 422, "INVALID_MOMENT")
