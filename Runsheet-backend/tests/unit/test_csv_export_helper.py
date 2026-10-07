"""Pure unit tests for services.csv_export (data-export design §10)."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext
from services import csv_export
from services.csv_export import (
    ExportCapExceededDuringStream,
    ExportColumn,
    KeysetPage,
    KeysetSource,
    StaticSource,
    escape_cell,
    export_filename,
    export_guard,
    export_rate_key,
    stream_csv_export,
)

TENANT = TenantContext(tenant_id="tenant-a", user_id="u1", has_pii_access=False, roles=["admin"])
COLUMNS = [ExportColumn("id", lambda r: r["id"]), ExportColumn("name", lambda r: r.get("name"))]


def _request():
    return SimpleNamespace(state=SimpleNamespace(request_id="req-1"), headers={}, client=None)


async def _drain(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


def _run(coro):
    return asyncio.run(coro)


def _audit_records(caplog):
    return [r for r in caplog.records if r.getMessage() == "data_export"]


# ---------------------------------------------------------------- escaping

@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_formula_prefixes_escaped(prefix):
    assert escape_cell(prefix + "1+1") == "'" + prefix + "1+1"


@pytest.mark.parametrize("value,expected", [
    ("a=b", "a=b"), ("", ""), (None, ""), (-5, "-5"), (-1.5, "-1.5"),
    (True, "true"), (False, "false"),
    (datetime(2026, 10, 1, tzinfo=timezone.utc), "2026-10-01T00:00:00+00:00"),
    (["a", "b"], "a; b"), (["@x"], "'@x"),
    ({"b": 1, "a": 2}, '{"a": 2, "b": 1}'),
])
def test_escape_cell_formatting(value, expected):
    assert escape_cell(value) == expected


def test_comma_quote_newline_round_trip():
    tricky = 'a, "b"\nc'
    body = csv_export._encode_rows([[escape_cell(tricky)]])
    assert list(csv.reader(io.StringIO(body.decode("utf-8")))) == [[tricky]]


# ---------------------------------------------------------------- filename

def test_export_filename_format_and_sanitization():
    now = datetime(2026, 10, 4, 23, 30, tzinfo=timezone.utc)
    assert export_filename("orders", "demo-tenant", now) == "orders_demo-tenant_20261004.csv"
    name = export_filename("orders", 'acme/"x"\r\n', now)
    assert name == "orders_acme--x---_20261004.csv"
    for bad in ('"', "\r", "\n", ";", "/"):
        assert bad not in name
    assert export_filename("ifta", "", now) == "ifta_tenant_20261004.csv"


# ---------------------------------------------------------------- stream

def test_bom_header_crlf_and_headers():
    async def go():
        resp = await stream_csv_export(
            request=_request(), tenant=TENANT, export_type="orders", columns=COLUMNS,
            source=StaticSource([{"id": "o1", "name": "=bad"}, {"id": "o2"}]), filters={},
        )
        return resp, await _drain(resp)

    resp, body = _run(go())
    assert body[:3] == b"\xef\xbb\xbf"
    assert body[3:].decode("utf-8") == "id,name\r\no1,'=bad\r\no2,\r\n"
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    assert resp.headers["content-disposition"].startswith('attachment; filename="orders_tenant-a_')
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_cap_rejects_before_stream(caplog):
    started = []

    class Src(StaticSource):
        async def pages(self):
            started.append(True)
            async for p in super().pages():
                yield p

    caplog.set_level(logging.INFO, logger="services.csv_export")
    with pytest.raises(AppException) as exc:
        _run(stream_csv_export(
            request=_request(), tenant=TENANT, export_type="orders", columns=COLUMNS,
            source=Src([{"id": str(i)} for i in range(4)]), filters={}, max_rows=3,
        ))
    assert exc.value.status_code == 413
    assert exc.value.error_code == "EXPORT_TOO_LARGE"
    assert exc.value.details == {"row_count": 4, "max_rows": 3}
    assert started == []
    (rec,) = _audit_records(caplog)
    assert rec.extra_data["outcome"] == "rejected_too_large"
    assert rec.extra_data["row_count"] == 4


def test_cap_race_aborts(caplog):
    class Lying(StaticSource):
        async def count(self):
            return 3

    caplog.set_level(logging.INFO, logger="services.csv_export")

    async def go():
        resp = await stream_csv_export(
            request=_request(), tenant=TENANT, export_type="orders", columns=COLUMNS,
            source=Lying([{"id": str(i)} for i in range(4)]), filters={}, max_rows=3,
        )
        await _drain(resp)

    with pytest.raises(ExportCapExceededDuringStream):
        _run(go())
    (rec,) = _audit_records(caplog)
    assert rec.extra_data["outcome"] == "aborted_cap_race"


def test_mid_stream_failure_is_failed(caplog):
    class Boom(StaticSource):
        async def pages(self):
            yield [{"id": "1"}]
            raise RuntimeError("store down")

    caplog.set_level(logging.INFO, logger="services.csv_export")

    async def go():
        resp = await stream_csv_export(
            request=_request(), tenant=TENANT, export_type="jobs", columns=COLUMNS,
            source=Boom([{"id": "1"}]), filters={},
        )
        await _drain(resp)

    with pytest.raises(RuntimeError):
        _run(go())
    (rec,) = _audit_records(caplog)
    assert rec.extra_data["outcome"] == "failed"
    assert rec.extra_data["row_count"] == 1


def test_count_failure_emits_one_failed_audit_line(caplog):
    """OI-24: a count() that raises still leaves an audit line."""
    class CountBoom(StaticSource):
        async def count(self):
            raise RuntimeError("store down")

    caplog.set_level(logging.INFO, logger="services.csv_export")
    with pytest.raises(RuntimeError):
        _run(stream_csv_export(
            request=_request(), tenant=TENANT, export_type="orders", columns=COLUMNS,
            source=CountBoom([]), filters={"status": "pending"},
        ))
    (rec,) = _audit_records(caplog)
    assert rec.levelno == logging.WARNING
    assert rec.extra_data["outcome"] == "failed"
    assert rec.extra_data["row_count"] == 0
    assert rec.extra_data["filters"] == {"status": "pending"}


def test_audit_line_fields_and_q_redaction(caplog):
    caplog.set_level(logging.INFO, logger="services.csv_export")

    async def go():
        resp = await stream_csv_export(
            request=_request(), tenant=TENANT, export_type="orders", columns=COLUMNS,
            source=StaticSource([{"id": "o1", "name": "Secret Row Value"}]),
            filters={"q": "Jane Doe 555-0100", "status": "pending", "customer_id": None},
        )
        await _drain(resp)

    _run(go())
    (rec,) = _audit_records(caplog)
    data = rec.extra_data
    for key in ("tenant_id", "user_id", "export_type", "filters", "row_count", "outcome"):
        assert key in data
    assert data["tenant_id"] == "tenant-a" and data["user_id"] == "u1"
    assert data["outcome"] == "completed" and data["row_count"] == 1
    assert data["filters"] == {"q": {"present": True, "length": 17}, "status": "pending"}
    dumped = json.dumps(data)
    assert "Jane" not in dumped and "555-0100" not in dumped
    assert "Secret Row Value" not in dumped
    assert '"length": 17' in dumped


# ---------------------------------------------------------------- paging

def _fake_fetch(rows, calls, *, drop_ids=()):
    async def fetch(after, size, with_total):
        calls.append((after, size, with_total))
        start = 0 if after is None else next(i for i, r in enumerate(rows) if r["id"] == after[1]) + 1
        raw = rows[start:start + size]
        return KeysetPage(
            rows=[r for r in raw if r["id"] not in drop_ids],
            raw_count=len(raw),
            total=len(rows) if with_total else None,
            last_key=(None, raw[-1]["id"]) if raw else None,
        )
    return fetch


def test_keyset_source_pages_all_rows_once():
    rows = [{"id": f"r{i}"} for i in range(5)]
    calls = []
    src = KeysetSource(_fake_fetch(rows, calls), page_size=2)

    async def collect():
        out = []
        async for page in src.pages():
            out.extend(page)
        return out

    assert [r["id"] for r in _run(collect())] == ["r0", "r1", "r2", "r3", "r4"]
    assert [c[2] for c in calls] == [False, False, False]


def test_dropped_row_does_not_stop_paging():
    rows = [{"id": f"r{i}"} for i in range(5)]
    src = KeysetSource(_fake_fetch(rows, [], drop_ids={"r1"}), page_size=2)

    async def collect():
        out = []
        async for page in src.pages():
            out.extend(page)
        return out

    assert [r["id"] for r in _run(collect())] == ["r0", "r2", "r3", "r4"]


def test_count_uses_size_one_probe():
    calls = []
    src = KeysetSource(_fake_fetch([{"id": "a"}, {"id": "b"}], calls), page_size=2)
    assert _run(src.count()) == 2
    assert calls == [(None, 1, True)]


def test_count_override():
    async def count():
        return 42
    src = KeysetSource(_fake_fetch([], []), count=count)
    assert _run(src.count()) == 42


# ---------------------------------------------------------------- guard / key

def test_export_rate_key_stamped_and_fallback():
    stamped = SimpleNamespace(
        state=SimpleNamespace(export_tenant_id="t", export_user_id="u"), headers={}, client=None,
    )
    assert export_rate_key(stamped) == "export:t:u"
    from starlette.requests import Request
    raw = Request({"type": "http", "headers": [], "client": ("10.0.0.9", 1234), "state": {}})
    assert export_rate_key(raw) == "10.0.0.9"


def test_export_guard_requires_roles():
    with pytest.raises(ValueError):
        export_guard()


def test_export_guard_stamps_and_rejects():
    dep = export_guard("admin")
    req = SimpleNamespace(state=SimpleNamespace())
    assert _run(dep(req, TENANT)) is TENANT
    assert req.state.export_user_id == "u1" and req.state.export_tenant_id == "tenant-a"
    driver = TenantContext(tenant_id="tenant-a", user_id="u2", has_pii_access=False, roles=["driver"])
    with pytest.raises(AppException) as exc:
        _run(dep(SimpleNamespace(state=SimpleNamespace()), driver))
    assert exc.value.status_code == 403
