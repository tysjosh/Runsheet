"""Long-lived WebSockets close after sign-out or revoke (OI-11).

The handshake verifies the session once. ``_ws_loop`` now re-checks the
session handle from the handshake every ``WS_SESSION_RECHECK_SECONDS`` and
closes the socket with ``4001`` when the core says it has ended. ``None``
(unknown, e.g. a core blip) keeps it open. No handle means no task.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import bootstrap.websockets as ws
from Agents.agent_ws_manager import AgentActivityWSManager


@pytest.fixture(autouse=True)
def _reset_seams(monkeypatch):
    ws.configure_ws_session_verifier(None)
    ws.configure_ws_session_alive_check(None)
    monkeypatch.setattr(ws, "_session_recheck_seconds", lambda: 0.05)
    yield
    ws.configure_ws_session_verifier(None)
    ws.configure_ws_session_alive_check(None)


def _verifier(claims):
    async def fake_verify(access_token, anti_csrf):
        return dict(claims) if access_token else None

    return fake_verify


class _AliveCheck:
    """Alive-check fake. Returns ``result`` and records each handle checked."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def __call__(self, handle):
        self.calls.append(handle)
        return self.result


def _app():
    app = FastAPI()
    mgr = AgentActivityWSManager()

    @app.websocket("/ws/test")
    async def endpoint(websocket: WebSocket):
        tenant_id = await ws._authenticate_tenant(websocket)
        if not tenant_id:
            return await ws._reject(websocket)
        await mgr.connect(websocket, tenant_id=tenant_id)
        handler = lambda s, raw: ws._json_echo_handler(s, raw, "/ws/test", tenant_id)
        await ws._ws_loop(websocket, mgr, "/ws/test", tenant_id, handler=handler)

    return app, mgr


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_ended_session_closes_socket_with_4001():
    ws.configure_ws_session_verifier(_verifier({"tenant_id": "t1", "sessionHandle": "h1"}))
    check = _AliveCheck(True)
    ws.configure_ws_session_alive_check(check)
    app, _mgr = _app()

    with TestClient(app).websocket_connect("/ws/test?token=tok") as sock:
        assert sock.receive_json()["type"] == "connection"
        assert _wait_for(lambda: len(check.calls) >= 1)
        # Still alive: the socket answers a ping.
        sock.send_json({"type": "ping"})
        assert sock.receive_json()["type"] == "pong"

        check.result = False  # signed out / revoked
        with pytest.raises(WebSocketDisconnect) as exc_info:
            sock.receive_json()

    assert exc_info.value.code == 4001
    assert exc_info.value.reason == "Session ended"
    assert set(check.calls) == {"h1"}


def test_unknown_result_keeps_socket_open():
    ws.configure_ws_session_verifier(_verifier({"tenant_id": "t1", "sessionHandle": "h1"}))
    check = _AliveCheck(None)
    ws.configure_ws_session_alive_check(check)
    app, _mgr = _app()

    with TestClient(app).websocket_connect("/ws/test?token=tok") as sock:
        assert sock.receive_json()["type"] == "connection"
        assert _wait_for(lambda: len(check.calls) >= 3)
        sock.send_json({"type": "ping"})
        assert sock.receive_json()["type"] == "pong"


def test_no_session_handle_starts_no_task():
    ws.configure_ws_session_verifier(_verifier({"tenant_id": "t1"}))
    check = _AliveCheck(False)
    ws.configure_ws_session_alive_check(check)
    app, _mgr = _app()

    with TestClient(app).websocket_connect("/ws/test?token=tok") as sock:
        assert sock.receive_json()["type"] == "connection"
        time.sleep(0.2)
        sock.send_json({"type": "ping"})
        assert sock.receive_json()["type"] == "pong"

    assert check.calls == []


def test_zero_interval_disables_the_recheck(monkeypatch):
    monkeypatch.setattr(ws, "_session_recheck_seconds", lambda: 0)
    websocket = MagicMock()
    websocket.state.ws_session_handle = "h1"

    assert ws._start_session_watch(websocket, "/ws/test", "t1") is None


async def test_handle_from_claims_is_stored_on_the_socket():
    websocket = MagicMock()
    websocket.headers = {}
    websocket.query_params = {"token": "tok"}
    ws.configure_ws_session_verifier(_verifier({"tenant_id": "t1", "sessionHandle": "h9"}))

    assert await ws._authenticate_tenant(websocket) == "t1"
    assert websocket.state.ws_session_handle == "h9"


async def test_default_check_returns_none_on_core_error(monkeypatch):
    import supertokens_python.recipe.session.asyncio as st_session

    async def boom(_handle):
        raise RuntimeError("core unreachable")

    monkeypatch.setattr(st_session, "get_session_information", boom)
    assert await ws._default_ws_session_alive("h1") is None


async def test_default_check_maps_missing_session_to_false(monkeypatch):
    import supertokens_python.recipe.session.asyncio as st_session

    async def gone(_handle):
        return None

    async def present(_handle):
        return object()

    monkeypatch.setattr(st_session, "get_session_information", gone)
    assert await ws._default_ws_session_alive("h1") is False
    monkeypatch.setattr(st_session, "get_session_information", present)
    assert await ws._default_ws_session_alive("h1") is True


def test_setting_defaults_to_60_seconds():
    from config.settings import Settings

    assert Settings.model_fields["ws_session_recheck_seconds"].default == 60
