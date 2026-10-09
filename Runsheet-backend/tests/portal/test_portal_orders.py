"""Portal order requests (design §4.1-§4.5; OID-1..3, ORD-1, ORD-1b, ORD-2,
ORD-5, ORD-6).

[real-auth] ``main.app`` with fake session verifiers and the
:class:`OrderHarness` (real pipeline, order and tank repositories over one
in-memory document store).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from errors.exceptions import AppException
from fuel.order_models import PORTAL_REVIEW_HOLD_REASON
from fuel.services.order_intake_pipeline import portal_event_id, portal_order_id
from portal.services.portal_order_service import REJECTED_MESSAGE
from tests.portal.conftest import CUSTOMER_A, CUSTOMER_B, T1, T2, CUSTOMER_C, call, iso

TANK = "QA-TANK-A1"


def order_body(tank_id: str = TANK, *, cid=None, quantity=None, start=None, end=None, **extra):
    start = start or datetime.now(timezone.utc) + timedelta(days=1)
    end = end or start + timedelta(hours=8)
    body = {
        "client_event_id": cid or str(uuid.uuid4()),
        "customer_tank_id": tank_id,
        "quantity": quantity or {"mode": "gallons", "gallons": 100},
        "window_start": start.isoformat() if isinstance(start, datetime) else start,
        "window_end": end.isoformat() if isinstance(end, datetime) else end,
    }
    body.update(extra)
    return body


@pytest.fixture
def orders(portal_on, portal_orders, cA):
    portal_orders.add_tank(T1, CUSTOMER_A, TANK, product="HEATING_OIL", lat=41.88, lon=-87.63,
                           external_tank_id="QA-EXT-7")
    return portal_orders


def post(client, session, body):
    return call(client, "POST", "/api/portal/orders", session, json=body)


def _err(resp):
    return resp.json()["error_code"]


# ---------------------------------------------------------------------------
# OID-1 / OID-2: portal ordering off (portal-fixes B2: the tenant's portal
# ordering setting, not the order_intake_pipeline rollout flag)
# ---------------------------------------------------------------------------


class _SettingsRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture
def portal_ordering(cA):  # noqa: ARG001 — after the session fixture resets the guard
    """``portal_ordering(T1, False)`` turns the tenant's portal ordering off."""
    from ops.middleware import tenant_guard
    from services.tenant_settings import TenantSettingsService

    redis = _SettingsRedis()
    tenant_guard.configure_tenant_guard(TenantSettingsService(redis_client=redis))

    def set_(tenant_id, enabled):
        key = f"tenant:{tenant_id}:portal_ordering"
        if enabled:
            redis.data.pop(key, None)
        else:
            redis.data[key] = "disabled"

    try:
        yield set_
    finally:
        tenant_guard.configure_tenant_guard(None)


def test_portal_ordering_off_409(client, orders, cA, portal_ordering):
    portal_ordering(T1, False)
    resp = post(client, cA, order_body())
    assert resp.status_code == 409
    assert _err(resp) == "ORDER_INTAKE_DISABLED"
    assert orders.order_writes() == 0
    assert orders.ingest_calls == []
    me = call(client, "GET", "/api/portal/me", cA).json()["data"]
    assert me["ordering_available"] is False


def test_portal_ordering_off_for_one_tenant_only(client, orders, cA, cC, portal_ordering):
    portal_ordering(T2, False)
    assert post(client, cA, order_body()).status_code == 201
    assert call(client, "GET", "/api/portal/me", cA).json()["data"]["ordering_available"] is True
    assert call(client, "GET", "/api/portal/me", cC).json()["data"]["ordering_available"] is False


def test_replay_while_ordering_off_returns_original(client, orders, cA, portal_ordering):
    body = order_body()
    first = post(client, cA, body)
    assert first.status_code == 201, first.text
    writes = orders.order_writes()

    portal_ordering(T1, False)
    replay = post(client, cA, body)
    assert replay.status_code == 200
    assert replay.json()["data"]["order_id"] == first.json()["data"]["order_id"]
    assert len(orders.ingest_calls) == 1
    assert orders.order_writes() == writes


@pytest.mark.parametrize("state", ["disabled", "shadow", "active_gated", "active_auto"])
def test_portal_ordering_works_whatever_the_pipeline_flag(client, orders, cA, state):
    """The shared rollout flag doesn't gate portal requests (B2)."""
    orders.flags.state = state
    resp = post(client, cA, order_body())
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["status_code"] == "awaiting_confirmation"
    assert orders.order_writes() == 1
    assert call(client, "GET", "/api/portal/me", cA).json()["data"]["ordering_available"] is True


# ---------------------------------------------------------------------------
# OID-3: shadow / active_gated / active_auto write a held web_portal order
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["shadow", "active_gated", "active_auto"])
def test_shadow_and_active_write(client, orders, cA, state):
    orders.flags.state = state
    body = order_body(po_number="PO-77", notes="Back gate\nRing bell")
    resp = post(client, cA, body)
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    oid = portal_order_id(T1, cA.user_id, body["client_event_id"])
    assert data["order_id"] == oid
    assert resp.headers["location"] == f"/api/portal/orders/{oid}"
    assert data["status_code"] == "awaiting_confirmation"
    assert data["status_label"] == "Awaiting confirmation"
    assert data["cancellable"] is True
    assert data["tank"] == {"customer_tank_id": TANK, "label": "QA-EXT-7"}

    stored = orders.order(oid)
    assert stored["status"] == "on_hold"
    assert stored["hold_reason"] == PORTAL_REVIEW_HOLD_REASON
    assert stored["intake_channel"] == "web_portal"
    assert stored["intake_channel_id"] == "web-portal"
    assert stored["product_code"] == "HEATING_OIL"
    assert (stored["ship_to_lat"], stored["ship_to_lon"]) == (41.88, -87.63)
    assert stored["customer_id"] == CUSTOMER_A
    assert stored["customer_name"] == "Customer A"
    assert stored["call_type"] == "will_call"
    assert stored["gallons_requested"] == 100
    assert stored["po_number"] == "PO-77"
    assert stored["special_instructions"] == "Back gate\nRing bell"
    assert stored["customer_phone"] is None and stored["customer_email"] is None
    # D4 fallback label: no earlier order for this tank.
    assert stored["ship_to_address"] == "Customer A — tank QA-EXT-7, ZIP 60601"
    (event,) = orders.store.events(oid)
    assert event["event_type"] == "order_placed"
    assert event["event_payload"] == {
        "intake_channel": "web_portal",
        "intake_channel_id": "web-portal",
        "actor_user_id": cA.user_id,
    }
    assert (T1, portal_event_id(cA.user_id, body["client_event_id"])) in orders.idempotency.keys


def test_ship_to_from_latest_tank_order_and_fill_to_full(client, orders, cA):
    orders.add_order(T1, CUSTOMER_A, "QA-ORD-OLD", customer_tank_id=TANK,
                     ship_to_address="77 Elm St, Springfield", status="delivered",
                     created_at=orders.now - timedelta(days=3))
    resp = post(client, cA, order_body(quantity={"mode": "fill_to_full"}))
    assert resp.status_code == 201, resp.text
    stored = orders.order(resp.json()["data"]["order_id"])
    assert stored["ship_to_address"] == "77 Elm St, Springfield"
    assert stored["fill_to_full"] is True
    assert stored["gallons_requested"] is None


# ---------------------------------------------------------------------------
# ORD-1 / ORD-1b: replays
# ---------------------------------------------------------------------------


def test_duplicate_client_event_id(client, orders, cA, sessions, portal_fakes):
    body = order_body()
    first = post(client, cA, body)
    second = post(client, cA, body)
    assert first.status_code == 201 and second.status_code == 200
    oid = first.json()["data"]["order_id"]
    assert second.json()["data"]["order_id"] == oid
    assert [o["order_id"] for o in orders.orders()] == [oid]

    other = sessions.customer(T1, CUSTOMER_A)
    portal_fakes.grants.grant(other)
    third = post(client, other, body)
    assert third.status_code == 201
    assert third.json()["data"]["order_id"] != oid
    assert len(orders.orders()) == 2


def test_replay_after_marker_loss_does_not_rewrite(client, orders, cA):
    body = order_body()
    oid = post(client, cA, body).json()["data"]["order_id"]
    # A dispatcher confirms it, then the Redis marker is lost.
    orders.store.poke("fuel_orders_current", oid, status="placed", hold_reason=None)
    orders.idempotency.clear()

    resp = post(client, cA, body)
    assert resp.status_code == 200
    assert resp.json()["data"]["order_id"] == oid
    assert resp.json()["data"]["status_code"] == "confirmed"
    assert len(orders.ingest_calls) == 1
    assert orders.order(oid)["status"] == "placed"


async def test_pipeline_existing_id_is_duplicate_without_hooks_or_writes(portal_orders):
    """ORD-1b pipeline unit case: the existing-id guard (review H3)."""
    from portal.api._authz import PortalScope
    from auth.test_auth import issue_test_context

    h = portal_orders
    scope = PortalScope(T1, CUSTOMER_A, "st-user-1",
                        issue_test_context(T1, roles=["customer"], customer_id=CUSTOMER_A))
    cid = str(uuid.uuid4())
    oid = portal_order_id(T1, scope.user_id, cid)
    h.add_order(T1, CUSTOMER_A, oid, status="placed", channel="web_portal")
    seen = []

    class _Hook:
        async def before_accept(self, draft):
            seen.append(draft["order_id"])
            return draft

        async def after_accept(self, order):
            seen.append(order["order_id"])

    h.pipeline.register_hook(_Hook())
    writes = h.order_writes()
    payload = {
        "customer_id": CUSTOMER_A, "customer_name": "Customer A", "ship_to_address": "x",
        "ship_to_lat": 1.0, "ship_to_lon": 2.0, "customer_tank_id": "QA-TANK-A1",
        "product_code": "PROPANE", "gallons_requested": 10.0, "call_type": "will_call",
    }
    result = await h.pipeline.ingest_portal(
        scope=scope, payload=payload, request_id="r1", client_event_id=cid,
    )
    assert result.status == "duplicate"
    assert result.order_id == oid
    assert seen == []
    assert h.order_writes() == writes
    assert h.order(oid)["status"] == "placed"
    assert (T1, portal_event_id(scope.user_id, cid)) in h.idempotency.keys


# ---------------------------------------------------------------------------
# ORD-2: tank scope and PD9 / F6 validation
# ---------------------------------------------------------------------------


def _normalized(resp, ident):
    body = dict(resp.json())
    body.pop("request_id", None)
    return json.dumps(body, sort_keys=True).replace(ident, "<id>")


def test_tank_out_of_scope_404(client, orders, cA):
    orders.add_tank(T1, CUSTOMER_A, "QA-TANK-A9", status="inactive")
    orders.add_tank(T1, CUSTOMER_B, "QA-TANK-B1")
    orders.add_tank(T2, CUSTOMER_C, "QA-TANK-C1")
    unknown = post(client, cA, order_body("QA-TANK-NOPE"))
    assert unknown.status_code == 404
    assert _err(unknown) == "RESOURCE_NOT_FOUND"
    for tank_id in ("QA-TANK-A9", "QA-TANK-B1", "QA-TANK-C1"):
        resp = post(client, cA, order_body(tank_id))
        assert resp.status_code == 404, tank_id
        assert _normalized(resp, tank_id) == _normalized(unknown, "QA-TANK-NOPE")
    assert orders.ingest_calls == []
    assert orders.order_writes() == 0


FIXED_NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
S = timedelta(seconds=1)


def _window(start, length=8 * H):
    return {"start": FIXED_NOW + start, "end": FIXED_NOW + start + length}


@pytest.mark.parametrize(
    "overrides,ok",
    [
        pytest.param({"window": _window(-24 * H + S, 25 * H)}, True, id="start-24h+1s"),
        pytest.param({"window": _window(-9 * H, 8 * H)}, False, id="window-already-over"),
        pytest.param({"window": _window(-24 * H - S)}, False, id="start-24h-1s"),
        pytest.param({"window": _window(timedelta(days=60) - S)}, True, id="start+60d-1s"),
        pytest.param({"window": _window(timedelta(days=60) + S)}, False, id="start+60d+1s"),
        pytest.param({"window": _window(H, 25 * H)}, True, id="window-25h"),
        pytest.param({"window": _window(H, 25 * H + S)}, False, id="window-25h+1s"),
        pytest.param({"window": _window(H, timedelta(0))}, False, id="end-equals-start"),
        pytest.param({"window_start": "2026-10-09T08:00:00"}, False, id="start-without-offset"),
        pytest.param({"window_end": "2026-10-09T16:00:00"}, False, id="end-without-offset"),
        pytest.param({"client_event_id": "not-a-uuid"}, False, id="client-event-id-not-uuid"),
        pytest.param({"drop": "client_event_id"}, False, id="client-event-id-missing"),
        pytest.param({"drop": "window_start"}, False, id="window-missing"),
        pytest.param({"tenant_id": T2}, False, id="body-tenant-id"),
        pytest.param({"customer_id": CUSTOMER_B}, False, id="body-customer-id"),
        pytest.param({"product_code": "DIESEL_2"}, False, id="unknown-field"),
        pytest.param({"quantity": {"mode": "liters", "gallons": 5}}, False, id="quantity-mode"),
        pytest.param({"quantity": {"mode": "gallons", "gallons": 0}}, False, id="gallons-zero"),
        pytest.param({"quantity": {"mode": "gallons"}}, False, id="gallons-missing"),
        pytest.param({"quantity": {"mode": "fill_to_full", "gallons": 5}}, False, id="fill-with-gallons"),
        pytest.param({"customer_tank_id": "bad id!"}, False, id="tank-id-pattern"),
        pytest.param({"po_number": "P" * 64}, True, id="po-64"),
        pytest.param({"po_number": "P" * 65}, False, id="po-65"),
        pytest.param({"po_number": "PO\t1"}, False, id="po-control-char"),
        pytest.param({"notes": "n" * 500}, True, id="notes-500"),
        pytest.param({"notes": "n" * 501}, False, id="notes-501"),
        pytest.param({"notes": "line1\nline2"}, True, id="notes-newline"),
        pytest.param({"notes": "bell\x07"}, False, id="notes-control-char"),
    ],
)
def test_pd9_validation_422(client, orders, cA, monkeypatch, overrides, ok):
    import portal.models as models

    monkeypatch.setattr(models, "_now", lambda: FIXED_NOW)
    overrides = dict(overrides)
    window = overrides.pop("window", None) or _window(24 * H)
    drop = overrides.pop("drop", None)
    body = order_body(start=window["start"], end=window["end"], **overrides)
    if drop:
        body.pop(drop)
    resp = post(client, cA, body)
    if ok:
        assert resp.status_code == 201, resp.text
    else:
        assert resp.status_code == 422, resp.text
        assert _err(resp) == "VALIDATION_ERROR"
        assert orders.ingest_calls == []
        assert orders.order_writes() == 0


def test_gallons_over_capacity_422(client, orders, cA):
    resp = post(client, cA, order_body(quantity={"mode": "gallons", "gallons": 500.5}))
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"
    assert resp.json()["details"]["fields"] == ["quantity.gallons"]
    assert orders.ingest_calls == []
    # Exactly the capacity is accepted when the reading isn't fresh enough to
    # say how much room is left.
    orders.add_tank(T1, CUSTOMER_A, "QA-TANK-A9", last_reading_at=None)
    assert post(client, cA, order_body("QA-TANK-A9", quantity={"mode": "gallons", "gallons": 500})).status_code == 201


def test_gallons_over_room_at_a_fresh_reading_422(client, orders, cA):
    # QA-TANK-A1: 500 gal, 250 in it, read just now: room for 250.
    resp = post(client, cA, order_body(quantity={"mode": "gallons", "gallons": 251}))
    assert resp.status_code == 422
    assert resp.json()["details"] == {"fields": ["quantity.gallons"], "max_gallons": 250}
    assert "room for about 250 gallons" in resp.json()["message"]
    assert orders.ingest_calls == []
    assert post(client, cA, order_body(quantity={"mode": "gallons", "gallons": 250})).status_code == 201


def test_gallons_below_minimum_422(client, orders, cA):
    resp = post(client, cA, order_body(quantity={"mode": "gallons", "gallons": 24}))
    assert resp.status_code == 422
    assert resp.json()["details"]["min_gallons"] == 25
    assert orders.ingest_calls == []
    assert post(client, cA, order_body(quantity={"mode": "gallons", "gallons": 25})).status_code == 201
    # A tank smaller than the minimum can still ask for its whole capacity.
    orders.add_tank(T1, CUSTOMER_A, "QA-TANK-A8", capacity=20.0, level=0.0)
    assert post(client, cA, order_body("QA-TANK-A8", quantity={"mode": "gallons", "gallons": 20})).status_code == 201


# ---------------------------------------------------------------------------
# ORD-5: cancel
# ---------------------------------------------------------------------------


def _cancel(client, session, order_id):
    return call(client, "POST", f"/api/portal/orders/{order_id}/cancel", session, json={})


def test_cancel_rules(client, orders, cA):
    oid = post(client, cA, order_body()).json()["data"]["order_id"]
    resp = _cancel(client, cA, oid)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status_code"] == "cancelled" and data["cancellable"] is False
    stored = orders.order(oid)
    assert stored["status"] == "cancelled" and stored["hold_reason"] is None
    cancelled = [e for e in orders.store.events(oid) if e["event_type"] == "order_cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["event_payload"] == {
        "old_status": "on_hold",
        "reason": "cancelled_by_customer",
        "notes": None,
        "actor_user_id": cA.user_id,
    }

    # Already cancelled.
    again = _cancel(client, cA, oid)
    assert again.status_code == 409 and _err(again) == "ORDER_NOT_CANCELLABLE"

    # Confirmed by a dispatcher, held for another reason, or not a portal order.
    released = post(client, cA, order_body()).json()["data"]["order_id"]
    orders.store.poke("fuel_orders_current", released, status="placed", hold_reason=None)
    other_hold = post(client, cA, order_body()).json()["data"]["order_id"]
    orders.store.poke("fuel_orders_current", other_hold, hold_reason="credit_limit_exceeded")
    orders.add_order(T1, CUSTOMER_A, "QA-ORD-DISP", customer_tank_id=TANK, status="on_hold",
                     hold_reason=PORTAL_REVIEW_HOLD_REASON, channel="dispatcher")
    for order_id in (released, other_hold, "QA-ORD-DISP"):
        resp = _cancel(client, cA, order_id)
        assert resp.status_code == 409, order_id
        assert _err(resp) == "ORDER_NOT_CANCELLABLE"
    assert orders.order(released)["status"] == "placed"
    assert orders.order(other_hold)["status"] == "on_hold"


def test_cancel_body_must_be_empty(client, orders, cA):
    oid = post(client, cA, order_body()).json()["data"]["order_id"]
    resp = call(client, "POST", f"/api/portal/orders/{oid}/cancel", cA, json={"reason": "x"})
    assert resp.status_code == 422
    assert orders.order(oid)["status"] == "on_hold"


# ---------------------------------------------------------------------------
# ORD-6: hook rejections are one generic 422
# ---------------------------------------------------------------------------


def _pricing_error():
    from commerce.services.pricing_engine import PricingError

    return PricingError("PRICING_NO_RULE_SECRET", "no rule for secret-account")


def _dyed_diesel():
    from compliance.hooks.dyed_diesel_intake_hook import DyedDieselOrderRejected

    return DyedDieselOrderRejected("DYED_DIESEL_SECRET", "no exemption certificate on file")


def _app_exception():
    return AppException("HOOK_SECRET_CODE", "secret hook text", status_code=409)


@pytest.mark.parametrize(
    "make_exc,class_name",
    [
        (_app_exception, "AppException"),
        (_pricing_error, "PricingError"),
        (_dyed_diesel, "DyedDieselOrderRejected"),
    ],
)
def test_hook_rejection_generic_422(client, orders, cA, caplog, make_exc, class_name):
    class _Rejecting:
        async def before_accept(self, draft):
            raise make_exc()

        async def after_accept(self, order):  # pragma: no cover
            raise AssertionError("must not run")

    orders.pipeline.register_hook(_Rejecting())
    caplog.set_level(logging.WARNING, logger="portal.services.portal_order_service")
    resp = post(client, cA, order_body())
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "ORDER_REQUEST_REJECTED"
    assert body["message"] == REJECTED_MESSAGE
    text = resp.text
    for secret in (class_name, "SECRET", "secret", "exemption"):
        assert secret not in text
    assert orders.order_writes() == 0
    warns = [r for r in caplog.records if r.name == "portal.services.portal_order_service"
             and r.levelno == logging.WARNING]
    assert any(class_name in r.getMessage() for r in warns)
