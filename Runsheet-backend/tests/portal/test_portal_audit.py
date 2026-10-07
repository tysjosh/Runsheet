"""``portal_audit`` lines (design §8.1; AUD-1, AC16).

[real-auth] Generic over every matched route, so later FEATs are covered.
"""
from __future__ import annotations

import logging

import pytest

from portal.audit import AUDIT_FIELDS, emit_portal_audit, outcome_for_status
from tests.portal.conftest import audit_records, call, fill_path, portal_routes

_REQUIRED = {
    "portal_audit",
    "timestamp",
    "tenant_id",
    "actor_user_id",
    "customer_id",
    "action",
    "target_ids",
    "outcome",
    "request_id",
}


def _assert_clean(record):
    assert set(record) <= AUDIT_FIELDS, set(record) - AUDIT_FIELDS
    assert _REQUIRED <= set(record)
    values = [v for k, v in record.items() if k not in ("timestamp", "target_ids")]
    values += list(record["target_ids"].values())
    for value in values:
        if isinstance(value, str):
            assert "@" not in value and not any(c.isspace() for c in value), value


def test_every_portal_route_one_line(portal_app, client, portal_on, cA, caplog):
    caplog.set_level(logging.INFO, logger="portal_audit")
    for method, route in portal_routes(portal_app):
        caplog.clear()
        resp = call(client, method, fill_path(route.path), cA, json={})
        lines = audit_records(caplog)
        assert len(lines) == 1, (method, route.path, lines)
        line = lines[0]
        _assert_clean(line)
        assert line["action"] == route.name
        assert line["path_template"] == route.path
        assert line["outcome"] == outcome_for_status(resp.status_code)
        assert line["tenant_id"] == cA.claims["tenant_id"]
        assert line["actor_user_id"] == cA.user_id
        assert line["customer_id"] == cA.claims["customer_id"]
        assert line["request_id"]
        assert resp.headers.get("cache-control") == "no-store"


def test_me_success_is_info_ok(client, portal_on, cA, caplog):
    caplog.set_level(logging.INFO, logger="portal_audit")
    resp = call(client, "GET", "/api/portal/me", cA)
    assert resp.status_code == 200
    (record,) = [r for r in caplog.records if r.name == "portal_audit"]
    assert record.levelno == logging.INFO
    assert record.extra_data["outcome"] == "ok"
    assert record.extra_data["request_id"] == resp.headers["x-request-id"]


def test_staff_403_and_disabled_404_carry_actor(client, set_flags, sessions, portal_fakes, caplog):
    caplog.set_level(logging.INFO, logger="portal_audit")
    staff = sessions.staff("admin")

    set_flags()
    resp = call(client, "GET", "/api/portal/me", staff)
    assert resp.status_code == 403
    (line,) = audit_records(caplog)
    _assert_clean(line)
    assert line["outcome"] == "forbidden"
    assert line["actor_user_id"] == staff.user_id
    assert line["customer_id"] is None

    caplog.clear()
    set_flags(portal=False)
    resp = call(client, "GET", "/api/portal/me", staff)
    assert resp.status_code == 404
    (line,) = audit_records(caplog)
    assert line["outcome"] == "not_found"
    assert line["actor_user_id"] == staff.user_id
    assert line["customer_id"] is None


def test_central_deny_http_warn_line(client, portal_on, cA, caplog):
    caplog.set_level(logging.INFO, logger="portal_audit")
    resp = call(client, "GET", "/api/fuel/mvp/forecasts/T-123/history", cA)
    assert resp.status_code == 403
    records = [r for r in caplog.records if r.name == "portal_audit"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    line = records[0].extra_data
    _assert_clean(line)
    assert line["outcome"] == "forbidden_route"
    assert line["channel"] == "http"
    assert line["customer_id"] == cA.claims["customer_id"]
    assert line["actor_user_id"] == cA.user_id
    assert "T-123" not in line["path_template"]


def test_emit_redacts_emails_and_drops_unknown_fields(caplog):
    caplog.set_level(logging.INFO, logger="portal_audit")
    emit_portal_audit(
        level=logging.INFO,
        tenant_id="t",
        actor_user_id="someone@example.com",
        customer_id="c",
        action="x",
        target_ids={"invoice_id": "has space"},
        outcome="ok",
        request_id="r",
        email="leak@example.com",
    )
    (line,) = audit_records(caplog)
    _assert_clean(line)
    assert "email" not in line
    assert line["actor_user_id"] == "{redacted}"
    assert line["target_ids"]["invoice_id"] == "{redacted}"


@pytest.mark.parametrize(
    "status,outcome",
    [(200, "ok"), (201, "ok"), (401, "unauthenticated"), (403, "forbidden"),
     (404, "not_found"), (409, "conflict"), (422, "invalid"), (429, "rate_limited"),
     (500, "error"), (503, "error")],
)
def test_outcome_map(status, outcome):
    assert outcome_for_status(status) == outcome
