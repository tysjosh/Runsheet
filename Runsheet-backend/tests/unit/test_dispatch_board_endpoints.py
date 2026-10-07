"""``/api/fuel/board`` routes (plan task 13; design K11, "External input validation"; R23, N3).

Roles, guard order, tenant isolation, the input table, idempotency-key rules,
the no-echo rule for 422s and the response envelope. The routes run on a bare
FastAPI app with the real exception handlers and a session override.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errors.handlers import register_exception_handlers
from fuel.api import dispatch_board_endpoints as board_api
from fuel.api import feature_flag_admin_endpoints as flag_api
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.unit._dispatch_board_fakes import OTHER, T, TODAY, FakeFlags, Harness

BASE = "/api/fuel/board"
DAY = TODAY.isoformat()


class Session:
    def __init__(self) -> None:
        self.tenant_id = T
        self.user_id = "user-1"
        self.roles: List[str] = ["dispatcher"]

    def ctx(self) -> TenantContext:
        return TenantContext(tenant_id=self.tenant_id, user_id=self.user_id, has_pii_access=False, roles=list(self.roles))


class FakePublish:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def handle(self, **kwargs):
        self.calls.append(kwargs)
        return {"accepted": type(kwargs["body"]).__name__}

    async def status(self, **kwargs):
        return {"publish_id": kwargs["publish_id"]}


def make_app(h: Harness, flags: Any, session: Session, *, publish: Any = None):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(board_api.router)
    app.include_router(flag_api.router)
    app.dependency_overrides[get_tenant_context] = session.ctx
    board_api.configure_dispatch_board_endpoints(board_service=h.service, feature_flag_service=flags, publish_service=publish)
    flag_api.configure_feature_flag_admin(feature_flag_service=flags)
    return app


@pytest.fixture
def env():
    h = Harness()
    session = Session()
    flags = FakeFlags({T: "active_gated", OTHER: "active_gated"})
    publish = FakePublish()
    client = TestClient(make_app(h, flags, session, publish=publish))
    yield h, session, flags, client, publish
    board_api.configure_dispatch_board_endpoints(board_service=None, feature_flag_service=None)
    flag_api.configure_feature_flag_admin(feature_flag_service=None)


def command(type_: str, versions: Dict[str, int], **fields):
    return {"type": type_, "client_command_id": str(uuid.uuid4()), "expected_lane_versions": versions, **fields}


def error_code(resp) -> str:
    body = resp.json()
    return body.get("error_code") or body.get("error", {}).get("code") or body.get("detail", {}).get("error_code")


# ---- envelope and happy path ---------------------------------------------


def test_status_snapshot_command_envelope(env):
    h, session, flags, client, _ = env
    status = client.get(f"{BASE}/status")
    assert status.status_code == 200 and status.json() == {"data": {"mode": "active_gated"}, "request_id": ""}
    resp = client.post(f"{BASE}/{DAY}/commands", json=command("add_lane", {"T1": 0}, truck_id="T1"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"data", "request_id"} and body["data"]["lanes"][0]["truck_id"] == "T1"
    snap = client.get(f"{BASE}/{DAY}")
    assert snap.status_code == 200 and [l["truck_id"] for l in snap.json()["data"]["lanes"]] == ["T1"]
    history = client.get(f"{BASE}/{DAY}/history", params={"size": "10"})
    assert history.status_code == 200 and history.json()["data"]["items"][0]["type"] == "add_lane"
    h.seed_order("o1")
    val = client.post(f"{BASE}/{DAY}/validate", json={"item": {"kind": "order", "ids": ["o1"]}, "candidates": ["T1"]})
    assert val.status_code == 200 and "T1" in val.json()["data"]["results"]


def test_actor_comes_from_the_session_not_the_body(env):
    h, session, flags, client, _ = env
    body = command("add_lane", {"T1": 0}, truck_id="T1")
    body["actor_user_id"] = "forged"
    assert client.post(f"{BASE}/{DAY}/commands", json=body).status_code == 422
    body.pop("actor_user_id")
    session.user_id = "user-2"
    client.post(f"{BASE}/{DAY}/commands", json=body)
    assert h.store.command_docs()[0]["actor_user_id"] == "user-2"


# ---- roles and guard order (K11.1) ----------------------------------------


@pytest.mark.parametrize("roles", [["admin"], ["dispatcher"], ["dispatcher", "driver"]])
def test_admin_and_dispatcher_allowed(env, roles):
    _h, session, _f, client, _ = env
    session.roles = roles
    assert client.get(f"{BASE}/status").status_code == 200
    assert client.post(f"{BASE}/{DAY}/commands", json=command("add_lane", {"T1": 0}, truck_id="T1")).status_code == 200


@pytest.mark.parametrize("roles", [["driver"], ["platform_admin"], ["admin_ops"], []])
def test_other_roles_denied_in_an_active_tenant(env, roles):
    _h, session, _f, client, _ = env
    session.roles = roles
    for resp in (
        client.get(f"{BASE}/status"),
        client.get(f"{BASE}/{DAY}"),
        client.post(f"{BASE}/{DAY}/commands", json=command("add_lane", {"T1": 0}, truck_id="T1")),
        client.get(f"{BASE}/{DAY}/history"),
    ):
        assert resp.status_code == 403


def test_driver_in_a_disabled_tenant_gets_404_not_403(env):
    _h, session, flags, client, _ = env
    session.roles = ["driver"]
    flags.states[T] = "disabled"
    for resp in (
        client.get(f"{BASE}/status"),
        client.post(f"{BASE}/{DAY}/commands", json=command("add_lane", {"T1": 0}, truck_id="T1")),
    ):
        assert resp.status_code == 404 and error_code(resp) == "DISPATCH_BOARD_DISABLED"


# ---- tenant isolation (I9, R23.3) -----------------------------------------


def test_tenant_isolation(env):
    h, session, flags, client, _ = env
    h.seed_order("o1")
    assert client.post(f"{BASE}/{DAY}/commands", json=command("add_lane", {"T1": 0}, truck_id="T1")).status_code == 200
    session.tenant_id = OTHER
    snap = client.get(f"{BASE}/{DAY}").json()["data"]
    assert snap["lanes"] == [] and snap["trays"]["orders"] == [] and snap["trays"]["trucks"] == []
    assert client.get(f"{BASE}/{DAY}/history").json()["data"]["items"] == []
    # Another tenant's truck and order are simply unknown.
    resp = client.post(f"{BASE}/{DAY}/commands", json=command("add_lane", {"T1": 0}, truck_id="T1"))
    assert resp.status_code == 422 and resp.json()["details"]["reason"] == "unknown_truck"
    h.store.seed("truck_compartments", "Z_C1", {"tenant_id": OTHER, "truck_id": "Z1", "compartment_id": "C1", "capacity_liters": 9000, "allowed_grades": ["AGO"]})
    assert client.post(f"{BASE}/{DAY}/commands", json=command("add_lane", {"Z1": 0}, truck_id="Z1")).status_code == 200
    resp = client.post(f"{BASE}/{DAY}/commands", json=command("assign_orders", {"Z1": 1}, order_ids=["o1"], truck_id="Z1"))
    assert resp.status_code == 422 and resp.json()["details"]["reason"] == "unknown_order"
    # Tenant 1's draft is untouched.
    assert h.store.draft(T, TODAY).lanes.keys() == {"T1"}


# ---- input table -----------------------------------------------------------


@pytest.mark.parametrize(
    "path,reason",
    [
        ("not-a-date", "invalid_date"),
        ((TODAY - timedelta(days=8)).isoformat(), "date_out_of_range"),
        ((TODAY + timedelta(days=15)).isoformat(), "date_out_of_range"),
    ],
)
def test_service_date_rules(env, path, reason):
    _h, _s, _f, client, _ = env
    resp = client.get(f"{BASE}/{path}")
    assert resp.status_code == 422 and resp.json()["details"]["reason"] == reason


@pytest.mark.parametrize(
    "params,reason",
    [
        ({"lanes": "T1,bad id"}, "invalid_lanes"),
        ({"call_type": "monthly"}, "invalid_filter"),
        ({"product": "x" * 33}, "invalid_filter"),
        ({"window": "soon"}, "invalid_filter"),
    ],
)
def test_query_param_rules(env, params, reason):
    _h, _s, _f, client, _ = env
    resp = client.get(f"{BASE}/{DAY}", params=params)
    assert resp.status_code == 422 and resp.json()["details"]["reason"] == reason


def test_long_reason_is_422_and_never_echoed(env):
    _h, _s, _f, client, _ = env
    secret = "PRIVATE-NOTE-" + "y" * 490
    body = command("acknowledge_warning", {}, truck_id="T1", warning_id="0123456789abcdef", reason=secret)
    resp = client.post(f"{BASE}/{DAY}/commands", json=body)
    assert resp.status_code == 422 and error_code(resp) == "VALIDATION_ERROR"
    assert "PRIVATE-NOTE" not in resp.text


def test_missing_command_id_is_missing_idempotency_key_without_echo(env):
    _h, _s, _f, client, _ = env
    body = {"type": "add_lane", "truck_id": "SECRET-TRUCK-VALUE", "expected_lane_versions": {}}
    resp = client.post(f"{BASE}/{DAY}/commands", json=body)
    assert resp.status_code == 422 and error_code(resp) == "MISSING_IDEMPOTENCY_KEY"
    assert "SECRET-TRUCK-VALUE" not in resp.text


def test_publish_body_rules(env):
    _h, _s, _f, client, publish = env
    lanes = [{"truck_id": "SECRETLANE", "expected_version": 1}]
    resp = client.post(f"{BASE}/{DAY}/publish", json={"lanes": lanes})
    assert resp.status_code == 422 and error_code(resp) == "MISSING_IDEMPOTENCY_KEY"
    assert "SECRETLANE" not in resp.text
    resp = client.post(f"{BASE}/{DAY}/publish", json={"dry_run": True, "lanes": lanes})
    assert resp.status_code == 200 and resp.json()["data"] == {"accepted": "PublishPreviewBody"}
    resp = client.post(f"{BASE}/{DAY}/publish", json={"dry_run": "true", "lanes": lanes})
    assert resp.status_code == 422 and error_code(resp) == "MISSING_IDEMPOTENCY_KEY"
    for bad in ([lanes], "publish", 12):
        resp = client.post(f"{BASE}/{DAY}/publish", content=json.dumps(bad), headers={"content-type": "application/json"})
        assert resp.status_code == 422 and error_code(resp) == "VALIDATION_ERROR", bad
    resp = client.post(f"{BASE}/{DAY}/publish", json={"client_request_id": str(uuid.uuid4()), "lanes": lanes})
    assert resp.status_code == 200 and resp.json()["data"] == {"accepted": "PublishBody"}
    assert publish.calls[-1]["tenant_id"] == T and publish.calls[-1]["user_id"] == "user-1"


def test_publish_and_reject_without_services_are_503(env):
    h, session, flags, _client, _ = env
    client = TestClient(make_app(h, flags, session, publish=None))
    resp = client.post(f"{BASE}/{DAY}/publish", json={"dry_run": True, "lanes": [{"truck_id": "T1", "expected_version": 0}]})
    assert resp.status_code == 503 and resp.json()["details"]["reason"] == "service_not_configured"
    resp = client.post(f"{BASE}/{DAY}/suggestions/plan-1/reject", json={})
    assert resp.status_code == 503
    assert client.get(f"{BASE}/{DAY}/publish/pub-1").status_code == 404


def test_path_id_and_history_rules(env):
    _h, _s, _f, client, _ = env
    assert client.get(f"{BASE}/{DAY}/publish/bad%20id").status_code == 422
    assert client.post(f"{BASE}/{DAY}/suggestions/bad%20id/reject", json={}).status_code == 422
    for params in ({"size": "51"}, {"size": "abc"}, {"truck_id": "bad id"}, {"cursor": "bad|cursor"}):
        resp = client.get(f"{BASE}/{DAY}/history", params=params)
        assert resp.status_code == 422 and error_code(resp) == "VALIDATION_ERROR", params


def test_validate_body_rules(env):
    _h, _s, _f, client, _ = env
    resp = client.post(f"{BASE}/{DAY}/validate", json={"item": {"kind": "order", "ids": ["o1"]}, "candidates": [f"T{i}" for i in range(61)]})
    assert resp.status_code == 422
    assert client.post(f"{BASE}/{DAY}/validate", json=None).status_code == 422


def test_bootstrap_wires_the_board_once_with_lazy_validators():
    """``bootstrap/agents.py`` builds the services, configures the router and
    mounts it idempotently; HOS (built later by ``bootstrap/driver.py``) is
    resolved at call time."""
    from bootstrap.agents import _wire_dispatch_board
    from bootstrap.container import ServiceContainer
    from fuel.services.dispatch_validation import Lazy

    h = Harness()
    container = ServiceContainer()
    container.ops_feature_flags = FakeFlags({T: "active_gated"})
    container.order_repository = h.orders
    container.driver_repository = h.drivers
    app = FastAPI()
    app.include_router(board_api.router)  # main.py includes it at import time
    try:
        _wire_dispatch_board(app, container, h.store, None)
        assert board_api.configured_board_service() is container.dispatch_board_service
        paths = [r.path for r in app.routes if getattr(r, "path", "").startswith(BASE)]
        assert len(paths) == len(set(paths))
        validation = container.dispatch_validation_service
        hos = validation._deps["hos_advisory_service"]
        assert isinstance(hos, Lazy) and hos.resolve() is None
        container.hos_advisory_service = h.hos
        assert hos.resolve() is h.hos
    finally:
        board_api.configure_dispatch_board_endpoints(board_service=None, feature_flag_service=None)


def test_block_refusal_and_conflict_shapes(env):
    h, _s, _f, client, _ = env
    h.seed_order("o1", status="on_hold")
    client.post(f"{BASE}/{DAY}/commands", json=command("add_lane", {"T1": 0}, truck_id="T1"))
    resp = client.post(f"{BASE}/{DAY}/commands", json=command("assign_orders", {"T1": 1}, order_ids=["o1"], truck_id="T1"))
    assert resp.status_code == 422 and error_code(resp) == "BOARD_COMMAND_BLOCKED"
    resp = client.post(f"{BASE}/{DAY}/commands", json=command("pair_driver", {"T1": 0}, truck_id="T1", driver_id="d1"))
    assert resp.status_code == 409 and error_code(resp) == "BOARD_LANE_CONFLICT"
    assert resp.json()["details"]["lanes"][0]["version"] == 1
