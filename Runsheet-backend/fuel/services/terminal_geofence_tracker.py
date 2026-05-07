"""
ELD-geofence-derived terminal wait tracking.

This module implements Task 7.8 of the fuel-ops-hardening spec: automatic
Terminal_Wait_Report generation from GPS / ELD telemetry. Requirement 8.4.3
mandates that "WHERE a Geotab or equivalent GPS/ELD integration is active,
THE Platform SHALL derive wait_minutes automatically from geofence
entry/exit events at the Terminal's location and SHALL persist them with
source ``eld_geofence``."

The Geotab connector itself (Task 9.6) is not yet implemented. This
module therefore exposes a **reusable service** that the future
connector can call without having to understand geofencing math,
pending-event bookkeeping, or the Redis-cache invalidation that keeps
the ``/wait-summary`` endpoint in sync. When Task 9.6 lands, its
``sync_pull`` loop only has to do one thing for each position sample:

    tracker = TerminalGeofenceTracker(
        redis_client=..., wait_report_repository=..., terminal_repository=...
    )
    await tracker.process_truck_position(
        tenant_id=tenant_id,
        truck_id=truck.truck_id,
        lat=telemetry.location_lat,
        lon=telemetry.location_lon,
        observed_at=telemetry.observed_at,
    )

Everything else — proximity calculation, enter/exit debouncing across
multiple nearby terminals, the ``terminal_wait_reports`` write, and the
``terminal_wait:{tenant_id}:{terminal_id}`` cache invalidation that
mirrors what ``POST /api/fuel/terminals/{id}/wait-reports`` does — is
handled by the tracker.

Design decisions
----------------

* **Reusable state is kept in Redis, not in process memory.** The
  Geotab connector runs on a schedule across process restarts and
  potentially across replicas. A "pending enter" event therefore must
  survive restarts and be visible to any replica that sees the matching
  "exit" position. Redis with an 8-hour TTL gives us both durability and
  an automatic garbage-collection mechanism for stuck enters (e.g. a
  truck that entered the 500 m buffer and then had its ELD fail — we
  don't want its enter event hanging around forever producing
  spurious 20-hour wait reports).

* **Graceful degradation everywhere.** A flaky Redis or a transient
  ES failure must never propagate up into the Geotab connector's sync
  loop and cause a retry storm. Every failure path logs a warning and
  returns a well-defined sentinel (``GeofenceEventResult.none``) so the
  connector can keep processing subsequent positions.

* **500 m buffer is explicit and reusable.** The :func:`is_within_geofence`
  helper takes explicit terminal / truck coordinates and returns a
  boolean. It's exported so diagnostic tooling and tests can invoke the
  exact same predicate used by the dispatcher.

* **One Redis key per (tenant, truck, terminal) triple.** Using the
  pending-enter key's presence as the source of truth for "are we
  currently inside this terminal's buffer?" means we don't need a
  second "currently-inside" structure — the enter event carries its
  own implicit state.

* **Cache invalidation matches the manual-submission path.** The
  ``POST /wait-reports`` endpoint deletes
  ``terminal_wait:{tenant_id}:{terminal_id}`` after each successful
  write so the next ``/wait-summary`` read recomputes from ES and
  picks up the new observation immediately. The tracker does the same
  on exit so an automatic report has the same freshness semantics as
  a driver-submitted one.

How Task 9.6 should call this service
-------------------------------------

The Geotab connector should:

1. Construct a single :class:`TerminalGeofenceTracker` at bootstrap,
   sharing the same async Redis client used by
   :mod:`fuel.api.fuel_ops_endpoints` (so the cache invalidation key
   matches) and the same
   :class:`fuel.terminal_models.TerminalWaitReportRepository` the REST
   endpoints use.

2. In its ``sync_pull`` loop, for each freshly-observed truck position
   (lat, lon, observed_at, truck_id, tenant_id), call
   :meth:`TerminalGeofenceTracker.process_truck_position`. The tracker
   resolves terminals once per call via the injected
   :class:`TerminalRepository`, so the connector does not need to pass
   a terminal list or understand which terminals exist.

3. Do **not** call :meth:`TerminalGeofenceTracker.record_geofence_event`
   directly — that method exists for tests and for future integrations
   that already know the (truck, terminal, event_type) tuple (e.g. a
   Geotab Zone exit event fired by the Geotab API itself).

The connector must **not** swallow or transform the result. All error
handling is done inside the tracker; the connector should simply log
and move on, exactly as it would for any other per-sample no-op.

Validates: Requirement 8.4.3.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, List, Literal, Optional

from driver.services.geo_utils import haversine_distance_meters
from fuel.services.terminal_wait_resolver import (
    TERMINAL_WAIT_CACHE_KEY_TEMPLATE,
)
from fuel.terminal_models import (
    Terminal,
    TerminalWaitReport,
    TerminalWaitReportRepository,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Buffer radius (in meters) around a terminal's point location that
#: defines the geofence. Requirement 8.4.3 does not specify a numeric
#: radius; 500 m is the Task 7.8 brief's default and matches common GPS
#: accuracy budgets (consumer GPS is reliable to ~5 m, enterprise ELD
#: hardware to ~3 m, so 500 m gives ample margin while still tightly
#: distinguishing "at the rack" from "driving past on the highway").
GEOFENCE_BUFFER_METERS: float = 500.0


#: Redis key template used to store the pending "enter" event for a
#: given (tenant, truck, terminal) triple. Kept here rather than imported
#: from elsewhere so the contract is visible at a glance and a casual
#: search for the key finds this module first.
PENDING_ENTER_KEY_TEMPLATE: str = (
    "terminal_geofence_pending:{tenant_id}:{truck_id}:{terminal_id}"
)


#: TTL (in seconds) applied to every pending-enter write. 8 hours is the
#: Task 7.8 brief's default and conservatively outlives any legitimate
#: terminal dwell (a truck that sits at a rack for 8 hours has a
#: separate problem that operations will notice independently). When a
#: pending enter times out, the next matching exit event simply
#: performs a no-op (``_load_pending_enter`` returns ``None``), which is
#: the correct failure mode — we never emit a fabricated wait report.
PENDING_ENTER_TTL_SECONDS: int = 8 * 60 * 60


#: Reject dwells longer than this number of minutes. Complements the
#: Redis TTL: even if the TTL is overridden in tests to be very long,
#: the tracker refuses to write an absurd wait_minutes value so a
#: bug in clock handling cannot produce nonsense data.
MAX_DWELL_MINUTES: float = 24 * 60  # 24 hours


#: ``Literal`` re-export so callers don't have to import it themselves.
GeofenceEventType = Literal["enter", "exit"]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeofenceEventResult:
    """The outcome of a single geofence event being processed.

    The tracker returns one of these so the caller (Geotab connector,
    tests, diagnostic tooling) can observe what the dispatcher decided
    without having to read Redis or ES to find out. The ``event``
    attribute is the *effective* event type after debouncing:

        * ``enter`` — the tracker just persisted a pending enter.
        * ``exit``  — the tracker just persisted a wait report and
                       cleared the pending enter.
        * ``none``  — the event was a no-op (duplicate enter,
                       unmatched exit, or a fallback when Redis was
                       unreachable).

    ``wait_report`` is populated only on a successful ``exit`` and
    carries the newly-created :class:`TerminalWaitReport` so tests can
    assert the computed wait_minutes without a second round-trip to ES.
    """

    event: Literal["enter", "exit", "none"]
    terminal_id: str
    truck_id: str
    reason: Optional[str] = None
    wait_report: Optional[TerminalWaitReport] = None

    @classmethod
    def none(
        cls, terminal_id: str, truck_id: str, reason: str
    ) -> "GeofenceEventResult":
        return cls(
            event="none",
            terminal_id=terminal_id,
            truck_id=truck_id,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Geofence predicate
# ---------------------------------------------------------------------------


def is_within_geofence(
    terminal_lat: float,
    terminal_lon: float,
    truck_lat: float,
    truck_lon: float,
    buffer_meters: float = GEOFENCE_BUFFER_METERS,
) -> bool:
    """Return ``True`` iff the truck is within ``buffer_meters`` of the terminal.

    Uses :func:`driver.services.geo_utils.haversine_distance_meters` so
    every proximity check in the fuel stack shares one formula. The
    predicate is inclusive of the boundary so a truck whose GPS puts it
    exactly on the 500 m ring is considered inside — mirrors how the
    sourcing / traffic code treats its own distance thresholds.

    Args:
        terminal_lat / terminal_lon: Terminal point location in decimal
            degrees. Assumed already validated by the :class:`Terminal`
            Pydantic model to be in the WGS-84 range.
        truck_lat / truck_lon: Truck position in decimal degrees. The
            caller is responsible for basic sanity checks; absurd
            values (e.g. ``lat=1_000``) will still return a boolean but
            the answer will reflect the haversine of those coordinates.
        buffer_meters: Override the default 500 m buffer. Exposed for
            tests that want to exercise boundary behaviour without
            having to juggle lat/lon coordinates that happen to sit on
            the 500 m ring.

    Returns:
        ``True`` when the haversine distance is less than or equal to
        ``buffer_meters``, else ``False``.
    """

    if buffer_meters < 0:
        raise ValueError("buffer_meters must be non-negative")
    distance = haversine_distance_meters(
        terminal_lat, terminal_lon, truck_lat, truck_lon
    )
    return distance <= buffer_meters


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class TerminalGeofenceTracker:
    """Translate truck positions into :class:`TerminalWaitReport` rows.

    The tracker is stateless in-process — all durable state is in Redis
    and ES — so it is safe to construct once at bootstrap and share
    across worker tasks / replicas.

    Args:
        redis_client: Async Redis client used to store pending-enter
            events and to invalidate the ``terminal_wait:...`` cache.
            Optional for unit tests; when ``None`` every enter/exit is
            a no-op (returns :meth:`GeofenceEventResult.none`). The
            connector should always supply a real client — without one
            the tracker cannot correlate an enter with a later exit.
        wait_report_repository: The same
            :class:`TerminalWaitReportRepository` used by the REST
            endpoints so automatic and manual reports land in the same
            index with identical schemas. Optional — tests may elide
            it, but a production deployment must supply one.
        terminal_repository: Optional :class:`TerminalRepository` used
            by :meth:`process_truck_position` to resolve the candidate
            terminal list for each position sample. When omitted
            (e.g. tests that only exercise :meth:`record_geofence_event`
            directly), :meth:`process_truck_position` raises.
        pending_ttl_seconds: Override the 8-hour pending-enter TTL.
            Primarily for tests that want to exercise the expiry path
            without waiting eight hours.

    Validates: Requirement 8.4.3.
    """

    def __init__(
        self,
        *,
        redis_client: Any = None,
        wait_report_repository: Optional[TerminalWaitReportRepository] = None,
        terminal_repository: Any = None,
        pending_ttl_seconds: int = PENDING_ENTER_TTL_SECONDS,
    ) -> None:
        if pending_ttl_seconds <= 0:
            raise ValueError("pending_ttl_seconds must be positive")
        self._redis = redis_client
        self._wait_repo = wait_report_repository
        self._terminal_repo = terminal_repository
        self._pending_ttl = int(pending_ttl_seconds)

    # ------------------------------------------------------------------
    # Public API — single event
    # ------------------------------------------------------------------

    async def record_geofence_event(
        self,
        tenant_id: str,
        truck_id: str,
        terminal_id: str,
        event_type: GeofenceEventType,
        observed_at: datetime,
        location_lat: float,
        location_lon: float,
    ) -> GeofenceEventResult:
        """Process a single ``enter`` / ``exit`` signal.

        Enter semantics:

            * Persists ``{"enter_at": observed_at, "location_lat": ...,
              "location_lon": ...}`` at
              ``terminal_geofence_pending:{tenant_id}:{truck_id}:{terminal_id}``
              with ``PENDING_ENTER_TTL_SECONDS`` TTL.
            * A duplicate enter (key already present) is ignored so a
              noisy GPS that crosses and re-crosses the 500 m ring
              while the truck is stationary doesn't re-stamp the
              enter timestamp — the first crossing is the wait-start.

        Exit semantics:

            * Reads the pending-enter payload. If missing, the exit is
              ignored (unmatched exits are a routine occurrence when the
              service restarts mid-dwell or when a truck was already
              inside the buffer the first time we heard about it).
            * Computes ``wait_minutes = max(0, (observed_at - enter_at)
              / 60)``. Negative deltas (clock skew) round to zero rather
              than raising.
            * Writes a :class:`TerminalWaitReport` through the repository
              with ``source="eld_geofence"``, ``reporter_id=None``,
              ``truck_id`` populated.
            * Clears the pending-enter key so subsequent events start
              fresh.
            * Invalidates ``terminal_wait:{tenant_id}:{terminal_id}`` so
              the very next ``/wait-summary`` read recomputes with the
              new observation (mirrors the manual-submission path).

        All failures — Redis outage, ES persistence error, invalid
        repository response — are caught, logged, and surfaced as a
        :class:`GeofenceEventResult` with ``event="none"``. This method
        never raises up to the caller.

        Args:
            tenant_id: Tenant that owns both the truck and the terminal.
                Both are validated to be the same tenant by virtue of
                the tenant-scoped repositories used downstream.
            truck_id: Truck whose position generated the event.
            terminal_id: Terminal whose buffer the truck crossed.
            event_type: Either ``"enter"`` or ``"exit"``.
            observed_at: When the boundary crossing actually happened.
                Must be timezone-aware; naive datetimes are rejected so
                the rolling-window aggregation is not silently corrupted
                by timezone ambiguity.
            location_lat / location_lon: Truck position at the moment of
                the event. Stored on the pending-enter payload so
                diagnostic tooling can reconstruct the entry point and
                also used to back-fill a ``TerminalWaitReport`` for any
                consumer that wants to know where the truck was when it
                first crossed the ring.

        Returns:
            A :class:`GeofenceEventResult` describing what happened.
        """

        if not _is_nonempty_str(tenant_id):
            raise ValueError("tenant_id must be a non-empty string")
        if not _is_nonempty_str(truck_id):
            raise ValueError("truck_id must be a non-empty string")
        if not _is_nonempty_str(terminal_id):
            raise ValueError("terminal_id must be a non-empty string")
        if event_type not in ("enter", "exit"):
            raise ValueError(
                f"event_type must be 'enter' or 'exit', got {event_type!r}"
            )
        if not isinstance(observed_at, datetime):
            raise TypeError(
                f"observed_at must be a datetime, got {type(observed_at).__name__}"
            )
        if observed_at.tzinfo is None:
            raise ValueError(
                "observed_at must be timezone-aware; naive datetimes are rejected"
            )
        _validate_coord(location_lat, location_lon)

        if event_type == "enter":
            return await self._handle_enter(
                tenant_id=tenant_id,
                truck_id=truck_id,
                terminal_id=terminal_id,
                observed_at=observed_at,
                location_lat=location_lat,
                location_lon=location_lon,
            )
        return await self._handle_exit(
            tenant_id=tenant_id,
            truck_id=truck_id,
            terminal_id=terminal_id,
            observed_at=observed_at,
            location_lat=location_lat,
            location_lon=location_lon,
        )

    # ------------------------------------------------------------------
    # Public API — dispatcher
    # ------------------------------------------------------------------

    async def process_truck_position(
        self,
        tenant_id: str,
        truck_id: str,
        lat: float,
        lon: float,
        observed_at: datetime,
        *,
        terminal_repository: Any = None,
    ) -> List[GeofenceEventResult]:
        """Evaluate a single truck position against every tenant terminal.

        This is the entry point the Geotab connector (Task 9.6) should
        call for each telemetry sample. The tracker does the rest:

            1. Resolves the tenant's active terminals via the injected
               :class:`TerminalRepository` (or the ``terminal_repository``
               keyword override, primarily for tests that want to pass
               a specific list without configuring a full repository).
            2. For each terminal, evaluates the 500 m geofence predicate
               against the current position.
            3. Looks up the pending-enter key for (tenant, truck,
               terminal). The key's presence / absence is the source
               of truth for "is the truck currently inside this
               terminal's buffer?".
            4. Emits ``enter`` when inside and no pending key, ``exit``
               when outside and a pending key exists, and a ``none``
               no-op otherwise.

        Returns a list of :class:`GeofenceEventResult` — one per
        terminal that produced an ``enter`` or ``exit``; terminals that
        produced no-op decisions are elided so the caller's log volume
        stays proportional to real activity. If the injected repository
        cannot be resolved (both the constructor arg and the call-time
        arg are ``None``), the method raises — a caller that wants a
        stateless dispatch without terminals should use
        :meth:`record_geofence_event` directly.

        Args:
            tenant_id: Tenant owning the truck.
            truck_id: Telemetry source.
            lat / lon: Current truck position.
            observed_at: Timestamp of the position sample. Must be
                timezone-aware.
            terminal_repository: Override the repository supplied at
                construction time. Tests use this to inject a fake
                without constructing a new tracker.
        """

        if not _is_nonempty_str(tenant_id):
            raise ValueError("tenant_id must be a non-empty string")
        if not _is_nonempty_str(truck_id):
            raise ValueError("truck_id must be a non-empty string")
        if not isinstance(observed_at, datetime):
            raise TypeError(
                f"observed_at must be a datetime, got {type(observed_at).__name__}"
            )
        if observed_at.tzinfo is None:
            raise ValueError(
                "observed_at must be timezone-aware; naive datetimes are rejected"
            )
        _validate_coord(lat, lon)

        repo = terminal_repository or self._terminal_repo
        if repo is None:
            raise ValueError(
                "TerminalGeofenceTracker.process_truck_position requires a "
                "terminal_repository either on the tracker or the call"
            )

        try:
            terminals = await repo.list_for_tenant(tenant_id, status="active")
        except TypeError:
            # Some fakes / older repositories don't accept ``status=``.
            # Fall back to an unfiltered list; we'll still filter below.
            terminals = await repo.list_for_tenant(tenant_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TerminalGeofenceTracker.process_truck_position: terminal "
                "list failed tenant=%s truck=%s: %s",
                tenant_id,
                truck_id,
                exc,
            )
            return []

        results: List[GeofenceEventResult] = []
        for terminal in _filter_active(terminals):
            inside = is_within_geofence(
                terminal.location_lat,
                terminal.location_lon,
                lat,
                lon,
            )
            has_pending = await self._has_pending_enter(
                tenant_id=tenant_id,
                truck_id=truck_id,
                terminal_id=terminal.terminal_id,
            )

            if inside and not has_pending:
                result = await self._handle_enter(
                    tenant_id=tenant_id,
                    truck_id=truck_id,
                    terminal_id=terminal.terminal_id,
                    observed_at=observed_at,
                    location_lat=lat,
                    location_lon=lon,
                )
                results.append(result)
            elif not inside and has_pending:
                result = await self._handle_exit(
                    tenant_id=tenant_id,
                    truck_id=truck_id,
                    terminal_id=terminal.terminal_id,
                    observed_at=observed_at,
                    location_lat=lat,
                    location_lon=lon,
                )
                results.append(result)
            # inside && pending -> still dwelling, no-op
            # !inside && !pending -> far from terminal, no-op

        return results

    # ------------------------------------------------------------------
    # Internals — enter / exit handling
    # ------------------------------------------------------------------

    async def _handle_enter(
        self,
        *,
        tenant_id: str,
        truck_id: str,
        terminal_id: str,
        observed_at: datetime,
        location_lat: float,
        location_lon: float,
    ) -> GeofenceEventResult:
        if self._redis is None:
            logger.debug(
                "TerminalGeofenceTracker._handle_enter: no redis; dropping "
                "enter tenant=%s truck=%s terminal=%s",
                tenant_id,
                truck_id,
                terminal_id,
            )
            return GeofenceEventResult.none(
                terminal_id=terminal_id,
                truck_id=truck_id,
                reason="no_redis_client",
            )

        key = _pending_key(tenant_id, truck_id, terminal_id)
        # Idempotency: if a pending enter already exists we keep the
        # earlier timestamp so the eventual wait covers the full dwell.
        try:
            existing = await self._redis.get(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TerminalGeofenceTracker._handle_enter: Redis GET failed "
                "tenant=%s truck=%s terminal=%s: %s",
                tenant_id,
                truck_id,
                terminal_id,
                exc,
            )
            return GeofenceEventResult.none(
                terminal_id=terminal_id,
                truck_id=truck_id,
                reason="redis_unavailable",
            )

        if existing is not None:
            return GeofenceEventResult.none(
                terminal_id=terminal_id,
                truck_id=truck_id,
                reason="duplicate_enter",
            )

        payload = json.dumps(
            {
                "enter_at": _to_iso(observed_at),
                "location_lat": float(location_lat),
                "location_lon": float(location_lon),
            }
        )
        try:
            await self._redis.set(key, payload, ex=self._pending_ttl)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TerminalGeofenceTracker._handle_enter: Redis SET failed "
                "tenant=%s truck=%s terminal=%s: %s",
                tenant_id,
                truck_id,
                terminal_id,
                exc,
            )
            return GeofenceEventResult.none(
                terminal_id=terminal_id,
                truck_id=truck_id,
                reason="redis_unavailable",
            )

        logger.info(
            "TerminalGeofenceTracker: enter tenant=%s truck=%s terminal=%s at=%s",
            tenant_id,
            truck_id,
            terminal_id,
            _to_iso(observed_at),
        )
        return GeofenceEventResult(
            event="enter",
            terminal_id=terminal_id,
            truck_id=truck_id,
            reason="recorded",
        )

    async def _handle_exit(
        self,
        *,
        tenant_id: str,
        truck_id: str,
        terminal_id: str,
        observed_at: datetime,
        location_lat: float,
        location_lon: float,
    ) -> GeofenceEventResult:
        if self._redis is None:
            return GeofenceEventResult.none(
                terminal_id=terminal_id,
                truck_id=truck_id,
                reason="no_redis_client",
            )

        pending = await self._load_pending_enter(
            tenant_id=tenant_id,
            truck_id=truck_id,
            terminal_id=terminal_id,
        )
        if pending is None:
            # Unmatched exit — most commonly happens after a service
            # restart that lost the in-flight enter, or the first time
            # we see a truck that was already at a terminal before
            # telemetry turned on. The correct behaviour is to drop
            # the signal silently rather than fabricate a wait_minutes
            # value we cannot justify.
            return GeofenceEventResult.none(
                terminal_id=terminal_id,
                truck_id=truck_id,
                reason="unmatched_exit",
            )

        enter_at = pending
        delta_seconds = (observed_at - enter_at).total_seconds()
        # Clamp negative deltas (clock skew) and absurd deltas (stuck
        # pending enters that predate the service). The TTL is the
        # first line of defense; this clamp is belt-and-braces.
        wait_minutes = max(0.0, delta_seconds / 60.0)
        if wait_minutes > MAX_DWELL_MINUTES:
            logger.warning(
                "TerminalGeofenceTracker: rejecting implausible dwell "
                "tenant=%s truck=%s terminal=%s wait_minutes=%.2f; "
                "clearing pending enter",
                tenant_id,
                truck_id,
                terminal_id,
                wait_minutes,
            )
            await self._clear_pending_enter(
                tenant_id=tenant_id,
                truck_id=truck_id,
                terminal_id=terminal_id,
            )
            return GeofenceEventResult.none(
                terminal_id=terminal_id,
                truck_id=truck_id,
                reason="implausible_dwell",
            )

        if self._wait_repo is None:
            logger.warning(
                "TerminalGeofenceTracker._handle_exit: no wait_report_repository; "
                "dropping exit tenant=%s truck=%s terminal=%s",
                tenant_id,
                truck_id,
                terminal_id,
            )
            # Still clear the pending key — leaving it behind would
            # cause the next legitimate exit to be swallowed as a
            # duplicate.
            await self._clear_pending_enter(
                tenant_id=tenant_id,
                truck_id=truck_id,
                terminal_id=terminal_id,
            )
            return GeofenceEventResult.none(
                terminal_id=terminal_id,
                truck_id=truck_id,
                reason="no_wait_report_repository",
            )

        try:
            report = await self._wait_repo.create(
                tenant_id,
                {
                    "tenant_id": tenant_id,
                    "terminal_id": terminal_id,
                    "wait_minutes": float(wait_minutes),
                    "source": "eld_geofence",
                    "reporter_id": None,
                    "truck_id": truck_id,
                    "observed_at": observed_at,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            # ES failure — log, leave the pending key in place so a
            # retry at the next telemetry sample can succeed, and
            # surface a no-op. Swallowing the exception keeps the
            # Geotab connector's sync loop alive.
            logger.warning(
                "TerminalGeofenceTracker._handle_exit: repository.create "
                "failed tenant=%s truck=%s terminal=%s: %s",
                tenant_id,
                truck_id,
                terminal_id,
                exc,
            )
            return GeofenceEventResult.none(
                terminal_id=terminal_id,
                truck_id=truck_id,
                reason="persistence_failed",
            )

        # Report written successfully — now clear state and invalidate
        # the wait-summary cache. Neither is allowed to fail the exit.
        await self._clear_pending_enter(
            tenant_id=tenant_id,
            truck_id=truck_id,
            terminal_id=terminal_id,
        )
        await self._invalidate_wait_summary_cache(
            tenant_id=tenant_id, terminal_id=terminal_id
        )

        logger.info(
            "TerminalGeofenceTracker: exit tenant=%s truck=%s terminal=%s "
            "wait_minutes=%.2f report=%s",
            tenant_id,
            truck_id,
            terminal_id,
            wait_minutes,
            report.report_id,
        )
        return GeofenceEventResult(
            event="exit",
            terminal_id=terminal_id,
            truck_id=truck_id,
            reason="recorded",
            wait_report=report,
        )

    # ------------------------------------------------------------------
    # Internals — Redis
    # ------------------------------------------------------------------

    async def _has_pending_enter(
        self, *, tenant_id: str, truck_id: str, terminal_id: str
    ) -> bool:
        if self._redis is None:
            return False
        key = _pending_key(tenant_id, truck_id, terminal_id)
        try:
            raw = await self._redis.get(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TerminalGeofenceTracker._has_pending_enter: Redis GET "
                "failed tenant=%s truck=%s terminal=%s: %s",
                tenant_id,
                truck_id,
                terminal_id,
                exc,
            )
            return False
        return raw is not None

    async def _load_pending_enter(
        self, *, tenant_id: str, truck_id: str, terminal_id: str
    ) -> Optional[datetime]:
        if self._redis is None:
            return None
        key = _pending_key(tenant_id, truck_id, terminal_id)
        try:
            raw = await self._redis.get(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TerminalGeofenceTracker._load_pending_enter: Redis GET "
                "failed tenant=%s truck=%s terminal=%s: %s",
                tenant_id,
                truck_id,
                terminal_id,
                exc,
            )
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning(
                    "TerminalGeofenceTracker._load_pending_enter: undecodable "
                    "payload tenant=%s truck=%s terminal=%s",
                    tenant_id,
                    truck_id,
                    terminal_id,
                )
                return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "TerminalGeofenceTracker._load_pending_enter: JSON decode "
                "failed tenant=%s truck=%s terminal=%s: %s",
                tenant_id,
                truck_id,
                terminal_id,
                exc,
            )
            return None
        if not isinstance(payload, dict):
            return None
        enter_raw = payload.get("enter_at")
        if not isinstance(enter_raw, str):
            return None
        try:
            # ``datetime.fromisoformat`` handles the ``+00:00`` offset
            # we wrote on the way in; the ``Z`` alias is also supported
            # on Python 3.11+ but we normalise just in case.
            enter_at = datetime.fromisoformat(enter_raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning(
                "TerminalGeofenceTracker._load_pending_enter: invalid "
                "enter_at=%r tenant=%s truck=%s terminal=%s",
                enter_raw,
                tenant_id,
                truck_id,
                terminal_id,
            )
            return None
        if enter_at.tzinfo is None:
            enter_at = enter_at.replace(tzinfo=timezone.utc)
        return enter_at

    async def _clear_pending_enter(
        self, *, tenant_id: str, truck_id: str, terminal_id: str
    ) -> None:
        if self._redis is None:
            return
        key = _pending_key(tenant_id, truck_id, terminal_id)
        try:
            await self._redis.delete(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TerminalGeofenceTracker._clear_pending_enter: Redis "
                "DELETE failed tenant=%s truck=%s terminal=%s: %s",
                tenant_id,
                truck_id,
                terminal_id,
                exc,
            )

    async def _invalidate_wait_summary_cache(
        self, *, tenant_id: str, terminal_id: str
    ) -> None:
        if self._redis is None:
            return
        key = TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
            tenant_id=tenant_id, terminal_id=terminal_id
        )
        try:
            await self._redis.delete(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TerminalGeofenceTracker._invalidate_wait_summary_cache: "
                "Redis DELETE failed tenant=%s terminal=%s: %s",
                tenant_id,
                terminal_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _pending_key(tenant_id: str, truck_id: str, terminal_id: str) -> str:
    return PENDING_ENTER_KEY_TEMPLATE.format(
        tenant_id=tenant_id, truck_id=truck_id, terminal_id=terminal_id
    )


def _to_iso(value: datetime) -> str:
    """Normalise a timezone-aware datetime to UTC ISO-8601."""

    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_coord(lat: float, lon: float) -> None:
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        raise TypeError("lat/lon must be numeric")
    if not (-90.0 <= float(lat) <= 90.0):
        raise ValueError(f"lat out of range: {lat}")
    if not (-180.0 <= float(lon) <= 180.0):
        raise ValueError(f"lon out of range: {lon}")


def _filter_active(terminals: Iterable[Any]) -> List[Terminal]:
    """Return only terminals with ``status == 'active'``.

    Defensive: some :class:`TerminalRepository` mocks ignore the
    ``status`` filter and return every row. We belt-and-brace that with
    a second filter here so a misconfigured fake cannot make the
    tracker emit enters/exits against inactive / deleted terminals.
    """

    out: List[Terminal] = []
    for terminal in terminals or []:
        status = getattr(terminal, "status", None)
        if status is None or status == "active":
            out.append(terminal)
    return out


__all__ = [
    "GEOFENCE_BUFFER_METERS",
    "MAX_DWELL_MINUTES",
    "PENDING_ENTER_KEY_TEMPLATE",
    "PENDING_ENTER_TTL_SECONDS",
    "GeofenceEventResult",
    "GeofenceEventType",
    "TerminalGeofenceTracker",
    "is_within_geofence",
]
