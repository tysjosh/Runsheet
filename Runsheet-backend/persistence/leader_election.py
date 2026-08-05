"""Leader election for the periodic background jobs.

Every background job in this application starts unconditionally in every
process. That is why the API has been pinned to exactly one task: two processes
meant two AR-aging snapshots for the same day, two overdue sweeps racing the
same invoices (``invoice_events`` has a unique ``(invoice_id,
sequence_number)``, so a race *raises* rather than double-writing), and two
copies of every autonomous agent re-escalating the same shipment because the
cooldown tracker is per-process memory.

One task means every deploy is a downtime window, because ECS must stop the old
task before starting the new one. This module removes that constraint: any
number of processes may run, and exactly one of them runs the sweeps.

## Why one role lock rather than a lock per job

The obvious design gives each job its own advisory lock. It does not fit here,
and the reason is arithmetic. A session-scoped advisory lock has to be held by a
connection that lives as long as the loop, so N singleton jobs cost N permanent
connections. There are ~34 of them (15 sweep loops in ``bootstrap/`` plus 19
registered autonomous agents) against ``database_pool_size`` 10 +
``database_max_overflow`` 5. The jobs would exhaust the pool and the request
path would block on checkout waiting for locks that are never released.

So leadership is a single *role*: "this process runs the periodic jobs". One
lock, one held connection. Followers serve HTTP and run no sweeps. That is
exactly today's behaviour on the leader, minus the requirement that it be the
only process alive.

The cost is honest: background work is not spread across replicas. Spreading it
needs per-job locks, which needs a connection budget this application does not
have, or transaction-scoped locks taken per cycle, which is a larger change to
every job. Neither is required to make deploys safe, which is the point of this.

## Why a session-scoped lock and not a lease

``pg_try_advisory_lock`` is released automatically when the connection drops, so
a process that is killed -9 does not leave a lock held. There is no lease to
renew and no stale-lock TTL to tune, which are the two things lease-based
election gets wrong under GC pauses.

The catch, learned the hard way on the outbox relay: the lock belongs to one
specific backend, and the work runs on *other* sessions from the pool. If the
lock-holding connection dies — an Aurora failover, or an
``idle_in_transaction_session_timeout`` reaping a session that is idle by design
— Postgres releases the lock and another process legitimately takes it, while a
loop that only checked at startup keeps running. Leadership is therefore
re-verified before every cycle by comparing ``pg_backend_pid()`` against the pid
the lock was granted on.

## Key space

The lock uses the two-argument ``pg_try_advisory_lock(classid, objid)`` form.
Postgres keeps that in a **different key space** from the one-argument bigint
form the outbox relay uses, so the relay's key and a sweep key cannot collide
even if their bytes coincide. Verified against a real server: the relay's
``0x52554E534845544F`` and ``(0x52554E53, 0x4845544F)`` were both granted, on
two different sessions, at the same time.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from typing import Awaitable, Callable, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: Namespace for every lock in this module. The paired ``objid`` identifies the
#: role. ``0x52554E53`` is "RUNS".
LOCK_CLASS_ID = 0x52554E53

#: ``objid`` for the one role that exists today: run the periodic background
#: jobs. Derived from a name via :func:`role_object_id` like any other, but
#: named here because it is the app-wide default.
SWEEPS_ROLE = "runsheet.periodic-sweeps"

#: Sentinel for "this backend does not implement advisory locks" (SQLite under
#: test). Distinct from ``None``, which means "not currently held" and triggers
#: a re-contend.
LOCK_UNSUPPORTED = -1


def role_object_id(name: str) -> int:
    """Map a role name to a stable signed 32-bit advisory-lock ``objid``.

    Uses blake2s rather than the builtin ``hash``: ``hash(str)`` is salted per
    process by ``PYTHONHASHSEED``, so two replicas would derive *different* keys
    from the same name, never contend, and both run the sweeps — the exact
    failure this module exists to prevent, with nothing logged.

    The result is signed because ``pg_try_advisory_lock`` takes ``integer``.
    """
    digest = hashlib.blake2s(name.encode("utf-8"), digest_size=4).digest()
    unsigned = int.from_bytes(digest, "big")
    return unsigned - 0x1_0000_0000 if unsigned >= 0x8000_0000 else unsigned


# ---------------------------------------------------------------------------
# Advisory-lock primitives
#
# Shared with persistence/outbox_relay.py so there is one implementation of
# "did the connection holding my lock go away", which is the part that is easy
# to get subtly wrong.
# ---------------------------------------------------------------------------


async def backend_pid(session) -> Optional[int]:
    """Server-side pid of ``session``'s current connection, or ``None``.

    ``None`` means the probe itself failed, i.e. the connection is unusable —
    which answers the ownership question the same way a changed pid does.
    """
    try:
        result = await session.execute(text("SELECT pg_backend_pid()"))
        value = result.scalar()
        return int(value) if value is not None else None
    except Exception:  # noqa: BLE001 — a broken connection answers the question
        # Roll back so the session can check out a fresh connection instead of
        # staying wedged in a failed transaction.
        with contextlib.suppress(Exception):
            await session.rollback()
        return None


async def try_advisory_lock(session, key: int) -> tuple[bool, bool]:
    """Take the session-scoped one-arg (bigint) advisory lock.

    Returns ``(granted, supported)``. ``supported`` is False where advisory
    locks do not exist (SQLite under test); those also report ``granted``,
    because there is no second process to contend with and failing closed would
    disable every background job for the whole unit suite. Callers need the
    distinction because they cannot verify a lock that does not exist.
    """
    try:
        result = await session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        )
        return bool(result.scalar()), True
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await session.rollback()
        return True, False


async def try_advisory_lock_pair(session, classid: int, objid: int) -> tuple[bool, bool]:
    """Take the session-scoped two-arg advisory lock. See :func:`try_advisory_lock`.

    The two-arg form lives in its own key space, which is why a role lock here
    cannot collide with the relay's bigint key.
    """
    try:
        result = await session.execute(
            text("SELECT pg_try_advisory_lock(:classid, :objid)"),
            {"classid": classid, "objid": objid},
        )
        return bool(result.scalar()), True
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await session.rollback()
        return True, False


class RoleLock:
    """A session-scoped advisory lock representing one singleton role.

    Encapsulates the part that is easy to get wrong: the lock belongs to one
    backend, so "do I still hold it" is a question about the connection, not
    about the lock table.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.object_id = role_object_id(name)
        #: pid the lock was granted on. ``None`` = not held.
        #: ``LOCK_UNSUPPORTED`` = backend has no advisory locks.
        self._pid: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._pid is not None

    async def acquire(self, session) -> bool:
        """Contend for the lock, remembering which connection holds it."""
        granted, supported = await try_advisory_lock_pair(
            session, LOCK_CLASS_ID, self.object_id
        )
        if not supported:
            self._pid = LOCK_UNSUPPORTED
            return True
        if not granted:
            self._pid = None
            return False

        pid = await backend_pid(session)
        if pid is None:
            # The lock call succeeded but the pid probe did not, so the
            # connection died in between. Report not-held rather than disabling
            # verification for the rest of the process's life.
            self._pid = None
            return False
        self._pid = pid
        return True

    async def still_held(self, session) -> bool:
        """True while the lock is still held on the connection it was granted on.

        A pid probe is sufficient: advisory locks live until released or until
        the backend goes away, so an unchanged pid means the grant stands. A
        changed pid means SQLAlchemy handed this session a replacement
        connection, which can only have happened after the original was closed
        or killed — and the lock died with it.
        """
        if self._pid == LOCK_UNSUPPORTED:
            return True  # no advisory locks, hence no second holder to fear
        if self._pid is None:
            return False

        pid = await backend_pid(session)
        if pid is not None and pid == self._pid:
            return True

        logger.warning(
            "Lost the connection holding the %r role lock (pid %s -> %s); the "
            "lock was released with it. Re-contending before running again.",
            self.name,
            self._pid,
            pid,
        )
        self._pid = None
        return False

    def forget(self) -> None:
        self._pid = None


class SweepLeader:
    """Elects one process to run the periodic background jobs.

    Start it once during bootstrap. It holds a single Postgres advisory lock on
    a single long-lived connection and keeps :attr:`is_leader` current;
    background jobs consult it before each cycle via :meth:`wait_until_leader`
    or the :func:`run_periodic` helper.

    Leadership is *not* sticky across the process's life — it is re-verified
    every ``verify_interval_seconds``, so a failover that releases the lock is
    noticed and either re-taken or conceded. A follower that observes the
    leader's connection drop takes over within one verify interval.

    Without a Postgres source-of-truth configured, every process reports itself
    leader. That is correct for development (there is one process, and the
    persistence layer is dormant anyway) and unreachable in staging/production,
    where settings refuse to start without ``database_url``.
    """

    def __init__(
        self,
        *,
        role: str = SWEEPS_ROLE,
        verify_interval_seconds: float = 5.0,
    ) -> None:
        self._lock = RoleLock(role)
        self._verify_interval = verify_interval_seconds
        self._leader = asyncio.Event()
        self._stopped = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._degraded = False

    # -- state ---------------------------------------------------------

    @property
    def is_leader(self) -> bool:
        return self._leader.is_set()

    @property
    def role(self) -> str:
        return self._lock.name

    def describe(self) -> dict:
        """Health-endpoint friendly snapshot."""
        return {
            "role": self._lock.name,
            "is_leader": self.is_leader,
            "advisory_lock_object_id": self._lock.object_id,
            "election_active": self._task is not None and not self._task.done(),
            "degraded_no_database": self._degraded,
        }

    # -- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        """Begin contending for leadership in the background."""
        from persistence.database import is_persistence_enabled

        if not is_persistence_enabled():
            # No Postgres: nothing to elect against. Announce it loudly rather
            # than letting a single-process assumption go unstated.
            self._degraded = True
            self._leader.set()
            logger.warning(
                "Sweep leader election disabled — no database_url configured, so "
                "this process assumes it is the only one and will run every "
                "periodic job. Safe on a single-process development stack; "
                "staging and production refuse to start without database_url."
            )
            return

        if self._task is not None and not self._task.done():
            logger.debug("Sweep leader election already running")
            return

        self._stopped.clear()
        self._task = asyncio.create_task(self._elect_forever(), name="sweep-leader")
        logger.info(
            "Sweep leader election started (role=%r, objid=%d, verify every %.1fs)",
            self._lock.name,
            self._lock.object_id,
            self._verify_interval,
        )

    async def stop(self) -> None:
        """Stand down and release the lock by closing its connection."""
        self._stopped.set()
        self._leader.clear()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        self._lock.forget()
        logger.info("Sweep leader election stopped")

    # -- election loop -------------------------------------------------

    async def _elect_forever(self) -> None:
        from persistence.database import session_scope

        try:
            # The lock is session-scoped, so the session must outlive every
            # verification — it cannot be opened and closed per check.
            async with session_scope() as lock_session:
                while not self._stopped.is_set():
                    try:
                        await self._election_tick(lock_session)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 — never let election die
                        logger.exception(
                            "Sweep leader election tick failed; treating this "
                            "process as a follower until the next tick"
                        )
                        self._leader.clear()
                        self._lock.forget()
                    if await self._sleep_unless_stopped(self._verify_interval):
                        break
        except asyncio.CancelledError:
            pass
        finally:
            self._leader.clear()
            self._lock.forget()

    async def _election_tick(self, lock_session) -> None:
        if self._lock.held:
            if await self._lock.still_held(lock_session):
                return
            # Lost it. Fall through and re-contend immediately rather than
            # waiting a full interval, but stand down first so no cycle starts
            # while ownership is unknown.
            self._leader.clear()

        became_leader = await self._lock.acquire(lock_session)
        if became_leader and not self._leader.is_set():
            self._leader.set()
            logger.info(
                "This process is now the sweep leader (role=%r) — periodic jobs "
                "will run here",
                self._lock.name,
            )
        elif not became_leader and self._leader.is_set():
            self._leader.clear()
            logger.info(
                "This process is no longer the sweep leader (role=%r) — periodic "
                "jobs will stand down",
                self._lock.name,
            )
        elif not became_leader:
            logger.debug(
                "Sweep leader held elsewhere (role=%r) — standing by",
                self._lock.name,
            )

    async def _sleep_unless_stopped(self, seconds: float) -> bool:
        """Sleep ``seconds``; return True if :meth:`stop` was called instead."""
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return False
        return True

    # -- consumer API --------------------------------------------------

    async def wait_until_leader(self, timeout: Optional[float] = None) -> bool:
        """Block until this process is the leader. Returns False on timeout."""
        if self.is_leader:
            return True
        try:
            await asyncio.wait_for(self._leader.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True


#: Process-wide leader, set by bootstrap. ``None`` means leader election was
#: never wired, which :func:`run_periodic` treats as "run" so that a script or
#: test driving a job directly is not silently a no-op.
_leader: Optional[SweepLeader] = None


def set_sweep_leader(leader: Optional[SweepLeader]) -> None:
    """Register the process-wide sweep leader (called from bootstrap)."""
    global _leader
    _leader = leader


def get_sweep_leader() -> Optional[SweepLeader]:
    return _leader


def is_sweep_leader() -> bool:
    """True when this process should run periodic jobs.

    Returns True when election was never wired: a one-shot CLI invocation or a
    unit test calling a sweep cycle directly must not become a silent no-op just
    because no election is running.
    """
    return _leader is None or _leader.is_leader


async def run_periodic(
    name: str,
    interval_seconds: float,
    cycle: Callable[[], Awaitable[None]],
    *,
    run_immediately: bool = False,
) -> None:
    """Run ``cycle`` every ``interval_seconds``, but only while leader.

    Replaces the ``while True: await asyncio.sleep(n); ...`` bodies that were
    copied across ``bootstrap/``. Three behaviours those copies each had to get
    right individually:

    * A follower skips the cycle instead of exiting, so leadership can move to
      this process later without restarting it.
    * An exception inside ``cycle`` is logged and the loop continues. A sweep
      that dies on one bad row must not stay dead until the next deploy.
    * ``asyncio.CancelledError`` propagates, so shutdown is not swallowed.
    """
    logger.info(
        "Periodic job %r scheduled every %.0fs%s",
        name,
        interval_seconds,
        " (first run immediate)" if run_immediately else "",
    )
    first = True
    try:
        while True:
            if not (first and run_immediately):
                await asyncio.sleep(interval_seconds)
            first = False

            if not is_sweep_leader():
                logger.debug("Periodic job %r skipped — not the sweep leader", name)
                continue

            try:
                await cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a bad cycle must not kill the loop
                logger.exception("Periodic job %r cycle failed", name)
    except asyncio.CancelledError:
        logger.info("Periodic job %r cancelled", name)
        raise


__all__ = [
    "LOCK_CLASS_ID",
    "LOCK_UNSUPPORTED",
    "SWEEPS_ROLE",
    "RoleLock",
    "SweepLeader",
    "backend_pid",
    "get_sweep_leader",
    "is_sweep_leader",
    "role_object_id",
    "run_periodic",
    "set_sweep_leader",
    "try_advisory_lock",
    "try_advisory_lock_pair",
]
