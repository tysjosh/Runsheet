"""Outbox relay: drains unpublished outbox rows into Elasticsearch.

The relay is the bridge that keeps the ES read/search projection consistent
with the Postgres source-of-truth. It polls ``outbox_events`` for rows with
``published_at IS NULL`` (oldest first), indexes each ``payload`` into its
``target_index`` under the aggregate id, and stamps ``published_at`` on
success. Failures increment ``attempts`` and record ``last_error`` so a poison
row does not wedge the queue forever.

Delivery is at-least-once: each event is an idempotent upsert (same ES id), so
re-delivering after a crash between "indexed" and "marked published" is safe.

Run it as a periodic background task (e.g. from the bootstrap scheduler) by
calling :meth:`run_forever`, or drive a single drain with :meth:`drain_once`
(used by tests and one-shot CLI invocations).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Optional

from sqlalchemy import select, text

from persistence.database import session_scope
from persistence.models import OutboxEventORM

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 10

#: Postgres advisory-lock key identifying "the outbox relay". Arbitrary but
#: fixed: any two processes using this constant contend for the same lock.
#:
#: Why a lock at all. ``drain_once`` selects ``published_at IS NULL`` ordered by
#: id with no row claiming, so two relays select the SAME rows and both index
#: them. Re-indexing one event is harmless (same ES id, idempotent upsert), but
#: two relays running concurrently can also interleave: relay A takes event 3
#: and relay B takes event 5 for the same aggregate, B commits first, and the
#: OLDER payload lands last — leaving the Elasticsearch projection permanently
#: behind the Postgres row it is supposed to mirror, with nothing logged.
#:
#: ``FOR UPDATE SKIP LOCKED`` would stop the duplicate work but not the
#: inversion, which is the part that corrupts the projection. Ordering across
#: concurrent relays needs per-aggregate partitioning; until then exactly one
#: relay may be active, and this lock enforces that rather than trusting a
#: deployment note.
_RELAY_ADVISORY_LOCK_KEY = 0x52554E53_4845544F  # "RUNS" "HETO"

#: Sentinel for ``_lock_pid`` meaning "this backend has no advisory locks"
#: (SQLite in tests). Distinct from ``None``, which means "not currently known
#: to hold the lock" and triggers a re-contend.
_LOCK_UNSUPPORTED = -1


async def _backend_pid(session) -> Optional[int]:
    """Return the server-side pid of ``session``'s current connection.

    The advisory lock is *session*-scoped, so it belongs to one specific
    backend. If the pid changes, the connection this loop believed it held the
    lock on is gone and the lock went with it. ``None`` means the query itself
    failed — the connection is unusable, which is the same conclusion.
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


async def _acquire_relay_lock(session) -> tuple[bool, bool]:
    """Take the session-scoped advisory lock. Returns ``(granted, supported)``.

    ``pg_try_advisory_lock`` is session-scoped and released automatically when
    the connection drops, so a relay that is killed does not leave the lock held
    — no lease renewal and no stale-lock cleanup to get wrong.

    ``supported`` is False on databases that do not implement advisory locks
    (SQLite in tests). Those report ``granted`` too: there is no second process
    to contend with there, and failing closed would disable the relay for the
    whole unit suite. The caller needs the distinction because it cannot verify
    a lock that does not exist.
    """
    try:
        result = await session.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": _RELAY_ADVISORY_LOCK_KEY},
        )
        return bool(result.scalar()), True
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        return True, False


async def _try_acquire_relay_lock(session) -> bool:
    """Boolean form of :func:`_acquire_relay_lock` for one-shot callers."""
    granted, _supported = await _acquire_relay_lock(session)
    return granted


class OutboxRelay:
    """Projects transactional-outbox rows into Elasticsearch.

    Args:
        es_service: An object exposing ``async index_document(index, id, doc)``
            (the app's ``ElasticsearchService`` satisfies this).
        batch_size: Max events drained per :meth:`drain_once` call.
    """

    def __init__(self, es_service, *, batch_size: int = 100) -> None:
        self._es = es_service
        self._batch_size = batch_size
        self._stopped = asyncio.Event()
        # Backend pid the advisory lock was granted on. None = not held;
        # _LOCK_UNSUPPORTED = backend has no advisory locks (see the sentinel).
        self._lock_pid: Optional[int] = None

    async def drain_once(self) -> int:
        """Project up to ``batch_size`` unpublished events. Returns the count published."""
        from services.time_utils import utcnow

        published = 0
        async with session_scope() as session:
            result = await session.execute(
                select(OutboxEventORM)
                .where(OutboxEventORM.published_at.is_(None))
                .where(OutboxEventORM.attempts < _MAX_ATTEMPTS)
                .order_by(OutboxEventORM.id.asc())
                .limit(self._batch_size)
            )
            events = list(result.scalars().all())

            for event in events:
                try:
                    await self._es.index_document(
                        event.target_index,
                        event.aggregate_id,
                        dict(event.payload),
                    )
                    event.published_at = utcnow()
                    published += 1
                except Exception as exc:  # noqa: BLE001 — record and continue
                    event.attempts += 1
                    event.last_error = str(exc)[:1000]
                    logger.warning(
                        "Outbox relay failed to project event id=%s (%s/%s): %s",
                        event.id, event.attempts, _MAX_ATTEMPTS, exc,
                    )
            # session_scope commits published_at / attempts updates atomically.

        if published:
            logger.info("Outbox relay published %d event(s) to Elasticsearch", published)
        return published

    async def run_forever(self, *, poll_interval_seconds: float = 1.0) -> None:
        """Continuously drain the outbox until :meth:`stop` is called.

        Only one relay across the whole deployment actually drains: the loop
        holds a Postgres advisory lock for as long as it runs, and an instance
        that cannot take the lock stands down and re-checks. Standing down is
        correct follower behaviour, not an error, so it is logged at INFO.

        This is what makes replicas safe. Every background job in this
        application starts unconditionally per process, so N replicas meant N
        relays racing the same rows; see the note on
        ``_RELAY_ADVISORY_LOCK_KEY`` for why that corrupts the projection rather
        than merely duplicating work.

        Holding the lock is re-verified before every drain, because the lock is
        session-scoped and ``drain_once`` runs on a *different* session. If the
        lock-holding connection dies — a managed-Postgres failover, or an
        ``idle_in_transaction_session_timeout`` reaping this deliberately idle
        session — Postgres releases the lock, another instance can take it, and a
        loop that only checked once at startup would keep draining alongside it.
        That is the exact two-relay hazard the lock exists to prevent, arriving
        by a different route.
        """
        logger.info("Outbox relay started (poll every %.1fs)", poll_interval_seconds)
        # The lock is session-scoped, so it has to be held by a connection that
        # lives as long as the loop — not taken and released per drain.
        async with session_scope() as lock_session:
            holding = False
            while not self._stopped.is_set():
                if holding:
                    holding = await self._still_holds_lock(lock_session)
                    if not holding:
                        continue  # re-enter the standby path below
                else:
                    holding = await self._take_lock(lock_session)
                    if not holding:
                        logger.info(
                            "Outbox relay standing down — another instance holds "
                            "the relay lock. Re-checking in %.1fs.",
                            poll_interval_seconds,
                        )
                        if await self._sleep_unless_stopped(poll_interval_seconds):
                            logger.info("Outbox relay stopped while standing down")
                            return
                        continue
                    logger.info("Outbox relay holds the relay lock — draining")

                try:
                    drained = await self.drain_once()
                except Exception:  # noqa: BLE001 — never let the loop die
                    logger.exception("Outbox relay drain cycle failed; backing off")
                    drained = 0
                # Idle: sleep the poll interval. Busy: loop tight to clear backlog.
                if drained == 0:
                    await self._sleep_unless_stopped(poll_interval_seconds)
        logger.info("Outbox relay stopped")

    async def _take_lock(self, session) -> bool:
        """Contend for the relay lock, remembering which connection holds it."""
        granted, supported = await _acquire_relay_lock(session)
        if not supported:
            self._lock_pid = _LOCK_UNSUPPORTED
            return True
        if not granted:
            self._lock_pid = None
            return False

        pid = await _backend_pid(session)
        if pid is None:
            # The advisory-lock call succeeded but the pid probe did not, so the
            # connection died in between. Treat the lock as not held rather than
            # disabling verification for the rest of the process's life.
            self._lock_pid = None
            return False
        self._lock_pid = pid
        return True

    async def _still_holds_lock(self, session) -> bool:
        """True while the lock is still held on the connection it was granted on.

        A pid probe is enough: advisory locks live until released or until the
        backend goes away, so an unchanged pid means the grant stands. A changed
        pid means SQLAlchemy handed this session a replacement connection, which
        can only have happened after the original one was closed or killed — and
        the lock died with it.
        """
        if self._lock_pid == _LOCK_UNSUPPORTED:
            return True  # no advisory locks, hence no second holder to fear

        pid = await _backend_pid(session)
        if pid is not None and pid == self._lock_pid:
            return True

        logger.warning(
            "Outbox relay lost the connection holding the relay lock "
            "(pid %s -> %s); the lock was released with it. Re-contending "
            "before draining again.",
            self._lock_pid,
            pid,
        )
        self._lock_pid = None
        return False

    async def _sleep_unless_stopped(self, seconds: float) -> bool:
        """Sleep ``seconds``; return True if :meth:`stop` was called instead."""
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return False
        return True

    def stop(self) -> None:
        """Signal :meth:`run_forever` to exit after the current cycle."""
        self._stopped.set()


async def project_pending(es_service, *, max_batches: Optional[int] = None) -> int:
    """Drain the outbox to completion (or ``max_batches`` cycles). Returns total published.

    Convenience entry point for CLI / backfill use.
    """
    relay = OutboxRelay(es_service)
    total = 0
    batches = 0
    while True:
        n = await relay.drain_once()
        total += n
        batches += 1
        if n == 0:
            break
        if max_batches is not None and batches >= max_batches:
            break
    return total
