"""``POST /api/commerce/pricing/resolve`` honours price-protection contracts (N-CFV-4).

The endpoint built ``SalesPricingEngine`` without a ``PriceProtectionService``,
so an active fixed_price contract was never consulted and the posted_price rule
won. Staging has no rack-price rows, which is also modelled here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commerce.api import pricing_endpoints
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "demo-tenant"

_CONTRACT = {
    "contract_id": "ppc-qa-fixed",
    "tenant_id": TENANT,
    "customer_id": "QA-CUST",
    "account_id": "QA-ACCT",
    "product_code": "DIESEL_2",
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

_RULE = {
    "rule_id": "rule-qa-posted",
    "tenant_id": TENANT,
    "product_code": "DIESEL_2",
    "strategy": "posted_price",
    "posted_price_cents": 333,
    "priority": 0,
    "effective_date": "2026-01-01",
    "status": "active",
    "created_at": "2026-01-01T00:00:00+00:00",
    "updated_at": "2026-01-01T00:00:00+00:00",
}


class _FakeES:
    def __init__(self, *, with_contract: bool) -> None:
        self._with_contract = with_contract

    async def search_documents(self, index, query, size=10, **kwargs):
        if index == "price_protection_contracts" and self._with_contract:
            return {"hits": {"hits": [{"_source": dict(_CONTRACT)}]}}
        if index == "pricing_rules":
            return {"hits": {"hits": [{"_source": dict(_RULE)}]}}
        return {"hits": {"hits": [], "total": {"value": 0}}}

    async def index_document(self, index, doc_id, doc):
        return {"result": "created"}


def _client(es) -> TestClient:
    pricing_endpoints.configure_pricing_api(es_service=es)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(pricing_endpoints.router)
    install_test_auth(app)
    return TestClient(app, raise_server_exceptions=False)


def _resolve(client: TestClient):
    settings = SimpleNamespace(
        commerce_backbone_enabled=True, commerce_pricing_engine_enabled=True
    )
    with patch("commerce.api.price_book_endpoints.get_settings", return_value=settings):
        return client.post(
            "/api/commerce/pricing/resolve",
            json={
                "customer_id": "QA-CUST",
                "product_code": "DIESEL_2",
                "gallons": 100,
                "terminal_id": "QA-TERM",
                "effective_date": "2026-10-06",
            },
            headers=auth_headers(TENANT, roles=["platform_admin", "admin"]),
        )


@pytest.fixture(autouse=True)
def _reset_es():
    yield
    pricing_endpoints._es_service = None


def test_active_fixed_price_contract_wins_over_rule():
    resp = _resolve(_client(_FakeES(with_contract=True)))

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["effective_price_cents"] == 310
    assert data["contract_id"] == "ppc-qa-fixed"
    assert data["contract_type"] == "fixed_price"


def test_market_dependent_contract_without_rack_price_is_422(monkeypatch):
    monkeypatch.setitem(_CONTRACT, "contract_type", "cap_price")
    monkeypatch.setitem(_CONTRACT, "fixed_price_cents", None)
    monkeypatch.setitem(_CONTRACT, "price_cap_cents", 320)

    resp = _resolve(_client(_FakeES(with_contract=True)))

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error_code"] == "pricing.rack_price_unavailable"
    assert body["request_id"]


def test_without_contract_rule_price_applies():
    resp = _resolve(_client(_FakeES(with_contract=False)))

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["effective_price_cents"] == 333
    assert data["contract_id"] is None
