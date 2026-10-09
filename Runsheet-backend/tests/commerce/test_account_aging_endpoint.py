"""``GET /api/commerce/accounts/{id}/aging`` ages by days past due (F12),
with the same rule as ``ARAgingService.bucket_for_invoice``."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commerce.api import account_endpoints
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "demo-tenant"
# Receivables aging is platform_admin only (as designed).
HEADERS = auth_headers(TENANT, roles=["platform_admin", "admin"])
NOW = datetime(2026, 10, 8, 19, 41, tzinfo=timezone.utc)
_SETTINGS = SimpleNamespace(
    commerce_backbone_enabled=True,
    commerce_customers_enabled=True,
    commerce_pricing_engine_enabled=True,
    commerce_invoicing_enabled=True,
)


def _hit(remaining, *, due=None, issued="2026-08-01T00:00:00Z"):
    src = {"remaining_cents": remaining, "issued_at": issued}
    if due:
        src["due_date"] = due
    return {"_source": src}


@pytest.fixture
def client_and_es():
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": [
        _hit(700, due="2026-10-20"),          # not yet due -> current
        _hit(300, due="2026-09-18"),          # 20 days past due -> 1-30
        _hit(200, due="2026-06-01"),          # 129 days -> 90+
        _hit(100),                            # no due_date: issued 08-01 -> 68 -> 61-90
    ]}})
    account_service = MagicMock()
    account_service._es = es
    account_service.get = AsyncMock(return_value={"account_id": "acct-1"})
    account_endpoints.configure_account_api(
        account_service=account_service, credit_service=MagicMock()
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(account_endpoints.router)
    install_test_auth(app)
    with patch.object(account_endpoints, "get_settings", return_value=_SETTINGS), \
         patch("services.time_utils.utcnow", return_value=NOW):
        yield TestClient(app, raise_server_exceptions=False), es
    account_endpoints._account_service = None
    account_endpoints._credit_service = None


def test_account_aging_buckets_by_days_past_due(client_and_es):
    client, es = client_and_es
    resp = client.get("/api/commerce/accounts/acct-1/aging", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data == {
        "account_id": "acct-1",
        "bucket_current_cents": 700,
        "bucket_0_30_cents": 300,
        "bucket_31_60_cents": 0,
        "bucket_61_90_cents": 100,
        "bucket_90_plus_cents": 200,
        "total_open_cents": 1300,
    }
    query = es.search_documents.await_args.args[1]
    assert "due_date" in query["_source"]
