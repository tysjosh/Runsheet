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


# ---------------------------------------------------------------------------
# ``statuses`` (any-of) filter on both read paths (customer portal, OI-06)
# ---------------------------------------------------------------------------

_STATUSES = ("draft", "open", "partial", "paid", "overdue", "void")
_VISIBLE = ["open", "partial", "paid", "overdue", "void"]


def test_must_clauses_statuses_terms_with_status():
    from commerce.services.invoice_service import _invoice_must_clauses

    assert _invoice_must_clauses(statuses=["open", "void"]) == [
        {"terms": {"status": ["open", "void"]}}
    ]
    assert _invoice_must_clauses("open", statuses=("open",)) == [
        {"term": {"status": "open"}},
        {"terms": {"status": ["open"]}},
    ]
    assert _invoice_must_clauses() == []
    # An empty sequence fails closed (matches nothing) rather than widening.
    assert _invoice_must_clauses(statuses=[]) == [{"terms": {"status": []}}]


def _invoice_doc(tenant, invoice_id, status, created_at):
    return {
        "tenant_id": tenant, "invoice_id": invoice_id, "status": status,
        "customer_id": "cust_1", "created_at": created_at,
    }


@pytest.fixture
def es_service(monkeypatch):
    from commerce.services.invoice_service import INVOICES_CURRENT_INDEX, InvoiceService
    from config.settings import clear_settings_cache
    from tests.unit._loading_plan_fakes import InMemoryDocumentStore

    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "false")
    clear_settings_cache()
    store = InMemoryDocumentStore()
    for i, status in enumerate(_STATUSES):
        doc = _invoice_doc("t1", f"inv-{status}", status, f"2026-10-0{i + 1}T00:00:00+00:00")
        store.seed(INVOICES_CURRENT_INDEX, doc["invoice_id"], doc)
    store.seed(INVOICES_CURRENT_INDEX, "inv-t2", _invoice_doc("t2", "inv-t2", "open", "2026-10-09"))
    yield InvoiceService(store)
    clear_settings_cache()


async def test_es_path_statuses_excludes_draft(es_service):
    listed = await es_service.list(tenant_id="t1", statuses=_VISIBLE, limit=50)
    ids = {i["invoice_id"] for i in listed["items"]}
    assert ids == {f"inv-{s}" for s in _VISIBLE}
    assert await es_service.count(tenant_id="t1", statuses=_VISIBLE) == 5
    both = await es_service.list(tenant_id="t1", status="draft", statuses=_VISIBLE)
    assert both["items"] == []
    assert await es_service.count(tenant_id="t1", statuses=[]) == 0
    assert await es_service.count(tenant_id="t1") == 6


@pytest.fixture
async def pg_engine(monkeypatch):
    from config.settings import clear_settings_cache
    import persistence.models  # noqa: F401  (registers the tables on Base)
    from persistence.database import Base, dispose_engine, get_engine

    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()
    eng = get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await dispose_engine()
        clear_settings_cache()


async def test_bridge_path_statuses_excludes_draft(pg_engine):
    from commerce.services.invoice_service import InvoiceService
    from persistence.database import session_scope
    from persistence.repositories import InvoiceRepository

    repo = InvoiceRepository()
    async with session_scope() as session:
        for tenant, status in [("t1", s) for s in _STATUSES] + [("t2", "open")]:
            row = await repo.create(
                session, invoice_id=f"inv-{tenant}-{status}", tenant_id=tenant,
                customer_id="cust_1", account_id="acct_1",
                line_items=[{"line_id": f"l-{tenant}-{status}", "product_code": "DSL",
                             "quantity_gallons": 1.0, "unit_price_cents": 300,
                             "subtotal_cents": 300}],
            )
            row.status = status
        await session.flush()

    es = MagicMock()
    es.search_documents = AsyncMock(side_effect=AssertionError("ES must not be read"))
    svc = InvoiceService(es)
    listed = await svc.list(tenant_id="t1", statuses=_VISIBLE, limit=50)
    assert {i["invoice_id"] for i in listed["items"]} == {f"inv-t1-{s}" for s in _VISIBLE}
    assert await svc.count(tenant_id="t1", statuses=_VISIBLE) == 5
    assert (await svc.list(tenant_id="t1", status="draft", statuses=_VISIBLE))["items"] == []
    assert await svc.count(tenant_id="t1", statuses=[]) == 0
    assert await svc.count(tenant_id="t1") == 6
