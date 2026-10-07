"""A customer session can't open any WebSocket (design §2.2 E3; XR-4).

[real-auth] Every ``WebSocketRoute`` on ``main.app`` closes a customer
handshake with 4001 before accept and logs a WARN ``portal_audit`` line.
Staff sessions still connect (regression): managers and the receive loop are
stubbed because they need the bootstrap lifecycle.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from tests.portal.conftest import audit_records


def _ws_paths(app):
    return sorted(r.path for r in app.routes if isinstance(r, WebSocketRoute))


def test_every_ws_route_refuses_customer(portal_app, client, portal_on, cA, caplog):
    paths = _ws_paths(portal_app)
    assert len(paths) >= 10
    caplog.set_level(logging.INFO, logger="portal_audit")
    for path in paths:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"{path}?token={cA.key}") as ws:
                ws.receive_text()
        assert exc.value.code == 4001, path

    lines = [r for r in audit_records(caplog) if r.get("channel") == "websocket"]
    assert len(lines) == len(paths)
    for line in lines:
        assert line["outcome"] == "forbidden_route"
        assert line["customer_id"] == cA.claims["customer_id"]
        assert line["actor_user_id"] == cA.user_id
    warn = [r for r in caplog.records if r.name == "portal_audit"]
    assert all(r.levelno == logging.WARNING for r in warn)


class _FakeManager:
    async def connect(self, websocket, **_kw):
        await websocket.accept()

    async def connect_driver(self, websocket, **_kw):
        await websocket.accept()


@pytest.fixture
def stub_ws_backends(monkeypatch):
    import Agents.support.plan_execution_ws_manager as plan_mgr
    import bootstrap.websockets as ws
    import commerce.websocket.commerce_ws as commerce_ws
    import fuel.services.fuel_planning_ws_manager as fuel_mgr

    manager = _FakeManager()

    class _Container:
        def __getattr__(self, _name):
            return manager

    async def _loop(websocket, _mgr, endpoint, *_a, **_kw):
        await websocket.send_json({"connected": endpoint})
        await websocket.close()

    monkeypatch.setattr(ws, "_container", lambda _app: _Container())
    monkeypatch.setattr(ws, "_ws_loop", _loop)
    monkeypatch.setattr(plan_mgr, "get_plan_execution_ws_manager", lambda: manager)
    monkeypatch.setattr(fuel_mgr, "get_fuel_planning_ws_manager", lambda: manager)
    monkeypatch.setattr(commerce_ws, "get_commerce_invoice_ws_manager", lambda: manager)


def test_staff_sessions_still_connect(portal_app, client, portal_on, sessions, stub_ws_backends):
    """Regression: the customer check doesn't touch staff handshakes."""
    for path in _ws_paths(portal_app):
        role = "driver" if path == "/ws/driver" else "dispatcher"
        staff = sessions.staff(role)
        with client.websocket_connect(f"{path}?token={staff.key}") as ws:
            assert ws.receive_json() == {"connected": path}


async def test_resolve_ws_claims_returns_none_for_customer(sessions, caplog):
    """E3 at the choke point: claims holding ``customer`` resolve to ``None``."""
    import bootstrap.websockets as ws

    customer = sessions.customer("demo-tenant", "QA-PORTAL-CUST-A")
    mixed = sessions.customer("demo-tenant", None, roles=["customer", "admin"])
    staff = sessions.staff("admin")

    def _socket(key):
        return SimpleNamespace(
            headers={},
            query_params={"token": key},
            url=SimpleNamespace(path="/ws/ops"),
            state=SimpleNamespace(),
        )

    assert await ws._resolve_ws_claims(_socket(customer.key)) is None
    assert await ws._resolve_ws_claims(_socket(mixed.key)) is None
    assert (await ws._resolve_ws_claims(_socket(staff.key)))["roles"] == ["admin"]
