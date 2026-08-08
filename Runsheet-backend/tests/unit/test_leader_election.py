"""One process runs the periodic jobs, so the API can run more than one task.

Every background job in this application starts unconditionally in every
process, which is why the API was pinned to ``desiredCount: 1`` and every ECS
deploy therefore needed a stop-then-start downtime window. Two processes meant
two AR-aging snapshots for the same day, two overdue sweeps racing invoices whose
``invoice_events`` carries a unique ``(invoice_id, sequence_number)`` — so a race
*raises* — and two copies of every autonomous agent re-escalating the same entity
because the cooldown tracker is per-process memory.

These tests pin the properties that make leadership trustworthy:

1. **Key derivation is stable across processes.** ``hash(str)`` is salted per
   process by ``PYTHONHASHSEED``, so deriving the lock key that way would give
   two replicas different keys, no contention, and both running every job. This
   is the single most dangerous way for the whole mechanism to fail silently.
2. **Ownership is re-verified, not assumed.** The lock is session-scoped and the
   work runs on other pooled sessions. A failover releasing the lock must be
   noticed.
3. **A follower skips, it does not exit.** Leadership has to be able to move to a
   process later without restarting it.
4. **A backend without advisory locks still runs jobs.** SQLite under test has no
   second process to contend with; failing closed would disable every job in the
   suite.
"""
from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from persistence.leader_election import (
    LOCK_CLASS_ID,
    LOCK_UNSUPPORTED,
    SWEEPS_ROLE,
    RoleLock,
    SweepLeader,
    get_sweep_leader,
    is_sweep_leader,
    role_object_id,
    run_periodic,
    set_sweep_leader,
)


class _Session:
    """Session double whose advisory-lock call returns a scripted answer.

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


@pytest.fixture(autouse=True)
def _no_ambient_leader():
    """Keep the process-wide leader unset unless a test sets it."""
    original = get_sweep_leader()
    set_sweep_leader(None)
    yield
    set_sweep_leader(original)


# ---------------------------------------------------------------------------
# 1. Key derivation
# ---------------------------------------------------------------------------


class TestKeyDerivation:
    def test_same_name_gives_the_same_key(self):
        assert role_object_id("a.b") == role_object_id("a.b")

    def test_different_names_give_different_keys(self):
        assert role_object_id("a.b") != role_object_id("a.c")

    def test_key_fits_a_signed_32_bit_integer(self):
        """``pg_try_advisory_lock(classid, objid)`` takes ``integer``."""
        for name in (SWEEPS_ROLE, "x", "y" * 500, ""):
            key = role_object_id(name)
            assert -(2**31) <= key < 2**31, (name, key)

    def test_key_is_stable_across_processes_not_salted_by_pythonhashseed(self):
        """The failure this guards against is total and silent.

        ``hash(str)`` is randomised per interpreter unless PYTHONHASHSEED is
        fixed. Deriving the lock key that way would give two replicas two
        different keys: neither would ever contend, both would believe they were
        leader, and every sweep would run twice with nothing logged. So derive
        the key in two subprocesses with *different* hash seeds and require the
        same answer.
        """
        code = (
            "from persistence.leader_election import role_object_id, SWEEPS_ROLE;"
            "print(role_object_id(SWEEPS_ROLE))"
        )
        outs = []
        for seed in ("0", "1", "12345"):
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                env={"PYTHONHASHSEED": seed, "ENVIRONMENT": "test", "PATH": "/usr/bin:/bin"},
                cwd=".",
            )
            assert proc.returncode == 0, proc.stderr
            outs.append(proc.stdout.strip())

        assert len(set(outs)) == 1, (
            f"role_object_id is not stable across PYTHONHASHSEED values: {outs}. "
            "Two replicas would derive different keys, never contend, and both "
            "would run every periodic job."
        )
        assert outs[0] == str(role_object_id(SWEEPS_ROLE))


# ---------------------------------------------------------------------------
# 2. The lock itself
# ---------------------------------------------------------------------------


class TestRoleLock:
    @pytest.mark.asyncio
    async def test_acquire_uses_the_two_argument_form_and_the_shared_class_id(self):
        """The two-arg form lives in its own key space, which is what keeps a
        role lock from colliding with the outbox relay's bigint key."""
        lock = RoleLock(SWEEPS_ROLE)
        session = _Session(granted=True)
        assert await lock.acquire(session) is True

        sql, params = session.statements[0]
        assert "pg_try_advisory_lock(:classid, :objid)" in sql
        assert params == {"classid": LOCK_CLASS_ID, "objid": lock.object_id}

    @pytest.mark.asyncio
    async def test_lock_held_elsewhere_is_not_acquired(self):
        lock = RoleLock(SWEEPS_ROLE)
        assert await lock.acquire(_Session(granted=False)) is False
        assert lock.held is False

    @pytest.mark.asyncio
    async def test_a_backend_without_advisory_locks_is_allowed(self):
        lock = RoleLock(SWEEPS_ROLE)
        assert await lock.acquire(_Session(raises=True)) is True
        assert lock._pid == LOCK_UNSUPPORTED

    @pytest.mark.asyncio
    async def test_unsupported_backend_never_reports_a_lost_lock(self):
        lock = RoleLock(SWEEPS_ROLE)
        await lock.acquire(_Session(raises=True))
        assert await lock.still_held(_Session(raises=True)) is True

    @pytest.mark.asyncio
    async def test_still_held_is_true_while_the_pid_is_unchanged(self):
        lock = RoleLock(SWEEPS_ROLE)
        session = _Session(granted=True, pid=111)
        await lock.acquire(session)
        assert await lock.still_held(session) is True

    @pytest.mark.asyncio
    async def test_a_replaced_connection_means_the_lock_is_gone(self):
        """This is the Aurora-failover case, and the one a startup-only check
        misses: the lock session dies, Postgres releases the lock, another
        process legitimately takes it, and a loop that checked once keeps going."""
        lock = RoleLock(SWEEPS_ROLE)
        session = _Session(granted=True, pid=111)
        await lock.acquire(session)

        session.pid = 222  # SQLAlchemy handed the session a new connection
        assert await lock.still_held(session) is False
        assert lock.held is False

    @pytest.mark.asyncio
    async def test_a_dead_connection_means_the_lock_is_gone(self):
        lock = RoleLock(SWEEPS_ROLE)
        session = _Session(granted=True, pid=111)
        await lock.acquire(session)

        assert await lock.still_held(_Session(raises=True)) is False

    @pytest.mark.asyncio
    async def test_a_granted_lock_whose_pid_probe_fails_is_not_held(self):
        """Reporting held here would disable verification for the process's life."""
        lock = RoleLock(SWEEPS_ROLE)

        class _GrantsThenDies(_Session):
            async def execute(self, statement, params=None):
                sql = str(statement)
                if "pg_backend_pid" in sql:
                    raise RuntimeError("connection went away")
                return await super().execute(statement, params)

        assert await lock.acquire(_GrantsThenDies(granted=True)) is False


# ---------------------------------------------------------------------------
# 3. SweepLeader
# ---------------------------------------------------------------------------


class TestSweepLeaderWithoutADatabase:
    @pytest.mark.asyncio
    async def test_no_database_means_this_process_is_the_leader(self):
        """Correct on a single-process dev stack, and unreachable in
        staging/production where settings refuse to start without database_url."""
        leader = SweepLeader()
        with patch(
            "persistence.database.is_persistence_enabled", return_value=False
        ):
            await leader.start()

        assert leader.is_leader is True
        assert leader.describe()["degraded_no_database"] is True
        await leader.stop()

    @pytest.mark.asyncio
    async def test_no_database_starts_no_election_task(self):
        leader = SweepLeader()
        with patch(
            "persistence.database.is_persistence_enabled", return_value=False
        ):
            await leader.start()
        assert leader.describe()["election_active"] is False
        await leader.stop()


class TestSweepLeaderElection:
    @staticmethod
    def _patched_scope(session):
        @contextlib.asynccontextmanager
        async def _scope():
            yield session

        return _scope

    @pytest.mark.asyncio
    async def test_taking_the_lock_makes_this_process_leader(self):
        leader = SweepLeader(verify_interval_seconds=0.01)
        session = _Session(granted=True, pid=7)

        with patch(
            "persistence.database.is_persistence_enabled", return_value=True
        ), patch(
            "persistence.database.session_scope", self._patched_scope(session)
        ):
            await leader.start()
            for _ in range(50):
                if leader.is_leader:
                    break
                await asyncio.sleep(0.01)
            assert leader.is_leader is True
            await leader.stop()

    @pytest.mark.asyncio
    async def test_a_follower_never_claims_leadership(self):
        leader = SweepLeader(verify_interval_seconds=0.01)
        session = _Session(granted=False, pid=7)

        with patch(
            "persistence.database.is_persistence_enabled", return_value=True
        ), patch(
            "persistence.database.session_scope", self._patched_scope(session)
        ):
            await leader.start()
            await asyncio.sleep(0.1)
            assert leader.is_leader is False
            await leader.stop()

    @pytest.mark.asyncio
    async def test_losing_the_lock_connection_stands_the_leader_down(self):
        leader = SweepLeader(verify_interval_seconds=0.01)
        session = _Session(granted=True, pid=7)

        with patch(
            "persistence.database.is_persistence_enabled", return_value=True
        ), patch(
            "persistence.database.session_scope", self._patched_scope(session)
        ):
            await leader.start()
            for _ in range(50):
                if leader.is_leader:
                    break
                await asyncio.sleep(0.01)
            assert leader.is_leader is True

            # The lock connection dies AND the lock is now held elsewhere, so the
            # re-contend fails: this process must stand down rather than keep
            # running sweeps alongside the new holder.
            session.pid = 999
            session._granted = False
            for _ in range(60):
                if not leader.is_leader:
                    break
                await asyncio.sleep(0.01)
            assert leader.is_leader is False
            await leader.stop()

    @pytest.mark.asyncio
    async def test_stop_releases_leadership(self):
        leader = SweepLeader(verify_interval_seconds=0.01)
        session = _Session(granted=True, pid=7)

        with patch(
            "persistence.database.is_persistence_enabled", return_value=True
        ), patch(
            "persistence.database.session_scope", self._patched_scope(session)
        ):
            await leader.start()
            for _ in range(50):
                if leader.is_leader:
                    break
                await asyncio.sleep(0.01)
            await leader.stop()

        assert leader.is_leader is False
        assert leader.describe()["election_active"] is False


# ---------------------------------------------------------------------------
# 4. run_periodic
# ---------------------------------------------------------------------------


class _FakeLeader:
    def __init__(self, is_leader: bool):
        self._is_leader = is_leader

    @property
    def is_leader(self) -> bool:
        return self._is_leader


class TestRunPeriodic:
    @pytest.mark.asyncio
    async def test_no_registered_leader_runs_the_cycle(self):
        """A one-shot CLI invocation or a direct unit test must not become a
        silent no-op just because no election is running."""
        assert is_sweep_leader() is True

        ran = asyncio.Event()

        async def cycle():
            ran.set()

        task = asyncio.create_task(
            run_periodic("t", 0.01, cycle, run_immediately=True)
        )
        await asyncio.wait_for(ran.wait(), timeout=1.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_the_leader_runs_the_cycle(self):
        set_sweep_leader(_FakeLeader(True))
        ran = asyncio.Event()

        async def cycle():
            ran.set()

        task = asyncio.create_task(
            run_periodic("t", 0.01, cycle, run_immediately=True)
        )
        await asyncio.wait_for(ran.wait(), timeout=1.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_a_follower_skips_the_cycle_but_keeps_the_loop_alive(self):
        """Exiting would mean leadership could never move to this process."""
        set_sweep_leader(_FakeLeader(False))
        calls = []

        async def cycle():
            calls.append(1)

        task = asyncio.create_task(
            run_periodic("t", 0.005, cycle, run_immediately=True)
        )
        await asyncio.sleep(0.1)
        assert calls == [], "a follower ran the cycle"
        assert not task.done(), "the loop exited instead of standing by"

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_leadership_acquired_later_starts_running_cycles(self):
        follower = _FakeLeader(False)
        set_sweep_leader(follower)
        calls = []

        async def cycle():
            calls.append(1)

        task = asyncio.create_task(run_periodic("t", 0.005, cycle))
        await asyncio.sleep(0.05)
        assert calls == []

        follower._is_leader = True  # election moved leadership here
        await asyncio.sleep(0.05)
        assert calls, "cycles did not resume after becoming leader"

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_a_failing_cycle_does_not_kill_the_loop(self):
        """A sweep that dies on one bad row must not stay dead until the next
        deploy."""
        set_sweep_leader(_FakeLeader(True))
        calls = []

        async def cycle():
            calls.append(1)
            raise RuntimeError("bad row")

        task = asyncio.create_task(
            run_periodic("t", 0.005, cycle, run_immediately=True)
        )
        await asyncio.sleep(0.08)
        assert len(calls) > 1, f"loop stopped after a failure: {calls}"
        assert not task.done()

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_cancellation_propagates_so_shutdown_works(self):
        set_sweep_leader(_FakeLeader(True))

        async def cycle():
            return None

        task = asyncio.create_task(run_periodic("t", 5.0, cycle))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_run_immediately_does_not_wait_for_the_first_interval(self):
        set_sweep_leader(_FakeLeader(True))
        ran = asyncio.Event()

        async def cycle():
            ran.set()

        task = asyncio.create_task(
            run_periodic("t", 30.0, cycle, run_immediately=True)
        )
        await asyncio.wait_for(ran.wait(), timeout=1.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_default_sleeps_before_the_first_cycle(self):
        """Boot must not be delayed by a retention sweep."""
        set_sweep_leader(_FakeLeader(True))
        calls = []

        async def cycle():
            calls.append(1)

        task = asyncio.create_task(run_periodic("t", 30.0, cycle))
        await asyncio.sleep(0.05)
        assert calls == []

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
