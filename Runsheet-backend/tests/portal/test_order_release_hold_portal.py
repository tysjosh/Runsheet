"""Staff confirm of a portal request (design §4.2, §4.4; ORD-4 a, a2, b, c, d).

[real-auth] The portal request is submitted as customer A; a dispatcher
confirms it with the existing ``POST /api/orders/{id}/release-hold``, wired to
the same in-memory store.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from fuel.order_models import PORTAL_REVIEW_HOLD_REASON
from tests.portal.conftest import CUSTOMER_A, T1, call
from tests.portal.test_portal_orders import TANK, order_body


@pytest.fixture
def staff_orders(portal_orders):
    """Point the staff order routes at the harness repository; restore after."""
    import fuel.api.order_endpoints as oe

    names = ("_order_intake_pipeline", "_order_repository", "_driver_repository",
             "_driver_counter_service", "_order_service", "_ref_resolver")
    saved = {n: getattr(oe, n) for n in names}
    saved_hooks = list(oe._release_hold_hooks)
    oe.configure_order_endpoints(
        order_intake_pipeline=portal_orders.pipeline,
        order_repository=portal_orders.order_repo,
    )
    try:
        yield portal_orders
    finally:
        for n, v in saved.items():
            setattr(oe, n, v)
        oe._release_hold_hooks[:] = saved_hooks


@pytest.fixture
def h(portal_on, staff_orders, cA):
    staff_orders.add_tank(T1, CUSTOMER_A, TANK)
    return staff_orders


class _Passthrough:
    async def before_accept(self, draft):
        return draft

    async def after_accept(self, order):
        return None


class _CreditHoldStub:
    """Stands in for ``CreditCheckHook`` once orders carry an account (B7)."""

    async def before_accept(self, draft):
        draft["status"] = "on_hold"
        draft["hold_reason"] = "credit_limit_exceeded"
        return draft

    async def after_accept(self, order):
        return None


def _submit(client, session):
    resp = call(client, "POST", "/api/portal/orders", session, json=order_body())
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["order_id"]


def _release(client, sessions, order_id):
    dispatcher = sessions.staff("dispatcher")
    return call(client, "POST", f"/api/orders/{order_id}/release-hold", dispatcher, json={})


def _register(h, hooks):
    for hook in hooks:
        h.pipeline.register_hook(hook)


@pytest.mark.parametrize("position", ["first", "last"])
def test_portal_order_held_then_released_to_placed(client, sessions, h, cA, position):
    """(a) + (c): lands on the portal hold whatever the hook order; release-hold
    moves it to placed and nothing re-holds it."""
    hooks = [_Passthrough(), _Passthrough()]
    hooks.insert(0 if position == "first" else len(hooks), _Passthrough())
    _register(h, hooks)
    oid = _submit(client, cA)
    assert h.order(oid)["status"] == "on_hold"
    assert h.order(oid)["hold_reason"] == PORTAL_REVIEW_HOLD_REASON

    resp = _release(client, sessions, oid)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "placed"
    stored = h.order(oid)
    assert stored["status"] == "placed" and stored["hold_reason"] is None
    events = [e["event_type"] for e in h.store.events(oid)]
    assert events == ["order_placed", "order_released_from_hold"]

    portal = call(client, "GET", f"/api/portal/orders/{oid}", cA).json()["data"]
    assert portal["status_code"] == "confirmed" and portal["cancellable"] is False


@pytest.mark.parametrize("position", ["first", "last"])
def test_hook_set_hold_is_never_replaced(client, sessions, h, cA, position):
    """(b) + (c): a hold a hook set stays; the portal shows "On hold"."""
    hooks = [_Passthrough(), _Passthrough()]
    hooks.insert(0 if position == "first" else len(hooks), _CreditHoldStub())
    _register(h, hooks)
    oid = _submit(client, cA)
    stored = h.order(oid)
    assert stored["status"] == "on_hold"
    assert stored["hold_reason"] == "credit_limit_exceeded"

    portal = call(client, "GET", f"/api/portal/orders/{oid}", cA).json()["data"]
    assert portal["status_code"] == "on_hold"
    assert portal["status_label"] == "On hold"
    assert portal["cancellable"] is False
    assert "credit" not in str(portal)


def test_real_pricing_and_credit_hooks_are_inert(client, h, cA, monkeypatch):
    """(a2) pins DV10: with the real hooks on, a portal order (no account_id)
    passes through them unchanged and lands on the portal hold. B7 changes this
    test deliberately."""
    from commerce.hooks.intake_hooks import CreditCheckHook, PricingHook
    from config.settings import clear_settings_cache

    monkeypatch.setenv("COMMERCE_PRICING_ENGINE_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_CREDIT_HOLDS_ENABLED", "true")
    clear_settings_cache()
    engine = MagicMock()
    engine.resolve = AsyncMock()
    credit = MagicMock()
    credit.check = AsyncMock()
    _register(h, [PricingHook(engine), CreditCheckHook(credit)])

    oid = _submit(client, cA)
    stored = h.order(oid)
    assert stored["status"] == "on_hold"
    assert stored["hold_reason"] == PORTAL_REVIEW_HOLD_REASON
    assert stored["unit_price_cents"] is None and stored["total_cents"] is None
    credit.check.assert_not_awaited()
    assert not engine.method_calls


def test_release_hold_cas_loses_to_customer_cancel(client, sessions, h, cA):
    """(d) F3: the order is cancelled between release-hold's read and its
    write; the response is 409 and the order stays cancelled."""
    oid = _submit(client, cA)
    raced = []

    def _cancel_first(op, index, doc_id):
        if op == "atomic_update" and index == "fuel_orders_current" and doc_id == oid and not raced:
            raced.append(True)
            h.store.poke(index, oid, status="cancelled", hold_reason=None)

    h.store.hooks.append(_cancel_first)
    resp = _release(client, sessions, oid)
    assert raced
    assert resp.status_code == 409, resp.text
    body = resp.json()
    code = body.get("error_code") or body.get("detail", {}).get("error_code")
    assert code == "INVALID_STATUS_TRANSITION"
    assert h.order(oid)["status"] == "cancelled"
    assert [e["event_type"] for e in h.store.events(oid)] == ["order_placed"]


def test_customer_cancel_loses_to_release(client, h, cA):
    """F3 the other way: confirmed between the portal read and its write."""
    oid = _submit(client, cA)

    def _release_first(op, index, doc_id):
        if op == "atomic_update" and doc_id == oid and h.order(oid)["status"] == "on_hold":
            h.store.poke(index, oid, status="placed", hold_reason=None)

    h.store.hooks.append(_release_first)
    resp = call(client, "POST", f"/api/portal/orders/{oid}/cancel", cA, json={})
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "ORDER_NOT_CANCELLABLE"
    assert h.order(oid)["status"] == "placed"


def test_staff_list_filters_on_hold_reason(client, sessions, h, cA):
    oid = _submit(client, cA)
    h.add_order(T1, CUSTOMER_A, "QA-ORD-OTHER-HOLD", status="on_hold", hold_reason="credit check")
    dispatcher = sessions.staff("dispatcher")
    resp = call(client, "GET", f"/api/orders?hold_reason={PORTAL_REVIEW_HOLD_REASON}", dispatcher)
    assert resp.status_code == 200, resp.text
    assert [o["order_id"] for o in resp.json()["items"]] == [oid]
