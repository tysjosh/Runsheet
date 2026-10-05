"""Order-layer primitives for the loading-plan executor and MVP dispatch.

Covers design K5 (link CAS), K5a (guarded upsert and guarded transition) and
the FREEZE rule 2 release-by-claim-id (plan decision P3), over an in-memory
document store with real ``atomic_update`` semantics.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from errors.exceptions import AppException
from fuel.order_repository import (
    AssignmentClaim,
    FuelOrderRepository,
    OrderChangedConcurrentlyError,
    OrderWriteDiscardedError,
    _ts,
)
from fuel.services.order_service import OrderService
from tests.unit._loading_plan_fakes import (
    ORDERS,
    InMemoryDocumentStore,
    fuel_order_doc,
)

T = "tenant-1"
TS0 = "2026-07-29T12:00:00Z"
TS1 = "2026-07-29T12:05:00Z"
FUTURE = "2099-01-01T00:00:00Z"


def _repo(*docs):
    store = InMemoryDocumentStore()
    for doc in docs:
        store.seed(ORDERS, doc["order_id"], doc)
    return store, FuelOrderRepository(store)


def _write_count(store):
    return len(store.writes(ORDERS))


# ---------------------------------------------------------------------------
# get_current
# ---------------------------------------------------------------------------


async def test_get_current_reads_stored_doc_and_hides_other_tenants():
    store, repo = _repo(fuel_order_doc("o1"), fuel_order_doc("o2", tenant_id="other"))
    current = await repo.get_current(T, "o1")
    assert current["order_id"] == "o1"
    assert isinstance(current["last_event_timestamp"], str)
    assert await repo.get_current(T, "o2") is None
    assert await repo.get_current(T, "missing") is None
    assert store.calls("search_documents") == []


# ---------------------------------------------------------------------------
# Guarded upsert (K5a)
# ---------------------------------------------------------------------------


def _next(doc, **changes):
    out = dict(doc)
    out.update(changes)
    return out


async def test_unguarded_upsert_unchanged_returns_bool():
    doc = fuel_order_doc("o1")
    store, repo = _repo(doc)
    assert await repo.upsert_with_last_event_timestamp(T, _next(doc, last_event_timestamp=TS1)) is True
    # Equal timestamp: discarded, still a bool.
    assert await repo.upsert_with_last_event_timestamp(T, _next(doc, last_event_timestamp=TS1)) is False


async def test_guarded_upsert_returns_stored_doc():
    doc = fuel_order_doc("o1", status="confirmed")
    store, repo = _repo(doc)
    stored = await repo.upsert_with_last_event_timestamp(
        T,
        _next(doc, status="scheduled", last_event_timestamp=TS1),
        expected_status="confirmed",
        expected_last_event_timestamp=doc["last_event_timestamp"],
    )
    assert isinstance(stored, dict)
    assert stored == store.doc(ORDERS, "o1")
    assert stored["status"] == "scheduled"
    assert _ts(stored["last_event_timestamp"]) == _ts(TS1)


async def test_guarded_upsert_status_mismatch_refuses_without_write():
    doc = fuel_order_doc("o1", status="cancelled")
    store, repo = _repo(doc)
    with pytest.raises(OrderChangedConcurrentlyError) as raised:
        await repo.upsert_with_last_event_timestamp(
            T, _next(doc, last_event_timestamp=TS1), expected_status="confirmed"
        )
    assert (raised.value.expected_status, raised.value.actual_status) == ("confirmed", "cancelled")
    assert _write_count(store) == 0
    assert store.doc(ORDERS, "o1") == doc


async def test_guarded_upsert_timestamp_mismatch_refuses_without_write():
    doc = fuel_order_doc("o1", last_event_timestamp=TS1)
    store, repo = _repo(doc)
    with pytest.raises(OrderChangedConcurrentlyError) as raised:
        await repo.upsert_with_last_event_timestamp(
            T,
            _next(doc, last_event_timestamp=FUTURE),
            expected_last_event_timestamp=TS0,
        )
    assert raised.value.actual_status == "confirmed"
    assert _write_count(store) == 0


async def test_guarded_upsert_compares_timestamps_by_value():
    doc = fuel_order_doc("o1", last_event_timestamp="2026-07-29T12:00:00Z")
    store, repo = _repo(doc)
    stored = await repo.upsert_with_last_event_timestamp(
        T,
        _next(doc, last_event_timestamp=TS1),
        expected_status="confirmed",
        expected_last_event_timestamp="2026-07-29T12:00:00.000000+00:00",
    )
    assert _ts(stored["last_event_timestamp"]) == _ts(TS1)


async def test_guarded_upsert_not_newer_is_discarded():
    doc = fuel_order_doc("o1", last_event_timestamp=TS1)
    store, repo = _repo(doc)
    with pytest.raises(OrderWriteDiscardedError):
        await repo.upsert_with_last_event_timestamp(
            T,
            _next(doc, status="scheduled", last_event_timestamp=TS0),
            expected_status="confirmed",
            expected_last_event_timestamp=TS1,
        )
    assert _write_count(store) == 0


async def test_guarded_upsert_missing_doc_is_changed_with_no_status():
    store, repo = _repo()
    with pytest.raises(OrderChangedConcurrentlyError) as raised:
        await repo.upsert_with_last_event_timestamp(
            T, fuel_order_doc("gone"), expected_status="confirmed"
        )
    assert raised.value.actual_status is None
    assert store.doc(ORDERS, "gone") is None


async def test_guarded_upsert_unparseable_stored_timestamp_raises_value_error():
    doc = fuel_order_doc("o1")
    store, repo = _repo(doc)
    store.poke(ORDERS, "o1", last_event_timestamp="garbage")
    with pytest.raises(ValueError):
        await repo.upsert_with_last_event_timestamp(
            T, _next(doc, last_event_timestamp=TS1), expected_last_event_timestamp=TS0
        )


# ---------------------------------------------------------------------------
# claim_assignment (K5 link CAS)
# ---------------------------------------------------------------------------


async def _claim(repo, order_id="o1", *, run="run-1", asset="truck-1", status="confirmed", claim="c-1"):
    return await repo.claim_assignment(
        T, order_id, run_id=run, asset_id=asset, expected_status=status, claim_id=claim
    )


async def test_claim_links_and_records_claim_id_only():
    doc = fuel_order_doc("o1", assigned_driver_id="D1")
    store, repo = _repo(doc)
    claim = await _claim(repo)
    assert isinstance(claim, AssignmentClaim)
    assert (claim.outcome, claim.reason) == ("linked", None)
    stored = store.doc(ORDERS, "o1")
    assert claim.order == stored
    assert (stored["assigned_run_id"], stored["assigned_asset_id"], stored["assigned_claim_id"]) == (
        "run-1",
        "truck-1",
        "c-1",
    )
    # The claim never touches the timestamp or the driver.
    assert stored["last_event_timestamp"] == doc["last_event_timestamp"]
    assert stored["assigned_driver_id"] == "D1"


async def test_claim_treats_empty_string_links_as_unlinked():
    store, repo = _repo(fuel_order_doc("o1", assigned_run_id="", assigned_asset_id=""))
    claim = await _claim(repo)
    assert claim.outcome == "linked"
    assert store.doc(ORDERS, "o1")["assigned_run_id"] == "run-1"


async def test_already_linked_keeps_claim_id_and_writes_nothing():
    doc = fuel_order_doc(
        "o1", status="scheduled", assigned_run_id="run-1", assigned_asset_id="truck-1", assigned_claim_id="attempt-A"
    )
    store, repo = _repo(doc)
    claim = await _claim(repo, status="scheduled", claim="dispatch-B")
    assert (claim.outcome, claim.reason) == ("already_linked", None)
    assert claim.order == doc
    assert store.doc(ORDERS, "o1")["assigned_claim_id"] == "attempt-A"
    assert _write_count(store) == 0


@pytest.mark.parametrize(
    ("seed", "reason"),
    [
        (None, "order_not_found"),
        ({"tenant_id": "other"}, "order_not_found"),
        ({"status": "scheduled"}, "order_changed_since_plan"),
        ({"assigned_run_id": "run-2"}, "order_committed_elsewhere"),
        ({"assigned_asset_id": "truck-2"}, "order_committed_elsewhere"),
        ({"assigned_run_id": "run-1", "assigned_asset_id": "truck-2"}, "order_committed_elsewhere"),
    ],
)
async def test_claim_refusals(seed, reason):
    store, repo = _repo(*( [fuel_order_doc("o1", **seed)] if seed is not None else []))
    claim = await _claim(repo)
    assert (claim.outcome, claim.reason) == ("refused", reason)
    if reason == "order_not_found":
        assert claim.order is None
    else:
        assert claim.order["order_id"] == "o1"
    assert _write_count(store) == 0
    if seed is not None:
        assert store.doc(ORDERS, "o1").get("assigned_claim_id") is None


@pytest.mark.parametrize(
    ("stored_ts", "expected_ts"),
    [(TS1, TS0), ("garbage", TS0), (None, TS0), (TS0, None)],
    ids=["newer", "unparseable", "stored_missing", "expected_missing"],
)
async def test_claim_refuses_when_stored_timestamp_differs_from_callers_read(stored_ts, expected_ts):
    # Review pass 1, finding 1: a same-status edit between the caller's read
    # and the claim (an ERP re-sync changing gallons_requested) is refused.
    store, repo = _repo(fuel_order_doc("o1", last_event_timestamp=TS0))
    store.poke(ORDERS, "o1", gallons_requested=999.0, last_event_timestamp=stored_ts)
    claim = await repo.claim_assignment(
        T, "o1", run_id="run-1", asset_id="truck-1", expected_status="confirmed",
        claim_id="c-1", expected_last_event_timestamp=expected_ts,
    )
    assert (claim.outcome, claim.reason) == ("refused", "order_changed_since_plan")
    assert _write_count(store) == 0
    stored = store.doc(ORDERS, "o1")
    assert (stored.get("assigned_run_id"), stored.get("assigned_claim_id")) == (None, None)


async def test_claim_timestamp_guard_compares_by_value_not_string():
    store, repo = _repo(fuel_order_doc("o1", last_event_timestamp="2026-07-29T12:00:00+00:00"))
    claim = await repo.claim_assignment(
        T, "o1", run_id="run-1", asset_id="truck-1", expected_status="confirmed",
        claim_id="c-1", expected_last_event_timestamp=TS0,  # same instant, "Z" form
    )
    assert claim.outcome == "linked"


async def test_claim_timestamp_guard_refuses_a_stale_already_linked_read():
    doc = fuel_order_doc(
        "o1", status="scheduled", assigned_run_id="run-1", assigned_asset_id="truck-1",
        assigned_claim_id="attempt-A", last_event_timestamp=TS1,
    )
    store, repo = _repo(doc)
    claim = await repo.claim_assignment(
        T, "o1", run_id="run-1", asset_id="truck-1", expected_status="scheduled",
        claim_id="attempt-B", expected_last_event_timestamp=TS0,
    )
    assert (claim.outcome, claim.reason) == ("refused", "order_changed_since_plan")
    assert _write_count(store) == 0


async def test_claim_rejects_blank_claim_id():
    _store, repo = _repo(fuel_order_doc("o1"))
    with pytest.raises(ValueError):
        await _claim(repo, claim="")


# ---------------------------------------------------------------------------
# release_assignment (FREEZE rule 2)
# ---------------------------------------------------------------------------


async def _release(repo, order_id="o1", *, run="run-1", asset="truck-1", claim="c-1"):
    return await repo.release_assignment(T, order_id, run_id=run, asset_id=asset, claim_id=claim)


async def test_release_clears_links_even_after_timestamp_and_driver_changed():
    # FREEZE test (b) at repository level.
    store, repo = _repo(fuel_order_doc("o1"))
    await _claim(repo)
    store.poke(ORDERS, "o1", assigned_driver_id="D2", last_event_timestamp=TS1)
    assert await _release(repo) is True
    stored = store.doc(ORDERS, "o1")
    assert (stored["assigned_run_id"], stored["assigned_asset_id"], stored["assigned_claim_id"]) == (None, None, None)
    assert stored["assigned_driver_id"] == "D2"
    assert stored["last_event_timestamp"] == TS1
    assert store.events() == []


@pytest.mark.parametrize(
    ("change", "kwargs"),
    [
        ({}, {"claim": "someone-else"}),
        ({}, {"run": "run-2"}),
        ({}, {"asset": "truck-2"}),
        ({"tenant_id": "other"}, {}),
        ({"status": "dispatched"}, {}),
        ({"status": "in_transit"}, {}),
        ({"status": "delivered"}, {}),
        ({"assigned_run_id": "run-2"}, {}),
    ],
)
async def test_release_is_noop_unless_this_claim_owns_the_links(change, kwargs, caplog):
    store, repo = _repo(fuel_order_doc("o1", assigned_driver_id="D1"))
    await _claim(repo)
    if change:
        store.poke(ORDERS, "o1", **change)
    before = store.doc(ORDERS, "o1")
    with caplog.at_level("INFO", logger="fuel.order_repository"):
        assert await _release(repo, **kwargs) is False
    assert store.doc(ORDERS, "o1") == before
    assert any("release_assignment: no-op" in r.getMessage() for r in caplog.records)


async def test_release_missing_order_is_noop():
    _store, repo = _repo()
    assert await _release(repo) is False


# ---------------------------------------------------------------------------
# OrderService guarded transition (K5a steps 1-5)
# ---------------------------------------------------------------------------


def _service(repo):
    ws = AsyncMock()
    service = OrderService(order_repo=repo, ws_manager=ws)  # default clock
    subscribers = {name: AsyncMock() for name in ("order.confirmed", "order.scheduled")}
    for name, handler in subscribers.items():
        service.subscribe(name, handler)
    return service, ws, subscribers


@pytest.mark.parametrize("source", ["get_current", "list_for_tenant"])
async def test_guarded_chain_placed_confirmed_scheduled(source):
    store, repo = _repo(fuel_order_doc("o1", status="placed"))
    service, ws, subs = _service(repo)
    if source == "get_current":
        order = await repo.get_current(T, "o1")
    else:
        (model,) = await repo.list_for_tenant(T)
        order = model.model_dump(mode="python")

    for status in ("confirmed", "scheduled"):
        await service.apply_status_transition(order, status, guard_stored_state=True)
        stored = store.doc(ORDERS, "o1")
        assert stored["status"] == status
        assert order["last_event_timestamp"] == stored["last_event_timestamp"]
        assert order["updated_at"] == stored["updated_at"]

    assert [e["event_type"] for e in store.events("o1")] == ["order_confirmed", "order_scheduled"]
    subs["order.confirmed"].assert_awaited_once()
    subs["order.scheduled"].assert_awaited_once()
    assert ws.broadcast.await_count == 2


async def test_guarded_transition_carries_links_into_the_write():
    store, repo = _repo(fuel_order_doc("o1"))
    service, *_ = _service(repo)
    claim = await _claim(repo)
    order = dict(claim.order)
    order["assigned_driver_id"] = "D1"
    await service.apply_status_transition(order, "scheduled", guard_stored_state=True)
    stored = store.doc(ORDERS, "o1")
    assert (stored["assigned_run_id"], stored["assigned_claim_id"], stored["assigned_driver_id"]) == (
        "run-1",
        "c-1",
        "D1",
    )


@pytest.mark.parametrize(
    ("interference", "error"),
    [
        ({"last_event_timestamp": TS1}, OrderChangedConcurrentlyError),
        ({"status": "cancelled"}, OrderChangedConcurrentlyError),
        (None, OrderWriteDiscardedError),
    ],
)
async def test_refused_guarded_transition_restores_and_has_no_side_effects(interference, error):
    if interference is None:
        # Stored timestamp in the future: the new stamp is not newer.
        doc = fuel_order_doc("o1", status="confirmed", last_event_timestamp=FUTURE, hold_reason="keep")
    else:
        doc = fuel_order_doc("o1", status="confirmed", hold_reason="keep")
    store, repo = _repo(doc)
    service, ws, subs = _service(repo)
    order = await repo.get_current(T, "o1")
    snapshot = {k: order[k] for k in ("status", "last_event_timestamp", "updated_at", "hold_reason")}
    if interference is not None:
        store.poke(ORDERS, "o1", **interference)
    before = store.doc(ORDERS, "o1")

    with pytest.raises(error) as raised:
        await service.apply_status_transition(order, "scheduled", guard_stored_state=True)

    assert not isinstance(raised.value, AppException)
    assert {k: order[k] for k in snapshot} == snapshot
    assert store.doc(ORDERS, "o1") == before
    assert store.events() == []
    assert ws.mock_calls == []
    subs["order.scheduled"].assert_not_awaited()


async def test_unguarded_transition_keeps_event_before_upsert_order():
    store, repo = _repo(fuel_order_doc("o1", status="confirmed"))
    service, *_ = _service(repo)
    order = await repo.get_current(T, "o1")
    await service.apply_status_transition(order, "scheduled")
    ops = [(op, idx) for op, idx, _id in store.writes()]
    assert ops == [("index_document", "fuel_order_events"), ("atomic_update", ORDERS)]
