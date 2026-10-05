"""PlanExecutionLock: per-tenant serialization (design FREEZE rule 1, plan P1)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import OperationalError

from persistence.leader_election import LOCK_CLASS_ID, role_object_id
from persistence.plan_execution_lock import (
    PLAN_EXECUTION_LOCK,
    PLAN_EXECUTION_LOCK_CLASS_ID,
    PlanExecutionLock,
    PlanExecutionLockTimeout,
)


async def _holder(lock, tenant, name, log, *, hold_for=0.02):
    async with lock.hold(tenant):
        log.append(("enter", name))
        await asyncio.sleep(hold_for)
        log.append(("exit", name))


async def test_same_tenant_holders_serialize():
    lock = PlanExecutionLock(use_postgres=False)
    log = []
    await asyncio.gather(
        _holder(lock, "t1", "a", log),
        _holder(lock, "t1", "b", log),
        _holder(lock, "t1", "c", log),
    )
    # Every enter is immediately followed by its own exit: no overlap.
    for i in range(0, len(log), 2):
        assert log[i][0] == "enter"
        assert log[i + 1] == ("exit", log[i][1])
    assert len(log) == 6


async def test_different_tenants_overlap():
    # FREEZE test (c): different tenants do not block each other.
    lock = PlanExecutionLock(use_postgres=False, timeout_seconds=0.5)
    t1_inside = asyncio.Event()
    t2_done = asyncio.Event()

    async def t1():
        async with lock.hold("t1"):
            t1_inside.set()
            await asyncio.wait_for(t2_done.wait(), timeout=1)

    async def t2():
        await t1_inside.wait()
        async with lock.hold("t2"):
            t2_done.set()

    await asyncio.gather(t1(), t2())
    assert t2_done.is_set()


async def test_waiter_past_timeout_raises():
    lock = PlanExecutionLock(use_postgres=False, timeout_seconds=0.05)
    inside = asyncio.Event()
    release = asyncio.Event()

    async def blocker():
        async with lock.hold("t1"):
            inside.set()
            await release.wait()

    task = asyncio.create_task(blocker())
    await inside.wait()
    with pytest.raises(PlanExecutionLockTimeout) as raised:
        async with lock.hold("t1"):
            pytest.fail("acquired a held lock")
    assert raised.value.tenant_id == "t1"
    release.set()
    await task
    # The lock is usable again after the holder leaves.
    async with lock.hold("t1"):
        pass


async def test_exception_in_block_releases_lock():
    lock = PlanExecutionLock(use_postgres=False, timeout_seconds=0.1)
    with pytest.raises(RuntimeError, match="boom"):
        async with lock.hold("t1"):
            raise RuntimeError("boom")
    async with lock.hold("t1"):
        pass


async def test_blank_tenant_rejected():
    lock = PlanExecutionLock(use_postgres=False)
    with pytest.raises(ValueError):
        async with lock.hold(" "):
            pass


async def test_singleton_defaults_to_engine_detection():
    assert isinstance(PLAN_EXECUTION_LOCK, PlanExecutionLock)
    assert PLAN_EXECUTION_LOCK.timeout_seconds == 30.0
    assert PLAN_EXECUTION_LOCK._use_postgres is None


# ---------------------------------------------------------------------------
# Postgres layer, against a recording fake session (no database).
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, fail_with=None):
        self.statements = []
        self.fail_with = fail_with

    async def execute(self, stmt, params=None):
        self.statements.append((str(stmt), params))
        if self.fail_with is not None and "pg_advisory_xact_lock" in str(stmt):
            raise self.fail_with


def _factory(session, events):
    @asynccontextmanager
    async def scope():
        events.append("open")
        try:
            yield session
            events.append("commit")
        except Exception:
            events.append("rollback")
            raise

    return scope


async def test_postgres_layer_takes_xact_lock_with_tenant_key():
    session, events = _FakeSession(), []
    lock = PlanExecutionLock(
        session_factory=_factory(session, events), use_postgres=True, timeout_seconds=2
    )
    async with lock.hold("t1"):
        events.append("body")
    assert events == ["open", "body", "commit"]
    (set_stmt, _), (lock_stmt, params) = session.statements
    assert set_stmt.startswith("SET LOCAL lock_timeout = '")
    assert set_stmt.endswith("ms'")
    assert "pg_advisory_xact_lock(:classid, :objid)" in lock_stmt
    assert params == {
        "classid": PLAN_EXECUTION_LOCK_CLASS_ID,
        "objid": role_object_id("plan-execution:t1"),
    }
    assert PLAN_EXECUTION_LOCK_CLASS_ID != LOCK_CLASS_ID


async def test_postgres_lock_timeout_maps_to_plan_execution_lock_timeout():
    class _Orig(Exception):
        sqlstate = "55P03"

    session, events = _FakeSession(OperationalError("SELECT", {}, _Orig())), []
    lock = PlanExecutionLock(session_factory=_factory(session, events), use_postgres=True)
    with pytest.raises(PlanExecutionLockTimeout):
        async with lock.hold("t1"):
            pytest.fail("body ran without the lock")
    assert events == ["open", "rollback"]
    # The in-process layer was released too.
    lock._use_postgres = False
    async with lock.hold("t1"):
        pass


async def test_postgres_other_errors_propagate():
    class _Orig(Exception):
        sqlstate = "08006"

    session = _FakeSession(OperationalError("SELECT", {}, _Orig()))
    lock = PlanExecutionLock(session_factory=_factory(session, []), use_postgres=True)
    with pytest.raises(OperationalError):
        async with lock.hold("t1"):
            pass


async def test_body_exception_rolls_back_lock_transaction():
    session, events = _FakeSession(), []
    lock = PlanExecutionLock(session_factory=_factory(session, events), use_postgres=True)
    with pytest.raises(KeyError):
        async with lock.hold("t1"):
            raise KeyError("x")
    assert events == ["open", "rollback"]
