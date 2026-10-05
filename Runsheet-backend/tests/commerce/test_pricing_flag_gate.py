"""Pricing-rules, pricing/resolve and price-protection share the price-books
flag gate (finding C-1).

With ``commerce_pricing_engine_enabled`` off these routers answer 404
``PRICING_DISABLED`` for every role, before any role check. With it on, the
existing staff-only role check applies.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commerce.api import price_protection_endpoints, pricing_endpoints
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "demo-tenant"

ROUTES = [
    pytest.param("GET", "/api/commerce/pricing-rules", None, id="pricing-rules-list"),
    pytest.param(
        "POST",
        "/api/commerce/pricing/resolve",
        {"customer_id": "QA-CUST", "product_code": "DIESEL_2", "quantity_gallons": 100},
        id="pricing-resolve",
    ),
    pytest.param("GET", "/api/commerce/price-protection-contracts", None, id="price-protection-list"),
]


def _settings(*, pricing: bool):
    return SimpleNamespace(
        commerce_backbone_enabled=True,
        commerce_pricing_engine_enabled=pricing,
    )


def _error_code(resp) -> str | None:
    body = resp.json()
    if isinstance(body, dict) and "error_code" in body:
        return body["error_code"]
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail.get("error_code") if isinstance(detail, dict) else None


@pytest.fixture
def client():
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": [], "total": {"value": 0}}})
    pricing_endpoints.configure_pricing_api(es_service=es)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(pricing_endpoints.router)
    app.include_router(price_protection_endpoints.router)
    install_test_auth(app)
    return TestClient(app, raise_server_exceptions=False)


def _call(client, method, url, body, roles):
    return client.request(method, url, json=body, headers=auth_headers(TENANT, roles=roles))


@pytest.mark.parametrize(("method", "url", "body"), ROUTES)
@pytest.mark.parametrize("roles", [["admin"], ["platform_admin", "admin"]])
def test_pricing_off_is_404_for_every_role(client, method, url, body, roles):
    with patch("commerce.api.price_book_endpoints.get_settings", return_value=_settings(pricing=False)):
        resp = _call(client, method, url, body, roles)
    assert resp.status_code == 404, resp.text
    assert _error_code(resp) == "PRICING_DISABLED"


@pytest.mark.parametrize(("method", "url", "body"), ROUTES)
def test_pricing_on_tenant_admin_is_403(client, method, url, body):
    with patch("commerce.api.price_book_endpoints.get_settings", return_value=_settings(pricing=True)):
        resp = _call(client, method, url, body, ["admin"])
    assert resp.status_code == 403, resp.text
    assert _error_code(resp) == "INSUFFICIENT_ROLE"


@pytest.mark.parametrize(("method", "url", "body"), ROUTES)
def test_pricing_on_platform_admin_passes_the_gate(client, method, url, body):
    with patch("commerce.api.price_book_endpoints.get_settings", return_value=_settings(pricing=True)):
        resp = _call(client, method, url, body, ["platform_admin", "admin"])
    assert resp.status_code not in (403, 404), resp.text
