"""Commerce flag-gate 404s use the standard error envelope (findings C12, C-2).

They used ``HTTPException(detail={...})``, which nests the code under
``detail`` and carries no ``request_id``. As ``AppException`` they render
top-level ``error_code`` + ``request_id`` like every other API error. The
string codes are unchanged; the UI reads top-level ``error_code`` first.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from commerce.api import (
    account_endpoints,
    ar_aging_endpoints,
    customer_endpoints,
    invoice_endpoints,
    payment_endpoints,
    price_book_endpoints,
)
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

_ALL_ON = dict(
    commerce_backbone_enabled=True,
    commerce_customers_enabled=True,
    commerce_pricing_engine_enabled=True,
    commerce_invoicing_enabled=True,
)

# (module, gate, flag switched off, expected error_code)
GATES = [
    (account_endpoints, "require_accounts_enabled", "commerce_backbone_enabled", "COMMERCE_DISABLED"),
    (ar_aging_endpoints, "require_ar_aging_enabled", "commerce_backbone_enabled", "COMMERCE_DISABLED"),
    (customer_endpoints, "require_customers_enabled", "commerce_backbone_enabled", "COMMERCE_DISABLED"),
    (customer_endpoints, "require_customers_enabled", "commerce_customers_enabled", "CUSTOMERS_DISABLED"),
    (invoice_endpoints, "require_invoicing_enabled", "commerce_backbone_enabled", "COMMERCE_DISABLED"),
    (invoice_endpoints, "require_invoicing_enabled", "commerce_invoicing_enabled", "INVOICING_DISABLED"),
    (payment_endpoints, "require_payments_enabled", "commerce_backbone_enabled", "COMMERCE_DISABLED"),
    (payment_endpoints, "require_payments_enabled", "commerce_invoicing_enabled", "INVOICING_DISABLED"),
    (price_book_endpoints, "require_pricing_enabled", "commerce_backbone_enabled", "COMMERCE_DISABLED"),
    (price_book_endpoints, "require_pricing_enabled", "commerce_pricing_engine_enabled", "PRICING_DISABLED"),
]


@pytest.mark.parametrize(
    ("module", "gate", "flag", "code"),
    [pytest.param(*g, id=f"{g[0].__name__.rsplit('.', 1)[-1]}-{g[3]}") for g in GATES],
)
def test_flag_off_renders_standard_envelope(module, gate, flag, code):
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/probe", dependencies=[Depends(getattr(module, gate))])
    async def _probe():
        return {"ok": True}

    install_test_auth(app)
    settings = SimpleNamespace(**{**_ALL_ON, flag: False})
    with patch.object(module, "get_settings", return_value=settings):
        resp = TestClient(app).get("/probe", headers=auth_headers("demo-tenant", roles=["platform_admin", "admin"]))

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error_code"] == code
    assert body.get("request_id")
    assert "detail" not in body
