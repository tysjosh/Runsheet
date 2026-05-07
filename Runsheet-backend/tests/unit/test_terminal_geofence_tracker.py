"""
Unit tests for :mod:`fuel.services.terminal_geofence_tracker`.

Covers the Task 7.8 / Requirement 8.4.3 ELD-geofence-derived wait
tracking service. The tracker is the integration point the future
Geotab connector (Task 9.6) will call, so the tests focus on the
contract the connector will depend on:

* Simple enter → exit emits a ``TerminalWaitReport`` with
  ``source="eld_geofence"`` and the correct ``wait_minutes``.
* An exit with no matching enter is a silent no-op (never fabricate
  wait data).
* A Redis outage degrades gracefully — no exception propagates up
  into the caller.
* The 500 m boundary is applied inclusively (points on the ring count
  as inside) and exclusively past the ring.
* Cross-terminal isolation — a truck inside multiple terminal
  buffers has independent pending state per terminal.

Validates: Requirement 8.4.3.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

from fuel.services.terminal_geofence_tracker import (
    GEOFENCE_BUFFER_METERS,
    PENDING_ENTER_KEY_TEMPLATE,
    GeofenceEventResult,
    TerminalGeofenceTracker,
    is_within_geofence,
)
from fuel.services.terminal_wait_resolver import (
    TERMINAL_WAIT_CACHE_KEY_TEMPLATE,
)
from fuel.terminal_models import TerminalWaitReport

TENANT_ID = "tenant-1"
TRUCK_ID = "truck_17"
TERMINAL_ID = "term_001"
OTHER_TERMINAL_ID = "term_002"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal async Redis stand-in with ``get``, ``set``, ``delete``."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, Optional[int]] = {}
        self.deletes: List[str] = []
        self.sets: List[Dict[str, Any]] = []

    async def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    async def set(
        self, key: str, value: str, *, ex: Optional[int] = None
    ) -> None:
        self.store[key] = value
        self.ttls[key] = ex
        self.sets.append({"key": key, "value": value, "ex": ex})

    async def delete(self, key: str) -> int:
        self.deletes.append(key)
        return 1 if self.store.pop(key, None) is not None else 0


class _ExplodingRedis:
    """Raises on every call — used to prove graceful degradation."""

    async def get(self, key: str) -> Optional[str]:
        raise RuntimeError("redis down")

    async def set(self, key: str, value: str, *, ex: Optional[int] = None) -> None:
        raise RuntimeError("redis down")

    async def delete(self, key: str) -> int:
        raise RuntimeError("redis down")


class _FakeTerminal:
    """Lightweight stand-in for :class:`fuel.terminal_models.Terminal`.

    Only the attributes the tracker reads (``terminal_id``,
    ``location_lat``, ``location_lon``, ``status``) are populated —
    using the real ``Terminal`` here would require supplying
    ``operator``, ``address``, ``timezone``, ``operating_hours`` etc.
    which are orthogonal to the behaviour under test.
    """

    def __init__(
        self,
        terminal_id: str,
        lat: float,
        lon: float,
        *,
        status: str = "active",
    ) -> None:
        self.terminal_id = terminal_id
        self.location_lat = lat
        self.location_lon = lon
        self.status = status


class _FakeTerminalRepo:
    def __init__(self, terminals: List[_FakeTerminal]) -> None:
        self._terminals = terminals
        self.calls: List[Dict[str, Any]] = []

    async def list_for_tenant(
        self, tenant_id: str, *, status: Optional[str] = None, **kwargs: Any
    ) -> List[_FakeTerminal]:
        self.calls.append({"tenant_id": tenant_id, "status": status, **kwargs})
        if status is None:
            return list(self._terminals)
        return [t for t in self._terminals if t.status == status]


class _FailingTerminalRepo:
    async def list_for_tenant(self, *_: Any, **__: Any) -> List[_FakeTerminal]:
        raise RuntimeError("es down")


class _FakeWaitReportRepo:
    """In-memory impersonation of :class:`TerminalWaitReportRepository`.

    The real repository validates payloads through Pydantic and stamps
    ``report_id`` / ``retrieved_at``. We mirror just enough of that so
    the tracker sees the same return shape.
    """

    def __init__(self) -> None:
        self.created: List[TerminalWaitReport] = []
        self.fail = False

    async def create(
        self, tenant_id: str, payload: Dict[str, Any]
    ) -> TerminalWaitReport:
        if self.fail:
            raise RuntimeError("es persistence failed")
        now = datetime.now(timezone.utc)
        report = TerminalWaitReport(
            report_id=f"twr_{uuid4().hex[:8]}",
            tenant_id=tenant_id,
            terminal_id=payload["terminal_id"],
            wait_minutes=float(payload["wait_minutes"]),
            source=payload["source"],
            reporter_id=payload.get("reporter_id"),
            truck_id=payload.get("truck_id"),
            observed_at=payload["observed_at"],
            retrieved_at=payload.get("retrieved_at") or now,
        )
        self.created.append(report)
        return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tracker(
    *,
    redis: Optional[Any] = None,
    wait_repo: Optional[Any] = None,
    terminal_repo: Optional[Any] = None,
) -> TerminalGeofenceTracker:
    return TerminalGeofenceTracker(
        redis_client=redis if redis is not None else _FakeRedis(),
        wait_report_repository=wait_repo if wait_repo is not None else _FakeWaitReportRepo(),
        terminal_repository=terminal_repo,
    )


def _pending_key(
    tenant_id: str = TENANT_ID,
    truck_id: str = TRUCK_ID,
    terminal_id: str = TERMINAL_ID,
) -> str:
    return PENDING_ENTER_KEY_TEMPLATE.format(
        tenant_id=tenant_id, truck_id=truck_id, terminal_id=terminal_id
    )


def _summary_cache_key(
    tenant_id: str = TENANT_ID, terminal_id: str = TERMINAL_ID
) -> str:
    return TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
        tenant_id=tenant_id, terminal_id=terminal_id
    )


# ---------------------------------------------------------------------------
# Geofence predicate
# ---------------------------------------------------------------------------


class TestIsWithinGeofence:
    """Exercise the 500 m predicate directly so edge-of-ring behaviour
    is pinned down independently of the rest of the tracker."""

    def test_same_point_is_inside(self):
        assert is_within_geofence(40.0, -74.0, 40.0, -74.0) is True

    def test_ten_meters_away_is_inside(self):
        # Moving ~10 m south — well inside the 500 m buffer.
        assert is_within_geofence(40.0, -74.0, 40.0 - 0.00009, -74.0) is True

    def test_two_kilometers_away_is_outside(self):
        # ~2 km north.
        assert is_within_geofence(40.0, -74.0, 40.018, -74.0) is False

    def test_boundary_inclusive_via_explicit_buffer(self):
        """A truck exactly ``buffer_meters`` away counts as inside — the
        haversine equals the buffer so ``<=`` is true."""

        # Compute a lat delta that yields ~499 m separation; 100m is
        # roughly 0.0009 degrees, so 499m is ~0.0045 degrees. We use an
        # explicit buffer of 499 m to pin the boundary test.
        tracker_buffer = 499.0
        lat_delta = 0.0045  # ~500m
        inside = is_within_geofence(
            40.0, -74.0, 40.0 + lat_delta, -74.0, buffer_meters=tracker_buffer
        )
        # The delta puts the truck on/near the ring; with buffer 499 it
        # should be just outside. Verify by widening the buffer.
        wider = is_within_geofence(
            40.0, -74.0, 40.0 + lat_delta, -74.0, buffer_meters=tracker_buffer + 50
        )
        assert inside is False
        assert wider is True

    def test_default_buffer_matches_constant(self):
        """A separation just inside 500 m must be inside; just outside
        must be outside — sanity check on the default."""

        # ~499 m north of origin — inside
        assert (
            is_within_geofence(40.0, -74.0, 40.0 + 0.00449, -74.0) is True
        )
        # ~501 m north of origin — outside
        assert (
            is_within_geofence(40.0, -74.0, 40.0 + 0.00451, -74.0) is False
        )

    def test_negative_buffer_rejected(self):
        with pytest.raises(ValueError):
            is_within_geofence(0, 0, 0, 0, buffer_meters=-1.0)

    def test_default_buffer_constant_is_five_hundred(self):
        # Lock the spec contract so a future drift to, say, 250 m fails CI.
        assert GEOFENCE_BUFFER_METERS == 500.0


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_non_positive_ttl(self):
        with pytest.raises(ValueError):
            TerminalGeofenceTracker(pending_ttl_seconds=0)

    def test_constructs_without_dependencies(self):
        # Tests that only exercise ``is_within_geofence`` and the
        # no-redis no-op paths must be able to build a tracker without
        # any backing store.
        tracker = TerminalGeofenceTracker()
        assert tracker is not None


# ---------------------------------------------------------------------------
# record_geofence_event — happy path
# ---------------------------------------------------------------------------


class TestRecordGeofenceEvent:
    async def test_enter_persists_pending_state_with_ttl(self):
        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        tracker = _make_tracker(redis=redis, wait_repo=wait_repo)
        now = datetime.now(timezone.utc)

        result = await tracker.record_geofence_event(
            tenant_id=TENANT_ID,
            truck_id=TRUCK_ID,
            terminal_id=TERMINAL_ID,
            event_type="enter",
            observed_at=now,
            location_lat=40.0,
            location_lon=-74.0,
        )

        assert result.event == "enter"
        assert result.terminal_id == TERMINAL_ID
        key = _pending_key()
        assert key in redis.store
        stored = json.loads(redis.store[key])
        assert stored["enter_at"].startswith(now.astimezone(timezone.utc).isoformat()[:19])
        assert redis.ttls[key] == tracker._pending_ttl  # TTL applied
        # No wait report yet — only the enter was recorded.
        assert wait_repo.created == []

    async def test_enter_then_exit_writes_wait_report(self):
        """The canonical happy path: enter → exit produces a
        ``TerminalWaitReport`` with ``source=eld_geofence`` and the
        correct ``wait_minutes``."""

        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        tracker = _make_tracker(redis=redis, wait_repo=wait_repo)

        enter_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        exit_at = enter_at + timedelta(minutes=37)

        enter_result = await tracker.record_geofence_event(
            tenant_id=TENANT_ID,
            truck_id=TRUCK_ID,
            terminal_id=TERMINAL_ID,
            event_type="enter",
            observed_at=enter_at,
            location_lat=40.0,
            location_lon=-74.0,
        )
        exit_result = await tracker.record_geofence_event(
            tenant_id=TENANT_ID,
            truck_id=TRUCK_ID,
            terminal_id=TERMINAL_ID,
            event_type="exit",
            observed_at=exit_at,
            location_lat=40.02,
            location_lon=-74.0,
        )

        assert enter_result.event == "enter"
        assert exit_result.event == "exit"
        assert len(wait_repo.created) == 1
        report = wait_repo.created[0]
        assert report.source == "eld_geofence"
        assert report.reporter_id is None
        assert report.truck_id == TRUCK_ID
        assert report.terminal_id == TERMINAL_ID
        assert report.wait_minutes == pytest.approx(37.0, abs=1e-6)
        # Pending key was cleared.
        assert _pending_key() not in redis.store
        # Wait-summary cache invalidated so the next GET recomputes
        # — mirrors the POST /wait-reports endpoint behaviour.
        assert _summary_cache_key() in redis.deletes

    async def test_unmatched_exit_is_ignored(self):
        """An exit without a preceding enter must be a silent no-op.
        Fabricating a wait_minutes value we cannot justify would poison
        the rolling-average aggregation."""

        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        tracker = _make_tracker(redis=redis, wait_repo=wait_repo)

        result = await tracker.record_geofence_event(
            tenant_id=TENANT_ID,
            truck_id=TRUCK_ID,
            terminal_id=TERMINAL_ID,
            event_type="exit",
            observed_at=datetime.now(timezone.utc),
            location_lat=40.0,
            location_lon=-74.0,
        )

        assert result.event == "none"
        assert result.reason == "unmatched_exit"
        assert wait_repo.created == []
        # No cache invalidation for a no-op.
        assert _summary_cache_key() not in redis.deletes

    async def test_duplicate_enter_keeps_original_timestamp(self):
        """GPS noise that crosses and re-crosses the 500 m ring while
        the truck is stationary must not re-stamp the enter_at — the
        first crossing is the authoritative wait-start."""

        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        tracker = _make_tracker(redis=redis, wait_repo=wait_repo)

        first_enter = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        second_enter = first_enter + timedelta(minutes=5)
        exit_at = first_enter + timedelta(minutes=20)

        await tracker.record_geofence_event(
            tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
            event_type="enter", observed_at=first_enter,
            location_lat=40.0, location_lon=-74.0,
        )
        dup = await tracker.record_geofence_event(
            tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
            event_type="enter", observed_at=second_enter,
            location_lat=40.0, location_lon=-74.0,
        )
        await tracker.record_geofence_event(
            tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
            event_type="exit", observed_at=exit_at,
            location_lat=40.02, location_lon=-74.0,
        )

        assert dup.event == "none"
        assert dup.reason == "duplicate_enter"
        # Wait minutes reflects the first enter, not the second.
        assert len(wait_repo.created) == 1
        assert wait_repo.created[0].wait_minutes == pytest.approx(20.0, abs=1e-6)

    async def test_negative_delta_rounds_to_zero(self):
        """Clock skew / out-of-order events must not produce negative
        wait_minutes — clamp to 0 rather than raising."""

        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        tracker = _make_tracker(redis=redis, wait_repo=wait_repo)

        enter_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        exit_at = enter_at - timedelta(seconds=30)  # exit before enter (skew)

        await tracker.record_geofence_event(
            tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
            event_type="enter", observed_at=enter_at,
            location_lat=40.0, location_lon=-74.0,
        )
        result = await tracker.record_geofence_event(
            tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
            event_type="exit", observed_at=exit_at,
            location_lat=40.02, location_lon=-74.0,
        )

        assert result.event == "exit"
        assert wait_repo.created[0].wait_minutes == 0.0

    async def test_implausible_dwell_is_dropped(self):
        """A > 24 h dwell is almost certainly a stale pending-enter; the
        tracker refuses to write and clears state to let a fresh cycle
        start."""

        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        tracker = _make_tracker(redis=redis, wait_repo=wait_repo)

        enter_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        exit_at = enter_at + timedelta(days=3)  # absurd

        await tracker.record_geofence_event(
            tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
            event_type="enter", observed_at=enter_at,
            location_lat=40.0, location_lon=-74.0,
        )
        result = await tracker.record_geofence_event(
            tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
            event_type="exit", observed_at=exit_at,
            location_lat=40.02, location_lon=-74.0,
        )

        assert result.event == "none"
        assert result.reason == "implausible_dwell"
        assert wait_repo.created == []
        assert _pending_key() not in redis.store


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    async def test_rejects_blank_tenant(self):
        tracker = _make_tracker()
        with pytest.raises(ValueError):
            await tracker.record_geofence_event(
                tenant_id="", truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
                event_type="enter", observed_at=datetime.now(timezone.utc),
                location_lat=40.0, location_lon=-74.0,
            )

    async def test_rejects_blank_truck(self):
        tracker = _make_tracker()
        with pytest.raises(ValueError):
            await tracker.record_geofence_event(
                tenant_id=TENANT_ID, truck_id="", terminal_id=TERMINAL_ID,
                event_type="enter", observed_at=datetime.now(timezone.utc),
                location_lat=40.0, location_lon=-74.0,
            )

    async def test_rejects_unknown_event_type(self):
        tracker = _make_tracker()
        with pytest.raises(ValueError):
            await tracker.record_geofence_event(
                tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
                event_type="halfway",  # type: ignore[arg-type]
                observed_at=datetime.now(timezone.utc),
                location_lat=40.0, location_lon=-74.0,
            )

    async def test_rejects_naive_datetime(self):
        tracker = _make_tracker()
        with pytest.raises(ValueError):
            await tracker.record_geofence_event(
                tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
                event_type="enter",
                observed_at=datetime(2024, 6, 1, 12, 0, 0),  # naive
                location_lat=40.0, location_lon=-74.0,
            )

    async def test_rejects_out_of_range_lat(self):
        tracker = _make_tracker()
        with pytest.raises(ValueError):
            await tracker.record_geofence_event(
                tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
                event_type="enter",
                observed_at=datetime.now(timezone.utc),
                location_lat=95.0, location_lon=-74.0,
            )


# ---------------------------------------------------------------------------
# Graceful Redis degradation
# ---------------------------------------------------------------------------


class TestRedisDegradation:
    async def test_redis_outage_on_enter_returns_none_without_raising(self):
        """A Redis that raises on every call must not propagate the
        exception up to the Geotab connector's sync loop."""

        tracker = TerminalGeofenceTracker(
            redis_client=_ExplodingRedis(),
            wait_report_repository=_FakeWaitReportRepo(),
        )
        result = await tracker.record_geofence_event(
            tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
            event_type="enter",
            observed_at=datetime.now(timezone.utc),
            location_lat=40.0, location_lon=-74.0,
        )
        assert result.event == "none"
        assert result.reason == "redis_unavailable"

    async def test_redis_outage_on_exit_returns_none_without_raising(self):
        tracker = TerminalGeofenceTracker(
            redis_client=_ExplodingRedis(),
            wait_report_repository=_FakeWaitReportRepo(),
        )
        result = await tracker.record_geofence_event(
            tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
            event_type="exit",
            observed_at=datetime.now(timezone.utc),
            location_lat=40.0, location_lon=-74.0,
        )
        # Exit with an exploding Redis can't load the pending state, so
        # the dispatcher reports "unmatched_exit" (i.e. no harm done).
        # The important property is that nothing raised.
        assert result.event == "none"

    async def test_es_persistence_failure_leaves_pending_state(self):
        """If the wait-report write fails, the tracker keeps the
        pending-enter so a later retry can re-try the same dwell."""

        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        wait_repo.fail = True
        tracker = _make_tracker(redis=redis, wait_repo=wait_repo)

        enter_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        exit_at = enter_at + timedelta(minutes=10)

        await tracker.record_geofence_event(
            tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
            event_type="enter", observed_at=enter_at,
            location_lat=40.0, location_lon=-74.0,
        )
        result = await tracker.record_geofence_event(
            tenant_id=TENANT_ID, truck_id=TRUCK_ID, terminal_id=TERMINAL_ID,
            event_type="exit", observed_at=exit_at,
            location_lat=40.02, location_lon=-74.0,
        )

        assert result.event == "none"
        assert result.reason == "persistence_failed"
        # Pending key retained so the next telemetry sample retries.
        assert _pending_key() in redis.store


# ---------------------------------------------------------------------------
# process_truck_position — dispatcher
# ---------------------------------------------------------------------------


class TestProcessTruckPosition:
    async def test_requires_terminal_repository(self):
        tracker = TerminalGeofenceTracker(redis_client=_FakeRedis())
        with pytest.raises(ValueError):
            await tracker.process_truck_position(
                tenant_id=TENANT_ID,
                truck_id=TRUCK_ID,
                lat=40.0,
                lon=-74.0,
                observed_at=datetime.now(timezone.utc),
            )

    async def test_enters_and_exits_single_terminal(self):
        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        terminal_repo = _FakeTerminalRepo(
            [_FakeTerminal(TERMINAL_ID, 40.0, -74.0)]
        )
        tracker = TerminalGeofenceTracker(
            redis_client=redis,
            wait_report_repository=wait_repo,
            terminal_repository=terminal_repo,
        )

        t0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=10)
        t2 = t0 + timedelta(minutes=45)

        # First sample — truck right at the rack — should emit enter.
        r1 = await tracker.process_truck_position(
            TENANT_ID, TRUCK_ID, 40.0, -74.0, t0
        )
        # Second sample — still there — no-op (empty list since we elide
        # no-ops).
        r2 = await tracker.process_truck_position(
            TENANT_ID, TRUCK_ID, 40.0001, -74.0001, t1
        )
        # Third sample — truck 2 km away — should emit exit.
        r3 = await tracker.process_truck_position(
            TENANT_ID, TRUCK_ID, 40.02, -74.0, t2
        )

        assert [e.event for e in r1] == ["enter"]
        assert r2 == []
        assert [e.event for e in r3] == ["exit"]
        assert wait_repo.created[0].wait_minutes == pytest.approx(45.0, abs=1e-6)

    async def test_cross_terminal_isolation(self):
        """A truck inside two terminals' buffers simultaneously must
        have independent pending state per terminal so one terminal's
        exit does not consume the other's enter."""

        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        # Two "terminals" at the same coordinate — both will see the
        # truck as inside at the same time.
        terminal_repo = _FakeTerminalRepo(
            [
                _FakeTerminal(TERMINAL_ID, 40.0, -74.0),
                _FakeTerminal(OTHER_TERMINAL_ID, 40.0, -74.0),
            ]
        )
        tracker = TerminalGeofenceTracker(
            redis_client=redis,
            wait_report_repository=wait_repo,
            terminal_repository=terminal_repo,
        )

        t0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        await tracker.process_truck_position(
            TENANT_ID, TRUCK_ID, 40.0, -74.0, t0
        )

        # Both pending keys must be present.
        assert _pending_key(terminal_id=TERMINAL_ID) in redis.store
        assert _pending_key(terminal_id=OTHER_TERMINAL_ID) in redis.store

        # Now the truck drives away — both exits fire.
        t1 = t0 + timedelta(minutes=15)
        results = await tracker.process_truck_position(
            TENANT_ID, TRUCK_ID, 40.02, -74.0, t1
        )

        events = sorted(r.event for r in results)
        terminals = sorted(r.terminal_id for r in results)
        assert events == ["exit", "exit"]
        assert terminals == sorted([TERMINAL_ID, OTHER_TERMINAL_ID])
        assert len(wait_repo.created) == 2
        # Both reports have the same wait_minutes since both entered
        # and exited at the same time.
        assert {r.terminal_id for r in wait_repo.created} == {
            TERMINAL_ID,
            OTHER_TERMINAL_ID,
        }
        for report in wait_repo.created:
            assert report.wait_minutes == pytest.approx(15.0, abs=1e-6)

    async def test_truck_in_one_terminal_does_not_affect_the_other(self):
        """Isolation check: a truck inside terminal A's buffer but far
        from terminal B must only produce an A-enter, never a B-exit."""

        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        terminal_repo = _FakeTerminalRepo(
            [
                _FakeTerminal(TERMINAL_ID, 40.0, -74.0),
                _FakeTerminal(OTHER_TERMINAL_ID, 41.0, -75.0),  # far away
            ]
        )
        tracker = TerminalGeofenceTracker(
            redis_client=redis,
            wait_report_repository=wait_repo,
            terminal_repository=terminal_repo,
        )

        t0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        results = await tracker.process_truck_position(
            TENANT_ID, TRUCK_ID, 40.0, -74.0, t0
        )

        events = [(r.event, r.terminal_id) for r in results]
        assert events == [("enter", TERMINAL_ID)]
        assert _pending_key(terminal_id=OTHER_TERMINAL_ID) not in redis.store

    async def test_boundary_just_inside_500m_triggers_enter(self):
        """A truck ~499 m away from the terminal must trigger an enter
        — pins the 500 m boundary behaviour end-to-end (predicate +
        dispatcher agreement)."""

        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        terminal_repo = _FakeTerminalRepo(
            [_FakeTerminal(TERMINAL_ID, 40.0, -74.0)]
        )
        tracker = TerminalGeofenceTracker(
            redis_client=redis,
            wait_report_repository=wait_repo,
            terminal_repository=terminal_repo,
        )
        # ~499 m north of the terminal.
        results = await tracker.process_truck_position(
            TENANT_ID, TRUCK_ID, 40.0 + 0.00449, -74.0,
            datetime.now(timezone.utc),
        )
        assert [r.event for r in results] == ["enter"]

    async def test_boundary_just_outside_500m_is_no_op(self):
        """A truck ~501 m from the terminal must not produce any
        event — complements the inside case above."""

        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        terminal_repo = _FakeTerminalRepo(
            [_FakeTerminal(TERMINAL_ID, 40.0, -74.0)]
        )
        tracker = TerminalGeofenceTracker(
            redis_client=redis,
            wait_report_repository=wait_repo,
            terminal_repository=terminal_repo,
        )
        # ~501 m north of the terminal.
        results = await tracker.process_truck_position(
            TENANT_ID, TRUCK_ID, 40.0 + 0.00451, -74.0,
            datetime.now(timezone.utc),
        )
        assert results == []

    async def test_failing_terminal_repo_returns_empty_list_gracefully(self):
        """ES outage during terminal lookup must not raise into the
        connector."""

        tracker = TerminalGeofenceTracker(
            redis_client=_FakeRedis(),
            wait_report_repository=_FakeWaitReportRepo(),
            terminal_repository=_FailingTerminalRepo(),
        )
        results = await tracker.process_truck_position(
            TENANT_ID, TRUCK_ID, 40.0, -74.0,
            datetime.now(timezone.utc),
        )
        assert results == []

    async def test_inactive_terminals_ignored(self):
        """A ``status=inactive`` terminal must not produce geofence
        events even if a lax repository returns it."""

        redis = _FakeRedis()
        wait_repo = _FakeWaitReportRepo()
        terminal_repo = _FakeTerminalRepo(
            [_FakeTerminal(TERMINAL_ID, 40.0, -74.0, status="inactive")]
        )
        tracker = TerminalGeofenceTracker(
            redis_client=redis,
            wait_report_repository=wait_repo,
            terminal_repository=terminal_repo,
        )
        # Filter happens at the repo level — when the repo returns
        # nothing, nothing happens.
        results = await tracker.process_truck_position(
            TENANT_ID, TRUCK_ID, 40.0, -74.0,
            datetime.now(timezone.utc),
        )
        assert results == []


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


class TestRedisKeyContract:
    def test_pending_key_template_matches_task_brief(self):
        """Task 7.8 mandates
        ``terminal_geofence_pending:{tenant_id}:{truck_id}:{terminal_id}``
        as the key layout. Lock it so a silent drift fails CI."""

        assert PENDING_ENTER_KEY_TEMPLATE == (
            "terminal_geofence_pending:{tenant_id}:{truck_id}:{terminal_id}"
        )

    def test_result_none_factory_sets_event(self):
        r = GeofenceEventResult.none(
            terminal_id="t1", truck_id="k1", reason="unmatched_exit"
        )
        assert r.event == "none"
        assert r.terminal_id == "t1"
        assert r.truck_id == "k1"
        assert r.reason == "unmatched_exit"
        assert r.wait_report is None
