"""``FuelOrderRepository.relink_dispatched_assignment`` (dispatch-board K8.3).

The relink is the only way a dispatched order moves to another run, truck or
driver (freeze rule 2). It is a full from-link compare-and-set that never
changes status and always advances ``last_event_timestamp``, which is what
makes a stale driver write fail the K5a guard (K8.6).
"""
from __future__ import annotations

import pytest

from fuel.order_repository import FuelOrderRepository
from persistence.timestamps import parse_ts
from tests.unit._loading_plan_fakes import ORDERS, InMemoryDocumentStore, fuel_order_doc

pytestmark = pytest.mark.asyncio

T = "tenant-1"
FROM = {"from_run_id": "bp-a-r1", "from_asset_id": "truck-a", "from_driver_id": "drv-a"}
TO = {"to_run_id": "bp-b-r1", "to_asset_id": "truck-b", "to_driver_id": "drv-b"}
FUTURE_TS = "2099-01-01T00:00:00+00:00"


def _store(**overrides) -> InMemoryDocumentStore:
    store = InMemoryDocumentStore()
    fields = dict(
        tenant_id=T,
        status="dispatched",
        assigned_run_id="bp-a-r1",
        assigned_asset_id="truck-a",
        assigned_driver_id="drv-a",
        assigned_claim_id="claim-0",
    )
    fields.update(overrides)
    store.seed(ORDERS, "ord-1", fuel_order_doc("ord-1", **fields))
    return store


async def _relink(store, **kwargs):
    args = {**FROM, **TO, "claim_id": "claim-1", **kwargs}
    return await FuelOrderRepository(store).relink_dispatched_assignment(T, "ord-1", **args)


async def test_relinked_writes_links_and_advances_timestamp():
    store = _store()
    before = store.doc(ORDERS, "ord-1")
    assert await _relink(store) == "relinked"
    after = store.doc(ORDERS, "ord-1")
    assert (after["assigned_run_id"], after["assigned_asset_id"], after["assigned_driver_id"]) == (
        "bp-b-r1", "truck-b", "drv-b",
    )
    assert after["assigned_claim_id"] == "claim-1"
    assert after["status"] == "dispatched"
    assert parse_ts(after["last_event_timestamp"]) > parse_ts(before["last_event_timestamp"])
    # Nothing else on the order changed.
    for key in ("gallons_requested", "customer_id", "product_code", "delivery_window_start"):
        assert after[key] == before[key]


async def test_timestamp_is_strictly_later_even_when_stored_is_in_the_future():
    store = _store(last_event_timestamp=FUTURE_TS, updated_at=FUTURE_TS)
    assert await _relink(store) == "relinked"
    stored = store.doc(ORDERS, "ord-1")["last_event_timestamp"]
    assert parse_ts(stored) > parse_ts(FUTURE_TS)


async def test_already_relinked_is_a_no_write():
    store = _store(assigned_run_id="bp-b-r1", assigned_asset_id="truck-b", assigned_driver_id="drv-b")
    assert await _relink(store) == "already_relinked"
    assert store.writes(ORDERS) == []


@pytest.mark.parametrize(
    "field, value",
    [
        ("assigned_run_id", "bp-other-r1"),
        ("assigned_asset_id", "truck-z"),
        ("assigned_driver_id", "drv-z"),
    ],
)
async def test_refused_on_each_mismatched_from_field(field, value):
    store = _store(**{field: value})
    assert await _relink(store) == "refused"
    assert store.writes(ORDERS) == []


@pytest.mark.parametrize("status", ["scheduled", "in_transit", "delivered", "failed", "cancelled"])
async def test_refused_on_non_dispatched_status_and_never_writes_status(status):
    store = _store(status=status)
    assert await _relink(store) == "refused"
    assert store.writes(ORDERS) == []
    assert store.doc(ORDERS, "ord-1")["status"] == status


async def test_refused_for_foreign_tenant_and_missing_order():
    store = _store(tenant_id="tenant-2")
    assert await _relink(store) == "refused"
    assert store.writes(ORDERS) == []
    empty = InMemoryDocumentStore()
    assert await _relink(empty) == "refused"


async def test_swapped_from_and_to_is_the_rollback_relink():
    store = _store()
    assert await _relink(store) == "relinked"
    back = await FuelOrderRepository(store).relink_dispatched_assignment(
        T,
        "ord-1",
        from_run_id="bp-b-r1",
        from_asset_id="truck-b",
        from_driver_id="drv-b",
        to_run_id="bp-a-r1",
        to_asset_id="truck-a",
        to_driver_id="drv-a",
        claim_id="claim-rb",
    )
    assert back == "relinked"
    doc = store.doc(ORDERS, "ord-1")
    assert (doc["assigned_run_id"], doc["assigned_asset_id"], doc["assigned_driver_id"]) == (
        "bp-a-r1", "truck-a", "drv-a",
    )


@pytest.mark.parametrize("field", ["to_run_id", "to_asset_id", "from_run_id", "claim_id"])
async def test_blank_identifiers_are_rejected(field):
    with pytest.raises(ValueError):
        await _relink(_store(), **{field: " "})
