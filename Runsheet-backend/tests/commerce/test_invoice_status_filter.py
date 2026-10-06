"""``GET /api/commerce/invoices?status=`` only accepts real statuses (N-CFV-5).

An unknown status used to be passed through and return an empty 200, which
reads as "no invoices" rather than "bad filter".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commerce.api import invoice_endpoints
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

_SETTINGS = SimpleNamespace(
    commerce_backbone_enabled=True, commerce_invoicing_enabled=True
)


@pytest.fixture
def client_and_service():
    service = MagicMock()
    service.list = AsyncMock(
        return_value={"items": [], "next_cursor": None, "limit": 50}
    )
    invoice_endpoints.configure_invoice_api(invoice_service=service)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(invoice_endpoints.router)
    install_test_auth(app)
    with patch.object(invoice_endpoints, "get_settings", return_value=_SETTINGS):
        yield TestClient(app), service
    invoice_endpoints._invoice_service = None


def _get(client, query):
    return client.get(
        f"/api/commerce/invoices{query}",
        headers=auth_headers("demo-tenant", roles=["platform_admin", "admin"]),
    )


def test_unknown_status_is_422(client_and_service):
    client, service = client_and_service

    resp = _get(client, "?status=bogus")

    assert resp.status_code == 422, resp.text
    service.list.assert_not_called()


def test_known_status_is_forwarded_as_string(client_and_service):
    client, service = client_and_service

    resp = _get(client, "?status=open")

    assert resp.status_code == 200, resp.text
    assert service.list.call_args.kwargs["status"] == "open"


def test_no_status_is_none(client_and_service):
    client, service = client_and_service

    resp = _get(client, "")

    assert resp.status_code == 200, resp.text
    assert service.list.call_args.kwargs["status"] is None
