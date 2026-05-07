"""
Replan_Diff Pydantic models and diff helper for the overlay replanning path.

Capability 2 / Requirement 2.5 of the fuel-ops hardening spec introduces a
structured "what changed today" diff between an original route and its
patched successor. The Exception_Replanning_Agent (Task 4.10) and the
emergency-stop insertion path (Tasks 4.8 / 4.9) both emit these diffs,
persist them to the existing ``mvp_replan_events`` ES index, and surface
summary counts over the ``/ws/fuel-planning`` WebSocket channel.

The ``fuel_distribution_models.ReplanDiff`` already in use by the MVP
pipeline is a free-form "Nigerian-retail" contract (``stops_reordered``
as ids, ``volumes_reallocated`` as grade→liters, etc.) kept nested
inside ``ReplanEvent.diff``. Requirement 2.5.1 calls for a **flat,
ES-mapping-compatible** schema with ``diff_id`` at the top level and
typed nested arrays (``added_stops``, ``removed_stops``, and five more).
Those two shapes cannot be reconciled without breaking the MVP pipeline,
so this module introduces a **new** :class:`ReplanDiff` alongside the
MVP one. The two are distinguished by module path:

    - ``Agents.support.fuel_distribution_models.ReplanDiff``
      → MVP pipeline contract (embedded in ``ReplanEvent``).
    - ``Agents.support.replan_diff_models.ReplanDiff``  ← this module
      → fuel-ops-hardening overlay contract (top-level document).

Downstream code that needs the new shape imports from this module
explicitly.

The helper :func:`compute_replan_diff` derives a :class:`ReplanDiff`
from two route-like inputs (Pydantic models, dataclasses, or plain
dicts). It deliberately accepts a loose "route_like" protocol so it can
diff both the MVP :class:`~Agents.support.fuel_distribution_models.RoutePlan`
and the extended route structures added by later tasks without the
helper having to know about either one.

Validates: Requirements 2.5.1.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Nested stop-reference shapes
# ---------------------------------------------------------------------------


class StopRef(BaseModel):
    """Identifier for a stop referenced in ``added_stops`` / ``removed_stops``.

    Kept deliberately narrow so the diff document stays small: the
    consumer already has the full stop record elsewhere (on the
    original or patched route) and only needs enough context to render
    the change and fetch the rest on demand.
    """

    model_config = ConfigDict(extra="forbid")

    stop_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Stable stop identifier. For fuel-ops this is normally a "
            "station_id, customer_tank_id, or explicit stop_id, whichever "
            "the route uses as its primary key."
        ),
    )
    index: int = Field(
        ...,
        ge=0,
        description="Position of the stop in its originating route (0-based).",
    )
    gallons: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Planned gallons for the stop, if known.",
    )
    product_code: Optional[str] = Field(
        default=None,
        description="Canonical fuel product_code delivered at the stop, if known.",
    )
    eta: Optional[str] = Field(
        default=None,
        description=(
            "Scheduled ETA for the stop as an ISO-8601 string. Stored as a "
            "string so round-tripped diffs don't lose sub-second or "
            "timezone fidelity."
        ),
    )


class ReorderedStop(BaseModel):
    """A stop whose index changed between ``original`` and ``patched``."""

    model_config = ConfigDict(extra="forbid")

    stop_id: str = Field(..., min_length=1)
    before_index: int = Field(..., ge=0)
    after_index: int = Field(..., ge=0)


class ReassignedStop(BaseModel):
    """A stop that moved from one truck's route to another."""

    model_config = ConfigDict(extra="forbid")

    stop_id: str = Field(..., min_length=1)
    from_truck_id: str = Field(..., min_length=1)
    to_truck_id: str = Field(..., min_length=1)


class QuantityChange(BaseModel):
    """A stop whose planned gallons changed."""

    model_config = ConfigDict(extra="forbid")

    stop_id: str = Field(..., min_length=1)
    before_gallons: float = Field(..., ge=0.0)
    after_gallons: float = Field(..., ge=0.0)
    product_code: Optional[str] = Field(
        default=None,
        description=(
            "Canonical fuel product_code the change applies to. Optional so "
            "diffs against routes that don't record per-stop products still "
            "serialize."
        ),
    )


class EtaShift(BaseModel):
    """A stop whose scheduled ETA moved forward or backward."""

    model_config = ConfigDict(extra="forbid")

    stop_id: str = Field(..., min_length=1)
    before_eta: str = Field(
        ..., min_length=1, description="ISO-8601 timestamp before replan."
    )
    after_eta: str = Field(
        ..., min_length=1, description="ISO-8601 timestamp after replan."
    )
    shift_minutes: float = Field(
        ...,
        description=(
            "(after_eta - before_eta) in minutes. Positive when the stop "
            "moved later in the day, negative when it moved earlier."
        ),
    )


# ---------------------------------------------------------------------------
# Top-level Replan_Diff document
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """UTC-aware ``datetime.now`` wrapper for default factories."""

    return datetime.now(timezone.utc)


class ReplanDiff(BaseModel):
    """Structured diff between two route plan versions (Req 2.5.1).

    The shape is intentionally flat (``diff_id`` is top-level, not nested
    under an ``ReplanEvent``) so it can be indexed directly to an ES
    mapping whose ``nested`` fields are the six change arrays. Every
    array is a concrete Pydantic submodel so that:

        1. JSON round-tripping (``ReplanDiff.model_validate_json(``
           ``diff.model_dump_json()) == diff``) is a property we can
           test — see Req 2.5.5.
        2. Downstream consumers (WebSocket summary, dispatcher UI) get
           static types rather than ``dict[str, Any]`` bags.

    ``generated_at`` defaults to UTC ``now`` only on fresh construction;
    parsed documents keep whatever timestamp they carried, so a
    round-trip doesn't silently mutate history.
    """

    model_config = ConfigDict(extra="forbid")

    diff_id: str = Field(
        default_factory=lambda: str(uuid4()),
        min_length=1,
        description="Stable identifier for this diff document.",
    )
    original_route_id: str = Field(
        ...,
        min_length=1,
        description="Route id of the plan that was being replaced.",
    )
    patched_route_id: str = Field(
        ...,
        min_length=1,
        description="Route id of the resulting plan after replan.",
    )
    added_stops: List[StopRef] = Field(
        default_factory=list,
        description="Stops present in ``patched`` but not in ``original``.",
    )
    removed_stops: List[StopRef] = Field(
        default_factory=list,
        description="Stops present in ``original`` but not in ``patched``.",
    )
    reordered_stops: List[ReorderedStop] = Field(
        default_factory=list,
        description="Stops whose index changed between routes.",
    )
    reassigned_stops: List[ReassignedStop] = Field(
        default_factory=list,
        description="Stops that moved between trucks.",
    )
    quantity_changes: List[QuantityChange] = Field(
        default_factory=list,
        description="Stops whose planned gallons changed.",
    )
    eta_shifts: List[EtaShift] = Field(
        default_factory=list,
        description="Stops whose scheduled ETA shifted.",
    )
    generated_at: datetime = Field(
        default_factory=_utcnow,
        description="UTC timestamp the diff was computed.",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("generated_at")
    @classmethod
    def _ensure_tz_aware(cls, value: datetime) -> datetime:
        """Require a tz-aware datetime so round-trips preserve the offset.

        Pydantic v2 accepts naive datetimes by default; for an ES-indexed
        document we want the timezone to be explicit so downstream date
        math in the dispatcher UI is unambiguous.
        """

        if value.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return value

    def summary_counts(self) -> Dict[str, int]:
        """Return the summary counts used in the ``replan_diff_ready`` WS event.

        Defined as a method rather than a computed field so it doesn't
        appear in ``model_dump`` output — the persisted ES document is
        exactly what the schema describes.
        """

        return {
            "added": len(self.added_stops),
            "removed": len(self.removed_stops),
            "reordered": len(self.reordered_stops),
            "reassigned": len(self.reassigned_stops),
            "quantity_changes": len(self.quantity_changes),
            "eta_shifts": len(self.eta_shifts),
        }


# ---------------------------------------------------------------------------
# compute_replan_diff helper
# ---------------------------------------------------------------------------


def _coerce_route_like(route: Any) -> Dict[str, Any]:
    """Coerce a route-like input into a ``dict`` view for diffing.

    Accepts Pydantic models (``model_dump``), plain dicts, and attribute
    objects so callers don't have to pre-serialize. Returns a dict with
    at least ``route_id``, ``truck_id`` (may be ``None``), and ``stops``
    (list). Missing keys fall back to sensible defaults so upstream
    callers get a clear :class:`ValueError` at the first real mismatch
    rather than an opaque ``AttributeError``.
    """

    if isinstance(route, BaseModel):
        data = route.model_dump(mode="python")
    elif isinstance(route, dict):
        data = dict(route)
    else:
        data = {
            "route_id": getattr(route, "route_id", None),
            "truck_id": getattr(route, "truck_id", None),
            "stops": list(getattr(route, "stops", []) or []),
        }

    stops = data.get("stops") or []
    if not isinstance(stops, (list, tuple)):
        raise ValueError(
            f"Route-like input must have an iterable 'stops' field; got {type(stops)!r}"
        )
    data["stops"] = list(stops)
    return data


def _stop_identifier(stop: Any, fallback_index: int) -> Optional[str]:
    """Extract a stable stop identifier from a stop record.

    Prefers an explicit ``stop_id``, then ``station_id``, then
    ``customer_tank_id``. Returns ``None`` when none are present so the
    diff helper can skip mal-formed stops rather than crash.
    ``fallback_index`` is only used to produce a clear log message.
    """

    if isinstance(stop, BaseModel):
        stop = stop.model_dump(mode="python")

    if isinstance(stop, dict):
        for key in ("stop_id", "station_id", "customer_tank_id"):
            value = stop.get(key)
            if value:
                return str(value)
        logger.debug(
            "compute_replan_diff: stop at index %d has no identifier; skipping",
            fallback_index,
        )
        return None

    for attr in ("stop_id", "station_id", "customer_tank_id"):
        value = getattr(stop, attr, None)
        if value:
            return str(value)
    logger.debug(
        "compute_replan_diff: stop object at index %d has no identifier; skipping",
        fallback_index,
    )
    return None


def _stop_gallons(stop: Any) -> Optional[float]:
    """Extract planned gallons from a stop, summing ``drop`` if needed.

    Supports three common shapes in the codebase:

    * ``planned_gallons`` (fuel-ops extended routes)
    * ``gallons`` (convenience alias used in tests)
    * ``drop`` as a ``{grade: liters}`` dict (MVP ``RouteStop``); summed
      across grades, since the diff-level notion is total gallons.
    """

    if isinstance(stop, BaseModel):
        stop = stop.model_dump(mode="python")

    if isinstance(stop, dict):
        for key in ("planned_gallons", "gallons"):
            if key in stop and stop[key] is not None:
                return float(stop[key])
        drop = stop.get("drop")
        if isinstance(drop, dict) and drop:
            try:
                return float(sum(float(v) for v in drop.values()))
            except (TypeError, ValueError):
                return None
        return None

    for attr in ("planned_gallons", "gallons"):
        value = getattr(stop, attr, None)
        if value is not None:
            return float(value)
    drop = getattr(stop, "drop", None)
    if isinstance(drop, dict) and drop:
        try:
            return float(sum(float(v) for v in drop.values()))
        except (TypeError, ValueError):
            return None
    return None


def _stop_product_code(stop: Any) -> Optional[str]:
    """Extract a canonical product_code from a stop when available."""

    if isinstance(stop, BaseModel):
        stop = stop.model_dump(mode="python")

    if isinstance(stop, dict):
        for key in ("product_code", "fuel_grade"):
            value = stop.get(key)
            if value:
                return str(value)
        drop = stop.get("drop")
        if isinstance(drop, dict) and len(drop) == 1:
            return str(next(iter(drop.keys())))
        return None

    for attr in ("product_code", "fuel_grade"):
        value = getattr(stop, attr, None)
        if value:
            return str(value)
    drop = getattr(stop, "drop", None)
    if isinstance(drop, dict) and len(drop) == 1:
        return str(next(iter(drop.keys())))
    return None


def _stop_eta(stop: Any) -> Optional[str]:
    """Extract a stop ETA as an ISO-8601 string, or ``None`` if absent."""

    if isinstance(stop, BaseModel):
        stop = stop.model_dump(mode="python")

    if isinstance(stop, dict):
        value = stop.get("eta")
    else:
        value = getattr(stop, "eta", None)

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _eta_shift_minutes(before: str, after: str) -> Optional[float]:
    """Return (after - before) in minutes, or ``None`` if un-parseable.

    Uses :meth:`datetime.fromisoformat`, which accepts the extended ISO
    format emitted by ``datetime.isoformat()``. ``Z`` suffixes are
    normalized to ``+00:00`` because ``fromisoformat`` on Python < 3.11
    does not accept a trailing ``Z``.
    """

    def _parse(value: str) -> Optional[datetime]:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    a = _parse(before)
    b = _parse(after)
    if a is None or b is None:
        return None
    # If one side is tz-aware and the other isn't, assume naive is UTC.
    if (a.tzinfo is None) ^ (b.tzinfo is None):
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        else:
            b = b.replace(tzinfo=timezone.utc)
    return (b - a).total_seconds() / 60.0


def _index_stops_by_id(
    stops: List[Any],
) -> Tuple[Dict[str, int], List[Tuple[str, Any]]]:
    """Return ``(id→index, [(id, stop)])`` for stops with identifiers.

    Stops without identifiers are silently skipped (and logged) so a
    single malformed row cannot poison the diff. Duplicate ids keep the
    *first* occurrence, consistent with "first-seen wins" route layout.
    """

    id_to_index: Dict[str, int] = {}
    ordered: List[Tuple[str, Any]] = []
    for idx, stop in enumerate(stops):
        stop_id = _stop_identifier(stop, idx)
        if stop_id is None:
            continue
        if stop_id in id_to_index:
            logger.debug(
                "compute_replan_diff: duplicate stop_id %s at index %d; "
                "first occurrence wins",
                stop_id,
                idx,
            )
            continue
        id_to_index[stop_id] = idx
        ordered.append((stop_id, stop))
    return id_to_index, ordered


def compute_replan_diff(
    original: Any,
    patched: Any,
    *,
    generated_at: Optional[datetime] = None,
    diff_id: Optional[str] = None,
) -> ReplanDiff:
    """Compute a :class:`ReplanDiff` between two route-like inputs.

    The helper is deliberately permissive about input shape: it accepts
    the MVP :class:`~Agents.support.fuel_distribution_models.RoutePlan`,
    the extended route shapes introduced later in Phase 4, plain dicts
    with ``route_id`` / ``truck_id`` / ``stops`` keys, and attribute
    objects. That keeps the diff helper decoupled from any specific
    route Pydantic class so Task 4.10 can adopt it without introducing
    a circular import.

    Each stop is matched between ``original`` and ``patched`` by a
    stable identifier (``stop_id`` → ``station_id`` → ``customer_tank_id``).
    Stops whose identifier is present in only one side become
    ``added_stops`` / ``removed_stops``; stops present in both become
    potential ``reordered``, ``quantity_change``, or ``eta_shift`` entries
    depending on which fields differ.

    ``reassigned_stops`` is populated only when the two routes are
    carried by different trucks (``original.truck_id != patched.truck_id``
    and both are set). In that case every stop common to both routes is
    recorded as a reassignment, since a truck-level handoff moves all
    shared stops by definition. A route's internal reshuffle (same
    truck, different ordering) generates ``reordered_stops`` entries,
    not reassignments.

    Returns a validated :class:`ReplanDiff`. Raises :class:`ValueError`
    if either input is missing ``route_id``.
    """

    original_view = _coerce_route_like(original)
    patched_view = _coerce_route_like(patched)

    original_route_id = original_view.get("route_id")
    patched_route_id = patched_view.get("route_id")
    if not original_route_id or not patched_route_id:
        raise ValueError(
            "compute_replan_diff: both inputs must expose a non-empty route_id"
        )

    original_truck_id = original_view.get("truck_id")
    patched_truck_id = patched_view.get("truck_id")
    trucks_differ = (
        bool(original_truck_id)
        and bool(patched_truck_id)
        and original_truck_id != patched_truck_id
    )

    original_ids, original_ordered = _index_stops_by_id(
        original_view.get("stops", [])
    )
    patched_ids, patched_ordered = _index_stops_by_id(
        patched_view.get("stops", [])
    )

    added: List[StopRef] = []
    removed: List[StopRef] = []
    reordered: List[ReorderedStop] = []
    reassigned: List[ReassignedStop] = []
    quantity_changes: List[QuantityChange] = []
    eta_shifts: List[EtaShift] = []

    original_stop_by_id = {sid: stop for sid, stop in original_ordered}

    for stop_id, stop in original_ordered:
        if stop_id not in patched_ids:
            removed.append(
                StopRef(
                    stop_id=stop_id,
                    index=original_ids[stop_id],
                    gallons=_stop_gallons(stop),
                    product_code=_stop_product_code(stop),
                    eta=_stop_eta(stop),
                )
            )

    for stop_id, patched_stop in patched_ordered:
        if stop_id not in original_ids:
            added.append(
                StopRef(
                    stop_id=stop_id,
                    index=patched_ids[stop_id],
                    gallons=_stop_gallons(patched_stop),
                    product_code=_stop_product_code(patched_stop),
                    eta=_stop_eta(patched_stop),
                )
            )
            continue

        before_index = original_ids[stop_id]
        after_index = patched_ids[stop_id]
        if before_index != after_index:
            reordered.append(
                ReorderedStop(
                    stop_id=stop_id,
                    before_index=before_index,
                    after_index=after_index,
                )
            )

        if trucks_differ:
            reassigned.append(
                ReassignedStop(
                    stop_id=stop_id,
                    from_truck_id=str(original_truck_id),
                    to_truck_id=str(patched_truck_id),
                )
            )

        original_stop = original_stop_by_id[stop_id]
        before_gallons = _stop_gallons(original_stop)
        after_gallons = _stop_gallons(patched_stop)
        if (
            before_gallons is not None
            and after_gallons is not None
            and before_gallons != after_gallons
        ):
            quantity_changes.append(
                QuantityChange(
                    stop_id=stop_id,
                    before_gallons=before_gallons,
                    after_gallons=after_gallons,
                    product_code=(
                        _stop_product_code(patched_stop)
                        or _stop_product_code(original_stop)
                    ),
                )
            )

        before_eta = _stop_eta(original_stop)
        after_eta = _stop_eta(patched_stop)
        if before_eta and after_eta and before_eta != after_eta:
            shift = _eta_shift_minutes(before_eta, after_eta)
            if shift is not None:
                eta_shifts.append(
                    EtaShift(
                        stop_id=stop_id,
                        before_eta=before_eta,
                        after_eta=after_eta,
                        shift_minutes=shift,
                    )
                )

    kwargs: Dict[str, Any] = {
        "original_route_id": str(original_route_id),
        "patched_route_id": str(patched_route_id),
        "added_stops": added,
        "removed_stops": removed,
        "reordered_stops": reordered,
        "reassigned_stops": reassigned,
        "quantity_changes": quantity_changes,
        "eta_shifts": eta_shifts,
    }
    if diff_id is not None:
        kwargs["diff_id"] = diff_id
    if generated_at is not None:
        kwargs["generated_at"] = generated_at

    return ReplanDiff(**kwargs)


__all__ = [
    "ReplanDiff",
    "StopRef",
    "ReorderedStop",
    "ReassignedStop",
    "QuantityChange",
    "EtaShift",
    "compute_replan_diff",
]
