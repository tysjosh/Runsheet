"""``/ws/dispatch-board`` and ``DispatchBoardWSManager`` (plan task 18; design K10.1-K10.3; R14.4, R15.1, R12.8).

* ``_authenticate_dispatcher``: ``(tenant_id, sub, roles)``; a payload without
  ``sub``, a missing tenant or a role other than admin/dispatcher is refused.
* The real route: 4001 for a refused handshake and for a ``disabled``/unset
  board flag; a dispatcher in an active tenant connects and gets presence.
* Fan-out per ``(tenant, service_date)``, presence names and 45 s expiry, the
  256 KB payload fallback, and the broadcasts the board service sends.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import bootstrap.websockets as ws
from fuel.services.dispatch_board_ws_manager import PRESENCE_TTL_S, DispatchBoardWSManager
from tests.unit._dispatch_board_fakes import NOW, OTHER, T, TODAY, TOMORROW, FakeFlags, Harness

DAY = TODAY.isoformat()


@pytest.fixture(autouse=True)
def _reset_verifier():
    ws.configure_ws_session_verifier(None)
    yield
    ws.configure_ws_session_verifier(None)


def _verifier(claims: Optional[Dict[str, Any]]):
    async def verify(access_token, anti_csrf):
        return dict(claims) if access_token and claims is not None else None

    return verify


def _handshake():
    websocket = MagicMock()
    websocket.query_params = {}
    websocket.headers = {"authorization": "Bearer tok"}
    return websocket


# ---------------------------------------------------------------------------
# _authenticate_dispatcher (K10.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claims",
    [
        {"tenant_id": T, "roles": ["dispatcher"]},                    # no sub
        {"tenant_id": T, "sub": "", "roles": ["dispatcher"]},         # empty sub
        {"sub": "user-1", "roles": ["dispatcher"]},                   # no tenant
        {"tenant_id": T, "sub": "user-1", "roles": ["driver"]},       # wrong role
        {"tenant_id": T, "sub": "user-1", "roles": ["dispatcher_x"]},  # exact match only
        {"tenant_id": T, "sub": "user-1", "roles": "dispatcher"},     # not a list
        {"tenant_id": T, "sub": "user-1"},                            # no roles
    ],
    ids=["no-sub", "empty-sub", "no-tenant", "driver", "role-prefix", "roles-string", "no-roles"],
)
async def test_authenticate_dispatcher_refuses(claims):
    ws.configure_ws_session_verifier(_verifier(claims))
    assert await ws._authenticate_dispatcher(_handshake()) is None


@pytest.mark.parametrize("roles", [["dispatcher"], ["admin"], ["driver", "dispatcher"]])
async def test_authenticate_dispatcher_returns_tenant_sub_and_roles(roles):
    ws.configure_ws_session_verifier(_verifier({"tenant_id": T, "sub": "user-1", "roles": roles}))
    assert await ws._authenticate_dispatcher(_handshake()) == (T, "user-1", roles)


async def test_authenticate_dispatcher_refuses_an_unverified_session():
    ws.configure_ws_session_verifier(_verifier(None))
    assert await ws._authenticate_dispatcher(_handshake()) is None


# ---------------------------------------------------------------------------
# The real route
# ---------------------------------------------------------------------------


class _Names:
    async def resolve_actor_name(self, tenant_id, user_id):
        return {"user-1": "ana", "user-2": "ben"}.get(user_id, "Another dispatcher")


def _client(claims: Dict[str, Any], flags: Any, mgr: Optional[DispatchBoardWSManager] = None):
    ws.configure_ws_session_verifier(_verifier(claims))
    app = FastAPI()
    ws.register_websocket_routes(app)
    app.state.container = SimpleNamespace(
        dispatch_board_ws_manager=mgr or DispatchBoardWSManager(),
        ops_feature_flags=flags,
        dispatch_board_service=_Names(),
    )
    return TestClient(app)


def _close_code(client: TestClient, path: str) -> Any:
    try:
        with client.websocket_connect(path, headers={"authorization": "Bearer tok"}):
            return "ACCEPTED"
    except WebSocketDisconnect as exc:
        return exc.code


DISPATCHER = {"tenant_id": T, "sub": "user-1", "roles": ["dispatcher"]}


@pytest.mark.parametrize(
    "claims",
    [{"tenant_id": T, "roles": ["dispatcher"]}, {"tenant_id": T, "sub": "user-1", "roles": ["driver"]}],
    ids=["payload-without-sub", "driver-role"],
)
def test_route_rejects_refused_handshakes_with_4001(claims):
    client = _client(claims, FakeFlags({T: "active_gated"}))
    assert _close_code(client, f"/ws/dispatch-board?service_date={DAY}") == 4001


@pytest.mark.parametrize("flags", [FakeFlags({T: "disabled"}), FakeFlags({}), FakeFlags({T: "active_gated"}, error=True)], ids=["disabled", "unset", "redis-error"])
def test_route_rejects_a_disabled_tenant_with_4001(flags):
    client = _client(DISPATCHER, flags)
    assert _close_code(client, f"/ws/dispatch-board?service_date={DAY}") == 4001


def test_route_rejects_a_bad_service_date():
    client = _client(DISPATCHER, FakeFlags({T: "active_gated"}))
    assert _close_code(client, "/ws/dispatch-board?service_date=nope") == 1008
    assert _close_code(client, "/ws/dispatch-board") == 1008


@pytest.mark.parametrize("mode", ["shadow", "active_gated", "active_auto"])
def test_dispatcher_in_an_active_tenant_connects_and_gets_presence(mode):
    mgr = DispatchBoardWSManager()
    client = _client(DISPATCHER, FakeFlags({T: mode}), mgr)
    with client.websocket_connect(f"/ws/dispatch-board?service_date={DAY}", headers={"authorization": "Bearer tok"}) as sock:
        assert sock.receive_json()["type"] == "connection"
        first = sock.receive_json()
        assert first["type"] == "board_presence"
        assert first["data"]["service_date"] == DAY
        assert [(u["user_id"], u["name"], u["focus_truck_id"]) for u in first["data"]["users"]] == [("user-1", "ana", None)]
        sock.send_json({"type": "presence", "focus_truck_id": "T1"})
        update = sock.receive_json()
        assert update["type"] == "board_presence" and update["data"]["users"][0]["focus_truck_id"] == "T1"
        sock.send_json({"type": "ping"})
        assert sock.receive_json()["type"] == "pong"
        meta = next(iter(mgr._clients.values()))
        assert (meta["tenant_id"], meta["user_id"], meta["service_date"]) == (T, "user-1", DAY)
    assert mgr.active_connections == 0


# ---------------------------------------------------------------------------
# Manager: fan-out, presence, payload size
# ---------------------------------------------------------------------------


class FakeSocket:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.fail = False

    async def accept(self) -> None:
        pass

    async def send_json(self, data: Dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("gone")
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass

    def of(self, event_type: str) -> List[Dict[str, Any]]:
        return [m for m in self.sent if m.get("type") == event_type]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


async def _connect(mgr, sock, *, tenant=T, user="user-1", day=DAY, name="ana"):
    await mgr.connect_board(sock, tenant_id=tenant, user_id=user, service_date=day, name=name)


async def test_events_reach_only_the_same_tenant_and_service_date():
    mgr = DispatchBoardWSManager()
    same, other_day, other_tenant = FakeSocket(), FakeSocket(), FakeSocket()
    await _connect(mgr, same)
    await _connect(mgr, other_day, day=TOMORROW.isoformat())
    await _connect(mgr, other_tenant, tenant=OTHER)
    sent = await mgr.broadcast_board_event(T, DAY, "board_lane_stale", {"service_date": DAY, "truck_ids": ["T1"], "reason": "order_cancelled"})
    assert sent == 1
    (msg,) = same.of("board_lane_stale")
    assert set(msg) == {"type", "data", "timestamp"} and msg["data"]["truck_ids"] == ["T1"]
    assert other_day.of("board_lane_stale") == [] and other_tenant.of("board_lane_stale") == []
    # Presence never crosses tenants or days either.
    assert [u["user_id"] for u in same.of("board_presence")[-1]["data"]["users"]] == ["user-1"]
    assert await mgr.broadcast_board_event("", DAY, "board_lane_stale", {}) == 0


async def test_lanes_updated_over_the_size_cap_carries_only_ids_and_versions():
    mgr = DispatchBoardWSManager(max_payload_bytes=2048)
    sock = FakeSocket()
    await _connect(mgr, sock)
    small = {"service_date": DAY, "draft_version": 3, "lanes": [{"truck_id": "T1", "version": 2, "loads": []}]}
    await mgr.broadcast_board_event(T, DAY, "board_lanes_updated", small)
    assert sock.of("board_lanes_updated")[-1]["data"] == small
    big_lane = {"truck_id": "T2", "version": 7, "loads": [{"note": "x" * 4096}]}
    await mgr.broadcast_board_event(T, DAY, "board_lanes_updated", {**small, "lanes": [big_lane]})
    data = sock.of("board_lanes_updated")[-1]["data"]
    assert data["lanes"] == [{"truck_id": "T2", "version": 7}] and data["lanes_truncated"] is True
    assert data["draft_version"] == 3
    # Other event types are never trimmed.
    await mgr.broadcast_board_event(T, DAY, "board_publish_progress", {"lanes": [big_lane]})
    assert sock.of("board_publish_progress")[-1]["data"]["lanes"] == [big_lane]


def test_default_size_cap_is_256_kb():
    assert DispatchBoardWSManager().max_payload_bytes == 256 * 1024


async def test_presence_names_one_entry_per_user_and_45_second_expiry():
    clock = Clock()
    mgr = DispatchBoardWSManager(clock=clock)
    ana_tab1, ana_tab2, ben = FakeSocket(), FakeSocket(), FakeSocket()
    await _connect(mgr, ana_tab1)
    await _connect(mgr, ana_tab2)
    await _connect(mgr, ben, user="user-2", name="ben")
    users = ben.of("board_presence")[-1]["data"]["users"]
    assert [(u["user_id"], u["name"]) for u in users] == [("user-1", "ana"), ("user-2", "ben")]

    clock.now = NOW + timedelta(seconds=30)
    await mgr.handle_client_message(ben, '{"type": "presence", "focus_truck_id": "T2"}')
    clock.now = NOW + timedelta(seconds=PRESENCE_TTL_S + 1)
    # Ana sent nothing for 46 s: she drops out; Ben's last message was 16 s ago.
    assert [(u["user_id"], u["focus_truck_id"]) for u in mgr.presence(T, DAY)] == [("user-2", "T2")]
    await mgr.handle_client_message(ana_tab2, '{"type": "presence", "focus_truck_id": null}')
    assert [u["user_id"] for u in ana_tab1.of("board_presence")[-1]["data"]["users"]] == ["user-1", "user-2"]


async def test_disconnect_announces_the_new_presence_list():
    mgr = DispatchBoardWSManager()
    ana, ben = FakeSocket(), FakeSocket()
    await _connect(mgr, ana)
    await _connect(mgr, ben, user="user-2", name="ben")
    await mgr.disconnect(ana)
    assert [u["user_id"] for u in ben.of("board_presence")[-1]["data"]["users"]] == ["user-2"]
    await mgr.disconnect(ana)  # twice is harmless
    assert mgr.active_connections == 1


@pytest.mark.parametrize(
    "raw",
    ["not json", "[1]", '{"type": "presence", "focus_truck_id": "bad id"}', '{"type": "presence", "focus_truck_id": 5}', '{"type": "edit"}'],
)
async def test_bad_client_messages_get_an_error_frame_and_change_nothing(raw):
    mgr = DispatchBoardWSManager()
    sock = FakeSocket()
    await _connect(mgr, sock)
    before = len(sock.of("board_presence"))
    await mgr.handle_client_message(sock, raw)
    assert sock.sent[-1]["type"] == "error"
    assert len(sock.of("board_presence")) == before
    assert mgr.presence(T, DAY)[0]["focus_truck_id"] is None


async def test_a_dead_socket_is_dropped_during_a_broadcast():
    mgr = DispatchBoardWSManager()
    live, dead = FakeSocket(), FakeSocket()
    await _connect(mgr, live)
    await _connect(mgr, dead, user="user-2")
    dead.fail = True
    assert await mgr.broadcast_board_event(T, DAY, "board_suggestions_changed", {"service_date": DAY}) == 1
    assert mgr.active_connections == 1


# ---------------------------------------------------------------------------
# Broadcasts from the board service (R15.1, R14.4)
# ---------------------------------------------------------------------------


async def test_committed_command_broadcasts_lanes_with_the_actor_name():
    h = Harness()
    mgr = DispatchBoardWSManager()
    h.service._ws = mgr
    sock = FakeSocket()
    await _connect(mgr, sock, user="user-2", name="ben")
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    (msg,) = sock.of("board_lanes_updated")
    data = msg["data"]
    assert data["actor"] == {"user_id": "user-1", "name": "ana"}
    assert data["command_type"] == "add_lane" and data["draft_version"] == 1
    assert [(l["truck_id"], l["version"]) for l in data["lanes"]] == [("T1", 1)]
    # A refused command broadcasts nothing.
    with pytest.raises(Exception):
        await h.run("add_lane", truck_id="T2", versions={"T2": 5})
    assert len(sock.of("board_lanes_updated")) == 1


async def test_a_broadcast_failure_never_fails_the_command():
    h = Harness()

    class Broken:
        async def broadcast_board_event(self, *args):
            raise RuntimeError("socket layer down")

    h.service._ws = Broken()
    result = await h.run("add_lane", truck_id="T1", lanes=("T1",))
    assert result["draft_version"] == 1
