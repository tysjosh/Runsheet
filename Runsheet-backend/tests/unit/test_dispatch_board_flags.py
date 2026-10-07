"""Dispatch Board flag (plan task 12; design K11.1, K13; R1.3-R1.5, N6).

Every flag state against every endpoint class (reads, commands, publish,
suggestion reject), with the real ``FeatureFlagService`` over fake Redis
clients for the unset / Redis-error cases, and the admin flag pair.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from fuel.api import dispatch_board_endpoints as board_api
from fuel.api import feature_flag_admin_endpoints as flag_api
from ops.services.feature_flags import FeatureFlagService
from tests.unit._dispatch_board_fakes import OTHER, T, TODAY, FakeFlags, Harness
from tests.unit._loading_plan_fakes import DictRedis, RaisingRedis
from tests.unit.test_dispatch_board_endpoints import FakePublish, Session, error_code, make_app

BASE = "/api/fuel/board"
DAY = TODAY.isoformat()


def _requests(client):
    lanes = [{"truck_id": "T1", "expected_version": 0}]
    return {
        "status": lambda: client.get(f"{BASE}/status"),
        "snapshot": lambda: client.get(f"{BASE}/{DAY}"),
        "validate": lambda: client.post(f"{BASE}/{DAY}/validate", json={"item": {"kind": "truck", "ids": ["T1"]}, "candidates": ["T1"]}),
        "history": lambda: client.get(f"{BASE}/{DAY}/history"),
        "publish_status": lambda: client.get(f"{BASE}/{DAY}/publish/pub-1"),
        "command": lambda: client.post(
            f"{BASE}/{DAY}/commands",
            json={"type": "add_lane", "truck_id": "T1", "client_command_id": str(uuid.uuid4()), "expected_lane_versions": {"T1": 0}},
        ),
        "publish": lambda: client.post(f"{BASE}/{DAY}/publish", json={"client_request_id": str(uuid.uuid4()), "lanes": lanes}),
        "dry_run": lambda: client.post(f"{BASE}/{DAY}/publish", json={"dry_run": True, "lanes": lanes}),
        "reject": lambda: client.post(f"{BASE}/{DAY}/suggestions/plan-1/reject", json={}),
    }


READS = ("status", "snapshot", "validate", "history", "publish_status")
WRITES = ("command", "publish", "dry_run", "reject")


def _client(flags, *, roles=("dispatcher",)):
    h = Harness()
    session = Session()
    session.roles = list(roles)
    return TestClient(make_app(h, flags, session, publish=FakePublish())), h


def _real_flags(client_impl) -> FeatureFlagService:
    service = FeatureFlagService("redis://unused")
    service.client = client_impl
    return service


@pytest.fixture(autouse=True)
def _reset():
    yield
    board_api.configure_dispatch_board_endpoints(board_service=None, feature_flag_service=None)
    flag_api.configure_feature_flag_admin(feature_flag_service=None)


def test_unset_flag_is_404_on_reads_and_writes():
    client, h = _client(_real_flags(DictRedis({})))
    for name, call in _requests(client).items():
        resp = call()
        assert resp.status_code == 404 and error_code(resp) == "DISPATCH_BOARD_DISABLED", name
    assert h.store.docs["dispatch_board_drafts"] == {}


def test_explicit_disabled_is_404_everywhere():
    client, _h = _client(_real_flags(DictRedis({f"overlay_ff:dispatch_board:{T}": "disabled"})))
    for name, call in _requests(client).items():
        assert call().status_code == 404, name


def test_redis_error_is_404_on_reads_and_503_on_writes():
    client, h = _client(_real_flags(RaisingRedis()))
    calls = _requests(client)
    for name in READS:
        resp = calls[name]()
        assert resp.status_code == 404 and error_code(resp) == "DISPATCH_BOARD_DISABLED", name
    for name in WRITES:
        resp = calls[name]()
        assert resp.status_code == 503 and error_code(resp) == "DISPATCH_BOARD_MODE_UNAVAILABLE", name
    assert h.store.docs["dispatch_board_drafts"] == {}


def test_unconfigured_flag_service_fails_closed():
    client, _h = _client(None)
    calls = _requests(client)
    assert calls["snapshot"]().status_code == 404
    assert calls["command"]().status_code == 503


def test_shadow_reads_work_and_writes_are_read_only():
    client, h = _client(_real_flags(DictRedis({f"overlay_ff:dispatch_board:{T}": "shadow"})))
    calls = _requests(client)
    assert calls["status"]().json()["data"] == {"mode": "shadow"}
    snap = calls["snapshot"]()
    assert snap.status_code == 200 and snap.json()["data"]["read_only"] is True
    assert calls["validate"]().status_code == 200
    assert calls["history"]().status_code == 200
    for name in WRITES:
        resp = calls[name]()
        assert resp.status_code == 409 and error_code(resp) == "DISPATCH_BOARD_READ_ONLY", name
    assert h.store.docs["dispatch_board_drafts"] == {}


@pytest.mark.parametrize("state", ["active_gated", "active_auto"])
def test_active_states_behave_the_same(state):
    client, _h = _client(_real_flags(DictRedis({f"overlay_ff:dispatch_board:{T}": state})))
    calls = _requests(client)
    assert calls["status"]().json()["data"] == {"mode": state}
    assert calls["snapshot"]().json()["data"]["read_only"] is False
    assert calls["command"]().status_code == 200
    assert calls["dry_run"]().status_code == 200
    assert calls["publish"]().status_code == 200


def test_flag_is_per_tenant():
    flags = FakeFlags({OTHER: "active_gated"})
    client, _h = _client(flags)
    assert _requests(client)["status"]().status_code == 404


def test_flag_change_takes_effect_on_the_next_request():
    flags = FakeFlags({T: "active_gated"})
    client, _h = _client(flags)
    calls = _requests(client)
    assert calls["command"]().status_code == 200
    flags.states[T] = "disabled"  # kill switch
    assert calls["command"]().status_code == 404
    assert calls["status"]().status_code == 404


# ---- admin flag pair (K13) -----------------------------------------------


def _admin_client(flags, *, roles=("admin",), tenant_id=T):
    h = Harness()
    session = Session()
    session.roles = list(roles)
    session.tenant_id = tenant_id
    return TestClient(make_app(h, flags, session))


def test_admin_reads_and_sets_the_board_flag():
    flags = FakeFlags({})
    client = _admin_client(flags)
    resp = client.get(f"/api/ops/admin/feature-flags/{T}/dispatch-board")
    assert resp.status_code == 200 and resp.json()["data"] == {"tenant_id": T, "flag_key": "dispatch_board", "state": "disabled"}
    resp = client.post(f"/api/ops/admin/feature-flags/{T}/dispatch-board/shadow")
    assert resp.status_code == 200
    assert resp.json()["data"]["previous_state"] == "disabled" and resp.json()["data"]["new_state"] == "shadow"
    assert flags.states[T] == "shadow"


def test_admin_flag_pair_scope_and_state_checks():
    flags = FakeFlags({})
    assert _admin_client(flags, roles=("dispatcher",)).get(f"/api/ops/admin/feature-flags/{T}/dispatch-board").status_code == 403
    assert _admin_client(flags, tenant_id=OTHER).post(f"/api/ops/admin/feature-flags/{T}/dispatch-board/active_gated").status_code == 403
    bad = _admin_client(flags).post(f"/api/ops/admin/feature-flags/{T}/dispatch-board/on")
    intake = _admin_client(flags).post(f"/api/ops/admin/feature-flags/{T}/order-intake-pipeline/on")
    assert bad.status_code == intake.status_code and bad.status_code in (400, 422)
    assert flags.states == {}


def test_order_intake_flag_is_untouched_by_the_board_pair():
    flags = FakeFlags({})
    client = _admin_client(flags)
    client.post(f"/api/ops/admin/feature-flags/{T}/dispatch-board/active_gated")
    calls = []

    async def spy(flag_key, tenant_id, state, user_id):
        calls.append(flag_key)
        return "disabled"

    flags.set_overlay_state = spy
    client.post(f"/api/ops/admin/feature-flags/{T}/order-intake-pipeline/shadow")
    client.post(f"/api/ops/admin/feature-flags/{T}/dispatch-board/shadow")
    assert calls == ["order_intake_pipeline", "dispatch_board"]
