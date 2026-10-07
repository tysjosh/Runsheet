"""``relink_dispatched_assignment`` on a real PostgreSQL (dispatch-board K8.3).

Two concurrent relinks of one order from the same published link race under
``SELECT ... FOR UPDATE``: exactly one wins and the other is refused, so a
dispatched order can never end up linked to two runs or to a mix of them.
A driver write built from a read taken before a relink then fails the K5a
guard, which is what freeze rule 10 relies on.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from fuel.order_repository import FuelOrderRepository, OrderChangedConcurrentlyError
from tests.postgres.test_loading_plan_executor_postgres import _Namespaced
from tests.unit._loading_plan_fakes import ORDERS, fuel_order_doc

pytestmark = pytest.mark.asyncio


@pytest.fixture
def tenant() -> str:
    return f"pytest-relink-{uuid.uuid4().hex[:12]}"


async def _seed(ns, tenant):
    doc = fuel_order_doc(
        "ord-1",
        tenant_id=tenant,
        status="dispatched",
        assigned_run_id="bp-a-r1",
        assigned_asset_id="truck-a",
        assigned_driver_id="drv-a",
    )
    await ns.index_document(ORDERS, "ord-1", doc)
    return doc


async def test_concurrent_relinks_from_the_same_link_one_wins(store, index_name, tenant):
    ns = _Namespaced(store, index_name)
    await _seed(ns, tenant)
    repo = FuelOrderRepository(ns)

    async def relink(target):
        return await repo.relink_dispatched_assignment(
            tenant,
            "ord-1",
            from_run_id="bp-a-r1",
            from_asset_id="truck-a",
            from_driver_id="drv-a",
            to_run_id=f"bp-{target}-r1",
            to_asset_id=f"truck-{target}",
            to_driver_id=f"drv-{target}",
            claim_id=f"claim-{target}",
        )

    results = await asyncio.gather(*(relink(t) for t in ("b", "c", "d", "e")))
    assert sorted(results).count("relinked") == 1
    assert sorted(results).count("refused") == 3

    stored = await ns.get_document(ORDERS, "ord-1")
    winner = ["b", "c", "d", "e"][results.index("relinked")]
    assert (stored["assigned_run_id"], stored["assigned_asset_id"], stored["assigned_driver_id"]) == (
        f"bp-{winner}-r1", f"truck-{winner}", f"drv-{winner}",
    )
    assert stored["status"] == "dispatched"


async def test_stale_guarded_write_after_relink_is_refused(store, index_name, tenant):
    ns = _Namespaced(store, index_name)
    seeded = await _seed(ns, tenant)
    repo = FuelOrderRepository(ns)
    # The driver's read, taken before the board relinks.
    stale = dict(seeded)

    assert (
        await repo.relink_dispatched_assignment(
            tenant,
            "ord-1",
            from_run_id="bp-a-r1",
            from_asset_id="truck-a",
            from_driver_id="drv-a",
            to_run_id="bp-b-r1",
            to_asset_id="truck-b",
            to_driver_id="drv-b",
            claim_id="claim-b",
        )
        == "relinked"
    )

    stale.update(status="in_transit", last_event_timestamp="2099-01-01T00:00:00+00:00")
    with pytest.raises(OrderChangedConcurrentlyError):
        await repo.upsert_with_last_event_timestamp(
            tenant,
            stale,
            expected_status="dispatched",
            expected_last_event_timestamp=seeded["last_event_timestamp"],
        )
    stored = await ns.get_document(ORDERS, "ord-1")
    assert stored["assigned_run_id"] == "bp-b-r1"
    assert stored["status"] == "dispatched"
