"""Per-tenant mutual exclusion for applying and dispatching loading plans.

The loading-plan executor and MVP plan dispatch (``fuel/services/
plan_dispatch_service.py``) both claim orders for a run, write them, and, on
failure, release what they claimed. Run concurrently on overlapping orders they
race, and every timestamp compare-and-set that tried to make that race safe
opened a new gap. The design freeze (loading-plan-executor design.md, "Design
freeze" rule 1; plan decision P1) serializes them instead: both take this lock
for their whole claim -> write -> release sequence. Other order writers
(``assign_driver``, quantity PATCH, cancellation) do not take it; claim-id
ownership and guarded transitions cover them.

Two layers, always taken in this order:

1. **In-process** ``asyncio.Lock`` per tenant. It is always taken, so the
   guarantee holds within one process even without Postgres (unit tests, the
   persistence-disabled case, SQLite).
2. **Postgres** ``pg_advisory_xact_lock(classid, objid)`` when the document
   store is Postgres. It serializes across processes and tasks.

Transaction scope. The advisory lock is *transaction*-scoped and is taken on a
dedicated session (``session_scope()``) whose transaction stays open for the
whole ``async with`` block. Leaving the block ends that transaction (commit on
success, rollback on error), and Postgres releases the lock with it. If the
connection drops (pod kill, failover), Postgres ends the transaction and
releases the lock too, so there is no lease, TTL or stale lock to clean up.
``SET LOCAL lock_timeout`` bounds the wait; SQLSTATE 55P03 maps to
:class:`PlanExecutionLockTimeout`. The work inside the block runs on other
pooled sessions, so the cost is one held connection per in-flight execution.

Why not ``persistence.leader_election``. Its helpers are *session*-scoped
try-locks (``pg_try_advisory_lock``) built for one long-lived role holder that
re-verifies its backend pid every cycle. A per-request critical section needs a
blocking wait with a timeout and must release when the request ends, even if
the code forgets to unlock, which is exactly what a transaction-scoped lock
gives. Only the stable key hashing (``role_object_id``) is reused. The class id
differs from ``leader_election.LOCK_CLASS_ID``, so tenant keys can never
collide with role keys.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Dict, Optional

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from persistence.leader_election import role_object_id

logger = logging.getLogger(__name__)

#: Advisory-lock class id for "plan execution". ``0x504C4E58`` is "PLNX".
PLAN_EXECUTION_LOCK_CLASS_ID = 0x504C4E58

#: SQLSTATE ``lock_not_available``, raised when ``lock_timeout`` expires.
_LOCK_NOT_AVAILABLE = "55P03"


class PlanExecutionLockTimeout(Exception):
    """The tenant's plan-execution lock was not acquired within the timeout."""

    def __init__(self, tenant_id: str, timeout_seconds: float) -> None:
        self.tenant_id = tenant_id
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"plan-execution lock for tenant {tenant_id!r} not acquired "
            f"within {timeout_seconds:g}s"
        )


def _sqlstate(exc: BaseException) -> Optional[str]:
    """SQLSTATE of a DBAPI error across psycopg 3, asyncpg and psycopg2."""
    orig = getattr(exc, "orig", None) or exc
    for attr in ("sqlstate", "pgcode"):
        value = getattr(orig, attr, None)
        if value:
            return str(value)
    return None


def _postgres_backend() -> bool:
    """True when the persistence layer is on and its engine is PostgreSQL."""
    from persistence.database import get_engine, is_persistence_enabled

    if not is_persistence_enabled():
        return False
    return get_engine().dialect.name == "postgresql"


class PlanExecutionLock:
    """Per-tenant lock held across one plan apply or dispatch (FREEZE rule 1).

    Args:
        session_factory: Zero-arg callable returning an async context manager
            that yields a session and ends its transaction on exit. Defaults to
            ``persistence.database.session_scope``.
        timeout_seconds: Bound on the total wait (in-process plus Postgres).
        use_postgres: ``True``/``False`` forces the advisory layer on or off;
            ``None`` decides per ``hold`` from the configured engine.
    """

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        timeout_seconds: float = 30.0,
        use_postgres: Optional[bool] = None,
    ) -> None:
        self._session_factory = session_factory
        self._timeout = float(timeout_seconds)
        self._use_postgres = use_postgres
        # asyncio.Lock binds to the loop it first waits on, so locks are kept
        # per event loop; a module singleton then survives loop changes.
        self._locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, asyncio.Lock]]" = (
            weakref.WeakKeyDictionary()
        )

    @property
    def timeout_seconds(self) -> float:
        return self._timeout

    def _local_lock(self, tenant_id: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        per_loop = self._locks.get(loop)
        if per_loop is None:
            per_loop = {}
            self._locks[loop] = per_loop
        lock = per_loop.get(tenant_id)
        if lock is None:
            lock = asyncio.Lock()
            per_loop[tenant_id] = lock
        return lock

    def _postgres_enabled(self) -> bool:
        if self._use_postgres is not None:
            return self._use_postgres
        return _postgres_backend()

    @asynccontextmanager
    async def hold(self, tenant_id: str) -> AsyncIterator[None]:
        """Hold the tenant's lock for the body of the ``async with`` block.

        Raises :class:`PlanExecutionLockTimeout` when either layer is not
        acquired within ``timeout_seconds``. Exceptions from the body propagate
        unchanged and release both layers.
        """
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        started = time.monotonic()
        local = self._local_lock(tenant_id)
        try:
            await asyncio.wait_for(local.acquire(), timeout=self._timeout)
        except asyncio.TimeoutError:
            raise PlanExecutionLockTimeout(tenant_id, self._timeout) from None
        try:
            if not self._postgres_enabled():
                yield
                return
            remaining = max(self._timeout - (time.monotonic() - started), 0.001)
            async with self._advisory(tenant_id, remaining):
                yield
        finally:
            local.release()

    @asynccontextmanager
    async def _advisory(self, tenant_id: str, remaining: float) -> AsyncIterator[None]:
        factory = self._session_factory
        if factory is None:
            from persistence.database import session_scope

            factory = session_scope
        body_done = False
        try:
            async with factory() as session:
                timeout_ms = max(int(remaining * 1000), 1)
                try:
                    # An int literal; SET does not take bind parameters.
                    await session.execute(
                        text(f"SET LOCAL lock_timeout = '{timeout_ms}ms'")
                    )
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:classid, :objid)"),
                        {
                            "classid": PLAN_EXECUTION_LOCK_CLASS_ID,
                            "objid": role_object_id(f"plan-execution:{tenant_id}"),
                        },
                    )
                except DBAPIError as exc:
                    if _sqlstate(exc) == _LOCK_NOT_AVAILABLE:
                        raise PlanExecutionLockTimeout(
                            tenant_id, self._timeout
                        ) from None
                    raise
                yield
                body_done = True
        except Exception:
            if not body_done:
                raise
            # The body finished; only ending the lock transaction failed. The
            # lock is released with the connection either way, so the
            # completed work is not turned into an error.
            logger.warning(
                "plan-execution lock: ending the lock transaction failed "
                "for tenant %s; the lock is released with the connection",
                tenant_id,
                exc_info=True,
            )


#: Shared default for ``LoadingPlanExecutor`` and ``FuelPlanDispatchService``,
#: so in-process serialization holds even without Postgres (P1).
PLAN_EXECUTION_LOCK = PlanExecutionLock()


__all__ = [
    "PLAN_EXECUTION_LOCK",
    "PLAN_EXECUTION_LOCK_CLASS_ID",
    "PlanExecutionLock",
    "PlanExecutionLockTimeout",
]
