"""Dispatch Board publish and changes after publish (design K7, K8; plan tasks 15-16).

``BoardPublishService`` owns the publish request: the K7.2 preflight (fresh
validation, cross-day check, snapshot and allocation refresh), the K8.4 group
split, the dry run (K7.1), the all-or-none claim and the background worker.
A **first-publish group** (one lane holding no dispatched order) is published
here, load by load (K7.3). A **redispatch group** (a Modified lane, or any lane
holding a dispatched order, plus every lane a moved dispatched order came from
or goes to) is handed whole to ``BoardRedispatchService.run_group``, which runs
the seven K8.4 phases across all of the group's lanes (freeze rule 7).

This is the only board module that writes orders, plans, routes or executions
(I4, T-B10). Every order write goes through the existing primitives: the
loading-plan executor, ``FuelPlanDispatchService.dispatch`` and
``FuelOrderRepository.relink_dispatched_assignment``. Every plan, route and
execution write is an ``atomic_update`` (freeze rules 9-11).
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from Agents.support.mvp_es_mappings import (
    MVP_LOAD_PLANS_INDEX,
    MVP_PLAN_EXECUTIONS_INDEX,
    MVP_ROUTES_INDEX,
)
from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services import dispatch_board_engine as engine
from fuel.services import dispatch_board_eta as eta
from fuel.services import dispatch_board_telemetry as tm
from fuel.services.dispatch_board_es_mappings import DISPATCH_BOARD_DRAFTS_INDEX
from fuel.services.dispatch_board_models import (
    PUBLISHES_LIMIT,
    AttemptLoad,
    AttemptRelink,
    BoardDraft,
    Check,
    Lane,
    LaneContent,
    Link,
    Load,
    PublishBody,
    PublishedPlan,
    PublishPreviewBody,
    PublishResult,
    RedispatchAttempt,
    Scope,
    WarningAck,
    draft_doc_id,
    is_in_recovery,
)
from fuel.services.dispatch_validation import ValidationContext, lane_checks
from services.unit_conversion import GAL_TO_L

logger = logging.getLogger(__name__)

#: Publish claim lease (K7.2); renewed before each load or phase.
LEASE_SECONDS = 120.0
#: In-place retries of a failing forward step (K8.4 Recovery), seconds.
RETRY_DELAYS: Tuple[float, ...] = (1.0, 2.0, 4.0)
#: WARNING cadence while a forward recovery is pending (Q16).
FORWARD_WARNING_INTERVAL_S = 300.0
#: Phases that are rolled back on failure; the rest are completed forward.
ROLLBACK_PHASES = frozenset({"claimed", "retire", "stage_relink"})
FORWARD_PHASES = ("apply", "amend", "notify", "finalize")
#: Executor reasons for a not-yet-applied tray order that the drop rule removes.
_DROP_EXECUTOR_REASONS = frozenset({"order_changed_since_plan", "order_not_loadable"})
#: Overrides off the order's litres by more than this block the publish (K7.2).
OVERRIDE_TOLERANCE = 0.01
BOARD_SOURCE = "dispatch_board"
REVOKE_REASON = "reassigned_by_dispatcher"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hits(resp: Any) -> List[Dict[str, Any]]:
    return [h.get("_source") or {} for h in ((resp or {}).get("hits") or {}).get("hits") or []]


def _canonical(code: Optional[str]) -> Optional[str]:
    return engine._canonical(code)  # noqa: SLF001 - one canonicalisation for the board


def _resolve(value: Any) -> Any:
    """A collaborator or a ``Lazy`` wrapper (resolved at call time)."""
    resolver = getattr(value, "resolve", None)
    return resolver() if callable(resolver) else value


# ---------------------------------------------------------------------------
# Document builders (K7.3a)
# ---------------------------------------------------------------------------


def board_ids(load_id: str, revision: int) -> Tuple[str, str]:
    """``(plan_id, route_id)`` for a load revision; ``run_id == plan_id``."""
    return f"bp-{load_id}-r{revision}", f"br-{load_id}-r{revision}"


def _stop_liters(load: Load, order_id: str) -> Dict[str, float]:
    drop: Dict[str, float] = {}
    for alloc in load.allocations:
        if alloc.order_id == order_id and alloc.liters:
            code = _canonical(alloc.product_code) or "UNKNOWN"
            drop[code] = round(drop.get(code, 0.0) + float(alloc.liters), 3)
    if drop:
        return drop
    stop = next((s for s in load.stops if s.order_id == order_id), None)
    if stop is not None and stop.snapshot.gallons_requested:
        code = _canonical(stop.snapshot.product_code) or "UNKNOWN"
        return {code: round(float(stop.snapshot.gallons_requested) * GAL_TO_L, 3)}
    return {}


def build_plan_doc(
    *,
    tenant_id: str,
    service_date: date,
    lane: Lane,
    load: Load,
    load_seq: int,
    revision: int,
    now: datetime,
    created_by_attempt: Optional[str] = None,
    supersedes_plan_id: Optional[str] = None,
) -> Dict[str, Any]:
    """``mvp_load_plans/{plan_id}`` exactly as K7.3a declares it."""
    plan_id, _route_id = board_ids(load.load_id, revision)
    snapshots = {s.order_id: s.snapshot for s in load.stops}
    assignments = []
    for alloc in load.allocations:
        if not alloc.order_id or alloc.order_id not in snapshots:
            continue
        snap = snapshots[alloc.order_id]
        code = _canonical(alloc.product_code) or _canonical(snap.product_code)
        assignments.append(
            {
                "order_id": alloc.order_id,
                "station_id": snap.customer_id,
                "product_code": code,
                "fuel_grade": code,
                "compartment_id": alloc.compartment_id,
                "quantity_liters": float(alloc.liters),
                "compartment_capacity_liters": float(alloc.capacity_liters),
            }
        )
    doc: Dict[str, Any] = {
        "plan_id": plan_id,
        "run_id": plan_id,
        "tenant_id": tenant_id,
        "truck_id": lane.truck_id,
        "terminal_id": load.terminal_id,
        "status": "draft",
        "source": BOARD_SOURCE,
        "board_draft_id": draft_doc_id(tenant_id, service_date),
        "board_load_id": load.load_id,
        "service_date": service_date.isoformat(),
        "shift_id": load.shift_id,
        "load_seq": load_seq,
        "driver_id": lane.driver_id,
        "revision": revision,
        "supersedes_plan_id": supersedes_plan_id,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "assignments": assignments,
    }
    if created_by_attempt:
        doc["created_by_attempt"] = created_by_attempt
    return doc


def build_route_doc(
    *,
    tenant_id: str,
    service_date: date,
    lane: Lane,
    load: Load,
    revision: int,
    now: datetime,
    created_by_attempt: Optional[str] = None,
    supersedes_route_id: Optional[str] = None,
) -> Dict[str, Any]:
    """``mvp_routes/{route_id}`` exactly as K7.3a declares it."""
    plan_id, route_id = board_ids(load.load_id, revision)
    stops = []
    for seq, stop in enumerate(load.stops, start=1):
        entry: Dict[str, Any] = {
            "sequence": seq,
            "station_id": stop.snapshot.customer_tank_id or stop.snapshot.customer_id,
            "order_ids": [stop.order_id],
            "drop": _stop_liters(load, stop.order_id),
        }
        if stop.eta is not None:
            entry["eta"] = stop.eta.astimezone(timezone.utc).isoformat()
        stops.append(entry)
    doc: Dict[str, Any] = {
        "route_id": route_id,
        "plan_id": plan_id,
        "run_id": plan_id,
        "tenant_id": tenant_id,
        "truck_id": lane.truck_id,
        "status": "planned",
        "timestamp": now.isoformat(),
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "source": BOARD_SOURCE,
        "board_load_id": load.load_id,
        "service_date": service_date.isoformat(),
        "revision": revision,
        "supersedes_route_id": supersedes_route_id,
        "stops": stops,
    }
    distance = eta.distance_km([s.location for s in load.stops]) if load.stops else None
    if distance is not None:
        doc["distance_km"] = round(distance, 3)
    if created_by_attempt:
        doc["created_by_attempt"] = created_by_attempt
    return doc


def order_snapshots_for(load: Load) -> Dict[str, Dict[str, Any]]:
    """Executor ``order_snapshots`` from the refreshed stop snapshots (K7.3 step 4)."""
    return {
        s.order_id: {
            "customer_tank_id": s.snapshot.customer_tank_id,
            "gallons_requested": s.snapshot.gallons_requested,
            "fill_to_full": s.snapshot.fill_to_full,
        }
        for s in load.stops
    }


# ---------------------------------------------------------------------------
# Change classification (K8.2, P8) and publish groups (K8.4, P11)
# ---------------------------------------------------------------------------


@dataclass
class LoadChange:
    truck_id: str
    load_id: str
    load_class: str  # unchanged | new | new_revision | amend | removed
    info: List[str] = field(default_factory=list)


@dataclass
class _StartedView:
    orders: Dict[str, Dict[str, Any]]
    executions: Dict[str, int]


def _load_content(load: Optional[Load]) -> Any:
    if load is None:
        return None
    return engine._content_dict(LaneContent(loads=[load]))["loads"][0]  # noqa: SLF001 - same hash basis


def plan_changes(
    lanes: Sequence[Lane],
    orders: Mapping[str, Dict[str, Any]],
    executions: Mapping[str, int],
    now: datetime,
) -> Dict[str, LoadChange]:
    """Pure K8.2 classification: one class per load (P8).

    * **new**: no ``publish.plans`` entry (never-published lanes: every load);
    * **unchanged**: published, same content and same lane driver;
    * **new_revision**: published, changed, not started (+ ``driver_may_be_loading``
      when its published ``planned_start`` is past);
    * **amend**: published, changed, started (K8.1);
    * **removed**: published and now empty, not started.
    """
    view = _StartedView(dict(orders), dict(executions))
    out: Dict[str, LoadChange] = {}
    for lane in lanes:
        published = lane.publish.published_content
        published_driver = published.driver_id if published is not None else None
        current = {l.load_id: l for l in lane.loads}
        for load in lane.loads:
            if load.load_id in lane.publish.plans:
                continue
            if load.stops:
                out[load.load_id] = LoadChange(lane.truck_id, load.load_id, "new")
        for load_id in lane.publish.plans:
            now_load = current.get(load_id)
            started = engine.load_started(lane, load_id, view)
            before = engine.published_load(lane, load_id)
            if now_load is None or not now_load.stops:
                out[load_id] = LoadChange(lane.truck_id, load_id, "amend" if started else "removed")
                continue
            same = _load_content(now_load) == _load_content(before) and lane.driver_id == published_driver
            if same:
                out[load_id] = LoadChange(lane.truck_id, load_id, "unchanged")
            elif started:
                out[load_id] = LoadChange(lane.truck_id, load_id, "amend")
            else:
                info = []
                start = (before.planned_start if before is not None else None) or now_load.planned_start
                if start is not None and start < now:
                    info.append("driver_may_be_loading")
                out[load_id] = LoadChange(lane.truck_id, load_id, "new_revision", info)
    return out


@dataclass
class PublishGroup:
    truck_ids: List[str]
    kind: str  # first_publish | redispatch
    added_lanes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"truck_ids": list(self.truck_ids), "kind": self.kind, "added_lanes": list(self.added_lanes)}


def _published_holder(draft: BoardDraft) -> Dict[str, Tuple[str, str]]:
    """``order_id -> (truck_id, load_id)`` from every lane's ``published_content``."""
    out: Dict[str, Tuple[str, str]] = {}
    for truck_id, lane in draft.lanes.items():
        content = lane.publish.published_content
        if content is None:
            continue
        for load in content.loads:
            for stop in load.stops:
                out[stop.order_id] = (truck_id, load.load_id)
        for order_id in content.shelf:
            out.setdefault(order_id, (truck_id, ""))
    return out


def candidate_lanes(draft: BoardDraft, requested: Iterable[str]) -> List[str]:
    """Draft-only superset of every lane a requested lane can pull into its group."""
    holder = _published_holder(draft)
    seen: List[str] = []
    queue = [t for t in requested if t in draft.lanes]
    while queue:
        truck_id = queue.pop(0)
        if truck_id in seen:
            continue
        seen.append(truck_id)
        lane = draft.lanes[truck_id]
        ids = set(engine.lane_order_ids(lane))
        if lane.publish.published_content is not None:
            content = lane.publish.published_content
            ids.update(s.order_id for l in content.loads for s in l.stops)
        for order_id in ids:
            for other in (draft.order_index.get(order_id), (holder.get(order_id) or (None, None))[0]):
                if other and other not in seen and other in draft.lanes:
                    queue.append(other)
    return seen


def publish_groups(
    draft: BoardDraft,
    requested: Sequence[str],
    orders: Mapping[str, Dict[str, Any]],
) -> List[PublishGroup]:
    """K8.4 publish groups: connected components over moved dispatched orders (P11).

    Lanes X and Y are linked when a ``dispatched`` order linked to one of X's
    published runs sits on Y's loads or shelf. A component is a redispatch group
    when it holds a Modified lane or any dispatched order; every other requested
    lane is a first-publish group of one.
    """
    runs = engine.published_run_ids(draft)
    edges: Dict[str, Set[str]] = {t: set() for t in draft.lanes}
    holds_dispatched: Set[str] = set()
    for truck_id, lane in draft.lanes.items():
        for order_id in engine.lane_order_ids(lane):
            order = orders.get(order_id) or {}
            if order.get("status") != "dispatched":
                continue
            holds_dispatched.add(truck_id)
            home = runs.get(order.get("assigned_run_id") or "")
            if home is not None and home[0] != truck_id:
                edges[truck_id].add(home[0])
                edges[home[0]].add(truck_id)
    requested = [t for t in requested if t in draft.lanes]
    groups: List[PublishGroup] = []
    placed: Set[str] = set()
    for truck_id in requested:
        if truck_id in placed:
            continue
        component: List[str] = []
        queue = [truck_id]
        while queue:
            t = queue.pop(0)
            if t in component:
                continue
            component.append(t)
            queue.extend(sorted(edges.get(t, set()) - set(component)))
        ordered = [t for t in requested if t in component] + [t for t in component if t not in requested]
        placed.update(ordered)
        modified = any(
            draft.lanes[t].publish.published_hash is not None
            and engine.content_hash(draft.lanes[t]) != draft.lanes[t].publish.published_hash
            for t in ordered
        )
        redispatch = modified or any(t in holds_dispatched for t in ordered) or len(ordered) > 1
        groups.append(
            PublishGroup(
                truck_ids=ordered,
                kind="redispatch" if redispatch else "first_publish",
                added_lanes=[t for t in ordered if t not in requested],
            )
        )
    return groups


# ---------------------------------------------------------------------------
# Amend (K8.4 phase 4, P9)
# ---------------------------------------------------------------------------


@dataclass
class AmendResult:
    stops: List[Dict[str, Any]]
    kept_completed: List[str]
    changed: bool
    completed_stops: int
    total_stops: int


def _record_order(record: Mapping[str, Any], seq_to_order: Mapping[int, str]) -> Optional[str]:
    return record.get("order_id") or seq_to_order.get(int(record.get("sequence") or 0))


def amend_stops(
    execution_stops: Sequence[Mapping[str, Any]],
    route_stops: Sequence[Mapping[str, Any]],
    target_order_ids: Sequence[str],
    pinned_order_ids: Iterable[str],
) -> AmendResult:
    """Pure amend transform over the stored execution stops (freeze rule 6, I16, P9).

    Every non-``pending`` record and every pinned stop keeps its record,
    sequence included. Every other pending stop of the target order gets a
    fresh sequence above the highest sequence on the route or execution, in
    the target order. Pending records of removed stops are dropped; a removed
    stop that is no longer pending is kept and reported. Running it again on
    its own output changes nothing.
    """
    pinned = set(pinned_order_ids)
    seq_to_order: Dict[int, str] = {}
    for stop in route_stops:
        ids = stop.get("order_ids") or []
        if ids:
            seq_to_order[int(stop.get("sequence") or 0)] = ids[0]
    records = [dict(r) for r in execution_stops]
    target = list(target_order_ids)
    fixed: List[Dict[str, Any]] = []
    movable: Dict[str, Dict[str, Any]] = {}
    kept_completed: List[str] = []
    removed: List[str] = []
    for record in records:
        order_id = _record_order(record, seq_to_order)
        if record.get("status") != "pending" or (order_id in pinned):
            fixed.append(record)
            if order_id not in target and record.get("status") != "pending":
                kept_completed.append(order_id or "")
            continue
        if order_id in target:
            movable[order_id] = record
        else:
            removed.append(order_id or "")
    order_now = [o for o in target if o in movable]
    current_order = [
        _record_order(r, seq_to_order)
        for r in sorted(movable.values(), key=lambda r: int(r.get("sequence") or 0))
    ]
    fixed_max = max((int(r.get("sequence") or 0) for r in fixed), default=0)
    already = (
        not removed
        and current_order == order_now
        and all(int(r.get("sequence") or 0) > fixed_max for r in movable.values())
        and all(r.get("order_id") for r in movable.values())
    )
    if already:
        out = sorted(fixed + list(movable.values()), key=lambda r: int(r.get("sequence") or 0))
        changed = False
    else:
        high = max(
            [int(r.get("sequence") or 0) for r in records] + [int(s.get("sequence") or 0) for s in route_stops] + [0]
        )
        renumbered = [
            {**movable[o], "sequence": high + i, "order_id": o} for i, o in enumerate(order_now, start=1)
        ]
        out = sorted(fixed + renumbered, key=lambda r: int(r.get("sequence") or 0))
        changed = True
    completed = sum(1 for r in out if r.get("status") == "completed")
    return AmendResult(
        stops=out,
        kept_completed=[k for k in kept_completed if k],
        changed=changed,
        completed_stops=completed,
        total_stops=len(out),
    )


def route_stops_for(records: Sequence[Mapping[str, Any]], previous_route_stops: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Route stops matching an amended execution's records (the route always matches)."""
    by_order: Dict[str, Dict[str, Any]] = {}
    by_seq: Dict[int, Dict[str, Any]] = {}
    for stop in previous_route_stops:
        by_seq[int(stop.get("sequence") or 0)] = dict(stop)
        for order_id in stop.get("order_ids") or []:
            by_order[order_id] = dict(stop)
    out = []
    for record in records:
        seq = int(record.get("sequence") or 0)
        order_id = record.get("order_id")
        base = by_order.get(order_id) if order_id else None
        if base is None:
            base = by_seq.get(seq) or {
                "station_id": record.get("station_id"),
                "order_ids": [order_id] if order_id else [],
                "drop": dict(record.get("planned_quantities") or {}),
            }
        out.append({**base, "sequence": seq})
    return out


# ---------------------------------------------------------------------------
# Preflight result
# ---------------------------------------------------------------------------


@dataclass
class _Preflight:
    groups: List[PublishGroup] = field(default_factory=list)
    lanes: Dict[str, Lane] = field(default_factory=dict)  # refreshed copies of the claimed lanes
    checks: Dict[str, List[Check]] = field(default_factory=dict)
    not_ready: Dict[str, List[str]] = field(default_factory=dict)
    changes: Dict[str, LoadChange] = field(default_factory=dict)
    attempts: Dict[str, RedispatchAttempt] = field(default_factory=dict)  # truck_id -> attempt
    already_published: List[str] = field(default_factory=list)
    open_warnings: List[Dict[str, Any]] = field(default_factory=list)
    versions: Dict[str, int] = field(default_factory=dict)
    ctx: Optional[ValidationContext] = None
    resume_groups: List[Tuple[PublishGroup, RedispatchAttempt]] = field(default_factory=list)


class _LeaseLost(Exception):
    """Another publish request took the lanes over; this worker stops silently."""


class _GroupFailure(Exception):
    def __init__(self, result: Dict[str, Any]) -> None:
        super().__init__(result.get("reason") or result.get("stage") or "failed")
        self.result = result


class _StepError(Exception):
    """A forward step (apply, amend) that did not complete; retried in place."""

    def __init__(self, stage: str, reason: str, *, writes_made: bool = True, retryable: bool = True,
                 failures: Optional[List[Dict[str, Any]]] = None) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.writes_made = writes_made
        self.retryable = retryable
        self.failures = failures or []


# ---------------------------------------------------------------------------
# Shared store helpers
# ---------------------------------------------------------------------------


def _is_rollback_retired_stage(doc: Mapping[str, Any], attempt_id: Optional[str]) -> bool:
    """A staged doc that a rollback retired: staged by an attempt, never applied,
    never seen by a driver (R25), so a later attempt may rewrite it."""
    return (
        doc.get("status") == "superseded"
        and bool(doc.get("created_by_attempt"))
        and doc.get("created_by_attempt") != attempt_id
        and not doc.get("superseded_by_attempt")
        and not doc.get("execution_status")
    )


async def upsert_board_plan(es: Any, doc: Dict[str, Any], *, attempt_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """K7.3 step 2: create as ``draft``; while still ``draft`` overwrite the whole
    K7.3a header (terminal, shift, load order, driver, assignments) so an edit
    between a failed publish and its Retry lands (P10); any other status means
    already applied (the executor replays)."""
    tenant_id = doc["tenant_id"]

    def transform(cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if cur.get("tenant_id") != tenant_id or cur.get("board_load_id") != doc["board_load_id"]:
            return None
        if cur.get("status") == "draft":
            patched = {**cur, **{k: v for k, v in doc.items() if k != "created_at"}}
            if attempt_id:
                patched["created_by_attempt"] = attempt_id
            return patched
        if _is_rollback_retired_stage(cur, attempt_id):
            return dict(doc)
        return None

    stored, _applied = await es.atomic_update(MVP_LOAD_PLANS_INDEX, doc["plan_id"], transform, upsert=doc)
    return stored


async def upsert_board_route(es: Any, doc: Dict[str, Any], *, attempt_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """K7.3 step 3: like the plan, keyed by ``route_id``; stops change only while ``planned``."""
    tenant_id = doc["tenant_id"]

    def transform(cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if cur.get("tenant_id") != tenant_id or cur.get("board_load_id") != doc["board_load_id"]:
            return None
        if cur.get("status") == "planned":
            patched = {**cur, "stops": doc["stops"], "timestamp": doc["timestamp"], "updated_at": doc["updated_at"]}
            if "distance_km" in doc:
                patched["distance_km"] = doc["distance_km"]
            if attempt_id:
                patched["created_by_attempt"] = attempt_id
            return patched
        if _is_rollback_retired_stage(cur, attempt_id):
            return dict(doc)
        return None

    stored, _applied = await es.atomic_update(MVP_ROUTES_INDEX, doc["route_id"], transform, upsert=doc)
    return stored


async def update_lanes(
    es: Any,
    tenant_id: str,
    service_date: date,
    truck_ids: Sequence[str],
    mutate: Callable[[Lane], None],
    *,
    owner: Optional[str],
    attempt_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[BoardDraft]:
    """One draft ``atomic_update`` over ``truck_ids``, refused unless every lane is
    still owned by ``owner`` (the lease holder) and, when given, carries attempt
    ``attempt_id``. Returns the stored draft, or ``None`` when refused."""
    stamp = now or _utcnow()

    def transform(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        current = BoardDraft.model_validate(doc)
        for truck_id in truck_ids:
            lane = current.lanes.get(truck_id)
            if lane is None or lane.publish.attempt_id != owner:
                return None
            if attempt_id is not None and (lane.publish.attempt is None or lane.publish.attempt.attempt_id != attempt_id):
                return None
        for truck_id in truck_ids:
            mutate(current.lanes[truck_id])
        current.updated_at = stamp
        return current.model_dump(mode="json")

    stored, applied = await es.atomic_update(DISPATCH_BOARD_DRAFTS_INDEX, draft_doc_id(tenant_id, service_date), transform)
    return BoardDraft.model_validate(stored) if applied and stored else None


async def load_board_draft(es: Any, tenant_id: str, service_date: date) -> Optional[BoardDraft]:
    doc = await es.get_document(DISPATCH_BOARD_DRAFTS_INDEX, draft_doc_id(tenant_id, service_date))
    if not doc or doc.get("tenant_id") != tenant_id:
        return None
    return BoardDraft.model_validate(doc)


# ---------------------------------------------------------------------------
# BoardRedispatchService (K8)
# ---------------------------------------------------------------------------


@dataclass
class _Run:
    tenant_id: str
    service_date: date
    publish_id: str
    actor_user_id: str
    truck_ids: List[str]
    draft: BoardDraft
    attempt: RedispatchAttempt
    load_lane: Dict[str, str] = field(default_factory=dict)
    dropped: Dict[str, List[str]] = field(default_factory=dict)
    kept_completed: Dict[str, List[str]] = field(default_factory=dict)
    notifications_failed: List[str] = field(default_factory=list)
    empty_staged: Set[str] = field(default_factory=set)

    @property
    def attempt_id(self) -> str:
        return self.attempt.attempt_id

    def lane(self, truck_id: str) -> Lane:
        return self.draft.lanes[truck_id]

    def items(self, classes: Iterable[str]) -> List[Tuple[Lane, Optional[Load], str, AttemptLoad, int]]:
        """Attempt loads of the given classes: lanes in group order, loads in time order."""
        wanted = set(classes)
        out: List[Tuple[Lane, Optional[Load], str, AttemptLoad, int]] = []
        for truck_id in self.truck_ids:
            lane = self.lane(truck_id)
            seen: Set[str] = set()
            for seq, load in enumerate(lane.loads, start=1):
                al = self.attempt.loads.get(load.load_id)
                seen.add(load.load_id)
                if al is not None and al.load_class in wanted:
                    out.append((lane, load, load.load_id, al, seq))
            for load_id in lane.publish.plans:
                al = self.attempt.loads.get(load_id)
                if load_id not in seen and al is not None and al.load_class in wanted:
                    out.append((lane, None, load_id, al, 0))
        return out

    def content_after(self, truck_id: str) -> LaneContent:
        """The lane content this attempt publishes: the claimed content minus dropped orders."""
        lane = self.lane(truck_id)
        dropped = set(self.dropped.get(truck_id, []))
        content = lane.content().model_copy(deep=True)
        for load in content.loads:
            load.stops = [s for s in load.stops if s.order_id not in dropped]
            for order_id in dropped:
                load.allocation_overrides.pop(order_id, None)
        return content


def _result(**fields: Any) -> PublishResult:
    return PublishResult(**{k: v for k, v in fields.items() if v is not None})


class BoardRedispatchService:
    """Publish groups that hold a dispatched order or a Modified lane (K8.4).

    The phases run across all lanes of the group, each recorded in
    ``attempt.phase`` before it starts: retire, stage and relink (reversible),
    then apply, amend, invalidate, notify, finalize. A failure before apply is
    rolled back to the exact pre-attempt documents; a failure from apply on is
    completed forward from the recorded attempt (freeze rule 10). Drivers hear
    nothing until every operational write and the cache invalidation are done
    (freeze rules 4, 8).
    """

    def __init__(
        self,
        *,
        es_service: Any,
        order_repository: Any,
        executor: Any,
        dispatch_service: Any,
        execution_service: Any = None,
        driver_ws_manager: Any = None,
        orders_ws_manager: Any = None,
        work_cache_invalidator: Optional[Callable[[str, str], Awaitable[None]]] = None,
        telemetry: Optional[tm.BoardTelemetry] = None,
        clock: Optional[Callable[[], datetime]] = None,
        lease_seconds: float = LEASE_SECONDS,
        retry_delays: Sequence[float] = RETRY_DELAYS,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        forward_warning_interval_s: float = FORWARD_WARNING_INTERVAL_S,
        on_phase: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
    ) -> None:
        self._es = es_service
        self._orders = order_repository
        self._executor = executor
        self._dispatch = dispatch_service
        self._executions = execution_service
        self._driver_ws = driver_ws_manager
        self._orders_ws = orders_ws_manager
        self._invalidate = work_cache_invalidator
        self.telemetry = telemetry or tm.BoardTelemetry()
        self._clock = clock or _utcnow
        self._lease = float(lease_seconds)
        self._retry_delays = tuple(retry_delays)
        self._sleep = sleep or asyncio.sleep
        self._warn_interval = float(forward_warning_interval_s)
        #: Called after each phase is recorded, before it runs (tests and tracing).
        self.on_phase = on_phase
        self._tasks: Set["asyncio.Task[Any]"] = set()

    def now(self) -> datetime:
        return self._clock()

    # -- entry points ------------------------------------------------------

    async def run_group(
        self,
        *,
        tenant_id: str,
        service_date: date,
        publish_id: str,
        truck_ids: Sequence[str],
        actor_user_id: str,
    ) -> str:
        """Run (or resume) the recorded attempt of a claimed group. Returns the outcome."""
        draft = await load_board_draft(self._es, tenant_id, service_date)
        if draft is None or any(t not in draft.lanes for t in truck_ids):
            logger.warning("dispatch board redispatch: draft or lane missing tenant=%s", tenant_id)
            return "lease_lost"
        lanes = [draft.lanes[t] for t in truck_ids]
        attempt = lanes[0].publish.attempt
        if attempt is None or any(l.publish.attempt_id != publish_id for l in lanes):
            return "lease_lost"
        run = _Run(
            tenant_id=tenant_id,
            service_date=service_date,
            publish_id=publish_id,
            actor_user_id=actor_user_id,
            truck_ids=list(attempt.group_truck_ids) or list(truck_ids),
            draft=draft,
            attempt=attempt,
        )
        for truck_id in run.truck_ids:
            lane = run.lane(truck_id)
            for load in lane.loads:
                run.load_lane[load.load_id] = truck_id
            for load_id in lane.publish.plans:
                run.load_lane.setdefault(load_id, truck_id)
            if lane.publish.last_result is not None:
                run.dropped[truck_id] = list(lane.publish.last_result.dropped_orders)
        started = _time.monotonic()
        try:
            outcome = await self._run(run)
        except _LeaseLost:
            logger.warning("dispatch board redispatch: lease taken over tenant=%s publish=%s", tenant_id, publish_id)
            outcome = "lease_lost"
        self.telemetry.metric(tm.REDISPATCH_COUNT, 1, tenant_id=tenant_id, result=outcome)
        self.telemetry.metric(tm.PUBLISH_LANE_MS, (_time.monotonic() - started) * 1000, tenant_id=tenant_id, stage="redispatch", result=outcome)
        return outcome

    async def _run(self, run: _Run) -> str:
        phase = run.attempt.phase
        if run.attempt.recovery == "rollback" or phase in ROLLBACK_PHASES and phase != "claimed":
            # An interrupted reversible phase: undo it; the caller publishes fresh.
            done = await self.rollback(run, failure={"stage": phase, "reason": "interrupted"})
            return "rolled_back" if done else "failed"
        if phase == "claimed":
            try:
                await self._phase(run, "retire")
                await self._retire(run)
                await self._phase(run, "stage_relink")
                await self._stage(run)
                await self._relink(run)
            except (_LeaseLost, asyncio.CancelledError):
                raise
            except _GroupFailure as failure:
                logger.warning(
                    "dispatch board redispatch rolled back: stage=%s reason=%s",
                    failure.result.get("stage"), failure.result.get("reason"),
                )
                done = await self.rollback(run, failure=failure.result)
                return "rolled_back" if done else "failed"
            except Exception as exc:
                logger.error("dispatch board redispatch error before apply: %s", type(exc).__name__, exc_info=True)
                done = await self.rollback(run, failure={"stage": run.attempt.phase, "reason": "internal_error"})
                return "rolled_back" if done else "failed"
            phase = "apply"
        start = FORWARD_PHASES.index(phase) if phase in FORWARD_PHASES else 0
        try:
            for name in FORWARD_PHASES[start:]:
                await self._phase(run, name)
                if name == "apply":
                    await self._apply(run)
                elif name == "amend":
                    await self._amend(run)
                elif name == "notify":
                    await self._invalidate_all(run)
                    await self._notify(run)
                else:
                    await self._finalize(run)
        except (_LeaseLost, asyncio.CancelledError):
            raise
        except _StepError as err:
            await self._mark_forward_failed(run, err)
            return "failed"
        except Exception as exc:
            logger.error("dispatch board redispatch error after apply: %s", type(exc).__name__, exc_info=True)
            await self._mark_forward_failed(run, _StepError(run.attempt.phase, "internal_error"))
            return "failed"
        return "published"

    # -- phase bookkeeping ---------------------------------------------------

    async def _phase(self, run: _Run, name: str) -> None:
        lease = self.now() + timedelta(seconds=self._lease)

        def mutate(lane: Lane) -> None:
            assert lane.publish.attempt is not None
            lane.publish.attempt.phase = name  # type: ignore[assignment]
            lane.publish.lease_until = lease

        stored = await update_lanes(
            self._es, run.tenant_id, run.service_date, run.truck_ids, mutate,
            owner=run.publish_id, attempt_id=run.attempt_id, now=self.now(),
        )
        if stored is None:
            raise _LeaseLost()
        run.attempt.phase = name  # type: ignore[assignment]
        if self.on_phase is not None:
            await self.on_phase(name, {"publish_id": run.publish_id, "attempt_id": run.attempt_id, "truck_ids": list(run.truck_ids)})

    async def _renew(self, run: _Run) -> None:
        """Renew ``lease_until`` on every group lane before each load, relink and
        in-place retry (K7.3, freeze rule 5). Refused once a Retry has taken the
        group over, which stops this worker before its next write."""
        lease = self.now() + timedelta(seconds=self._lease)

        def mutate(lane: Lane) -> None:
            lane.publish.lease_until = lease

        stored = await update_lanes(
            self._es, run.tenant_id, run.service_date, run.truck_ids, mutate,
            owner=run.publish_id, attempt_id=run.attempt_id, now=self.now(),
        )
        if stored is None:
            raise _LeaseLost()

    def _old(self, run: _Run, load_id: str) -> PublishedPlan:
        return run.lane(run.load_lane[load_id]).publish.plans[load_id]

    # -- phase 1: retire -----------------------------------------------------

    async def _find_executions(self, tenant_id: str, plan_id: str, route_id: str) -> List[Dict[str, Any]]:
        query = {
            "query": {"bool": {"filter": [
                {"term": {"tenant_id": tenant_id}},
                {"term": {"plan_id": plan_id}},
                {"term": {"route_id": route_id}},
            ]}},
            "size": 10,
        }
        return [d for d in _hits(await self._es.search_documents(MVP_PLAN_EXECUTIONS_INDEX, query, 10)) if d.get("tenant_id") == tenant_id]

    async def _retire(self, run: _Run) -> None:
        for lane, _load, load_id, al, _seq in run.items(("new_revision", "removed")):
            await self._renew(run)
            old = self._old(run, load_id)
            new_plan_id = al.plan_id if al.load_class == "new_revision" else None
            await self._retire_executions(run, old)
            await self._retire_doc(run, MVP_LOAD_PLANS_INDEX, old.plan_id, ("dispatched", "scheduled"), new_plan_id=new_plan_id)
            await self._retire_doc(run, MVP_ROUTES_INDEX, old.route_id, ("dispatched", "planned"))

    async def _retire_executions(self, run: _Run, old: PublishedPlan) -> None:
        now = self.now().isoformat()
        aid = run.attempt_id
        for execution in await self._find_executions(run.tenant_id, old.plan_id, old.route_id):
            verdict: Dict[str, bool] = {}

            def transform(cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                if cur.get("status") == "superseded":
                    verdict["done"] = cur.get("superseded_by_attempt") == aid
                    return None
                stops = cur.get("stops") or []
                if int(cur.get("completed_stops") or 0) != 0 or any(s.get("status") != "pending" for s in stops):
                    return None
                return {
                    **cur,
                    "status": "superseded",
                    "superseded_by_attempt": aid,
                    "status_before_retire": cur.get("status"),
                    "updated_at": now,
                }

            _doc, applied = await self._es.atomic_update(MVP_PLAN_EXECUTIONS_INDEX, execution["execution_id"], transform)
            if not applied and not verdict.get("done"):
                raise _GroupFailure({"stage": "retire", "reason": "load_started_concurrently"})

    async def _retire_doc(
        self, run: _Run, index: str, doc_id: str, from_statuses: Tuple[str, ...], *, new_plan_id: Optional[str] = None
    ) -> None:
        now = self.now().isoformat()
        aid = run.attempt_id
        tenant_id = run.tenant_id
        verdict: Dict[str, bool] = {}

        def transform(cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if cur.get("tenant_id") != tenant_id:
                return None
            if cur.get("status") == "superseded":
                verdict["done"] = cur.get("superseded_by_attempt") == aid
                return None
            if cur.get("status") not in from_statuses:
                return None
            out = {**cur, "status": "superseded", "superseded_by_attempt": aid, "status_before_retire": cur.get("status"), "updated_at": now}
            if new_plan_id:
                out["superseded_by_plan_id"] = new_plan_id
            return out

        doc, applied = await self._es.atomic_update(index, doc_id, transform)
        if doc is None:
            return  # nothing was ever written for this load (e.g. a failed first publish)
        if not applied and not verdict.get("done"):
            raise _GroupFailure({"stage": "retire", "reason": "load_started_concurrently"})

    # -- phase 2: stage and relink -------------------------------------------

    async def _stage(self, run: _Run) -> None:
        now = self.now()
        for lane, load, load_id, al, seq in run.items(("new", "new_revision")):
            assert load is not None
            await self._renew(run)
            supersedes = (None, None)
            if al.load_class == "new_revision":
                old = self._old(run, load_id)
                supersedes = (old.plan_id, old.route_id)
            plan = build_plan_doc(
                tenant_id=run.tenant_id, service_date=run.service_date, lane=lane, load=load, load_seq=seq,
                revision=al.revision, now=now, created_by_attempt=run.attempt_id, supersedes_plan_id=supersedes[0],
            )
            route = build_route_doc(
                tenant_id=run.tenant_id, service_date=run.service_date, lane=lane, load=load,
                revision=al.revision, now=now, created_by_attempt=run.attempt_id, supersedes_route_id=supersedes[1],
            )
            await upsert_board_plan(self._es, plan, attempt_id=run.attempt_id)
            await upsert_board_route(self._es, route, attempt_id=run.attempt_id)

    async def _relink(self, run: _Run) -> None:
        for entry in run.attempt.relinks:
            await self._renew(run)
            outcome = await self._relink_one(run, entry.order_id, entry.from_link, entry.to)
            if outcome == "refused":
                current = await self._orders.get_current(run.tenant_id, entry.order_id)
                raise _GroupFailure(
                    {
                        "stage": "relink",
                        "reason": "relink_refused",
                        "order_id": entry.order_id,
                        "observed_status": (current or {}).get("status"),
                    }
                )

    async def _relink_one(self, run: _Run, order_id: str, src: Link, dst: Link) -> str:
        outcome = await self._orders.relink_dispatched_assignment(
            run.tenant_id,
            order_id,
            from_run_id=src.run_id or "",
            from_asset_id=src.truck_id or "",
            from_driver_id=src.driver_id,
            to_run_id=dst.run_id or "",
            to_asset_id=dst.truck_id or "",
            to_driver_id=dst.driver_id,
            claim_id=run.attempt_id,
        )
        if outcome == "relinked":
            await self._reassigned_event(run, order_id, src, dst)
        return outcome

    async def _reassigned_event(self, run: _Run, order_id: str, src: Link, dst: Link) -> None:
        from fuel.services.order_id_generator import mint_event_id

        now = self.now()
        try:
            await self._orders.append_event(
                run.tenant_id,
                {
                    "event_id": mint_event_id(),
                    "order_id": order_id,
                    "tenant_id": run.tenant_id,
                    "event_type": "order_reassigned",
                    "event_payload": {
                        "from": src.model_dump(mode="json"),
                        "to": dst.model_dump(mode="json"),
                        "from_plan_id": src.run_id,
                        "to_plan_id": dst.run_id,
                        "attempt_id": run.attempt_id,
                        "actor_user_id": run.actor_user_id,
                        "source": BOARD_SOURCE,
                    },
                    "event_timestamp": now,
                    "ingested_at": now,
                    "source_schema_version": "1.0",
                    "trace_id": run.attempt_id,
                },
            )
        except Exception as exc:
            logger.warning("dispatch board order_reassigned event failed: %s", type(exc).__name__)

    # -- phase 3: apply ------------------------------------------------------

    async def _with_retries(self, run: _Run, stage: str, step: Callable[[], Awaitable[None]]) -> None:
        last: Optional[_StepError] = None
        for attempt, delay in enumerate((0.0,) + self._retry_delays):
            if attempt:
                await self._sleep(delay)
            await self._renew(run)
            try:
                await step()
                return
            except (_LeaseLost, asyncio.CancelledError):
                raise
            except _StepError as err:
                last = err
            except AppException as exc:
                code = getattr(exc.error_code, "value", exc.error_code)
                last = _StepError(stage, str((exc.details or {}).get("reason") or code))
            except Exception as exc:
                last = _StepError(stage, "internal_error")
                logger.warning("dispatch board %s step raised %s", stage, type(exc).__name__)
            logger.warning("dispatch board %s step failed (try %d): %s", stage, attempt + 1, last.reason if last else "")
        assert last is not None
        logger.error("dispatch board %s step failed after retries: %s", stage, last.reason)
        raise last

    async def _apply(self, run: _Run) -> None:
        for lane, load, load_id, al, _seq in run.items(("new", "new_revision")):
            assert load is not None
            await self._with_retries(run, "apply", lambda lane=lane, load=load, al=al: self._apply_load(run, lane, load, al))

    async def _apply_load(self, run: _Run, lane: Lane, load: Load, al: AttemptLoad) -> None:
        tenant_id = run.tenant_id
        for _round in range(len(load.stops) + 2):
            plan = await self._es.get_document(MVP_LOAD_PLANS_INDEX, al.plan_id)
            if not plan or plan.get("tenant_id") != tenant_id:
                raise _StepError("apply", "plan_missing")
            if plan.get("status") in ("dispatched", "completed"):
                return  # dispatched by this attempt already (resume)
            expected = sorted({a["order_id"] for a in plan.get("assignments") or [] if a.get("order_id")})
            if not expected:
                await self._retire_empty_stage(run, al)
                return
            if plan.get("execution_status") != "succeeded":
                snapshots = {k: v for k, v in order_snapshots_for(load).items() if k in expected}
                result = await self._executor.execute(
                    tenant_id=tenant_id,
                    plan_id=al.plan_id,
                    expected_order_ids=expected,
                    expected_truck_id=lane.truck_id,
                    order_snapshots=snapshots,
                    actor_user_id=run.actor_user_id,
                    action_id=None,
                    approved_at=self.now().isoformat(),
                    mode="active_gated",
                )
                if not result.success:
                    applied = set(result.applied_order_ids)
                    drops = [
                        f.get("order_id") for f in result.failures
                        if f.get("reason") in _DROP_EXECUTOR_REASONS and f.get("order_id") and f.get("order_id") not in applied
                    ]
                    if drops and len(drops) == len(result.failures) and self._can_drop(run, lane.truck_id, drops):
                        for order_id in drops:
                            await self._drop(run, lane.truck_id, al, order_id, reason=result.reason or "order_changed_since_plan")
                        continue
                    raise _StepError(
                        "apply", result.reason or "apply_failed", writes_made=bool(result.writes_made),
                        retryable=bool(result.retryable), failures=list(result.failures),
                    )
                plan = await self._es.get_document(MVP_LOAD_PLANS_INDEX, al.plan_id)
            try:
                await self._dispatch.dispatch(
                    tenant_id=tenant_id, plan_doc=plan, actor_user_id=run.actor_user_id,
                    driver_id=lane.driver_id, notify=False,
                )
                return
            except AppException as exc:
                details = exc.details or {}
                reason = str(details.get("reason") or "dispatch_failed")
                order_id = details.get("order_id")
                if reason == "order_not_dispatchable" and order_id and self._can_drop(run, lane.truck_id, [order_id]):
                    await self._drop(run, lane.truck_id, al, order_id, reason=reason)
                    continue
                raise _StepError("dispatch", reason) from None
        raise _StepError("apply", "drop_limit")

    def _can_drop(self, run: _Run, truck_id: str, order_ids: Iterable[str]) -> bool:
        """Each order is dropped at most once (drop rule)."""
        already = set(run.dropped.get(truck_id, []))
        return all(o not in already for o in order_ids)

    async def _drop(self, run: _Run, truck_id: str, al: AttemptLoad, order_id: str, *, reason: str) -> None:
        """Drop rule: remove an order from a staged, unseen plan and route; renumber 1..k."""
        now = self.now().isoformat()

        def plan_tx(cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if cur.get("status") not in ("draft", "scheduled"):
                return None
            kept = [a for a in cur.get("assignments") or [] if a.get("order_id") != order_id]
            return {**cur, "assignments": kept, "updated_at": now}

        def route_tx(cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if cur.get("status") != "planned":
                return None
            stops = [dict(s) for s in cur.get("stops") or [] if order_id not in (s.get("order_ids") or [])]
            for seq, stop in enumerate(stops, start=1):
                stop["sequence"] = seq
            return {**cur, "stops": stops, "updated_at": now}

        await self._es.atomic_update(MVP_LOAD_PLANS_INDEX, al.plan_id, plan_tx)
        await self._es.atomic_update(MVP_ROUTES_INDEX, al.route_id, route_tx)
        run.dropped.setdefault(truck_id, []).append(order_id)
        dropped = list(run.dropped[truck_id])
        logger.info("dispatch board drop rule: order dropped from staged load reason=%s", reason)

        def mutate(lane: Lane) -> None:
            base = lane.publish.last_result or PublishResult()
            lane.publish.last_result = base.model_copy(update={"dropped_orders": dropped})

        if await update_lanes(self._es, run.tenant_id, run.service_date, [truck_id], mutate, owner=run.publish_id, attempt_id=run.attempt_id) is None:
            raise _LeaseLost()

    async def _retire_empty_stage(self, run: _Run, al: AttemptLoad) -> None:
        """Every order of a staged load was dropped: retire the staged docs."""
        now = self.now().isoformat()

        def tx(cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if cur.get("status") not in ("draft", "planned", "scheduled"):
                return None
            return {**cur, "status": "superseded", "updated_at": now}

        await self._es.atomic_update(MVP_LOAD_PLANS_INDEX, al.plan_id, tx)
        await self._es.atomic_update(MVP_ROUTES_INDEX, al.route_id, tx)
        run.empty_staged.add(al.plan_id)

    # -- phase 4: amend ------------------------------------------------------

    async def _amend(self, run: _Run) -> None:
        for lane, load, load_id, al, _seq in run.items(("amend",)):
            await self._with_retries(run, "amend", lambda lane=lane, load=load, al=al: self._amend_load(run, lane, load, al))

    async def _amend_load(self, run: _Run, lane: Lane, load: Optional[Load], al: AttemptLoad) -> None:
        tenant_id = run.tenant_id
        now = self.now().isoformat()
        route = await self._es.get_document(MVP_ROUTES_INDEX, al.route_id)
        if not route or route.get("tenant_id") != tenant_id:
            raise _StepError("amend", "route_missing")
        executions = await self._find_executions(tenant_id, al.plan_id, al.route_id)
        if not executions:
            raise _StepError("amend", "execution_missing")
        execution_id = executions[0]["execution_id"]
        target = [s.order_id for s in load.stops] if load is not None else []
        route_stops = list(route.get("stops") or [])
        order_ids = {o for s in route_stops for o in s.get("order_ids") or []} | set(target)
        pinned: Set[str] = set()
        for order_id in order_ids:
            if engine.is_pinned(await self._orders.get_current(tenant_id, order_id)):
                pinned.add(order_id)

        def exec_tx(cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            res = amend_stops(cur.get("stops") or [], route_stops, target, pinned)
            status = "completed" if res.completed_stops >= res.total_stops else (cur.get("status") or "in_progress")
            if not res.changed and cur.get("total_stops") == res.total_stops and cur.get("status") == status:
                return None
            return {**cur, "stops": res.stops, "total_stops": res.total_stops, "completed_stops": res.completed_stops, "status": status, "updated_at": now}

        stored, _applied = await self._es.atomic_update(MVP_PLAN_EXECUTIONS_INDEX, execution_id, exec_tx)
        if stored is None:
            raise _StepError("amend", "execution_missing")
        final = amend_stops(stored.get("stops") or [], route_stops, target, pinned)
        if final.kept_completed:
            logger.warning("dispatch board amend kept %d stop(s) completed concurrently", len(final.kept_completed))
            kept = run.kept_completed.setdefault(lane.truck_id, [])
            kept.extend(o for o in final.kept_completed if o not in kept)
        new_route_stops = route_stops_for(stored.get("stops") or [], route_stops)
        finished = stored.get("status") == "completed"
        expected = al.revision

        def route_tx(cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            rev = int(cur.get("revision") or 1)
            if rev == expected:
                return {**cur, "stops": new_route_stops, "revision": rev + 1, "updated_at": now}
            return None

        doc, applied = await self._es.atomic_update(MVP_ROUTES_INDEX, al.route_id, route_tx)
        if not applied and int((doc or {}).get("revision") or 0) != expected + 1:
            raise _StepError("amend", "route_changed")
        keep_orders = {r.get("order_id") for r in stored.get("stops") or []} | {
            o for s in new_route_stops for o in s.get("order_ids") or []
        }
        became_completed: Dict[str, bool] = {}

        def plan_tx(cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            rev = int(cur.get("revision") or 1)
            out = dict(cur)
            changed = False
            if rev == expected:
                out["assignments"] = [a for a in cur.get("assignments") or [] if a.get("order_id") in keep_orders]
                out["revision"] = rev + 1
                changed = True
            elif rev != expected + 1:
                return None
            if finished and cur.get("status") == "dispatched":
                out["status"] = "completed"
                became_completed["yes"] = True
                changed = True
            if not changed:
                return None
            out["updated_at"] = now
            return out

        doc, applied = await self._es.atomic_update(MVP_LOAD_PLANS_INDEX, al.plan_id, plan_tx)
        if not applied and int((doc or {}).get("revision") or 0) != expected + 1:
            raise _StepError("amend", "plan_changed")
        if became_completed and self._executions is not None:
            # Freeze rule 11 (c): finish the load exactly like the last check-in does.
            for name in ("compute_outcomes", "compute_actual_cost"):
                try:
                    await getattr(self._executions, name)(al.plan_id, tenant_id)
                except Exception as exc:
                    logger.warning("dispatch board amend %s failed: %s", name, type(exc).__name__)

    # -- phases 5-6: invalidate and notify -----------------------------------

    def _touched_orders(self, run: _Run) -> List[str]:
        out: Set[str] = set()
        for truck_id in run.truck_ids:
            lane = run.lane(truck_id)
            for load_id, al in run.attempt.loads.items():
                if run.load_lane.get(load_id) != truck_id:
                    continue
                current = next((l for l in lane.loads if l.load_id == load_id), None)
                if current is not None:
                    out.update(s.order_id for s in current.stops)
                before = engine.published_load(lane, load_id)
                if before is not None:
                    out.update(s.order_id for s in before.stops)
        out.update(r.order_id for r in run.attempt.relinks)
        return sorted(out)

    async def _invalidate_all(self, run: _Run) -> None:
        if self._invalidate is None:
            return
        for order_id in self._touched_orders(run):
            try:
                await self._invalidate(run.tenant_id, order_id)
            except Exception as exc:
                logger.warning("dispatch board work cache invalidation failed: %s", type(exc).__name__)

    def _old_plan_of(self, run: _Run, order_id: str) -> Optional[str]:
        for truck_id in run.truck_ids:
            lane = run.lane(truck_id)
            content = lane.publish.published_content
            if content is None:
                continue
            for load in content.loads:
                if any(s.order_id == order_id for s in load.stops):
                    plan = lane.publish.plans.get(load.load_id)
                    return plan.plan_id if plan else None
        return None

    async def _notify(self, run: _Run) -> None:
        ws = _resolve(self._driver_ws)
        failed: List[str] = []

        async def send(method: str, driver_id: Optional[str], payload: Dict[str, Any]) -> None:
            if not driver_id or ws is None:
                return
            try:
                await getattr(ws, method)(driver_id, payload)
            except Exception as exc:
                logger.warning("dispatch board driver notification failed: %s", type(exc).__name__)
                if driver_id not in failed:
                    failed.append(driver_id)

        after: Dict[str, Set[str]] = {}
        for truck_id in run.truck_ids:
            lane = run.lane(truck_id)
            if lane.driver_id:
                content = run.content_after(truck_id)
                after.setdefault(lane.driver_id, set()).update(s.order_id for l in content.loads for s in l.stops)
        service_day = run.service_date.isoformat()
        # Revocations first: a driver who loses and regains nothing ends with no stale work.
        for driver_id in sorted(run.attempt.notify_baseline):
            lost = sorted(set(run.attempt.notify_baseline[driver_id]) - after.get(driver_id, set()))
            if lost:
                plan_ids = sorted({p for p in (self._old_plan_of(run, o) for o in lost) if p})
                await send("send_assignment_revoked", driver_id, {
                    "order_ids": lost, "plan_ids": plan_ids, "reason": REVOKE_REASON, "service_date": service_day,
                })
        # Freeze rule 11 (d): assignment payloads rebuilt from stored documents.
        for lane, _load, _load_id, al, _seq in run.items(("new", "new_revision")):
            plan = await self._es.get_document(MVP_LOAD_PLANS_INDEX, al.plan_id)
            if not plan or plan.get("created_by_attempt") != run.attempt_id or plan.get("status") != "dispatched":
                continue
            routes = await self._routes_of(run.tenant_id, al.plan_id, lane.truck_id)
            await send("send_assignment", lane.driver_id, {
                "plan_id": plan["plan_id"],
                "run_id": plan.get("run_id") or plan["plan_id"],
                "truck_id": plan.get("truck_id"),
                "route_ids": sorted(r["route_id"] for r in routes if r.get("route_id")),
                "order_ids": sorted({o for r in routes for s in r.get("stops") or [] for o in s.get("order_ids") or []}),
            })
        for lane, _load, _load_id, al, _seq in run.items(("amend",)):
            route = await self._es.get_document(MVP_ROUTES_INDEX, al.route_id) or {}
            await send("send_new_route", lane.driver_id, {
                "plan_id": al.plan_id,
                "route_id": al.route_id,
                "run_id": al.run_id,
                "truck_id": lane.truck_id,
                "order_ids": [o for s in route.get("stops") or [] for o in s.get("order_ids") or []],
                "revision": route.get("revision"),
            })
        orders_ws = _resolve(self._orders_ws)
        if orders_ws is not None:
            for entry in run.attempt.relinks:
                try:
                    await orders_ws.broadcast_order_assigned({
                        "order_id": entry.order_id,
                        "tenant_id": run.tenant_id,
                        "driver_id": entry.to.driver_id,
                        "asset_id": entry.to.truck_id,
                        "run_id": entry.to.run_id,
                    })
                except Exception as exc:
                    logger.warning("dispatch board order_assigned broadcast failed: %s", type(exc).__name__)
        run.notifications_failed = failed

    async def _routes_of(self, tenant_id: str, plan_id: str, truck_id: str) -> List[Dict[str, Any]]:
        query = {"query": {"bool": {"filter": [
            {"term": {"tenant_id": tenant_id}}, {"term": {"plan_id": plan_id}}, {"term": {"truck_id": truck_id}},
        ]}}, "size": 50}
        return [d for d in _hits(await self._es.search_documents(MVP_ROUTES_INDEX, query, 50)) if d.get("tenant_id") == tenant_id]

    # -- phase 7: finalize ---------------------------------------------------

    async def _finalize(self, run: _Run) -> None:
        now = self.now()
        dropped = run.dropped
        contents = {t: run.content_after(t) for t in run.truck_ids}
        attempt = run.attempt

        def mutate(lane: Lane) -> None:
            truck_id = lane.truck_id
            plans = dict(lane.publish.plans)
            for load_id, al in attempt.loads.items():
                if run.load_lane.get(load_id) != truck_id:
                    continue
                if al.load_class == "removed" or al.plan_id in run.empty_staged:
                    plans.pop(load_id, None)
                elif al.load_class in ("new", "new_revision"):
                    plans[load_id] = PublishedPlan(plan_id=al.plan_id, route_id=al.route_id, run_id=al.run_id, revision=al.revision)
                elif al.load_class == "amend":
                    plans[load_id] = PublishedPlan(plan_id=al.plan_id, route_id=al.route_id, run_id=al.run_id, revision=al.revision + 1)
            content = contents[truck_id]
            lane.version += 1
            lane.publish.plans = plans
            lane.publish.state = "published"
            lane.publish.published_version = lane.version
            lane.publish.published_content = content
            lane.publish.published_hash = engine.content_hash(content)
            lane.publish.lease_until = None
            lane.publish.attempt = None
            lane.publish.last_result = _result(
                state="published",
                publish_id=run.publish_id,
                dropped_orders=list(dropped.get(truck_id, [])),
                kept_completed_stops=list(run.kept_completed.get(truck_id, [])),
                notifications_failed=list(run.notifications_failed),
                group_truck_ids=list(run.truck_ids),
                at=now,
            )

        stored = await update_lanes(self._es, run.tenant_id, run.service_date, run.truck_ids, mutate, owner=run.publish_id, attempt_id=run.attempt_id, now=now)
        if stored is None:
            raise _LeaseLost()

    # -- recovery ------------------------------------------------------------

    def _staged_driver_ids(self, run: _Run) -> List[str]:
        drivers = {lane.driver_id for lane, *_rest in run.items(("new", "new_revision")) if lane.driver_id}
        return sorted(drivers)

    async def _mark_forward_failed(self, run: _Run, err: _StepError) -> None:
        now = self.now()
        drivers = self._staged_driver_ids(run)

        def mutate(lane: Lane) -> None:
            assert lane.publish.attempt is not None
            lane.version += 1
            lane.publish.state = "failed"
            lane.publish.lease_until = None
            lane.publish.attempt.recovery = "forward"
            lane.publish.last_result = _result(
                state="failed", stage=err.stage, reason=err.reason, writes_made=True, retryable=True,
                recovery="forward", failures=list(err.failures), publish_id=run.publish_id,
                dropped_orders=list(run.dropped.get(lane.truck_id, [])), drivers_without_routes=drivers,
                group_truck_ids=list(run.truck_ids), at=now,
            )

        if await update_lanes(self._es, run.tenant_id, run.service_date, run.truck_ids, mutate, owner=run.publish_id, attempt_id=run.attempt_id, now=now) is None:
            raise _LeaseLost()
        logger.warning(
            "dispatch board forward recovery pending: stage=%s reason=%s lanes=%d drivers=%d",
            err.stage, err.reason, len(run.truck_ids), len(drivers),
        )
        self._spawn_watch(run.tenant_id, run.service_date, list(run.truck_ids), run.attempt_id)

    def _spawn_watch(self, tenant_id: str, service_date: date, truck_ids: List[str], attempt_id: str) -> None:
        if self._warn_interval <= 0:
            return
        task = asyncio.create_task(self._watch_forward(tenant_id, service_date, truck_ids, attempt_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _watch_forward(self, tenant_id: str, service_date: date, truck_ids: List[str], attempt_id: str) -> None:
        """WARNING every interval while the forward recovery is still pending (Q16)."""
        while True:
            await self._sleep(self._warn_interval)
            try:
                draft = await load_board_draft(self._es, tenant_id, service_date)
            except Exception:
                continue
            pending = [
                t for t in truck_ids
                if draft is not None and t in draft.lanes
                and draft.lanes[t].publish.attempt is not None
                and draft.lanes[t].publish.attempt.attempt_id == attempt_id
                and draft.lanes[t].publish.attempt.recovery == "forward"
            ]
            if not pending:
                return
            drivers = {draft.lanes[t].driver_id for t in pending if draft.lanes[t].driver_id}  # type: ignore[union-attr]
            logger.warning(
                "dispatch board forward recovery still pending: tenant=%s lanes=%d drivers=%d",
                tenant_id, len(pending), len(drivers),
            )

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def rollback(self, run: _Run, *, failure: Dict[str, Any]) -> bool:
        """Undo phases 1-2 of the attempt (K8.4 Rollback); idempotent. ``True`` when done."""
        try:
            inconsistent = await self._relink_back(run)
            if inconsistent:
                self.telemetry.metric(tm.RECOVERY_INCONSISTENT, 1, tenant_id=run.tenant_id)
                logger.error("dispatch board rollback found an order state the guards exclude")
                await self._mark_rollback_pending(run, {"stage": "restore", "reason": "recovery_inconsistent"})
                return False
            await self._retire_staged(run)
            await self._restore_retired(run)
        except (_LeaseLost, asyncio.CancelledError):
            raise
        except Exception as exc:
            logger.error("dispatch board rollback step failed: %s", type(exc).__name__)
            await self._mark_rollback_pending(run, {"stage": "restore", "reason": "rollback_failed"})
            return False
        now = self.now()

        def mutate(lane: Lane) -> None:
            lane.version += 1
            lane.publish.state = "failed"
            lane.publish.lease_until = None
            lane.publish.attempt = None
            lane.publish.last_result = _result(
                state="failed",
                stage=failure.get("stage"),
                reason=failure.get("reason"),
                order_id=failure.get("order_id"),
                observed_status=failure.get("observed_status"),
                writes_made=False,
                rolled_back=True,
                retryable=True,
                publish_id=run.publish_id,
                group_truck_ids=list(run.truck_ids),
                at=now,
            )

        if await update_lanes(self._es, run.tenant_id, run.service_date, run.truck_ids, mutate, owner=run.publish_id, attempt_id=run.attempt_id, now=now) is None:
            raise _LeaseLost()
        return True

    async def _mark_rollback_pending(self, run: _Run, failure: Dict[str, Any]) -> None:
        now = self.now()

        def mutate(lane: Lane) -> None:
            assert lane.publish.attempt is not None
            lane.version += 1
            lane.publish.state = "failed"
            lane.publish.lease_until = None
            lane.publish.attempt.recovery = "rollback"
            lane.publish.last_result = _result(
                state="failed", stage=failure["stage"], reason=failure["reason"], writes_made=True,
                retryable=True, recovery="rollback", publish_id=run.publish_id,
                group_truck_ids=list(run.truck_ids), at=now,
            )

        await update_lanes(self._es, run.tenant_id, run.service_date, run.truck_ids, mutate, owner=run.publish_id, attempt_id=run.attempt_id, now=now)

    async def _relink_back(self, run: _Run) -> bool:
        """Rollback step 1. Returns ``True`` when an order is in a state the guards exclude."""
        for entry in run.attempt.relinks:
            await self._renew(run)
            current = await self._orders.get_current(run.tenant_id, entry.order_id)
            if current is None:
                continue
            links = Link(
                run_id=current.get("assigned_run_id"),
                truck_id=current.get("assigned_asset_id"),
                driver_id=current.get("assigned_driver_id"),
            )
            status = current.get("status")
            if links == entry.from_link:
                continue
            if status in ("failed", "cancelled"):
                logger.info("dispatch board rollback leaves a terminal order where it is")
                continue
            if links == entry.to and status == "dispatched":
                outcome = await self._relink_one(run, entry.order_id, entry.to, entry.from_link)
                if outcome in ("relinked", "already_relinked"):
                    continue
            return True
        return False

    async def _docs_where(self, index: str, field_name: str, value: str, tenant_id: str) -> List[Tuple[str, Dict[str, Any]]]:
        id_field = {MVP_LOAD_PLANS_INDEX: "plan_id", MVP_ROUTES_INDEX: "route_id", MVP_PLAN_EXECUTIONS_INDEX: "execution_id"}[index]
        query = {"query": {"bool": {"filter": [{"term": {"tenant_id": tenant_id}}, {"term": {field_name: value}}]}}, "size": 500}
        docs = _hits(await self._es.search_documents(index, query, 500))
        return [(d[id_field], d) for d in docs if d.get("tenant_id") == tenant_id and d.get(id_field)]

    async def _retire_staged(self, run: _Run) -> None:
        """Rollback step 2: staged docs back out of sight (never applied, R25)."""
        now = self.now().isoformat()
        for index, open_statuses in ((MVP_LOAD_PLANS_INDEX, ("draft",)), (MVP_ROUTES_INDEX, ("planned",))):
            for doc_id, _doc in await self._docs_where(index, "created_by_attempt", run.attempt_id, run.tenant_id):
                def tx(cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                    if cur.get("status") == "superseded":
                        return None
                    if cur.get("status") not in open_statuses:
                        raise RuntimeError("staged document already applied")
                    return {**cur, "status": "superseded", "updated_at": now}

                await self._es.atomic_update(index, doc_id, tx)

    async def _restore_retired(self, run: _Run) -> None:
        """Rollback step 3: every retired doc back to ``status_before_retire``."""
        now = self.now().isoformat()
        aid = run.attempt_id
        for index in (MVP_PLAN_EXECUTIONS_INDEX, MVP_LOAD_PLANS_INDEX, MVP_ROUTES_INDEX):
            for doc_id, _doc in await self._docs_where(index, "superseded_by_attempt", aid, run.tenant_id):
                def tx(cur: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                    if cur.get("status") != "superseded" or cur.get("superseded_by_attempt") != aid:
                        return None
                    out = {k: v for k, v in cur.items() if k not in ("superseded_by_attempt", "status_before_retire", "superseded_by_plan_id")}
                    out["status"] = cur.get("status_before_retire") or "dispatched"
                    out["updated_at"] = now
                    return out

                await self._es.atomic_update(index, doc_id, tx)

    def run_for(self, draft: BoardDraft, truck_ids: Sequence[str], *, publish_id: str, actor_user_id: str) -> _Run:
        """A run over a claimed group (the publish handler uses it for a synchronous rollback)."""
        attempt = draft.lanes[truck_ids[0]].publish.attempt
        assert attempt is not None
        run = _Run(
            tenant_id=draft.tenant_id, service_date=draft.service_date, publish_id=publish_id,
            actor_user_id=actor_user_id, truck_ids=list(attempt.group_truck_ids), draft=draft, attempt=attempt,
        )
        for truck_id in run.truck_ids:
            lane = run.lane(truck_id)
            for load in lane.loads:
                run.load_lane[load.load_id] = truck_id
            for load_id in lane.publish.plans:
                run.load_lane.setdefault(load_id, truck_id)
        return run


# ---------------------------------------------------------------------------
# BoardPublishService (K7)
# ---------------------------------------------------------------------------


def _not_ready(lanes: Dict[str, List[str]], *, reason: Optional[str] = None, **extra: Any) -> AppException:
    first = reason or next((r for rs in lanes.values() for r in rs), "not_ready")
    details: Dict[str, Any] = {"reason": first, "lanes": [{"truck_id": t, "reasons": rs} for t, rs in lanes.items()]}
    details.update(extra)
    return AppException(ErrorCode.BOARD_PUBLISH_NOT_READY, "Some trucks aren't ready to publish.", status_code=409, details=details)


class BoardPublishService:
    """Publish requests, dry runs and the publish worker (K7.1-K7.5)."""

    def __init__(
        self,
        *,
        es_service: Any,
        board_service: Any,
        executor: Any,
        dispatch_service: Any,
        redispatch_service: BoardRedispatchService,
        clock: Optional[Callable[[], datetime]] = None,
        lease_seconds: float = LEASE_SECONDS,
        new_id: Optional[Callable[[], str]] = None,
    ) -> None:
        self._es = es_service
        self._board = board_service
        self._executor = executor
        self._dispatch = dispatch_service
        self.redispatch = redispatch_service
        self._clock = clock or getattr(board_service, "now", None) or _utcnow
        self._lease = float(lease_seconds)
        self._new_id = new_id or (lambda: str(uuid.uuid4()))
        self._tasks: Set["asyncio.Task[Any]"] = set()
        self.telemetry: tm.BoardTelemetry = getattr(board_service, "telemetry", None) or tm.BoardTelemetry()

    def now(self) -> datetime:
        return self._clock()

    async def wait_idle(self) -> None:
        """Wait for every running publish worker (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- request -----------------------------------------------------------

    async def handle(self, *, tenant_id: str, user_id: str, service_date: date, body: Any, tz: str) -> Dict[str, Any]:
        """``POST /{service_date}/publish``: the dry run (200) or a real publish (202)."""
        _today, past = self._board.check_service_date(service_date, tz)
        if past:
            raise AppException(
                ErrorCode.DISPATCH_BOARD_READ_ONLY, "Past service days are read-only.", status_code=409,
                details={"reason": "past_service_day"},
            )
        draft = await self._board.load_draft(tenant_id, service_date, tz)
        if isinstance(body, PublishPreviewBody):
            return await self._dry_run(draft, body)
        return await self._publish(draft, body, user_id, tz)

    async def status(self, *, tenant_id: str, service_date: date, publish_id: str) -> Dict[str, Any]:
        draft = await load_board_draft(self._es, tenant_id, service_date)
        if draft is None or publish_id not in draft.publishes.values():
            raise AppException(ErrorCode.RESOURCE_NOT_FOUND, "Publish not found.", status_code=404)
        lanes = []
        for truck_id, lane in draft.lanes.items():
            owner = lane.publish.attempt_id
            result = lane.publish.last_result
            if owner != publish_id and (result is None or result.publish_id != publish_id):
                continue
            view = self._board.lane_view(lane)
            lanes.append({"truck_id": truck_id, "state": view.state, "last_result": view.publish.last_result.model_dump(mode="json") if view.publish.last_result else None})
        done = all(l["state"] not in ("publishing",) for l in lanes)
        return {"publish_id": publish_id, "lanes": lanes, "done": done}

    # -- preflight (K7.2) --------------------------------------------------

    def _refresh_lane(self, lane: Lane, ctx: ValidationContext) -> Lane:
        """Snapshot refresh (review finding 12): stops whose product, customer and
        tank are unchanged take the current order; allocations are recomputed.
        Started loads keep their allocations (K8.1)."""
        lane = lane.model_copy(deep=True)
        started = {ld for ld in lane.publish.plans if engine.load_started(lane, ld, ctx)}
        kept = {l.load_id: list(l.allocations) for l in lane.loads if l.load_id in started}
        for load in lane.loads:
            for stop in load.stops:
                order = ctx.orders.get(stop.order_id)
                if order is None or self._identity_changed(order, stop.snapshot):
                    continue
                fresh = engine.snapshot_of(order)
                stop.snapshot = fresh.model_copy(update={"status": stop.snapshot.status})
        engine.refresh_lane(lane, ctx)
        for load in lane.loads:
            if load.load_id in kept:
                load.allocations = kept[load.load_id]
        return lane

    @staticmethod
    def _identity_changed(order: Mapping[str, Any], snap: Any) -> bool:
        return (
            _canonical(order.get("product_code")) != _canonical(snap.product_code)
            or (order.get("customer_id") or None) != (snap.customer_id or None)
            or (order.get("customer_tank_id") or None) != (snap.customer_tank_id or None)
        )

    @staticmethod
    def _override_checks(lane: Lane, ctx: ValidationContext) -> List[Check]:
        from fuel.services.dispatch_board_models import FixLink

        out: List[Check] = []
        for load in lane.loads:
            for order_id, shares in load.allocation_overrides.items():
                liters = engine.order_liters(ctx.orders.get(order_id))
                total = sum(float(s.liters) for s in shares)
                if liters and abs(total - liters) > liters * OVERRIDE_TOLERANCE:
                    out.append(
                        Check(
                            check="compartment_fit",
                            outcome="block",
                            reason_code="override_stale",
                            message="Reset or update the compartment split.",
                            source="BoardPublish",
                            scope=Scope(truck_id=lane.truck_id, load_id=load.load_id, order_id=order_id),
                            fix_link=FixLink(kind="compartment", id=lane.truck_id),
                        )
                    )
        return out

    async def _preflight(
        self, draft: BoardDraft, requested: Sequence[str], warning_reasons: Mapping[str, str]
    ) -> _Preflight:
        pre = _Preflight()
        now = self.now()
        candidates: List[str] = []
        for truck_id in requested:
            lane = draft.lanes[truck_id]
            state = self._board._lane_state(lane)  # noqa: SLF001 - one lane-state rule
            if state == "published":
                pre.already_published.append(truck_id)
            elif state == "publishing":
                pre.not_ready[truck_id] = ["publishing"]
            elif is_in_recovery(lane, now) or lane.publish.attempt is not None:
                pre.not_ready[truck_id] = ["recovery_pending"]
            else:
                candidates.append(truck_id)
        if not candidates:
            return pre
        scope = candidate_lanes(draft, candidates)
        ctx = await self._board._context_for(draft, scope, fresh=True)  # noqa: SLF001 - shared context builder
        pre.ctx = ctx
        groups = publish_groups(draft, candidates, ctx.orders)
        claim = [t for g in groups for t in g.truck_ids]
        for truck_id in claim:
            if truck_id in candidates:
                continue
            lane = draft.lanes[truck_id]
            if lane.publish.state == "publishing" and not (lane.publish.lease_until and lane.publish.lease_until < now):
                pre.not_ready[truck_id] = ["publishing"]
            elif lane.publish.attempt is not None:
                pre.not_ready[truck_id] = ["recovery_pending"]
        work = draft.model_copy(deep=True)
        for truck_id in claim:
            work.lanes[truck_id] = self._refresh_lane(work.lanes[truck_id], ctx)
        if "orders" in ctx.unavailable:
            for truck_id in claim:
                pre.not_ready.setdefault(truck_id, []).append("check_unavailable")
        for truck_id in claim:
            lane = work.lanes[truck_id]
            checks = lane_checks(ctx, lane, draft=work) + self._override_checks(lane, ctx)
            pre.checks[truck_id] = checks
            reasons = list(pre.not_ready.get(truck_id, []))
            for check in checks:
                if check.outcome == "block" and check.reason_code not in reasons:
                    reasons.append(check.reason_code)
            has_stops = any(l.stops for l in lane.loads)
            if not lane.driver_id and has_stops:
                reasons.append("no_driver")
            if not has_stops and not lane.publish.plans:
                reasons.append("no_loads")
            if lane.shelf:
                reasons.append("unplaced_dispatched_order")
            for check in checks:
                if check.outcome != "warn" or check.reason_code == "no_driver" or not check.warning_id:
                    continue
                if check.warning_id in draft.acknowledged:
                    continue
                pre.open_warnings.append({"truck_id": truck_id, "warning_id": check.warning_id, "message": check.message})
                if check.warning_id not in warning_reasons and "warning_unacknowledged" not in reasons:
                    reasons.append("warning_unacknowledged")
            if reasons:
                pre.not_ready[truck_id] = list(dict.fromkeys(reasons))
            pre.lanes[truck_id] = lane
            pre.versions[truck_id] = draft.lanes[truck_id].version
        pre.groups = groups
        for group in groups:
            lanes = [work.lanes[t] for t in group.truck_ids]
            if group.kind == "redispatch":
                changes = plan_changes(lanes, ctx.orders, ctx.executions, now)
                pre.changes.update(changes)
                attempt = self._build_attempt(group, work, changes, ctx)
                for truck_id in group.truck_ids:
                    pre.attempts[truck_id] = attempt
            else:
                for lane in lanes:
                    for load in lane.loads:
                        if load.stops:
                            pre.changes[load.load_id] = LoadChange(lane.truck_id, load.load_id, "new")
        return pre

    def _build_attempt(
        self, group: PublishGroup, work: BoardDraft, changes: Mapping[str, LoadChange], ctx: ValidationContext
    ) -> RedispatchAttempt:
        """The fixed attempt record written at claim (K2.1): classes, target ids,
        relinks from recorded published links, and the notify baseline."""
        attempt_id = self._new_id()
        loads: Dict[str, AttemptLoad] = {}
        for load_id, change in changes.items():
            lane = work.lanes[change.truck_id]
            published = lane.publish.plans.get(load_id)
            if change.load_class == "unchanged":
                continue
            if change.load_class == "new":
                revision = 1
                plan_id, route_id = board_ids(load_id, revision)
            elif change.load_class == "new_revision":
                assert published is not None
                revision = published.revision + 1
                plan_id, route_id = board_ids(load_id, revision)
            else:
                assert published is not None
                revision, plan_id, route_id = published.revision, published.plan_id, published.route_id
            loads[load_id] = AttemptLoad.model_validate(
                {"class": change.load_class, "plan_id": plan_id, "route_id": route_id, "run_id": plan_id, "revision": revision}
            )
        published_link: Dict[str, Link] = {}
        baseline: Dict[str, List[str]] = {}
        for truck_id in group.truck_ids:
            lane = work.lanes[truck_id]
            content = lane.publish.published_content
            if content is None:
                continue
            for load in content.loads:
                plan = lane.publish.plans.get(load.load_id)
                for stop in load.stops:
                    if plan is not None:
                        published_link[stop.order_id] = Link(run_id=plan.run_id, truck_id=truck_id, driver_id=content.driver_id)
                    if content.driver_id:
                        baseline.setdefault(content.driver_id, []).append(stop.order_id)
        relinks: List[AttemptRelink] = []
        for truck_id in group.truck_ids:
            lane = work.lanes[truck_id]
            for load in lane.loads:
                al = loads.get(load.load_id)
                if al is not None and al.load_class in ("new", "new_revision"):
                    run_id = al.run_id
                elif load.load_id in lane.publish.plans:
                    run_id = lane.publish.plans[load.load_id].run_id
                else:
                    continue
                target = Link(run_id=run_id, truck_id=truck_id, driver_id=lane.driver_id)
                for stop in load.stops:
                    order = ctx.orders.get(stop.order_id) or {}
                    src = published_link.get(stop.order_id)
                    if order.get("status") != "dispatched" or src is None or src == target:
                        continue
                    relinks.append(AttemptRelink.model_validate({"order_id": stop.order_id, "from": src.model_dump(), "to": target.model_dump()}))
        return RedispatchAttempt(
            attempt_id=attempt_id,
            group_truck_ids=list(group.truck_ids),
            phase="claimed",
            loads=loads,
            relinks=relinks,
            notify_baseline={d: sorted(set(o)) for d, o in baseline.items()},
        )

    # -- dry run (K7.1) ----------------------------------------------------

    async def _dry_run(self, draft: BoardDraft, body: PublishPreviewBody) -> Dict[str, Any]:
        now = self.now()
        not_ready: Dict[str, List[str]] = {}
        requested: List[str] = []
        groups: List[Dict[str, Any]] = []
        loads_out: List[Dict[str, Any]] = []
        refs = {ref.truck_id: ref.expected_version for ref in body.lanes}
        recovery_seen: Set[str] = set()
        for truck_id, expected in refs.items():
            lane = draft.lanes.get(truck_id)
            if lane is None:
                not_ready[truck_id] = ["lane_not_found"]
                continue
            if lane.version != expected:
                not_ready[truck_id] = ["version_changed"]
                continue
            attempt = lane.publish.attempt
            if attempt is not None and is_in_recovery(lane, now):
                if attempt.attempt_id in recovery_seen:
                    continue
                recovery_seen.add(attempt.attempt_id)
                if not set(attempt.group_truck_ids) <= set(refs):
                    for t in attempt.group_truck_ids:
                        not_ready[t] = ["retry_whole_group"]
                    continue
                groups.append({"truck_ids": list(attempt.group_truck_ids), "kind": "redispatch", "added_lanes": []})
                load_lane: Dict[str, str] = {}
                for t in attempt.group_truck_ids:
                    group_lane = draft.lanes.get(t)
                    if group_lane is None:
                        continue
                    for load in group_lane.loads:
                        load_lane[load.load_id] = t
                    for load_id in group_lane.publish.plans:
                        load_lane.setdefault(load_id, t)
                for load_id, al in attempt.loads.items():
                    loads_out.append({"truck_id": load_lane.get(load_id, truck_id), "load_id": load_id, "class": al.load_class, "info": []})
                continue
            requested.append(truck_id)
        pre = await self._preflight(draft, requested, body.warning_reasons)
        for truck_id, reasons in pre.not_ready.items():
            not_ready.setdefault(truck_id, []).extend(r for r in reasons if r not in not_ready.get(truck_id, []))
        groups.extend(g.as_dict() for g in pre.groups)
        for change in pre.changes.values():
            loads_out.append({"truck_id": change.truck_id, "load_id": change.load_id, "class": change.load_class, "info": list(change.info)})
        return {
            "ready": not not_ready,
            "not_ready": [{"truck_id": t, "reasons": rs} for t, rs in not_ready.items()],
            "groups": groups,
            "loads": loads_out,
            "notifications": self._preview_notifications(pre, draft),
            "open_warnings": pre.open_warnings,
            "already_published": pre.already_published,
        }

    def _preview_notifications(self, pre: _Preflight, draft: BoardDraft) -> List[Dict[str, Any]]:
        before: Dict[str, Set[str]] = {}
        after: Dict[str, Set[str]] = {}
        route_updated: Set[str] = set()
        for truck_id, lane in pre.lanes.items():
            attempt = pre.attempts.get(truck_id)
            if attempt is not None:
                for driver_id, orders in attempt.notify_baseline.items():
                    before.setdefault(driver_id, set()).update(orders)
            if lane.driver_id:
                after.setdefault(lane.driver_id, set()).update(s.order_id for l in lane.loads for s in l.stops)
                if any(pre.changes.get(l.load_id) and pre.changes[l.load_id].load_class in ("amend", "new_revision") for l in lane.loads):
                    route_updated.add(lane.driver_id)
        names = {d: (doc.get("driver_name") or d) for d, doc in ((pre.ctx.drivers if pre.ctx else {}) or {}).items()}
        out = []
        for driver_id in sorted(set(before) | set(after)):
            revoke = sorted(before.get(driver_id, set()) - after.get(driver_id, set()))
            assign = sorted(after.get(driver_id, set()) - before.get(driver_id, set()))
            if not revoke and not assign and driver_id not in route_updated:
                continue
            out.append({
                "driver_id": driver_id,
                "name": names.get(driver_id, driver_id),
                "revoke_order_ids": revoke,
                "assign_order_ids": assign,
                "route_updated": driver_id in route_updated,
            })
        return out

    # -- real publish ------------------------------------------------------

    async def _publish(self, draft: BoardDraft, body: PublishBody, user_id: str, tz: str) -> Dict[str, Any]:
        if body.client_request_id in draft.publishes:
            return self._accepted_replay(draft, draft.publishes[body.client_request_id])
        try:
            return await self._publish_new(draft, body, user_id, tz)
        except AppException as exc:
            # A concurrent request with the same client_request_id claimed first:
            # answer with its publish_id, not a conflict (K7.5).
            if exc.status_code != 409:
                raise
            latest = await load_board_draft(self._es, draft.tenant_id, draft.service_date)
            if latest is None or body.client_request_id not in latest.publishes:
                raise
            return self._accepted_replay(latest, latest.publishes[body.client_request_id])

    async def _publish_new(self, draft: BoardDraft, body: PublishBody, user_id: str, tz: str) -> Dict[str, Any]:
        tenant_id, service_date = draft.tenant_id, draft.service_date
        refs = {ref.truck_id: ref.expected_version for ref in body.lanes}
        missing = [t for t in refs if t not in draft.lanes]
        if missing:
            raise _not_ready({t: ["lane_not_found"] for t in missing})
        stale = [t for t, v in refs.items() if draft.lanes[t].version != v]
        if stale:
            raise self._board._conflict(draft, stale, "version_changed")  # noqa: SLF001 - one conflict shape
        now = self.now()
        publish_id = self._new_id()
        for truck_id in refs:
            publish = draft.lanes[truck_id].publish
            if publish.state == "publishing" and not (publish.lease_until and publish.lease_until < now):
                raise AppException(
                    ErrorCode.BOARD_PUBLISH_IN_PROGRESS,
                    "This truck is being published. Try again when it finishes.",
                    status_code=409,
                    details={"reason": "publishing", "truck_id": truck_id},
                )

        # Lanes in recovery: Retry must cover the whole group (E30).
        recovery: Dict[str, RedispatchAttempt] = {}
        for truck_id in refs:
            lane = draft.lanes[truck_id]
            if lane.publish.attempt is not None and is_in_recovery(lane, now):
                attempt = lane.publish.attempt
                if not set(attempt.group_truck_ids) <= set(refs):
                    raise _not_ready(
                        {t: ["retry_whole_group"] for t in attempt.group_truck_ids},
                        reason="retry_whole_group", truck_ids=list(attempt.group_truck_ids),
                    )
                recovery[attempt.attempt_id] = attempt
        resume: List[RedispatchAttempt] = []
        rolled_back: Set[str] = set()
        for attempt in recovery.values():
            if attempt.recovery == "rollback" or attempt.phase in ROLLBACK_PHASES:
                await self._rollback_now(draft, attempt, publish_id, user_id)
                rolled_back.update(attempt.group_truck_ids)
            else:
                resume.append(attempt)
        if rolled_back:
            draft = await self._board.load_draft(tenant_id, service_date, tz)
        resume_lanes = {t for a in resume for t in a.group_truck_ids}
        fresh = [t for t in refs if t not in resume_lanes]
        pre = await self._preflight(draft, fresh, body.warning_reasons)
        if pre.not_ready:
            raise _not_ready(pre.not_ready)
        groups = [PublishGroup(list(a.group_truck_ids), "redispatch") for a in resume] + list(pre.groups)
        # Nothing to run (every lane already published): no claim, no publish_id.
        claimed = await self._claim(draft, pre, resume, publish_id, user_id, body) if groups else []
        order = {t: i for i, t in enumerate(refs)}
        groups.sort(key=lambda g: min(order.get(t, len(order)) for t in g.truck_ids))
        if groups:
            self._spawn(self._worker(tenant_id, service_date, publish_id, groups, user_id))
        return {
            "publish_id": publish_id if groups else None,
            "lanes": [{"truck_id": t, "state": "queued"} for t in claimed]
            + [{"truck_id": t, "state": "already_published"} for t in pre.already_published],
            "groups": [g.as_dict() for g in groups],
            "already_published": pre.already_published,
        }

    def _accepted_replay(self, draft: BoardDraft, publish_id: str) -> Dict[str, Any]:
        """The 202 body of an earlier request, rebuilt from the lanes it claimed (K7.1, K7.5)."""
        lanes: List[Dict[str, Any]] = []
        groups: List[Dict[str, Any]] = []
        grouped: Set[str] = set()
        for t, lane in draft.lanes.items():
            result = lane.publish.last_result
            owner = lane.publish.attempt_id == publish_id
            if not owner and (result is None or result.publish_id != publish_id):
                continue
            lanes.append({"truck_id": t, "state": self._board._lane_state(lane)})  # noqa: SLF001
            if t in grouped:
                continue
            if owner and lane.publish.attempt is not None:
                group = PublishGroup(list(lane.publish.attempt.group_truck_ids), "redispatch")
            elif result is not None and result.publish_id == publish_id and result.group_truck_ids:
                group = PublishGroup(list(result.group_truck_ids), "redispatch")
            else:
                group = PublishGroup([t], "first_publish")
            grouped.update(group.truck_ids)
            groups.append(group.as_dict())
        return {"publish_id": publish_id, "lanes": lanes, "groups": groups, "already_published": [], "replayed": True}

    async def _rollback_now(self, draft: BoardDraft, attempt: RedispatchAttempt, publish_id: str, user_id: str) -> None:
        """Retry of an interrupted or pending rollback: roll back first (K8.4 Recovery)."""
        now = self.now()
        lease = now + timedelta(seconds=self._lease)
        group = list(attempt.group_truck_ids)

        def transform(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            current = BoardDraft.model_validate(doc)
            for t in group:
                lane = current.lanes.get(t)
                if lane is None or lane.publish.attempt is None or lane.publish.attempt.attempt_id != attempt.attempt_id:
                    return None
                if not is_in_recovery(lane, now):
                    return None
            for t in group:
                lane = current.lanes[t]
                lane.publish.state = "publishing"
                lane.publish.attempt_id = publish_id
                lane.publish.lease_until = lease
            return current.model_dump(mode="json")

        stored, applied = await self._es.atomic_update(DISPATCH_BOARD_DRAFTS_INDEX, draft_doc_id(draft.tenant_id, draft.service_date), transform)
        if not applied or not stored:
            raise self._board._conflict(draft, group, "version_changed")  # noqa: SLF001
        run = self.redispatch.run_for(BoardDraft.model_validate(stored), group, publish_id=publish_id, actor_user_id=user_id)
        done = await self.redispatch.rollback(run, failure={"stage": attempt.phase, "reason": "interrupted"})
        if not done:
            raise _not_ready({t: ["recovery_pending"] for t in group}, reason="recovery_pending")

    async def _claim(
        self,
        draft: BoardDraft,
        pre: _Preflight,
        resume: Sequence[RedispatchAttempt],
        publish_id: str,
        user_id: str,
        body: PublishBody,
    ) -> List[str]:
        """One draft ``atomic_update``: acks, refreshed lanes, the lease and the
        attempt on every lane of a redispatch group, all or none (K7.2)."""
        now = self.now()
        lease = now + timedelta(seconds=self._lease)
        fresh = list(pre.lanes)
        resume_ids = {a.attempt_id: a for a in resume}
        resume_lanes = [t for a in resume for t in a.group_truck_ids]
        verdict: Dict[str, str] = {}
        open_ids = {w["warning_id"]: w["truck_id"] for w in pre.open_warnings}

        def transform(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            current = BoardDraft.model_validate(doc)
            if body.client_request_id in current.publishes:
                verdict["reason"] = "duplicate_request"  # _publish answers with the stored id
                return None
            for t in fresh:
                lane = current.lanes.get(t)
                if lane is None or lane.version != pre.versions[t]:
                    verdict["reason"] = "version_changed"
                    return None
                if lane.publish.state == "publishing" and not (lane.publish.lease_until and lane.publish.lease_until < now):
                    verdict["reason"] = "publishing"
                    return None
                if lane.publish.attempt is not None:
                    verdict["reason"] = "recovery_pending"
                    return None
            for t in resume_lanes:
                lane = current.lanes.get(t)
                attempt = lane.publish.attempt if lane else None
                if attempt is None or attempt.attempt_id not in resume_ids or not is_in_recovery(lane, now):
                    verdict["reason"] = "recovery_changed"
                    return None
            for t in fresh:
                lane = current.lanes[t]
                refreshed = pre.lanes[t]
                lane.loads = refreshed.loads
                lane.checks = pre.checks.get(t, [])
                lane.checks_computed_at = now
                lane.checks_stale = False
                lane.publish.state = "publishing"
                lane.publish.attempt_id = publish_id
                lane.publish.lease_until = lease
                lane.publish.attempt = pre.attempts.get(t)
                lane.publish.last_result = None
                lane.version += 1
            for t in resume_lanes:
                lane = current.lanes[t]
                lane.publish.state = "publishing"
                lane.publish.attempt_id = publish_id
                lane.publish.lease_until = lease
                lane.version += 1
            for warning_id, reason in body.warning_reasons.items():
                if warning_id in open_ids:
                    current.acknowledged[warning_id] = WarningAck(reason=reason, actor_user_id=user_id, at=now, truck_id=open_ids[warning_id])
            current.publishes[body.client_request_id] = publish_id
            current.publishes = dict(list(current.publishes.items())[-PUBLISHES_LIMIT:])
            current.updated_at = now
            return current.model_dump(mode="json")

        stored, applied = await self._es.atomic_update(
            DISPATCH_BOARD_DRAFTS_INDEX, draft_doc_id(draft.tenant_id, draft.service_date), transform
        )
        if not applied:
            stored_draft = BoardDraft.model_validate(stored) if stored else draft
            raise self._board._conflict(stored_draft, fresh + resume_lanes, verdict.get("reason", "version_changed"))  # noqa: SLF001
        return fresh + resume_lanes

    # -- worker (K7.3) -----------------------------------------------------

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _worker(
        self, tenant_id: str, service_date: date, publish_id: str, groups: Sequence[PublishGroup], user_id: str
    ) -> None:
        """Groups run sequentially in request order; one group's failure stops only it."""
        results: Dict[str, str] = {}
        for group in groups:
            try:
                if group.kind == "redispatch":
                    outcome = await self.redispatch.run_group(
                        tenant_id=tenant_id, service_date=service_date, publish_id=publish_id,
                        truck_ids=group.truck_ids, actor_user_id=user_id,
                    )
                    for t in group.truck_ids:
                        results[t] = outcome
                else:
                    for t in group.truck_ids:
                        results[t] = await self._first_publish(tenant_id, service_date, publish_id, t, user_id)
            except Exception as exc:
                logger.error("dispatch board publish worker error: %s", type(exc).__name__, exc_info=True)
                await self._fail_lanes(tenant_id, service_date, publish_id, group.truck_ids, {"stage": "worker", "reason": "worker_error", "writes_made": "unknown", "retryable": True})
                for t in group.truck_ids:
                    results[t] = "failed"
            for t in group.truck_ids:
                self.telemetry.metric(tm.PUBLISH_LANE_COUNT, 1, tenant_id=tenant_id, stage=group.kind, result=results.get(t))
            await self._progress(tenant_id, service_date, publish_id, group.truck_ids)
        await self._audit(tenant_id, service_date, publish_id, user_id, results)

    async def _progress(self, tenant_id: str, service_date: date, publish_id: str, truck_ids: Sequence[str]) -> None:
        draft = await load_board_draft(self._es, tenant_id, service_date)
        if draft is None:
            return
        views = [self._board.lane_view(draft.lanes[t]) for t in truck_ids if t in draft.lanes]
        await self._board._broadcast(tenant_id, service_date, "board_publish_progress", {  # noqa: SLF001
            "publish_id": publish_id,
            "lanes": [{"truck_id": v.truck_id, "state": v.state, "last_result": v.publish.last_result.model_dump(mode="json") if v.publish.last_result else None} for v in views],
        })
        await self._board._broadcast(tenant_id, service_date, "board_lanes_updated", {  # noqa: SLF001
            "service_date": service_date.isoformat(),
            "draft_version": draft.draft_version,
            "lanes": [v.model_dump(mode="json") for v in views],
        })

    async def _audit(self, tenant_id: str, service_date: date, publish_id: str, user_id: str, results: Mapping[str, str]) -> None:
        draft = await load_board_draft(self._es, tenant_id, service_date)
        lanes = []
        for truck_id, outcome in results.items():
            lane = draft.lanes.get(truck_id) if draft else None
            if lane is None:
                continue
            content = lane.publish.published_content
            lanes.append({
                "truck_id": truck_id,
                "plans": sorted(p.plan_id for p in lane.publish.plans.values()),
                "orders": sorted(s.order_id for l in (content.loads if content else []) for s in l.stops),
                "driver_id": lane.driver_id,
                "result": outcome,
            })
        actor = await self._board.resolve_actor_name(tenant_id, user_id)
        await self.telemetry.audit_publish(
            tenant_id=tenant_id, user_id=user_id, actor_name=actor, service_date=service_date.isoformat(), lanes=lanes,
        )

    async def _fail_lanes(
        self, tenant_id: str, service_date: date, publish_id: str, truck_ids: Sequence[str], result: Dict[str, Any]
    ) -> None:
        now = self.now()

        def mutate(lane: Lane) -> None:
            lane.version += 1
            lane.publish.state = "failed"
            lane.publish.lease_until = None
            lane.publish.last_result = _result(state="failed", publish_id=publish_id, at=now, **result)

        try:
            await update_lanes(self._es, tenant_id, service_date, truck_ids, mutate, owner=publish_id, now=now)
        except Exception as exc:
            logger.error("dispatch board could not mark lanes failed: %s", type(exc).__name__)

    async def _renew(self, tenant_id: str, service_date: date, publish_id: str, truck_id: str) -> bool:
        lease = self.now() + timedelta(seconds=self._lease)

        def mutate(lane: Lane) -> None:
            lane.publish.lease_until = lease

        return await update_lanes(self._es, tenant_id, service_date, [truck_id], mutate, owner=publish_id) is not None

    async def _first_publish(self, tenant_id: str, service_date: date, publish_id: str, truck_id: str, user_id: str) -> str:
        """K7.3 steps 1-6 for one lane that holds no dispatched order (freeze rule 7)."""
        started = _time.monotonic()
        draft = await load_board_draft(self._es, tenant_id, service_date)
        lane = draft.lanes.get(truck_id) if draft else None
        if lane is None or lane.publish.attempt_id != publish_id:
            return "lease_lost"
        plans: Dict[str, PublishedPlan] = {}
        writes_made = False

        async def fail(result: Dict[str, Any]) -> str:
            log = logger.error if result.get("writes_made") is True else logger.warning
            log("dispatch board publish lane failed: stage=%s reason=%s", result.get("stage"), result.get("reason"))
            await self._fail_lanes(tenant_id, service_date, publish_id, [truck_id], result)
            self.telemetry.metric(tm.PUBLISH_LANE_MS, (_time.monotonic() - started) * 1000, tenant_id=tenant_id, stage=result.get("stage"), result="failed")
            return "failed"

        now = self.now()
        for seq, load in enumerate(lane.loads, start=1):
            if not load.stops:
                continue
            if not await self._renew(tenant_id, service_date, publish_id, truck_id):
                logger.warning("dispatch board publish lease taken over")
                return "lease_lost"
            plan_doc = build_plan_doc(tenant_id=tenant_id, service_date=service_date, lane=lane, load=load, load_seq=seq, revision=1, now=now)
            route_doc = build_route_doc(tenant_id=tenant_id, service_date=service_date, lane=lane, load=load, revision=1, now=now)
            try:
                await upsert_board_plan(self._es, plan_doc)
                await upsert_board_route(self._es, route_doc)
            except Exception as exc:
                logger.error("dispatch board plan/route write failed: %s", type(exc).__name__)
                return await fail({"stage": "plan_write", "reason": "write_failed", "writes_made": writes_made, "retryable": True})
            stored_plan = await self._es.get_document(MVP_LOAD_PLANS_INDEX, plan_doc["plan_id"]) or plan_doc
            expected = sorted({a["order_id"] for a in stored_plan.get("assignments") or [] if a.get("order_id")})
            result = await self._executor.execute(
                tenant_id=tenant_id,
                plan_id=plan_doc["plan_id"],
                expected_order_ids=expected,
                expected_truck_id=truck_id,
                order_snapshots={k: v for k, v in order_snapshots_for(load).items() if k in expected},
                actor_user_id=user_id,
                action_id=None,
                approved_at=now.isoformat(),
                mode="active_gated",
            )
            if not result.success:
                return await fail({
                    "stage": "apply", "reason": result.reason, "writes_made": bool(result.writes_made) or writes_made,
                    "retryable": bool(result.retryable), "failures": list(result.failures),
                })
            writes_made = True
            stored_plan = await self._es.get_document(MVP_LOAD_PLANS_INDEX, plan_doc["plan_id"])
            try:
                await self._dispatch.dispatch(tenant_id=tenant_id, plan_doc=stored_plan, actor_user_id=user_id, driver_id=lane.driver_id)
            except AppException as exc:
                return await fail({"stage": "dispatch", "reason": str((exc.details or {}).get("reason") or "dispatch_failed"), "writes_made": True, "retryable": True})
            plan_id, route_id = board_ids(load.load_id, 1)
            plans[load.load_id] = PublishedPlan(plan_id=plan_id, route_id=route_id, run_id=plan_id, revision=1)

        done = self.now()

        def finalize(lane_: Lane) -> None:
            lane_.version += 1
            lane_.publish.plans = {**lane_.publish.plans, **plans}
            lane_.publish.state = "published"
            lane_.publish.published_version = lane_.version
            lane_.publish.published_content = lane_.content().model_copy(deep=True)
            lane_.publish.published_hash = engine.content_hash(lane_)
            lane_.publish.lease_until = None
            lane_.publish.attempt = None
            lane_.publish.last_result = _result(state="published", publish_id=publish_id, at=done)

        if await update_lanes(self._es, tenant_id, service_date, [truck_id], finalize, owner=publish_id, now=done) is None:
            logger.warning("dispatch board publish finalize refused: lease taken over")
            return "lease_lost"
        self.telemetry.metric(tm.PUBLISH_LANE_MS, (_time.monotonic() - started) * 1000, tenant_id=tenant_id, stage="finalize", result="published")
        return "published"


__all__ = [
    "AmendResult",
    "BoardPublishService",
    "BoardRedispatchService",
    "LoadChange",
    "PublishGroup",
    "amend_stops",
    "board_ids",
    "build_plan_doc",
    "build_route_doc",
    "candidate_lanes",
    "plan_changes",
    "publish_groups",
    "route_stops_for",
]
