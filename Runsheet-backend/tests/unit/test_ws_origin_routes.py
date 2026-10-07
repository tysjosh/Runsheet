"""
Staging finding F2 against the REAL WebSocket routes.

``/ws/agent-activity`` and ``/api/fleet/live`` accepted a valid session cookie
(and Bearer token) sent with ``Origin: https://evil.example.com``. These tests
mount :func:`bootstrap.websockets.register_websocket_routes` on a fresh app and
drive the actual handlers, with a verifier that accepts every credential, so
the only thing that can reject a handshake is the Origin check.

Every route authenticates through ``_resolve_ws_claims``; the last test pins
that by checking all eleven registered routes reject a cross-origin handshake.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import bootstrap.websockets as ws

_APP_ORIGIN = "https://app.example.test"
_EVIL_ORIGIN = "https://evil.example.com"

_ALL_ROUTES = (
    "/ws/ops",
    "/ws/scheduling",
    "/ws/orders",
    "/ws/notifications",
    "/ws/agent-activity",
    "/api/fleet/live",
    "/ws/driver",
    "/ws/plan-execution",
    "/ws/fuel-planning",
    "/ws/commerce/invoices",
    "/ws/dispatch-board",
)


class _SpyManager:
    def __init__(self) -> None:
        self.connects = []
        self._clients = []

    async def connect(self, websocket, **kwargs):
        self.connects.append(kwargs)
        self._clients.append(websocket)
        await websocket.accept()

    async def connect_driver(self, websocket, **kwargs):
        await self.connect(websocket, **kwargs)

    async def disconnect(self, websocket):
        pass


@pytest.fixture
def spy():
    return _SpyManager()


@pytest.fixture
def client(spy, monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", f'["{_APP_ORIGIN}"]')

    async def _always_valid(access_token, anti_csrf):
        if not access_token:
            return None
        return {"tenant_id": "t-1", "driver_id": "d-1"}

    ws.configure_ws_session_verifier(_always_valid)
    app = FastAPI()
    register = ws.register_websocket_routes
    register(app)
    app.state.container = SimpleNamespace(
        agent_ws_manager=spy,
        fleet_ws_manager=spy,
        ops_ws_manager=spy,
        scheduling_ws_manager=spy,
        orders_ws_manager=spy,
        notification_ws_manager=spy,
        driver_ws_manager=spy,
    )
    try:
        yield TestClient(app)
    finally:
        ws.configure_ws_session_verifier(None)


def _close_code(client, path, headers):
    try:
        with client.websocket_connect(path, headers=headers):
            return "ACCEPTED"
    except WebSocketDisconnect as exc:
        return exc.code


@pytest.mark.parametrize("path", ["/ws/agent-activity", "/api/fleet/live"])
@pytest.mark.parametrize(
    "credential",
    [{"cookie": "sAccessToken=tok"}, {"authorization": "Bearer tok"}],
    ids=["cookie", "bearer"],
)
def test_evil_origin_rejected_with_4001(client, spy, path, credential):
    assert _close_code(client, path, {**credential, "origin": _EVIL_ORIGIN}) == 4001
    assert spy.connects == []


@pytest.mark.parametrize("path", ["/ws/agent-activity", "/api/fleet/live"])
def test_allowed_origin_with_cookie_is_served(client, spy, path):
    headers = {"cookie": "sAccessToken=tok", "origin": _APP_ORIGIN}
    with client.websocket_connect(path, headers=headers) as sock:
        sock.send_json({"type": "ping"})
        assert sock.receive_json()["type"] == "pong"
    assert spy.connects == [{"tenant_id": "t-1"}]


@pytest.mark.parametrize("path", ["/ws/agent-activity", "/api/fleet/live"])
@pytest.mark.parametrize(
    "origin", [None, "http://testserver"], ids=["no-origin", "same-origin"]
)
def test_no_or_same_origin_with_bearer_is_served(client, spy, path, origin):
    headers = {"authorization": "Bearer tok"}
    if origin:
        headers["origin"] = origin
    with client.websocket_connect(path, headers=headers) as sock:
        sock.send_json({"type": "ping"})
        assert sock.receive_json()["type"] == "pong"
    assert len(spy.connects) == 1


@pytest.mark.parametrize("path", _ALL_ROUTES)
def test_every_route_rejects_cross_origin(client, spy, path):
    headers = {"cookie": "sAccessToken=tok", "origin": _EVIL_ORIGIN}
    assert _close_code(client, path, headers) == 4001
    assert spy.connects == []


def test_route_list_matches_registered_websocket_routes():
    app = FastAPI()
    ws.register_websocket_routes(app)
    registered = {
        route.path for route in app.routes if type(route).__name__ == "APIWebSocketRoute"
    }
    assert registered == set(_ALL_ROUTES)
