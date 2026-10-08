"""Portal invoices (design §5, R5; INV-1, AC14).

[real-auth] ``main.app`` with fake session verifiers. Invoices come from the
``portal_fakes.invoices`` harness: a real ``InvoiceService`` over an
``InMemoryDocumentStore`` (the document-store path), or, in the
``bridge_path`` cases, over SQLite through ``commerce_persistence_bridge``.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import timedelta

import pytest
from pypdf import PdfReader

from tests.portal.conftest import (
    CUSTOMER_A,
    CUSTOMER_B,
    T1,
    call,
    seed_invoice_isolation,
)

VISIBLE = ("open", "partial", "paid", "overdue", "void")
INVOICE_ROUTES = (
    "/api/portal/invoices",
    "/api/portal/invoices/export",
    "/api/portal/invoices/QA-INV-A-open",
    "/api/portal/invoices/QA-INV-A-open/pdf",
)


def _pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)


def _csv_rows(resp):
    text = resp.content.decode("utf-8")
    assert text.startswith("\ufeff")
    return list(csv.reader(io.StringIO(text[1:])))


# ---------------------------------------------------------------------------
# List and detail
# ---------------------------------------------------------------------------


def test_list_excludes_drafts_and_labels_void(client, portal_on, portal_fakes, cA):
    ids = seed_invoice_isolation(portal_fakes.invoices)
    resp = call(client, "GET", "/api/portal/invoices", cA)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {i["invoice_id"] for i in body["data"]} == {ids["A"][s] for s in VISIBLE}
    assert body["limit"] == 25 and body["next_cursor"] is None
    # Newest first.
    created = [i["created_at"] for i in body["data"]]
    assert created == sorted(created, reverse=True)
    void = next(i for i in body["data"] if i["status_code"] == "void")
    assert void["status_label"] == "Void"
    first = body["data"][0]
    assert first["account_display_name"] == "Account A Main"
    assert first["payment_attempt"] is None
    assert first["payable"] is False  # no portal Stripe connector configured
    # Unit price from the stored micros (2.966 $/gal -> 297 cents).
    assert first["line_items"] == [{
        "product_code": "PROPANE", "quantity_gallons": 100.0,
        "unit_price_cents": 297, "unit_price_dollars": "2.966",
        "subtotal_cents": 29660,
    }]


def test_draft_is_404_by_id_and_pdf(client, portal_on, portal_fakes, cA):
    ids = seed_invoice_isolation(portal_fakes.invoices)
    draft = ids["A"]["draft"]
    for path in (f"/api/portal/invoices/{draft}", f"/api/portal/invoices/{draft}/pdf"):
        resp = call(client, "GET", path, cA)
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"
        assert resp.json()["message"] == f"Invoice '{draft}' not found"


def test_detail_and_status_filter(client, portal_on, portal_fakes, cA):
    ids = seed_invoice_isolation(portal_fakes.invoices)
    detail = call(client, "GET", f"/api/portal/invoices/{ids['A']['void']}", cA)
    assert detail.status_code == 200
    assert detail.json()["data"]["status_label"] == "Void"

    only_void = call(client, "GET", "/api/portal/invoices?status=void", cA)
    assert [i["invoice_id"] for i in only_void.json()["data"]] == [ids["A"]["void"]]
    assert call(client, "GET", "/api/portal/invoices?status=draft", cA).status_code == 422
    assert call(client, "GET", "/api/portal/invoices?status=bogus", cA).status_code == 422


def test_date_filter_on_created_at(client, portal_on, portal_fakes, cA):
    h = portal_fakes.invoices
    ids = seed_invoice_isolation(h)
    # open: now-1d, partial: now-2d, paid: now-3d ...
    start = (h.now - timedelta(days=2, hours=1)).date().isoformat()
    resp = call(client, "GET", f"/api/portal/invoices?start_date={start}", cA)
    assert resp.status_code == 200
    got = {i["invoice_id"] for i in resp.json()["data"]}
    assert ids["A"]["open"] in got and ids["A"]["overdue"] not in got
    bad = call(client, "GET", "/api/portal/invoices?start_date=not-a-date", cA)
    assert bad.status_code == 422


def test_pagination_and_cursor_scope(client, portal_on, portal_fakes, cA):
    ids = seed_invoice_isolation(portal_fakes.invoices)
    first = call(client, "GET", "/api/portal/invoices?limit=2", cA).json()
    assert len(first["data"]) == 2 and first["next_cursor"]
    seen = [i["invoice_id"] for i in first["data"]]
    cursor = first["next_cursor"]
    while cursor:
        page = call(client, "GET", f"/api/portal/invoices?limit=2&cursor={cursor}", cA).json()
        seen += [i["invoice_id"] for i in page["data"]]
        cursor = page["next_cursor"]
    assert sorted(seen) == sorted(ids["A"][s] for s in VISIBLE)
    assert len(seen) == len(set(seen))

    # A cursor naming B's invoice answers like a malformed one.
    from portal.services.scoped_readers import encode_cursor

    foreign = encode_cursor(["2026-10-01T00:00:00+00:00", ids["B"]["open"]])
    for cur in (foreign, "garbage!!", encode_cursor(["x", "QA-INV-UNKNOWN"])):
        resp = call(client, "GET", f"/api/portal/invoices?cursor={cur}", cA)
        assert resp.status_code == 422, resp.text
        assert resp.json()["details"] == {"fields": ["cursor"]}


def test_account_names_fallback(client, portal_on, portal_fakes, cA, caplog):
    h = portal_fakes.invoices
    h.add_invoice(T1, CUSTOMER_A, "QA-INV-A-noacct", account_id="QA-ACCT-UNKNOWN")
    resp = call(client, "GET", "/api/portal/invoices", cA)
    assert resp.json()["data"][0]["account_display_name"] == "Account"

    seed_invoice_isolation(h)
    h.accounts.fail = RuntimeError("down")
    with caplog.at_level(logging.WARNING):
        resp = call(client, "GET", "/api/portal/invoices", cA)
    assert resp.status_code == 200
    assert {i["account_display_name"] for i in resp.json()["data"]} == {"Account"}
    assert any("account list failed" in r.getMessage() for r in caplog.records)
    for c in h.accounts.calls:
        assert c["tenant_id"] == T1 and c["customer_id"] == CUSTOMER_A


async def test_account_name_map_follows_cursor_with_cap():
    from types import SimpleNamespace

    from portal.services.projection import ACCOUNT_NAME_CAP, account_display_names
    from tests.portal.conftest import FakeAccountService

    accounts = FakeAccountService()
    for i in range(ACCOUNT_NAME_CAP + 20):
        accounts.add(T1, CUSTOMER_A, f"acct-{i:04d}", f"Name {i}")
    accounts.add(T1, CUSTOMER_B, "acct-b", "Other")
    scope = SimpleNamespace(tenant_id=T1, customer_id=CUSTOMER_A)
    names = await account_display_names(accounts, scope)
    assert len(names) == ACCOUNT_NAME_CAP
    assert "acct-0499" in names and "acct-0500" not in names and "acct-b" not in names
    assert [c["cursor"] for c in accounts.calls] == [None, "200", "400"]
    assert all(c["limit"] == 200 for c in accounts.calls)


# ---------------------------------------------------------------------------
# Draft exclusion on the Postgres read path (commerce_persistence_bridge)
# ---------------------------------------------------------------------------


@pytest.fixture
async def bridge_path(monkeypatch, portal_fakes):
    """Read invoices through ``commerce_persistence_bridge`` (SQLite)."""
    import persistence.models  # noqa: F401  (registers the tables on Base)
    from config.settings import clear_settings_cache
    from persistence.database import Base, dispose_engine, get_engine

    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()
    eng = get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def fail(*_a, **_k):
        raise AssertionError("document store must not be read")

    portal_fakes.invoices.store.search_documents = fail
    try:
        yield
    finally:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await dispose_engine()
        clear_settings_cache()


async def _seed_pg():
    from persistence.database import session_scope
    from persistence.repositories import InvoiceRepository

    repo = InvoiceRepository()
    async with session_scope() as session:
        for customer in (CUSTOMER_A, CUSTOMER_B):
            for status in ("draft",) + VISIBLE:
                row = await repo.create(
                    session, invoice_id=f"PG-{customer[-1]}-{status}", tenant_id=T1,
                    customer_id=customer, account_id="acct",
                    line_items=[{"line_id": f"l-{customer}-{status}", "product_code": "DSL",
                                 "quantity_gallons": 1.0, "unit_price_cents": 300,
                                 "subtotal_cents": 300}],
                )
                row.status = status
        await session.flush()


async def test_bridge_path_excludes_drafts(bridge_path, portal_on, cA):
    """INV-1 on the read-cutover path, through the async client (one loop)."""
    import httpx

    from main import app

    await _seed_pg()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.get("/api/portal/invoices", headers=cA.headers)
        assert resp.status_code == 200, resp.text
        assert {i["invoice_id"] for i in resp.json()["data"]} == {f"PG-A-{s}" for s in VISIBLE}
        draft = await ac.get("/api/portal/invoices/PG-A-draft", headers=cA.headers)
        assert draft.status_code == 404
        foreign = await ac.get("/api/portal/invoices/PG-B-open", headers=cA.headers)
        assert foreign.status_code == 404
        exported = await ac.get("/api/portal/invoices/export", headers=cA.headers)
        assert exported.status_code == 200
        rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8")[1:])))
        assert sorted(r[0] for r in rows[1:]) == sorted(f"PG-A-{s}" for s in VISIBLE)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_pdf_is_attachment_with_projected_text(client, portal_on, portal_fakes, cA):
    h = portal_fakes.invoices
    seed_invoice_isolation(h)
    h.add_invoice(T1, CUSTOMER_A, "QA-INV-A-PDF", status="void", account_id="QA-ACCT-A",
                  invoice_number='INV/2026 "7";x', lines=31)
    resp = call(client, "GET", "/api/portal/invoices/QA-INV-A-PDF/pdf", cA)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"] == 'attachment; filename="invoice_INV_2026__7__x.pdf"'
    reader = PdfReader(io.BytesIO(resp.content))
    assert len(reader.pages) == 2  # 30 line items per page
    text = _pdf_text(resp.content)
    for expected in ('INV/2026 "7";x', "Void", "Account A Main", "Customer A", T1,
                     "$2.966", "$296.60", "PROPANE", "Page 2 of 2"):
        assert expected in text, expected


def test_pdf_render_error_is_500(client, portal_on, portal_fakes, cA, monkeypatch, caplog):
    import portal.services.portal_invoice_service as pis

    seed_invoice_isolation(portal_fakes.invoices)

    def boom(*_a, **_k):
        raise RuntimeError("reportlab exploded")

    monkeypatch.setattr(pis, "render_invoice_pdf", boom)
    with caplog.at_level(logging.ERROR):
        resp = call(client, "GET", "/api/portal/invoices/QA-INV-A-open/pdf", cA)
    assert resp.status_code == 500
    assert resp.json()["error_code"] == "INTERNAL_ERROR"
    assert resp.json()["message"] == "Invoice PDF could not be generated"
    assert "exploded" not in resp.text
    errors = [r for r in caplog.records if "pdf render failed" in r.getMessage()]
    assert errors and errors[0].levelno == logging.ERROR
    assert errors[0].extra_data["invoice_id"] == "QA-INV-A-open"


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def test_csv_bom_escaping_and_scope(client, portal_on, portal_fakes, cA):
    h = portal_fakes.invoices
    ids = seed_invoice_isolation(h)
    h.add_invoice(T1, CUSTOMER_A, "QA-INV-A-formula", invoice_number="=HYPERLINK(\"x\")",
                  account_id="QA-ACCT-A")
    resp = call(client, "GET", "/api/portal/invoices/export", cA)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"].startswith('attachment; filename="portal_invoices_')
    rows = _csv_rows(resp)
    assert rows[0] == ["invoice_number", "issued_at", "due_date", "status", "account",
                       "total_gallons", "subtotal", "tax", "total", "paid", "remaining"]
    numbers = {r[0] for r in rows[1:]}
    assert numbers == {f"INV-{ids['A'][s]}" for s in VISIBLE} | {"'=HYPERLINK(\"x\")"}
    void = next(r for r in rows[1:] if r[0] == f"INV-{ids['A']['void']}")
    assert void[3] == "Void" and void[4] == "Account A Main"
    assert void[5:] == ["100.0", "296.60", "10.00", "306.60", "0.00", "306.60"]

    only_paid = _csv_rows(call(client, "GET", "/api/portal/invoices/export?status=paid", cA))
    assert [r[0] for r in only_paid[1:]] == [f"INV-{ids['A']['paid']}"]


def test_csv_cap_413(client, portal_on, portal_fakes, cA, monkeypatch):
    import services.csv_export as csv_export

    seed_invoice_isolation(portal_fakes.invoices)
    monkeypatch.setattr(csv_export, "MAX_EXPORT_ROWS", 4)
    resp = call(client, "GET", "/api/portal/invoices/export", cA)
    assert resp.status_code == 413
    assert resp.json()["error_code"] == "EXPORT_TOO_LARGE"
    assert resp.json()["details"] == {"row_count": 5, "max_rows": 4}


# ---------------------------------------------------------------------------
# Invoicing disabled (R5.6)
# ---------------------------------------------------------------------------


def test_invoicing_disabled_404(client, set_flags, portal_fakes, cA):
    """Backbone off is already 404 PORTAL_DISABLED (XR-8); invoicing off is this."""
    seed_invoice_isolation(portal_fakes.invoices)
    set_flags(invoicing=False)
    for path in INVOICE_ROUTES:
        resp = call(client, "GET", path, cA)
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["error_code"] == "INVOICING_DISABLED"
    me = call(client, "GET", "/api/portal/me", cA)
    assert me.status_code == 200 and me.json()["data"]["invoices_available"] is False


def test_invoice_service_unconfigured_404(client, portal_on, portal_fakes, cA):
    from portal.services.portal_invoice_service import configure_portal_invoices

    configure_portal_invoices(None)  # portal_fakes restores it
    for path in INVOICE_ROUTES:
        resp = call(client, "GET", path, cA)
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "INVOICING_DISABLED"
