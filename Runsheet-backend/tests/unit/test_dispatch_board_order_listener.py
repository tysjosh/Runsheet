"""``BoardOrderListener`` (plan task 19; design K10.4; R10.5, R15.2, R15.5).

A status change on an order that sits on a board lane marks that lane
``checks_stale`` (no version bump) on today's and later drafts and broadcasts
``board_lane_stale``; the next snapshot re-validates the lane. The handler never
raises into order processing.
"""
from __future__ import annotations

import copy
import logging
from datetime import timedelta
from typing import Any, Callable, Dict, List

import pytest

from fuel.services.dispatch_board_models import draft_doc_id
from fuel.services.dispatch_board_order_listener import BOARD_ORDER_EVENTS, BoardOrderListener
from tests.unit._dispatch_board_fakes import DRAFTS, ORDERS, OTHER, T, TODAY, TOMORROW, TZ, Harness


class FakeOrderService:
    """Mimics ``OrderService.subscribe`` / ``_notify_event_subscribers``."""

    def __init__(self) -> None:
        self.handlers: Dict[str, List[Callable]] = {}

    def subscribe(self, event_name: str, handler: Callable) -> None:
        self.handlers.setdefault(event_name, []).append(handler)

    async def transition(self, order: Dict[str, Any], status: str) -> None:
        for handler in self.handlers.get(f"order.{status}", []):
            await handler(order)


class Broadcasts:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    async def __call__(self, tenant_id, service_date, event_type, data) -> None:
        self.events.append((tenant_id, service_date, event_type, data))


def _listener(h: Harness, broadcast: Any = None) -> BoardOrderListener:
    return BoardOrderListener(
        es_service=h.store,
        broadcast=broadcast,
        timezone_for=lambda tenant_id: TZ,
        clock=lambda: h.now,
    )


async def _board(h: Harness) -> None:
    for oid in ("o1", "o2", "o3"):
        h.seed_order(oid)
    h.seed_order("o4", day=TOMORROW)
    await h.lane_with("T1", "o1", "o2", driver_id="d1")
    await h.lane_with("T2", "o3", driver_id="d2")
    await h.lane_with("T1", "o4", day=TOMORROW)


def test_bootstrap_subscribes_the_listener_and_wires_socket_and_suggestions():
    """``bootstrap/agents.py`` builds the listener in the board block and
    subscribes it on the order service; the board service broadcasts through
    the board socket manager and reads suggestions from the K9 service."""
    from fastapi import FastAPI

    from bootstrap.agents import _wire_dispatch_board
    from bootstrap.container import ServiceContainer
    from fuel.api import dispatch_board_endpoints as board_api
    from fuel.services.dispatch_board_suggestions import BoardSuggestionService
    from fuel.services.dispatch_board_ws_manager import DispatchBoardWSManager
    from tests.unit._dispatch_board_fakes import FakeFlags

    h = Harness()
    orders = FakeOrderService()
    container = ServiceContainer()
    container.ops_feature_flags = FakeFlags({T: "active_gated"})
    container.order_repository = h.orders
    container.driver_repository = h.drivers
    container.order_service = orders
    try:
        _wire_dispatch_board(FastAPI(), container, h.store, None)
        assert sorted(orders.handlers) == sorted(f"order.{s}" for s in BOARD_ORDER_EVENTS)
        board = container.dispatch_board_service
        assert isinstance(container.dispatch_board_ws_manager, DispatchBoardWSManager)
        assert board._ws is container.dispatch_board_ws_manager
        assert isinstance(container.dispatch_board_suggestion_service, BoardSuggestionService)
        assert board._suggestions is container.dispatch_board_suggestion_service
        assert board_api._suggestion_service is container.dispatch_board_suggestion_service
    finally:
        board_api.configure_dispatch_board_endpoints(board_service=None, feature_flag_service=None)


def test_subscribe_registers_every_k10_4_status():
    h = Harness()
    orders = FakeOrderService()
    _listener(h).subscribe(orders)
    assert sorted(orders.handlers) == sorted(f"order.{s}" for s in BOARD_ORDER_EVENTS)
    assert set(BOARD_ORDER_EVENTS) == {"cancelled", "on_hold", "scheduled", "dispatched", "in_transit", "delivered", "failed"}


async def test_status_change_marks_the_owning_lane_stale_without_a_version_bump():
    h = Harness()
    await _board(h)
    before = h.draft()
    broadcasts = Broadcasts()
    orders = FakeOrderService()
    _listener(h, broadcasts).subscribe(orders)
    await orders.transition({**h.store.doc(ORDERS, "o2"), "status": "cancelled"}, "cancelled")
    after = h.draft()
    assert after.lanes["T1"].checks_stale is True and after.lanes["T2"].checks_stale is False
    assert after.draft_version == before.draft_version
    assert {t: l.version for t, l in after.lanes.items()} == {t: l.version for t, l in before.lanes.items()}
    assert after.lanes["T1"].loads == before.lanes["T1"].loads
    assert broadcasts.events == [
        (T, TODAY, "board_lane_stale", {"service_date": TODAY.isoformat(), "truck_ids": ["T1"], "reason": "order_cancelled"})
    ]


async def test_every_draft_from_today_on_is_marked_and_past_drafts_are_not_read():
    h = Harness()
    await _board(h)
    # The same order on tomorrow's draft (e.g. a stale copy) and on a past draft.
    tomorrow = copy.deepcopy(h.store.doc(DRAFTS, draft_doc_id(T, TOMORROW)))
    tomorrow["lanes"]["T1"]["loads"][0]["stops"].append(copy.deepcopy(h.draft().lanes["T1"].loads[0].stops[0].model_dump(mode="json")))
    tomorrow["order_index"]["o1"] = "T1"
    tomorrow["order_ids"].append("o1")
    h.store.seed(DRAFTS, draft_doc_id(T, TOMORROW), tomorrow)
    yesterday = copy.deepcopy(h.store.doc(DRAFTS, draft_doc_id(T, TODAY)))
    past = TODAY - timedelta(days=1)
    yesterday["service_date"] = past.isoformat()
    h.store.seed(DRAFTS, draft_doc_id(T, past), yesterday)
    broadcasts = Broadcasts()
    marked = await _listener(h, broadcasts).on_order_event({**h.store.doc(ORDERS, "o1"), "status": "on_hold"}, "on_hold")
    assert sorted(marked) == [f"{TODAY.isoformat()}:T1", f"{TOMORROW.isoformat()}:T1"]
    assert h.store.doc(DRAFTS, draft_doc_id(T, past))["lanes"]["T1"]["checks_stale"] is False
    assert {e[1] for e in broadcasts.events} == {TODAY, TOMORROW}
    assert all(e[3]["reason"] == "order_on_hold" for e in broadcasts.events)


async def test_an_order_on_no_draft_or_another_tenants_draft_changes_nothing():
    h = Harness()
    await _board(h)
    foreign = copy.deepcopy(h.store.doc(DRAFTS, draft_doc_id(T, TODAY)))
    foreign["tenant_id"] = OTHER
    h.store.seed(DRAFTS, draft_doc_id(OTHER, TODAY), foreign)
    broadcasts = Broadcasts()
    listener = _listener(h, broadcasts)
    assert await listener.on_order_event({"tenant_id": T, "order_id": "o-unknown"}, "cancelled") == []
    marked = await listener.on_order_event({"tenant_id": T, "order_id": "o3"}, "dispatched")
    assert marked == [f"{TODAY.isoformat()}:T2"]
    assert h.store.doc(DRAFTS, draft_doc_id(OTHER, TODAY))["lanes"]["T2"]["checks_stale"] is False
    assert [e[0] for e in broadcasts.events] == [T]
    assert await listener.on_order_event({"order_id": "o1"}, "cancelled") == []


async def test_lookup_uses_term_order_ids_with_source_filtering():
    h = Harness()
    await _board(h)
    seen: List[tuple] = []
    inner = h.store.search_documents

    async def spy(index, query, size=10):
        seen.append((index, copy.deepcopy(query), size))
        return await inner(index, query, size)

    h.store.search_documents = spy  # type: ignore[method-assign]
    await _listener(h).on_order_event({"tenant_id": T, "order_id": "o1"}, "delivered")
    ((index, body, size),) = seen
    assert index == DRAFTS and size == 15
    filters = body["query"]["bool"]["filter"]
    assert {"term": {"order_ids": "o1"}} in filters and {"term": {"tenant_id": T}} in filters
    assert {"range": {"service_date": {"gte": TODAY.isoformat()}}} in filters
    assert body["_source"][:2] == ["service_date", "order_index"] and body["size"] == 15


async def test_already_stale_lane_is_not_rewritten_but_is_still_announced():
    h = Harness()
    await _board(h)
    broadcasts = Broadcasts()
    listener = _listener(h, broadcasts)
    await listener.on_order_event({"tenant_id": T, "order_id": "o1"}, "scheduled")
    writes = len(h.store.writes(DRAFTS))
    await listener.on_order_event({"tenant_id": T, "order_id": "o2"}, "dispatched")
    assert len(h.store.writes(DRAFTS)) == writes
    assert len(broadcasts.events) == 2


@pytest.mark.parametrize("failure", ["search", "write", "broadcast"])
async def test_failures_never_raise_into_order_processing(failure, caplog):
    h = Harness()
    await _board(h)

    async def broken_broadcast(*args):
        raise RuntimeError("socket down")

    class BrokenStore:
        def __init__(self, inner):
            self.inner = inner

        async def search_documents(self, *args, **kwargs):
            if failure == "search":
                raise ConnectionError("store down")
            return await self.inner.search_documents(*args, **kwargs)

        async def atomic_update(self, *args, **kwargs):
            if failure == "write":
                raise ConnectionError("store down")
            return await self.inner.atomic_update(*args, **kwargs)

    listener = BoardOrderListener(
        es_service=BrokenStore(h.store),
        broadcast=broken_broadcast if failure == "broadcast" else None,
        timezone_for=lambda tenant_id: TZ,
        clock=lambda: h.now,
    )
    orders = FakeOrderService()
    listener.subscribe(orders)
    with caplog.at_level(logging.WARNING, logger="fuel.services.dispatch_board_order_listener"):
        await orders.transition({"tenant_id": T, "order_id": "o1", "customer_name": "Acme Fuels Customer"}, "failed")
    assert "dispatch board order listener failed" in caplog.text
    assert "Acme" not in caplog.text


async def test_next_snapshot_revalidates_the_stale_lane():
    h = Harness()
    await _board(h)
    h.store.docs[ORDERS]["o2"]["status"] = "on_hold"
    await _listener(h).on_order_event({"tenant_id": T, "order_id": "o2"}, "on_hold")
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    lane = next(l for l in snap["lanes"] if l["truck_id"] == "T1")
    assert lane["checks_stale"] is False
    assert any(c["reason_code"] == "on_hold" and c["outcome"] == "block" for c in lane["checks"])
    assert h.draft().lanes["T1"].checks_stale is False  # written back, still no version bump
