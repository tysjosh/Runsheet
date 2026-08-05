"""Only one outbox relay may drain, or the Elasticsearch projection goes stale.

``drain_once`` selects ``published_at IS NULL`` ordered by id with **no row
claiming**. Two relays therefore select the same rows and both index them.
Re-indexing one event is harmless — same ES id, idempotent upsert — but two
relays can also interleave: relay A takes event 3, relay B takes event 5 for the
same aggregate, B commits first, and the OLDER payload lands last. The projection
is then permanently behind the row it mirrors, and nothing logs a thing.

That matters because every background job in this application starts
unconditionally per process. Load testing showed a single worker saturating
around 150 rps, and the obvious remedy — more replicas — would have started a
second relay. So the loop now holds a Postgres advisory lock and an instance that
cannot take it stands down.

``FOR UPDATE SKIP LOCKED`` was considered and rejected: it removes the duplicate
work but not the inversion, which is the part that corrupts the projection.
Fixing ordering properly needs per-aggregate partitioning; until then exactly one
relay is active and this enforces it rather than trusting a deployment note.
"""
from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from persistence.outbox_relay import (
    _RELAY_ADVISORY_LOCK_KEY,
    OutboxRelay,
    _try_acquire_relay_lock,
)


class _Session:
    """Minimal session whose advisory-lock call returns a scripted answer.

    ``pg_backend_pid()`` answers ``pid``, so a test can simulate the
    lock-holding connection being replaced by changing that attribute.
    """

    def __init__(self, granted=True, raises=False, pid=4242):
        self._granted = granted
        self._raises = raises
        self.pid = pid
        self.statements = []

    async def execute(self, statement, params=None):
        if self._raises:
            raise RuntimeError("advisory locks not implemented")
        sql = str(statement)
        self.statements.append((sql, params))
        result = MagicMock()
        result.scalar.return_value = (
            self.pid if "pg_backend_pid" in sql else self._granted
        )
        return result

    async def rollback(self):
        return None


@contextlib.asynccontextmanager
async def _scope(session):
    yield session


class TestLockAcquisition:
    @pytest.mark.asyncio
    async def test_granted_lock_returns_true_and_uses_the_fixed_key(self):
        session = _Session(granted=True)
        assert await _try_acquire_relay_lock(session) is True
        sql, params = session.statements[0]
        assert "pg_try_advisory_lock" in sql
        # A per-process key would make the lock meaningless — every instance
        # would take its own and all of them would drain.
        assert params == {"key": _RELAY_ADVISORY_LOCK_KEY}

    @pytest.mark.asyncio
    async def test_lock_held_elsewhere_returns_false(self):
        assert await _try_acquire_relay_lock(_Session(granted=False)) is False

    @pytest.mark.asyncio
    async def test_a_backend_without_advisory_locks_is_allowed(self):
        """SQLite has no advisory locks and no second process to contend with.

        Failing closed here would silently disable the relay for the whole unit
        suite, which would be a worse outcome than the risk it guards against.
        """
        assert await _try_acquire_relay_lock(_Session(raises=True)) is True


class TestOnlyTheLockHolderDrains:
    @pytest.mark.asyncio
    async def test_the_holder_drains(self):
        relay = OutboxRelay(MagicMock())
        relay.drain_once = AsyncMock(return_value=0)

        with patch(
            "persistence.outbox_relay.session_scope",
            lambda: _scope(_Session(granted=True)),
        ):
            task = asyncio.create_task(relay.run_forever(poll_interval_seconds=0.01))
            await asyncio.sleep(0.05)
            relay.stop()
            await asyncio.wait_for(task, timeout=2)

        assert relay.drain_once.await_count > 0, "the lock holder must drain"

    @pytest.mark.asyncio
    async def test_a_follower_never_drains(self):
        relay = OutboxRelay(MagicMock())
        relay.drain_once = AsyncMock(return_value=0)

        with patch(
            "persistence.outbox_relay.session_scope",
            lambda: _scope(_Session(granted=False)),
        ):
            task = asyncio.create_task(relay.run_forever(poll_interval_seconds=0.01))
            await asyncio.sleep(0.08)
            relay.stop()
            await asyncio.wait_for(task, timeout=2)

        assert relay.drain_once.await_count == 0, (
            "a follower drained anyway — two relays can invert the order of "
            "events for the same aggregate and leave ES permanently stale"
        )

    @pytest.mark.asyncio
    async def test_a_follower_reports_standing_down_at_info_not_error(self, caplog):
        """Standing down is correct follower behaviour, not a fault.

        Logged at ERROR it would page somebody on every replica of a healthy
        deployment, and alerts that fire when nothing is wrong get muted.
        """
        relay = OutboxRelay(MagicMock())
        relay.drain_once = AsyncMock(return_value=0)

        with caplog.at_level("INFO", logger="persistence.outbox_relay"), patch(
            "persistence.outbox_relay.session_scope",
            lambda: _scope(_Session(granted=False)),
        ):
            task = asyncio.create_task(relay.run_forever(poll_interval_seconds=0.01))
            await asyncio.sleep(0.05)
            relay.stop()
            await asyncio.wait_for(task, timeout=2)

        standing_down = [
            r for r in caplog.records if "standing down" in r.getMessage()
        ]
        assert standing_down, "a follower must say why it is not draining"
        assert all(r.levelname == "INFO" for r in standing_down), [
            r.levelname for r in standing_down
        ]

    @pytest.mark.asyncio
    async def test_a_follower_still_stops_when_asked(self):
        """A stood-down relay must not ignore shutdown.

        It sits in a re-check loop; if that loop did not observe the stop event
        the process would hang on shutdown and be killed instead.
        """
        relay = OutboxRelay(MagicMock())
        relay.drain_once = AsyncMock(return_value=0)

        with patch(
            "persistence.outbox_relay.session_scope",
            lambda: _scope(_Session(granted=False)),
        ):
            task = asyncio.create_task(relay.run_forever(poll_interval_seconds=5.0))
            await asyncio.sleep(0.02)
            relay.stop()
            # Would raise TimeoutError if the follower only woke on its own
            # 5-second poll rather than on the stop event.
            await asyncio.wait_for(task, timeout=1.0)

        assert task.done()


class TestDrainOnceIsUnchanged:
    """The claiming behaviour is deliberately untouched; the lock is the guard."""

    @pytest.mark.asyncio
    async def test_drain_once_does_not_take_the_lock_itself(self):
        """Callers like ``project_pending`` (CLI, backfill) must still work.

        The lock belongs to the long-running loop. Putting it in ``drain_once``
        would make a one-shot CLI drain fail whenever a server happened to be
        running, which is not the hazard being guarded.
        """
        relay = OutboxRelay(MagicMock())
        session = _Session(granted=False)

        async def _execute(statement, params=None):
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        session.execute = _execute  # type: ignore[method-assign]

        with patch(
            "persistence.outbox_relay.session_scope", lambda: _scope(session)
        ):
            assert await relay.drain_once() == 0


class _FailoverSession(_Session):
    """Hands the relay a replacement connection once it has drained.

    This is what a managed-Postgres failover looks like from inside the process:
    the connection the advisory lock was granted on is gone, SQLAlchemy checks
    out a new one, and Postgres has already released the lock.
    """

    def __init__(self, *, regranted: bool):
        super().__init__(granted=True, pid=100)
        self._regranted = regranted

    def replace_connection(self) -> None:
        self.pid = 200
        self._granted = self._regranted


class TestLockOwnershipIsRevalidated:
    """The lock is on ``lock_session``; ``drain_once`` uses a different session.

    So holding the lock at startup does not mean holding it now. Postgres
    releases a session-scoped advisory lock the moment the backend goes away —
    a failover, a `pg_terminate_backend`, or an
    ``idle_in_transaction_session_timeout`` reaping this deliberately idle
    session. Checking only once meant a relay could drain for the rest of its
    life holding nothing, while a second instance legitimately took the lock:
    the two-relay hazard restored by the back door.
    """

    async def _run(self, relay, session, *, seconds=0.1):
        with patch(
            "persistence.outbox_relay.session_scope", lambda: _scope(session)
        ):
            task = asyncio.create_task(relay.run_forever(poll_interval_seconds=0.005))
            await asyncio.sleep(seconds)
            relay.stop()
            await asyncio.wait_for(task, timeout=2)

    def _counting_relay(self, session, drains):
        relay = OutboxRelay(MagicMock())

        async def _drain():
            drains.append(1)
            session.replace_connection()
            return 0

        relay.drain_once = _drain  # type: ignore[method-assign]
        return relay

    @pytest.mark.asyncio
    async def test_it_stops_draining_when_another_instance_took_the_lock(self):
        session = _FailoverSession(regranted=False)
        drains: list[int] = []
        relay = self._counting_relay(session, drains)

        await self._run(relay, session)

        # 0.1s at a 5ms poll is ~20 cycles if ownership is never re-checked.
        assert len(drains) == 1, (
            f"drained {len(drains)} times after losing the lock connection — a "
            "second relay now holds the lock and both are projecting, which can "
            "land an older payload last and leave ES permanently stale"
        )

    @pytest.mark.asyncio
    async def test_it_resumes_when_it_can_retake_the_lock(self):
        """Losing the connection is not a reason to stop relaying for good.

        If nothing else claimed the lock, the relay is still the right instance
        to drain — it just needs the grant on its new connection.
        """
        session = _FailoverSession(regranted=True)
        drains: list[int] = []
        relay = self._counting_relay(session, drains)

        await self._run(relay, session)

        assert len(drains) > 1, "the relay never resumed after re-taking the lock"

    @pytest.mark.asyncio
    async def test_losing_the_lock_connection_is_logged_at_warning(self, caplog):
        """Unlike standing down, this is not routine — it means a lock was lost.

        INFO would bury it; ERROR would be wrong for a condition the relay
        recovers from on its own. The message carries both pids so the
        correlation with a failover event is checkable.
        """
        session = _FailoverSession(regranted=True)
        drains: list[int] = []
        relay = self._counting_relay(session, drains)

        with caplog.at_level("INFO", logger="persistence.outbox_relay"):
            await self._run(relay, session)

        lost = [r for r in caplog.records if "lost the connection" in r.getMessage()]
        assert lost, "a lost relay lock must be visible in the logs"
        assert all(r.levelname == "WARNING" for r in lost), [r.levelname for r in lost]
        assert "100" in lost[0].getMessage() and "200" in lost[0].getMessage()

    @pytest.mark.asyncio
    async def test_a_backend_without_advisory_locks_is_not_revalidated(self):
        """SQLite answers no ``pg_backend_pid``, so there is nothing to compare.

        Revalidating there would make every cycle look like a lost lock and the
        relay would never drain in the unit suite.
        """
        relay = OutboxRelay(MagicMock())
        relay.drain_once = AsyncMock(return_value=0)

        await self._run(relay, _Session(raises=True))

        assert relay.drain_once.await_count > 0
