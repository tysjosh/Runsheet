"""Margin CSV export (FR7, AC-37, Simplification 12).

``GET /api/commerce/margin/records/export`` goes through
``services.csv_export.stream_csv_export`` with ``export_type="margin"``: BOM,
formula escaping, the 50,000-row cap before the first byte, the per-user rate
limit, one audit line, the ``margin_<tenant>_<YYYYMMDD>.csv`` filename, admin
only and tenant scoped. A ``method=none`` row has empty cost cells, never 0,
with ``method`` and ``no_cost_reason`` right after ``revenue_usd``.
"""
from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List

import pytest
import pytest_asyncio

from commerce.api import margin_endpoints
from middleware.rate_limiter import limiter
from persistence.database import session_scope
from persistence.models import MarginRecordORM
from services.csv_export import EXPORT_RATE_LIMIT, MAX_EXPORT_ROWS
from tests.integration.commerce._margin_api import BASE, MarginApi, error_code

from .conftest import TENANT_A, TENANT_B, _candidate, _missing_cost

URL = f"{BASE}/records/export"
HEADER = [
    "record_id", "stage", "status", "origin", "version", "as_of", "order_id", "invoice_id",
    "line_index", "customer_id", "account_id", "product_code", "terminal_id",
    "gallons", "unit_price_micros", "unit_price_usd", "revenue_cents", "revenue_usd",
    "method", "no_cost_reason", "product_cost_micros", "adders_micros", "landed_cost_micros",
    "landed_cost_usd", "cost_cents", "cost_usd", "margin_cents", "margin_usd",
    "margin_per_gallon_micros", "margin_per_gallon_usd", "margin_pct",
    "flags", "floor_micros_used", "adders_configured", "terminal_unattributed", "computed_at",
]
COST_CELLS = (
    "product_cost_micros", "landed_cost_micros", "landed_cost_usd", "cost_cents", "cost_usd",
    "margin_cents", "margin_usd", "margin_per_gallon_micros", "margin_per_gallon_usd", "margin_pct",
)


@pytest_asyncio.fixture
async def api(repo, flag_on):
    limiter.reset()
    harness = MarginApi(repo)
    try:
        yield harness
    finally:
        await harness.client.aclose()
        margin_endpoints.configure_margin_api(margin_service=None, cost_entry_service=None)
        limiter.reset()


def rows_of(resp) -> List[List[str]]:
    assert resp.status_code == 200, resp.text
    assert resp.content[:3] == b"\xef\xbb\xbf"
    return list(csv.reader(io.StringIO(resp.content[3:].decode("utf-8"))))


def by_order(rows: List[List[str]]) -> Dict[str, Dict[str, str]]:
    header = rows[0]
    return {r[header.index("order_id")]: dict(zip(header, r)) for r in rows[1:]}


async def seed(repo, tenant: str = TENANT_A) -> None:
    await repo.write_record(tenant, _candidate(source_key="order:ORD-1", order_id="ORD-1"), "live")
    await repo.write_record(tenant, _missing_cost(source_key="order:ORD-2", order_id="ORD-2", input_hash="h2"), "live")
    await repo.write_record(
        tenant,
        _candidate(
            source_key="order:ORD-3", order_id="ORD-3", landed_cost_micros=3_200_000,
            product_cost_micros=3_200_000, cost_cents=320_000, margin_cents=-20_000,
            margin_per_gallon_micros=-200_000, margin_bp=-667, flag_negative_margin=True,
            customer_id="=HYPERLINK(\"x\")", input_hash="h3",
        ),
        "live",
    )


async def test_bom_header_filename_and_costed_values(api, repo):
    await seed(repo)
    resp = await api.as_("admin").client.get(URL)
    assert resp.headers["content-type"].startswith("text/csv")
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    assert f'filename="margin_{TENANT_A}_{today}.csv"' in resp.headers["content-disposition"]
    assert resp.headers["cache-control"] == "no-store"
    rows = rows_of(resp)
    assert rows[0] == HEADER
    costed = by_order(rows)["ORD-1"]
    assert costed["gallons"] == "1000.000000"
    assert costed["unit_price_usd"] == "3.000000"
    assert costed["revenue_usd"] == "3000.00"
    assert costed["method"] == "wac"
    assert costed["landed_cost_usd"] == "2.500000"
    assert costed["cost_cents"] == "250000" and costed["cost_usd"] == "2500.00"
    assert costed["margin_usd"] == "500.00"
    assert costed["margin_per_gallon_usd"] == "0.500000"
    assert costed["margin_pct"] == "16.67"
    assert costed["flags"] == ""
    assert costed["terminal_unattributed"] == "false"


async def test_method_none_row_has_empty_cost_cells_next_to_the_reason(api, repo):
    await seed(repo)
    rows = rows_of(await api.as_("admin").client.get(URL))
    header = rows[0]
    assert header[header.index("revenue_usd") + 1] == "method"
    assert header[header.index("revenue_usd") + 2] == "no_cost_reason"
    missing = by_order(rows)["ORD-2"]
    assert missing["method"] == "none"
    assert missing["no_cost_reason"] == "no_lots_no_rack"
    for cell in COST_CELLS:
        assert missing[cell] == "", cell  # empty, never "0" or "0.00"
    assert missing["revenue_usd"] == "3000.00"
    assert missing["flags"] == "missing_cost"


async def test_negative_money_is_numeric_and_text_is_formula_escaped(api, repo):
    await seed(repo)
    negative = by_order(rows_of(await api.as_("admin").client.get(URL)))["ORD-3"]
    assert negative["margin_usd"] == "-200.00"     # Decimal, not "'-200.00"
    assert negative["margin_pct"] == "-6.67"
    assert negative["margin_cents"] == "-20000"
    assert negative["customer_id"] == "'=HYPERLINK(\"x\")"
    assert negative["flags"] == "negative_margin"


async def test_tenant_scoped_and_query_tenant_id_ignored(api, repo):
    await seed(repo, TENANT_A)
    await seed(repo, TENANT_B)
    await repo.write_record(TENANT_B, _candidate(source_key="order:ORD-B", order_id="ORD-B", input_hash="hb"), "live")
    for url in (URL, f"{URL}?tenant_id={TENANT_B}"):
        assert sorted(by_order(rows_of(await api.as_("admin").client.get(url)))) == ["ORD-1", "ORD-2", "ORD-3"]


async def test_filters_narrow_the_export(api, repo):
    await seed(repo)
    rows = rows_of(await api.as_("admin").client.get(f"{URL}?flag=missing_cost"))
    assert list(by_order(rows)) == ["ORD-2"]
    rows = rows_of(await api.as_("admin").client.get(f"{URL}?start_date=2026-10-02"))
    assert rows[1:] == []


@pytest.mark.parametrize("roles", [["dispatcher"], ["driver"], ["platform_admin"], ["customer"]])
async def test_non_admin_is_403(api, repo, roles):
    resp = await api.as_(*roles).client.get(URL)
    assert resp.status_code == 403
    assert error_code(resp) == "INSUFFICIENT_ROLE"


async def test_flag_off_is_404_before_the_role_check(api, monkeypatch):
    monkeypatch.setattr(margin_endpoints, "is_persistence_enabled", lambda: False)
    for roles in (["admin"], ["dispatcher"]):
        resp = await api.as_(*roles).client.get(URL)
        assert resp.status_code == 404
        assert error_code(resp) == "COMMERCE_DISABLED"


async def test_audit_line_has_filters_and_no_row_data(api, repo, caplog):
    await seed(repo)
    caplog.set_level(logging.INFO, logger="services.csv_export")
    rows_of(await api.as_("admin").client.get(f"{URL}?flag=missing_cost&customer_id=CUST-1"))
    records = [r for r in caplog.records if r.getMessage() == "data_export"]
    assert len(records) == 1
    data = records[0].extra_data
    assert data["export_type"] == "margin"
    assert data["outcome"] == "completed"
    assert data["row_count"] == 1
    assert data["tenant_id"] == TENANT_A and data["user_id"] == "u-admin"
    assert data["filters"] == {"customer_id": "CUST-1", "flag": "missing_cost", "status": "active"}
    assert "3000" not in str(data)


async def test_rate_limit_is_429(api, repo):
    allowed = int(EXPORT_RATE_LIMIT.split("/")[0])
    c = api.as_("admin").client
    for _ in range(allowed):
        assert (await c.get(URL)).status_code == 200
    resp = await c.get(URL)
    assert resp.status_code == 429


async def _bulk_insert(repo, tenant: str, count: int) -> None:
    """``count`` valid rows through one executemany (the write protocol is not under test)."""
    template = (await repo.write_record(tenant, _candidate(source_key="order:SEED"), "live")).record
    columns = {c.name for c in MarginRecordORM.__table__.columns}
    base = {k: v for k, v in template.items() if k in columns}
    rows = []
    for i in range(count - 1):
        row = dict(base)
        row["record_id"] = f"mr_{uuid.uuid4().hex}"
        row["source_key"] = f"order:BULK-{i}"
        row["order_id"] = f"BULK-{i}"
        rows.append(row)
    async with session_scope() as session:
        await session.execute(MarginRecordORM.__table__.insert(), rows)


async def test_over_50000_rows_is_413_before_the_first_byte(api, repo, caplog, monkeypatch):
    await _bulk_insert(repo, TENANT_A, MAX_EXPORT_ROWS + 1)
    calls = []
    original = repo.list_records

    async def spy(*args, **kwargs):
        calls.append(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(repo, "list_records", spy)
    caplog.set_level(logging.INFO, logger="services.csv_export")
    resp = await api.as_("admin").client.get(URL)
    assert resp.status_code == 413
    assert error_code(resp) == "EXPORT_TOO_LARGE"
    assert resp.json()["details"] == {"row_count": MAX_EXPORT_ROWS + 1, "max_rows": MAX_EXPORT_ROWS}
    assert calls == []  # counted, never paged
    [record] = [r for r in caplog.records if r.getMessage() == "data_export"]
    assert record.extra_data["outcome"] == "rejected_too_large"


async def test_exactly_50000_rows_streams_every_row(api, repo):
    await _bulk_insert(repo, TENANT_A, MAX_EXPORT_ROWS)
    rows = rows_of(await api.as_("admin").client.get(URL))
    assert len(rows) - 1 == MAX_EXPORT_ROWS
    assert len({r[0] for r in rows[1:]}) == MAX_EXPORT_ROWS
