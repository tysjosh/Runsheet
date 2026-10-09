"""Leader election against a real Postgres — the properties mocks cannot prove.

Three claims that only a real server settles, because they are claims about
Postgres semantics rather than about this code:

1. **Two processes, one leader.** ``pg_try_advisory_lock`` must actually refuse
   the second contender. A mock can only prove we asked.
2. **Killing the leader hands over.** A session-scoped lock is released when its
   backend dies, which is the whole reason this design needs no lease renewal and
   no stale-lock TTL. If that is not true, a crashed leader wedges every periodic
   job until someone notices.
3. **The sweep lock cannot collide with the outbox relay's.** The relay holds a
   one-argument ``pg_try_advisory_lock(bigint)``; a role lock holds the
   two-argument ``(integer, integer)`` form. Postgres documents these as separate
   key spaces. This asserts it rather than trusting the sentence, because a
   collision would silently disable either the relay or every sweep.

Skipped when no Postgres is reachable, so the unit suite stays hermetic.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

pytestmark = pytest.mark.integration

DSN = "postgresql://runsheet:runsheet@localhost:5432/runsheet"

psycopg = pytest.importorskip("psycopg")


async def _connect():
    return await psycopg.AsyncConnection.connect(DSN, autocommit=True)


@pytest.fixture
async def pg():
    """A live connection, or skip."""
    try:
        conn = await asyncio.wait_for(_connect(), timeout=3.0)
    except Exception as exc:  # noqa: BLE001 — no server, no test
        pytest.skip(f"Postgres not reachable at {DSN}: {exc}")
    try:
        yield conn
    finally:
        with contextlib.suppress(Exception):
            await conn.close()


async def _scalar(conn, sql, params=None):
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return row[0]


class _RealSession:
    """Adapts a psycopg connection to the ``session.execute(text(...), params)``
    shape ``RoleLock`` expects, so the production code under test is exercised
    unchanged against a real backend.
    """

    def __init__(self, conn):
        self._conn = conn

    async def execute(self, statement, params=None):
        sql = str(statement)
        # SQLAlchemy named binds -> psycopg named binds.
        sql = sql.replace(":classid", "%(classid)s").replace(":objid", "%(objid)s")
        sql = sql.replace(":key", "%(key)s")
        cur = await self._conn.execute(sql, params or {})
        row = await cur.fetchone()

        class _Result:
            def scalar(self_inner):
                return row[0] if row else None

        return _Result()

    async def rollback(self):
        return None


# ---------------------------------------------------------------------------
# 1. Two contenders, one leader
# ---------------------------------------------------------------------------


class TestOnlyOneLeader:
    @pytest.mark.asyncio
    async def test_the_second_contender_is_refused(self, pg):
        from persistence.leader_election import RoleLock

        role = "test.leader-election.exclusive"
        a_conn = await _connect()
        b_conn = await _connect()
        try:
            lock_a = RoleLock(role)
            lock_b = RoleLock(role)

            assert await lock_a.acquire(_RealSession(a_conn)) is True
            assert await lock_b.acquire(_RealSession(b_conn)) is False, (
                "two processes both took the sweep lock — every periodic job "
                "would run twice"
            )
        finally:
            for c in (a_conn, b_conn):
                with contextlib.suppress(Exception):
                    await c.close()

    @pytest.mark.asyncio
    async def test_three_contenders_yield_exactly_one_holder(self, pg):
        from persistence.leader_election import RoleLock

        role = "test.leader-election.three-way"
        conns = [await _connect() for _ in range(3)]
        try:
            granted = []
            for conn in conns:
                lock = RoleLock(role)
                granted.append(await lock.acquire(_RealSession(conn)))
            assert sum(granted) == 1, granted
        finally:
            for c in conns:
                with contextlib.suppress(Exception):
                    await c.close()


# ---------------------------------------------------------------------------
# 2. Failover: killing the holder hands leadership over
# ---------------------------------------------------------------------------


class TestFailoverHandsOver:
    @pytest.mark.asyncio
    async def test_terminating_the_holder_releases_the_lock(self, pg):
        """No lease renewal, no stale-lock TTL: the lock dies with the backend."""
        from persistence.leader_election import RoleLock

        role = "test.leader-election.failover"
        holder = await _connect()
        standby = await _connect()
        try:
            lock_holder = RoleLock(role)
            lock_standby = RoleLock(role)

            assert await lock_holder.acquire(_RealSession(holder)) is True
            assert await lock_standby.acquire(_RealSession(standby)) is False

            holder_pid = await _scalar(holder, "SELECT pg_backend_pid()")
            assert lock_holder._pid == holder_pid

            await _scalar(pg, "SELECT pg_terminate_backend(%s)", (holder_pid,))
            await asyncio.sleep(0.4)

            assert await lock_standby.acquire(_RealSession(standby)) is True, (
                "the standby could not take over after the leader was killed — "
                "a crashed leader would wedge every periodic job"
            )
        finally:
            for c in (holder, standby):
                with contextlib.suppress(Exception):
                    await c.close()

    @pytest.mark.asyncio
    async def test_the_killed_holder_notices_it_lost_the_lock(self, pg):
        """The startup-only check this replaces would have kept running here."""
        from persistence.leader_election import RoleLock

        role = "test.leader-election.notices-loss"
        holder = await _connect()
        try:
            lock = RoleLock(role)
            session = _RealSession(holder)
            assert await lock.acquire(session) is True

            pid = await _scalar(holder, "SELECT pg_backend_pid()")
            await _scalar(pg, "SELECT pg_terminate_backend(%s)", (pid,))
            await asyncio.sleep(0.3)

            assert await lock.still_held(session) is False, (
                "the leader still believed it held the lock after its backend "
                "was killed"
            )
        finally:
            with contextlib.suppress(Exception):
                await holder.close()


# ---------------------------------------------------------------------------
# 3. Key-space separation from the outbox relay
# ---------------------------------------------------------------------------


class TestKeySpaceSeparationFromTheRelay:
    @pytest.mark.asyncio
    async def test_the_relay_key_and_a_role_lock_do_not_collide(self, pg):
        """Deliberately uses key bytes that WOULD collide if the spaces were one.

        The relay's 64-bit key is ``0x52554E534845544F``; the pair chosen here is
        ``(0x52554E53, 0x4845544F)`` — the same bytes, split. Both must be
        grantable at once, on different sessions.
        """
        from persistence.outbox_relay import _RELAY_ADVISORY_LOCK_KEY

        relay_conn = await _connect()
        sweep_conn = await _connect()
        try:
            got_relay = await _scalar(
                relay_conn,
                "SELECT pg_try_advisory_lock(%s)",
                (_RELAY_ADVISORY_LOCK_KEY,),
            )
            high = (_RELAY_ADVISORY_LOCK_KEY >> 32) & 0xFFFFFFFF
            low = _RELAY_ADVISORY_LOCK_KEY & 0xFFFFFFFF
            got_sweep = await _scalar(
                sweep_conn,
                "SELECT pg_try_advisory_lock(%s, %s)",
                (high, low),
            )

            assert got_relay is True
            assert got_sweep is True, (
                "the two-argument advisory-lock space overlaps the one-argument "
                "space on this server, so a role lock can collide with the "
                "outbox relay's key"
            )
        finally:
            for c in (relay_conn, sweep_conn):
                with contextlib.suppress(Exception):
                    await c.close()

    @pytest.mark.asyncio
    async def test_the_real_sweeps_role_does_not_collide_with_the_relay(self, pg):
        from persistence.leader_election import SWEEPS_ROLE, RoleLock
        from persistence.outbox_relay import _RELAY_ADVISORY_LOCK_KEY

        relay_conn = await _connect()
        sweep_conn = await _connect()
        try:
            assert (
                await _scalar(
                    relay_conn,
                    "SELECT pg_try_advisory_lock(%s)",
                    (_RELAY_ADVISORY_LOCK_KEY,),
                )
                is True
            )
            lock = RoleLock(SWEEPS_ROLE)
            assert await lock.acquire(_RealSession(sweep_conn)) is True
        finally:
            for c in (relay_conn, sweep_conn):
                with contextlib.suppress(Exception):
                    await c.close()


# ---------------------------------------------------------------------------
# 4. End to end: two SweepLeaders, one runs the jobs
# ---------------------------------------------------------------------------


class TestTwoSweepLeadersEndToEnd:
    @pytest.mark.asyncio
    async def test_only_one_of_two_leaders_runs_cycles(self, pg):
        """The whole point, exercised through the public API.

        Two ``SweepLeader`` instances against the same real Postgres, each with a
        ``run_periodic`` loop pointed at its own counter. Exactly one counter
        moves.
        """
        from persistence.leader_election import SweepLeader, run_periodic

        role = "test.leader-election.end-to-end"

        leaders = [
            SweepLeader(role=role, verify_interval_seconds=0.05),
            SweepLeader(role=role, verify_interval_seconds=0.05),
        ]
        conns = []
        counters = [[], []]
        tasks = []

        def _scope_for(conn):
            @contextlib.asynccontextmanager
            async def _scope():
                yield _RealSession(conn)

            return _scope

        try:
            import persistence.leader_election as le

            for index, leader in enumerate(leaders):
                conn = await _connect()
                conns.append(conn)
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr(
                        "persistence.database.is_persistence_enabled",
                        lambda: True,
                    )
                    mp.setattr(
                        "persistence.database.session_scope", _scope_for(conn)
                    )
                    await leader.start()
                    # Let the election tick inside the patch window.
                    await asyncio.sleep(0.2)

            elected = [leader.is_leader for leader in leaders]
            assert sum(elected) == 1, (
                f"expected exactly one leader, got {elected}"
            )

            # Now drive a periodic job per instance and confirm only the leader's
            # cycle runs. ``run_periodic`` reads the process-wide leader, so point
            # it at each instance in turn.
            for index, leader in enumerate(leaders):
                original = le.get_sweep_leader()
                le.set_sweep_leader(leader)
                try:
                    counter = counters[index]

                    async def cycle(c=counter):
                        c.append(1)

                    task = asyncio.create_task(
                        run_periodic(f"{role}.{index}", 0.02, cycle,
                                     run_immediately=True)
                    )
                    tasks.append(task)
                    await asyncio.sleep(0.1)
                finally:
                    le.set_sweep_leader(original)

            ran = [len(c) > 0 for c in counters]
            assert ran == elected, (
                f"cycles ran on {ran} but leadership was {elected} — a follower "
                "ran periodic work"
            )
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for leader in leaders:
                with contextlib.suppress(Exception):
                    await leader.stop()
            for c in conns:
                with contextlib.suppress(Exception):
                    await c.close()
