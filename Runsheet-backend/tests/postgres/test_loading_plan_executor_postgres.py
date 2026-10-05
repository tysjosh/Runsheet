"""The loading-plan executor's store-level behaviour on a real PostgreSQL
(design test plan "Postgres", L1077; FREEZE rule 1; K3, K5, K5a, K7, K11).

The in-memory fakes in ``tests/unit`` evaluate queries with
``document_matcher`` and serialize ``atomic_update`` in one event loop. Here
the same code runs on ``PostgresDocumentStore`` (``SELECT ... FOR UPDATE``,
jsonb query translation) with real concurrent connections, and the plan
lock's advisory layer runs ``pg_advisory_xact_lock``.

Every fixed index name the services use is routed under this test's unique
``index_name`` prefix (:class:`_Namespaced`), so the conftest cleanup removes
everything. Hybrid ``fuel_orders_current`` rows use a per-test tenant id and
are deleted by it. Skips without ``POSTGRES_TEST_URL`` (CI fails on a skip).
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text

from Agents.approval_queue_service import ApprovalQueueService, LoadingPlanOverlapError
from Agents.overlay.compartment_loading_agent import COMMITTED_ORDER_FIELD, _unlinked_clause
from fuel.order_repository import FuelOrderRepository, OrderChangedConcurrentlyError
from fuel.services.loading_plan_executor import LoadingPlanExecutor
from fuel.services.order_service import OrderService
from fuel.services.plan_dispatch_service import FuelPlanDispatchService
from persistence.leader_election import role_object_id
from persistence.plan_execution_lock import (
    PLAN_EXECUTION_LOCK_CLASS_ID,
    PlanExecutionLock,
    PlanExecutionLockTimeout,
)
from services.unit_conversion import GAL_TO_L
from tests.unit._loading_plan_fakes import (
    APPROVALS,
    EVENTS,
    ORDERS,
    PLANS,
    FakeActivityLog,
    FakeFeatureFlagService,
    SpyAgentWS,
    approval_entry,
    fuel_order_doc,
    loading_agent,
    plan_doc,
)
from tests.unit.test_plan_dispatch_service import FakeDriverRepository

_INDEX_METHODS = {
    "get_document", "index_document", "update_document", "delete_document",
    "atomic_update", "upsert_if_newer", "search_documents", "document_exists",
    "count",
}


class _Namespaced:
    """The real ``PostgresDocumentStore`` with every index under ``prefix``."""

    def __init__(self, inner: Any, prefix: str) -> None:
        self._inner = inner
        self._prefix = prefix

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name not in _INDEX_METHODS:
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            if "index" in kwargs:
                kwargs["index"] = self._prefix + kwargs["index"]
            else:
                args = (self._prefix + args[0], *args[1:])
            return await attr(*args, **kwargs)

        return call


@pytest.fixture
def tenant() -> str:
    return f"pytest-lpe-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def es(store, index_name) -> _Namespaced:
    return _Namespaced(store, f"{index_name}__")


def _oid(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}"


async def _seed(es, index: str, doc_id: str, doc: Dict[str, Any]) -> None:
    await es.index_document(index, doc_id, doc)


async def _events(es, tenant: str, order_id: str) -> List[str]:
    resp = await es.search_documents(
        EVENTS,
        {"query": {"bool": {"filter": [
            {"term": {"tenant_id": tenant}}, {"term": {"order_id": order_id}},
        ]}}, "sort": [{"event_timestamp": {"order": "asc"}}], "size": 50},
        50,
    )
    return [h["_source"]["event_type"] for h in resp["hits"]["hits"]]


def _executor(es, lock: PlanExecutionLock) -> LoadingPlanExecutor:
    repo = FuelOrderRepository(es)
    service = OrderService(order_repo=repo, ws_manager=AsyncMock(), driver_counter_service=AsyncMock())
    return LoadingPlanExecutor(
        es_service=es, order_repository=repo, order_service=service,
        feature_flag_service=FakeFeatureFlagService("active_gated"), plan_lock=lock,
    )


async def _execute(executor, tenant, plan, order_ids, action_id="act-1"):
    return await executor.execute(
        tenant_id=tenant, plan_id=plan["plan_id"], expected_order_ids=sorted(order_ids),
        expected_truck_id=plan["truck_id"], order_snapshots={}, actor_user_id="user-1",
        action_id=action_id, approved_at=None, mode="active_gated",
    )


# ---------------------------------------------------------------------------
# Plan claim, link CAS and guarded upsert under real row locks
# ---------------------------------------------------------------------------


async def test_concurrent_plan_claims_have_one_winner(es, tenant):
    oids = [_oid("ord"), _oid("ord")]
    orders = [fuel_order_doc(o, tenant_id=tenant, status="confirmed") for o in oids]
    for order in orders:
        await _seed(es, ORDERS, order["order_id"], order)
    plan = plan_doc(_oid("plan"), orders=orders, tenant_id=tenant)
    await _seed(es, PLANS, plan["plan_id"], plan)
    # Separate in-process locks, so only the store's claim CAS decides.
    a = _executor(es, PlanExecutionLock(use_postgres=False, timeout_seconds=10))
    b = _executor(es, PlanExecutionLock(use_postgres=False, timeout_seconds=10))

    results = await asyncio.gather(
        _execute(a, tenant, plan, oids, "act-a"), _execute(b, tenant, plan, oids, "act-b")
    )

    outcomes = sorted(r.outcome for r in results)
    assert outcomes.count("applied") == 1, outcomes
    assert set(outcomes) <= {"applied", "in_progress", "replayed"}
    loser = next(r for r in results if r.outcome != "applied")
    assert loser.applied_order_ids == [] or loser.replay
    stored = await es.get_document(PLANS, plan["plan_id"])
    assert stored["execution_status"] == "succeeded"
    for oid in oids:
        order = await es.get_document(ORDERS, oid)
        assert order["status"] == "scheduled"
        assert order["assigned_run_id"] == plan["run_id"]
        assert (await _events(es, tenant, oid)).count("order_scheduled") == 1


async def test_concurrent_claims_for_different_runs_link_one(es, tenant):
    oid = _oid("ord")
    await _seed(es, ORDERS, oid, fuel_order_doc(oid, tenant_id=tenant))
    repo = FuelOrderRepository(es)

    claims = await asyncio.gather(*(
        repo.claim_assignment(
            tenant, oid, run_id=f"run-{x}", asset_id=f"truck-{x}",
            expected_status="confirmed", claim_id=f"claim-{x}",
        )
        for x in ("a", "b")
    ))

    assert sorted(c.outcome for c in claims) == ["linked", "refused"]
    winner = next(c for c in claims if c.outcome == "linked")
    loser = next(c for c in claims if c.outcome == "refused")
    assert loser.reason == "order_committed_elsewhere"
    stored = await es.get_document(ORDERS, oid)
    assert stored["assigned_run_id"] == winner.order["assigned_run_id"]
    assert stored["assigned_claim_id"] == winner.order["assigned_claim_id"]


async def test_guarded_upsert_refuses_after_a_concurrent_status_change(es, tenant):
    oid = _oid("ord")
    await _seed(es, ORDERS, oid, fuel_order_doc(oid, tenant_id=tenant))
    repo = FuelOrderRepository(es)
    read = await repo.get_current(tenant, oid)
    # Another writer cancels the order between our read and our write.
    await es.atomic_update(ORDERS, oid, lambda c: {
        **c, "status": "cancelled", "last_event_timestamp": "2026-07-29T12:30:00+00:00",
    })

    with pytest.raises(OrderChangedConcurrentlyError) as raised:
        await repo.upsert_with_last_event_timestamp(
            tenant, {**read, "status": "scheduled",
                     "last_event_timestamp": "2026-07-29T13:00:00+00:00"},
            expected_status="confirmed",
            expected_last_event_timestamp=read["last_event_timestamp"],
        )

    assert raised.value.actual_status == "cancelled"
    assert (await es.get_document(ORDERS, oid))["status"] == "cancelled"


async def test_racing_guarded_upserts_apply_exactly_one(es, tenant):
    oid = _oid("ord")
    await _seed(es, ORDERS, oid, fuel_order_doc(oid, tenant_id=tenant))
    repo = FuelOrderRepository(es)
    read = await repo.get_current(tenant, oid)

    async def write(status: str, ts: str):
        try:
            return await repo.upsert_with_last_event_timestamp(
                tenant, {**read, "status": status, "last_event_timestamp": ts},
                expected_status="confirmed",
                expected_last_event_timestamp=read["last_event_timestamp"],
            )
        except OrderChangedConcurrentlyError as exc:
            return exc

    results = await asyncio.gather(
        write("scheduled", "2026-07-29T13:00:00+00:00"),
        write("cancelled", "2026-07-29T13:00:01+00:00"),
    )

    applied = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, OrderChangedConcurrentlyError)]
    assert len(applied) == 1 and len(refused) == 1
    assert (await es.get_document(ORDERS, oid))["status"] == applied[0]["status"]


# ---------------------------------------------------------------------------
# Guarded chains in one call (pass-2 finding 1)
# ---------------------------------------------------------------------------


async def test_executor_moves_placed_to_scheduled_in_one_call(es, tenant):
    oid = _oid("ord")
    order = fuel_order_doc(oid, tenant_id=tenant, status="placed")
    await _seed(es, ORDERS, oid, order)
    plan = plan_doc(_oid("plan"), orders=[order], tenant_id=tenant)
    await _seed(es, PLANS, plan["plan_id"], plan)
    executor = _executor(es, PlanExecutionLock(use_postgres=False, timeout_seconds=10))

    result = await _execute(executor, tenant, plan, [oid])

    assert result.outcome == "applied", result
    stored = await es.get_document(ORDERS, oid)
    assert stored["status"] == "scheduled"
    stored_plan = await es.get_document(PLANS, plan["plan_id"])
    assert stored["assigned_claim_id"] == stored_plan["execution_attempt_id"]
    assert await _events(es, tenant, oid) == ["order_assigned", "order_confirmed", "order_scheduled"]


async def test_dispatch_moves_confirmed_to_dispatched_in_one_call(es, tenant):
    oid = _oid("ord")
    await _seed(es, ORDERS, oid, fuel_order_doc(
        oid, tenant_id=tenant, status="confirmed", customer_tank_id="tank-1"))
    plan_id, run_id = _oid("plan"), _oid("run")
    plan = {
        "plan_id": plan_id, "run_id": run_id, "truck_id": "truck-1", "tenant_id": tenant,
        "status": "proposed",
        "assignments": [{"station_id": "tank-1", "compartment_id": "c-1", "order_id": oid,
                         "fuel_grade": "DIESEL_2", "quantity_liters": 1000}],
    }
    await _seed(es, PLANS, plan_id, plan)
    await _seed(es, "mvp_routes", f"route-{plan_id}", {
        "route_id": f"route-{plan_id}", "plan_id": plan_id, "run_id": run_id,
        "truck_id": "truck-1", "tenant_id": tenant,
        "stops": [{"station_id": "tank-1", "order_ids": [oid], "sequence": 0,
                   "eta": "2026-07-30T09:00:00+00:00", "drop": {"DIESEL_2": 1000}}],
    })
    repo = FuelOrderRepository(es)
    execution = AsyncMock()
    execution.create_execution = AsyncMock(return_value={"execution_id": "execution-1"})
    service = FuelPlanDispatchService(
        es_service=es,
        order_repository=repo,
        order_service=OrderService(order_repo=repo, ws_manager=AsyncMock()),
        driver_repository=FakeDriverRepository([
            {"driver_id": "driver-1", "tenant_id": tenant, "assigned_truck_id": "truck-1",
             "status": "active"},
        ]),
        execution_service=execution,
        driver_ws_manager=AsyncMock(),
        plan_lock=PlanExecutionLock(use_postgres=False, timeout_seconds=10),
    )

    result = await service.dispatch(tenant_id=tenant, plan_doc=plan, actor_user_id="dispatcher-1")

    assert result.newly_dispatched == 1
    stored = await es.get_document(ORDERS, oid)
    assert stored["status"] == "dispatched"
    assert await _events(es, tenant, oid) == ["order_assigned", "order_scheduled", "order_dispatched"]


# ---------------------------------------------------------------------------
# Query translation: include_unresolved, K11, terms on a jsonb array
# ---------------------------------------------------------------------------


def _approval_service(es) -> ApprovalQueueService:
    feedback = MagicMock()
    feedback.record_rejection = AsyncMock()
    return ApprovalQueueService(
        es_service=es, ws_manager=SpyAgentWS(), activity_log_service=FakeActivityLog(),
        feedback_service=feedback,
    )


async def test_include_unresolved_bool_should(es, tenant):
    plan = plan_doc("plan-x", orders=[{"order_id": "ord-x", "customer_id": "c"}], tenant_id=tenant)
    rows = {
        "pending": dict(status="pending"),
        "incomplete": dict(status="incomplete"),
        "failed": dict(status="failed"),
        "approved-loading": dict(status="approved"),
        "approved-other": dict(status="approved", tool_name="update_job_status"),
        "executed": dict(status="executed"),
        "rejected": dict(status="rejected"),
        "expired": dict(status="expired"),
        "shadowed": dict(status="shadowed"),
    }
    for i, (name, overrides) in enumerate(rows.items()):
        await _seed(es, APPROVALS, name, approval_entry(
            name, plan, tenant_id=tenant,
            proposed_at=f"2026-07-29T11:{i:02d}:00+00:00", **overrides))
    await _seed(es, APPROVALS, "other-tenant", approval_entry(
        "other-tenant", plan, tenant_id=f"{tenant}-b", status="pending"))
    svc = _approval_service(es)

    unresolved = await svc.list_pending(tenant, size=50, include_unresolved=True)
    pending = await svc.list_pending(tenant, size=50)

    assert sorted(i["action_id"] for i in unresolved["items"]) == [
        "approved-loading", "failed", "incomplete", "pending",
    ]
    assert unresolved["total"] == 4
    assert [i["action_id"] for i in pending["items"]] == ["pending"]


async def test_k11_unlinked_clause_through_the_store(es, tenant):
    docs = {
        "absent": fuel_order_doc(_oid("absent"), tenant_id=tenant),
        "null": fuel_order_doc(_oid("null"), tenant_id=tenant, assigned_run_id=None),
        "empty": fuel_order_doc(_oid("empty"), tenant_id=tenant, assigned_run_id=""),
        "linked": fuel_order_doc(_oid("linked"), tenant_id=tenant, status="scheduled",
                                 assigned_run_id="run-1", assigned_asset_id="truck-1"),
    }
    docs["absent"].pop(COMMITTED_ORDER_FIELD)
    for doc in docs.values():
        await _seed(es, ORDERS, doc["order_id"], doc)

    raw = await es.search_documents(ORDERS, {"query": {"bool": {"filter": [
        {"term": {"tenant_id": tenant}}, _unlinked_clause(COMMITTED_ORDER_FIELD),
    ]}}, "size": 50}, 50)
    agent = loading_agent(es)
    loadable = await agent._query_fuel_orders(tenant)
    committed = await agent._count_committed_loadable_orders(tenant)

    expected = sorted(docs[k]["order_id"] for k in ("absent", "null", "empty"))
    assert sorted(h["_source"]["order_id"] for h in raw["hits"]["hits"]) == expected
    assert sorted(o["order_id"] for o in loadable) == expected
    assert committed == 1


async def test_committed_tank_draw_through_the_store(es, tenant):
    tank = _oid("tank")
    for doc in (
        fuel_order_doc(_oid("linked"), tenant_id=tenant, status="scheduled",
                       customer_tank_id=tank, gallons_requested=100.0, assigned_run_id="run-1"),
        fuel_order_doc(_oid("disp"), tenant_id=tenant, status="dispatched",
                       customer_tank_id=tank, gallons_requested=50.0),
        # Uncommitted: not a draw (it is a candidate in its own right).
        fuel_order_doc(_oid("open"), tenant_id=tenant, customer_tank_id=tank,
                       gallons_requested=999.0),
    ):
        await _seed(es, ORDERS, doc["order_id"], doc)

    draw = await loading_agent(es)._committed_tank_draw(tenant, [tank])

    assert draw == {tank: pytest.approx(round(150.0 * GAL_TO_L, 2), abs=0.02)}


async def test_terms_on_order_ids_array_and_search_after_walk(es, tenant, monkeypatch):
    import Agents.approval_queue_service as aqs

    monkeypatch.setattr(aqs, "_OVERLAP_PAGE_SIZE", 2)
    shared = _oid("ord")
    plan = plan_doc("plan-p", orders=[
        {"order_id": shared, "customer_id": "c"}, {"order_id": _oid("ord"), "customer_id": "c"},
    ], tenant_id=tenant)
    pending_ids = [f"pending-{i}" for i in range(5)]
    for i, action_id in enumerate(pending_ids):
        await _seed(es, APPROVALS, action_id, approval_entry(
            action_id, plan, tenant_id=tenant, proposed_at=f"2026-07-29T11:{i:02d}:00+00:00"))
    unrelated = plan_doc("plan-u", orders=[{"order_id": _oid("ord"), "customer_id": "c"}],
                         tenant_id=tenant)
    await _seed(es, APPROVALS, "unrelated", approval_entry(
        "unrelated", unrelated, tenant_id=tenant, proposed_at="2026-07-29T11:30:00+00:00"))

    # terms on parameters.order_ids matches an element of the jsonb array.
    hits = await es.search_documents(APPROVALS, {"query": {"bool": {"filter": [
        {"term": {"tenant_id": tenant}}, {"terms": {"parameters.order_ids": [shared]}},
    ]}}, "size": 50}, 50)
    assert sorted(h["_source"]["action_id"] for h in hits["hits"]["hits"]) == pending_ids

    # The supersede guard walks 3 pages of 2 and expires every overlap.
    svc = _approval_service(es)
    await svc._check_loading_overlap(tenant, {shared}, "act-new", expire_pending=True)
    for action_id in pending_ids:
        assert (await es.get_document(APPROVALS, action_id))["status"] == "expired"
    assert (await es.get_document(APPROVALS, "unrelated"))["status"] == "pending"

    # A holder on the last page still refuses the approval.
    await _seed(es, APPROVALS, "holder", approval_entry(
        "holder", plan, tenant_id=tenant, status="executed",
        proposed_at="2026-07-29T11:59:00+00:00", execution_result={"attempt_id": "att-1"}))
    with pytest.raises(LoadingPlanOverlapError):
        await svc._check_loading_overlap(tenant, {shared}, "act-new-2", expire_pending=True)


# ---------------------------------------------------------------------------
# HybridReadRepository.search(unlinked_fields=...)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def hybrid_orders(pg_engine, pg_sessionmaker, tenant):
    from persistence.models import FuelOrderCurrentORM

    async with pg_engine.begin() as conn:
        await conn.run_sync(
            lambda c: FuelOrderCurrentORM.__table__.create(c, checkfirst=True)
        )
    ids: Dict[str, str] = {}
    async with pg_sessionmaker() as session:
        for kind in ("absent", "null", "empty", "linked"):
            oid = _oid(f"hy-{kind}")
            ids[kind] = oid
            doc = fuel_order_doc(oid, tenant_id=tenant)
            if kind == "absent":
                doc.pop(COMMITTED_ORDER_FIELD)
            elif kind == "empty":
                doc[COMMITTED_ORDER_FIELD] = ""
            elif kind == "linked":
                doc[COMMITTED_ORDER_FIELD] = "run-1"
            session.add(FuelOrderCurrentORM(
                order_id=oid, tenant_id=tenant, status=doc["status"], document=doc,
            ))
        await session.commit()
    try:
        yield ids
    finally:
        async with pg_engine.begin() as conn:
            await conn.execute(
                text("delete from fuel_orders_current where tenant_id = :t"), {"t": tenant}
            )


async def test_hybrid_search_unlinked_fields(pg_sessionmaker, tenant, hybrid_orders):
    from persistence.read_repositories import HybridReadRepository

    repo = HybridReadRepository("fuel_order")
    async with pg_sessionmaker() as session:
        unlinked = await repo.search(session, tenant, unlinked_fields=[COMMITTED_ORDER_FIELD], size=50)
        linked = await repo.search(session, tenant, exists_fields=[COMMITTED_ORDER_FIELD], size=50)
        swept = await repo.search_all_tenants(
            session, term_filters={"tenant_id": tenant},
            unlinked_fields=[COMMITTED_ORDER_FIELD], size=50,
        )

    expected = sorted(hybrid_orders[k] for k in ("absent", "null", "empty"))
    assert sorted(d["order_id"] for d in unlinked["items"]) == expected
    assert unlinked["total"] == 3
    assert sorted(d["order_id"] for d in swept) == expected
    # exists keeps "" (the agent drops it in Python), never null or absent.
    assert sorted(d["order_id"] for d in linked["items"]) == sorted(
        hybrid_orders[k] for k in ("empty", "linked")
    )


# ---------------------------------------------------------------------------
# FREEZE rule 1: the advisory layer of PlanExecutionLock
# ---------------------------------------------------------------------------


@pytest.fixture
def lock_sessions(pg_sessionmaker):
    """``session_scope``-shaped factory: one transaction per ``async with``."""

    @asynccontextmanager
    async def scope():
        async with pg_sessionmaker() as session:
            async with session.begin():
                yield session

    return scope


def _pg_lock(lock_sessions, timeout: float = 10.0) -> PlanExecutionLock:
    # A fresh instance per holder: separate in-process locks, so only the
    # Postgres advisory lock can serialize them.
    return PlanExecutionLock(use_postgres=True, session_factory=lock_sessions, timeout_seconds=timeout)


async def _advisory_holders(pg_sessionmaker, tenant_id: str) -> int:
    objid = role_object_id(f"plan-execution:{tenant_id}") & 0xFFFFFFFF
    async with pg_sessionmaker() as session:
        return (await session.execute(text(
            "select count(*) from pg_locks where locktype = 'advisory' and granted "
            "and classid::bigint = :c and objid::bigint = :o and objsubid = 2"
        ), {"c": PLAN_EXECUTION_LOCK_CLASS_ID, "o": objid})).scalar_one()


async def test_two_holds_for_one_tenant_serialize(lock_sessions, pg_sessionmaker, tenant):
    log: List[str] = []
    entered = asyncio.Event()

    async def first():
        async with _pg_lock(lock_sessions).hold(tenant):
            log.append("a-in")
            entered.set()
            assert await _advisory_holders(pg_sessionmaker, tenant) == 1
            await asyncio.sleep(0.3)
            log.append("a-out")

    async def second():
        await entered.wait()
        async with _pg_lock(lock_sessions).hold(tenant):
            log.append("b-in")
            log.append("b-out")

    await asyncio.wait_for(asyncio.gather(first(), second()), timeout=20)

    assert log == ["a-in", "a-out", "b-in", "b-out"]
    assert await _advisory_holders(pg_sessionmaker, tenant) == 0


async def test_holds_for_different_tenants_overlap(lock_sessions, tenant):
    t1, t2 = f"{tenant}-1", f"{tenant}-2"
    inside = {t1: asyncio.Event(), t2: asyncio.Event()}

    async def hold(me: str, other: str):
        async with _pg_lock(lock_sessions).hold(me):
            inside[me].set()
            # Both are inside at once, or this times out.
            await asyncio.wait_for(inside[other].wait(), timeout=5)

    await asyncio.wait_for(asyncio.gather(hold(t1, t2), hold(t2, t1)), timeout=20)


async def test_waiter_past_the_timeout_raises(lock_sessions, tenant):
    entered, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with _pg_lock(lock_sessions).hold(tenant):
            entered.set()
            await release.wait()

    task = asyncio.create_task(holder())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        with pytest.raises(PlanExecutionLockTimeout) as raised:
            async with _pg_lock(lock_sessions, timeout=0.2).hold(tenant):
                pytest.fail("acquired a lock another session holds")
        assert raised.value.tenant_id == tenant
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=10)

    # Released with the holder's transaction: the next hold gets it at once.
    async with _pg_lock(lock_sessions, timeout=2).hold(tenant):
        pass
