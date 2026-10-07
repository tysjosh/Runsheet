"""Dispatch Board engine: apply a command to a draft (design K4, K6).

Pure: no I/O. Everything the engine needs from outside (orders, locations,
compartments, terminals, executions, suggestions) comes from the validation
context the service builds before calling :func:`apply` (K4.2 steps 7-8). The
context is duck-typed; :class:`fuel.services.dispatch_validation.ValidationContext`
is the production one.

Rules owned here:

* order exclusivity (I1): an order is on at most one lane's stops or shelf,
  with ``order_index`` / ``order_ids`` rebuilt and asserted on every apply;
* best-fit load and position selection (K4.3, K3.6);
* derived lane fields: load start/end, stop ETAs and solver allocations;
* ``content_hash`` (K2.1, P7);
* limits (K15) and command-shape errors, raised as :class:`EngineError` and
  answered 422 ``VALIDATION_ERROR`` with ``reason``.

Post-publish rules (pinned stops, started loads, Q12) are state checks in
``DispatchValidationService.validate_lane``, comparing a lane with its
published content. A command that would break one therefore produces a lane
with a ``block`` and is refused at K4.2 step 9; moving a pinned stop back onto
the load its order is linked to removes the block (freeze rule 11 (e)).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from Agents.support.compartment_models import Compartment, DeliveryRequest
from Agents.support.compartment_solver import (
    legacy_grade_for_product,
    optimize_loading_plan,
    segregation_key,
)
from Agents.support.fuel_distribution_models import FuelGrade
from fuel.services import dispatch_board_eta as eta
from fuel.services.dispatch_board_models import (
    MAX_LANES,
    MAX_STOPS_PER_LOAD,
    AcceptSuggestionCommand,
    AddLaneCommand,
    Allocation,
    AssignOrdersCommand,
    BoardDraft,
    CompartmentShare,
    DeliveryWindow,
    DiscardLaneChangesCommand,
    Lane,
    LaneContent,
    LatLon,
    Load,
    MoveLoadCommand,
    MoveStopsCommand,
    OrderSnapshot,
    PairDriverCommand,
    RemoveLaneCommand,
    SetAllocationCommand,
    SetLoadShiftCommand,
    SetTerminalCommand,
    Stop,
    Target,
    UnassignOrdersCommand,
)
from fuel.services.fuel_product_catalog import UnknownFuelProductError, canonicalize
from services.unit_conversion import GAL_TO_L

#: Order statuses that pin a stop (K8.1).
PINNED_STATUSES = frozenset({"in_transit", "delivered", "failed"})
#: Order statuses of an order already handed to a driver.
DISPATCHED_STATUSES = frozenset({"dispatched", "in_transit", "delivered", "failed"})
#: Tolerance on a dispatcher's allocation override total (External input validation).
SHARE_TOLERANCE = 0.01


class EngineError(Exception):
    """A command the engine can't apply: 422 ``VALIDATION_ERROR`` with ``reason``."""

    def __init__(self, reason: str, *, fields: Optional[List[str]] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fields = fields or []


@dataclass
class ApplyResult:
    draft: BoardDraft
    touched: List[str]
    removed_lanes: List[str] = field(default_factory=list)
    #: For an ``assign_orders`` / ``move_stops`` best fit: where the first order landed.
    placed: Optional[Tuple[str, str, int]] = None


# ---------------------------------------------------------------------------
# Hashing and snapshots (K2.1, K6)
# ---------------------------------------------------------------------------


def _content_dict(content: LaneContent) -> Dict[str, Any]:
    return {
        "driver_id": content.driver_id,
        "loads": [
            {
                "load_id": load.load_id,
                "shift_id": load.shift_id,
                "terminal_id": load.terminal_id,
                "stops": [stop.order_id for stop in load.stops],
                "allocation_overrides": {
                    order_id: [
                        {"compartment_id": s.compartment_id, "liters": round(float(s.liters), 3)}
                        for s in shares
                    ]
                    for order_id, shares in sorted(load.allocation_overrides.items())
                },
            }
            for load in content.loads
        ],
        "shelf": list(content.shelf),
    }


def overrides_signature(load: Load) -> Any:
    """A load's allocation overrides in hash form (started loads keep them, K8.1)."""
    return _content_dict(LaneContent(loads=[load]))["loads"][0]["allocation_overrides"]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(lane_or_content: Any) -> str:
    """SHA-256 of a lane's dispatcher content (driver, loads, shelf), excluding
    checks, versions, ETAs, allocations, snapshots and publish state (P7)."""
    content = lane_or_content.content() if isinstance(lane_or_content, Lane) else lane_or_content
    return hashlib.sha256(canonical_json(_content_dict(content)).encode("utf-8")).hexdigest()


def payload_hash(payload: Dict[str, Any]) -> str:
    """SHA-256 of a command payload's canonical JSON (K2.2)."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def lane_content_dump(lane: Optional[Lane]) -> Optional[Dict[str, Any]]:
    """A lane's content as stored on the command log (``None`` when the lane is absent)."""
    if lane is None:
        return None
    return lane.content().model_dump(mode="json")


# ---------------------------------------------------------------------------
# Context accessors (duck-typed)
# ---------------------------------------------------------------------------


def _ctx(ctx: Any, name: str, default: Any) -> Any:
    value = getattr(ctx, name, None)
    return default if value is None else value


def _new_id(ctx: Any) -> str:
    factory = getattr(ctx, "new_id", None)
    return factory() if callable(factory) else uuid.uuid4().hex


def _to_point(value: Any) -> Optional[LatLon]:
    if value is None:
        return None
    if isinstance(value, LatLon):
        return value
    if isinstance(value, dict) and value.get("lat") is not None and value.get("lon") is not None:
        try:
            return LatLon(lat=float(value["lat"]), lon=float(value["lon"]))
        except Exception:
            return None
    lat, lon = getattr(value, "lat", None), getattr(value, "lon", None)
    if lat is not None and lon is not None:
        return LatLon(lat=float(lat), lon=float(lon))
    return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def order_liters(order: Optional[Dict[str, Any]]) -> Optional[float]:
    """Requested litres for an order, ``None`` when it has no requested gallons."""
    if not order:
        return None
    gallons = order.get("gallons_requested")
    try:
        gallons = float(gallons) if gallons is not None else None
    except (TypeError, ValueError):
        gallons = None
    if gallons is None or gallons <= 0:
        return None
    return round(gallons * GAL_TO_L, 2)


def snapshot_of(order: Dict[str, Any]) -> OrderSnapshot:
    """``Stop.snapshot`` from a current order document (K2.1)."""
    gallons = order.get("gallons_requested")
    try:
        gallons = float(gallons) if gallons is not None else None
    except (TypeError, ValueError):
        gallons = None
    return OrderSnapshot(
        product_code=order.get("product_code"),
        customer_id=order.get("customer_id"),
        customer_tank_id=order.get("customer_tank_id"),
        gallons_requested=gallons,
        fill_to_full=bool(order.get("fill_to_full") or False),
        window=DeliveryWindow(
            start=_parse_dt(order.get("delivery_window_start")),
            end=_parse_dt(order.get("delivery_window_end")),
        ),
        call_type=order.get("call_type"),
        status=order.get("status"),
    )


def published_run_ids(draft: BoardDraft) -> Dict[str, Tuple[str, str]]:
    """``run_id -> (truck_id, load_id)`` for every published load of the draft."""
    runs: Dict[str, Tuple[str, str]] = {}
    for truck_id, lane in draft.lanes.items():
        for load_id, plan in lane.publish.plans.items():
            runs[plan.run_id] = (truck_id, load_id)
    return runs


def published_load(lane: Lane, load_id: str) -> Optional[Load]:
    content = lane.publish.published_content
    if content is None:
        return None
    for load in content.loads:
        if load.load_id == load_id:
            return load
    return None


def is_pinned(order: Optional[Dict[str, Any]]) -> bool:
    return bool(order) and order.get("status") in PINNED_STATUSES


def load_started(lane: Lane, load_id: str, ctx: Any) -> bool:
    """K8.1: a published load with an order in_transit/delivered/failed, or an
    execution with ``completed_stops > 0``."""
    plan = lane.publish.plans.get(load_id)
    if plan is None:
        return False
    executions = _ctx(ctx, "executions", {})
    if int(executions.get(plan.plan_id, 0) or 0) > 0:
        return True
    orders = _ctx(ctx, "orders", {})
    order_ids: Set[str] = set()
    published = published_load(lane, load_id)
    if published is not None:
        order_ids.update(s.order_id for s in published.stops)
    for load in lane.loads:
        if load.load_id == load_id:
            order_ids.update(s.order_id for s in load.stops)
    return any(is_pinned(orders.get(oid)) for oid in order_ids)


# ---------------------------------------------------------------------------
# Index, limits and cross-lane rules
# ---------------------------------------------------------------------------


def lane_order_ids(lane: Lane) -> List[str]:
    ids = [stop.order_id for load in lane.loads for stop in load.stops]
    ids.extend(lane.shelf)
    return ids


def rebuild_index(draft: BoardDraft) -> None:
    """Recompute ``order_index`` and ``order_ids``; raise on a duplicate (I1)."""
    index: Dict[str, str] = {}
    for truck_id, lane in draft.lanes.items():
        for order_id in lane_order_ids(lane):
            if order_id in index:
                raise EngineError("order_on_two_lanes")
            index[order_id] = truck_id
    draft.order_index = index
    draft.order_ids = sorted(index)


def lane_window(lane: Lane) -> Optional[Tuple[datetime, datetime]]:
    starts = [l.planned_start for l in lane.loads if l.planned_start is not None]
    ends = [l.planned_end for l in lane.loads if l.planned_end is not None]
    if not starts or not ends:
        return None
    return (min(starts), max(ends))


def lanes_conflict_for_driver(a: Lane, b: Lane) -> bool:
    """Whether one driver can't hold both lanes: overlapping load windows, or a lane with none."""
    wa, wb = lane_window(a), lane_window(b)
    if wa is None or wb is None:
        return True
    return eta.intervals_overlap(wa, wb)


def double_booked(draft: BoardDraft, truck_id: str) -> List[str]:
    """Other lanes holding this lane's driver with a conflicting window (I8)."""
    lane = draft.lanes.get(truck_id)
    if lane is None or not lane.driver_id:
        return []
    return [
        other_id
        for other_id, other in draft.lanes.items()
        if other_id != truck_id and other.driver_id == lane.driver_id and lanes_conflict_for_driver(lane, other)
    ]


def cross_lane_violation(draft: BoardDraft) -> Optional[str]:
    """The two cross-lane rules the commit transform re-checks (K4.2): exclusivity and double booking."""
    seen: Set[str] = set()
    for lane in draft.lanes.values():
        for order_id in lane_order_ids(lane):
            if order_id in seen:
                return "order_exclusivity"
            seen.add(order_id)
    for truck_id in draft.lanes:
        if double_booked(draft, truck_id):
            return "driver_double_booked"
    return None


def _check_limits(draft: BoardDraft, touched: Iterable[str]) -> None:
    if len(draft.lanes) > MAX_LANES:
        raise EngineError("limit_exceeded")
    for truck_id in touched:
        lane = draft.lanes.get(truck_id)
        if lane is None:
            continue
        for load in lane.loads:
            if len(load.stops) > MAX_STOPS_PER_LOAD:
                raise EngineError("limit_exceeded")


# ---------------------------------------------------------------------------
# Derived lane fields: times, ETAs, allocations
# ---------------------------------------------------------------------------


def _compartments(ctx: Any, truck_id: str) -> List[Compartment]:
    return list(_ctx(ctx, "compartments", {}).get(truck_id, []) or [])


def _canonical(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    try:
        return canonicalize(code)
    except (UnknownFuelProductError, TypeError, ValueError):
        return str(code).strip().upper()


def delivery_requests(load: Load, ctx: Any, *, exclude: Iterable[str] = ()) -> List[DeliveryRequest]:
    """Solver requests for a load's orders, from the current order documents."""
    orders = _ctx(ctx, "orders", {})
    skip = set(exclude)
    requests: List[DeliveryRequest] = []
    for stop in load.stops:
        if stop.order_id in skip:
            continue
        order = orders.get(stop.order_id)
        liters = order_liters(order)
        if order is None or liters is None:
            continue
        code = _canonical(order.get("product_code"))
        grade = legacy_grade_for_product(code) if code else None
        try:
            fuel_grade = FuelGrade(grade or "AGO")
        except ValueError:
            fuel_grade = FuelGrade("AGO")
        requests.append(
            DeliveryRequest(
                station_id=str(order.get("customer_id") or stop.order_id),
                order_id=stop.order_id,
                fuel_grade=fuel_grade,
                product_code=code,
                quantity_liters=liters,
            )
        )
    return requests


def compute_allocations(truck_id: str, load: Load, ctx: Any, tenant_id: str) -> List[Allocation]:
    """Overrides as given, the rest from ``optimize_loading_plan`` on the free compartments."""
    compartments = _compartments(ctx, truck_id)
    by_id = {c.compartment_id: c for c in compartments}
    orders = _ctx(ctx, "orders", {})
    allocations: List[Allocation] = []
    used: Set[str] = set()
    for order_id, shares in load.allocation_overrides.items():
        product = _canonical((orders.get(order_id) or {}).get("product_code"))
        for share in shares:
            comp = by_id.get(share.compartment_id)
            allocations.append(
                Allocation(
                    order_id=order_id,
                    compartment_id=share.compartment_id,
                    product_code=product,
                    liters=float(share.liters),
                    capacity_liters=float(comp.capacity_liters) if comp else 0.0,
                )
            )
            used.add(share.compartment_id)
    free = [c for c in compartments if c.compartment_id not in used]
    requests = delivery_requests(load, ctx, exclude=load.allocation_overrides.keys())
    if free and requests:
        try:
            plan = optimize_loading_plan(free, requests, truck_id, tenant_id)
        except Exception:
            plan = None
        if plan is not None:
            for a in plan.assignments:
                allocations.append(
                    Allocation(
                        order_id=a.order_id,
                        compartment_id=a.compartment_id,
                        product_code=a.product_code,
                        liters=float(a.quantity_liters),
                        capacity_liters=float(a.compartment_capacity_liters),
                    )
                )
    return allocations


def _start_location(ctx: Any, truck_id: str, load: Load, previous: Optional[Load]) -> Optional[LatLon]:
    terminal = _to_point(_ctx(ctx, "terminal_locations", {}).get(load.terminal_id)) if load.terminal_id else None
    prev_last = None
    if previous is not None and previous.stops:
        prev_last = previous.stops[-1].location
    truck = _to_point(_ctx(ctx, "truck_positions", {}).get(truck_id))
    return eta.resolve_start_location(
        terminal_location=terminal,
        previous_load_last_stop=prev_last,
        truck_position=truck,
    )


def load_times(ctx: Any, truck_id: str, load: Load, previous: Optional[Load]) -> eta.LoadTimes:
    """Start, ETAs and end for one load given the one before it (K3.6)."""
    tz = _ctx(ctx, "timezone", "America/Chicago")
    service_date = getattr(ctx, "service_date")
    start = eta.default_load_start(service_date, load.shift_id, tz)
    if previous is not None and previous.planned_end is not None and previous.planned_end > start:
        start = previous.planned_end
    return eta.compute_load_times(
        start_location=_start_location(ctx, truck_id, load, previous),
        start_time=start,
        stop_locations=[s.location for s in load.stops],
        has_terminal=bool(load.terminal_id),
        tz_name=tz,
        windows_end=[s.snapshot.window.end for s in load.stops],
    )


def refresh_lane(lane: Lane, ctx: Any) -> None:
    """Recompute load times, stop ETAs and allocations for a lane in place."""
    tenant_id = getattr(ctx, "tenant_id", "")
    previous: Optional[Load] = None
    for load in lane.loads:
        times = load_times(ctx, lane.truck_id, load, previous)
        load.planned_start = times.start
        load.planned_end = times.end
        for stop, stop_eta in zip(load.stops, times.stop_etas):
            stop.eta = stop_eta
        load.allocations = compute_allocations(lane.truck_id, load, ctx, tenant_id)
        previous = load


# ---------------------------------------------------------------------------
# Placement helpers
# ---------------------------------------------------------------------------


def _find_load(draft: BoardDraft, load_id: str) -> Tuple[str, Lane, int]:
    for truck_id, lane in draft.lanes.items():
        for i, load in enumerate(lane.loads):
            if load.load_id == load_id:
                return truck_id, lane, i
    raise EngineError("unknown_load")


def _lane(draft: BoardDraft, truck_id: str) -> Lane:
    lane = draft.lanes.get(truck_id)
    if lane is None:
        raise EngineError("unknown_truck")
    return lane


def _remove_order(draft: BoardDraft, order_id: str) -> Optional[str]:
    """Take an order off whatever lane holds it. Returns that lane's truck id."""
    truck_id = draft.order_index.get(order_id)
    if truck_id is None:
        return None
    lane = draft.lanes[truck_id]
    for load in lane.loads:
        before = len(load.stops)
        load.stops = [s for s in load.stops if s.order_id != order_id]
        if len(load.stops) != before:
            load.allocation_overrides.pop(order_id, None)
    lane.shelf = [o for o in lane.shelf if o != order_id]
    draft.order_index.pop(order_id, None)
    return truck_id


def _drop_empty_loads(lane: Lane) -> None:
    """Empty draft loads disappear; published loads stay so a re-publish can retire them."""
    lane.loads = [l for l in lane.loads if l.stops or l.load_id in lane.publish.plans]


def _make_stop(order_id: str, ctx: Any) -> Stop:
    orders = _ctx(ctx, "orders", {})
    order = orders.get(order_id)
    if order is None:
        raise EngineError("unknown_order")
    location = _to_point(_ctx(ctx, "order_locations", {}).get(order_id))
    return Stop(order_id=order_id, snapshot=snapshot_of(order), location=location)


def _insertion_stops(load: Load) -> List[eta.InsertionStop]:
    return [eta.InsertionStop(location=s.location, window_end=s.snapshot.window.end) for s in load.stops]


def _best_index(ctx: Any, truck_id: str, lane: Lane, load_pos: int, stop: Stop) -> int:
    load = lane.loads[load_pos]
    previous = lane.loads[load_pos - 1] if load_pos > 0 else None
    tz = _ctx(ctx, "timezone", "America/Chicago")
    service_date = getattr(ctx, "service_date")
    start = eta.default_load_start(service_date, load.shift_id, tz)
    if previous is not None and previous.planned_end is not None and previous.planned_end > start:
        start = previous.planned_end
    return eta.best_insertion_index(
        _insertion_stops(load),
        eta.InsertionStop(location=stop.location, window_end=stop.snapshot.window.end),
        start_location=_start_location(ctx, truck_id, load, previous),
        start_time=start,
        has_terminal=bool(load.terminal_id),
        tz_name=tz,
    )


def _feasible(ctx: Any, truck_id: str, load: Load) -> bool:
    from Agents.support.compartment_solver import check_feasibility

    compartments = _compartments(ctx, truck_id)
    if not compartments:
        return False
    requests = delivery_requests(load, ctx)
    if not requests:
        return True
    try:
        return bool(check_feasibility(compartments, requests).feasible)
    except Exception:
        return False


def _new_load(ctx: Any, lane: Lane, shift_id: Optional[str] = None, **extra: Any) -> Load:
    if shift_id is None:
        shift_id = lane.loads[-1].shift_id if lane.loads else "day"
    return Load(load_id=_new_id(ctx), shift_id=shift_id, **extra)


def _place(ctx: Any, draft: BoardDraft, truck_id: str, stops: List[Stop], target: Target) -> Tuple[str, str, int]:
    """Insert ``stops`` into lane ``truck_id`` at ``target``. Returns (truck, load_id, first index)."""
    lane = _lane(draft, truck_id)
    if target.load_id == "new":
        if target.index not in (None, 0):
            raise EngineError("index_out_of_range")
        load = _new_load(ctx, lane)
        lane.loads.append(load)
        load.stops.extend(stops)
        return truck_id, load.load_id, 0
    if target.load_id is not None:
        pos = next((i for i, l in enumerate(lane.loads) if l.load_id == target.load_id), None)
        if pos is None:
            raise EngineError("unknown_load")
        load = lane.loads[pos]
        if target.index is not None:
            if target.index > len(load.stops):
                raise EngineError("index_out_of_range")
            load.stops[target.index:target.index] = stops
            return truck_id, load.load_id, target.index
        first = None
        for stop in stops:
            index = _best_index(ctx, truck_id, lane, pos, stop)
            load.stops.insert(index, stop)
            first = index if first is None else first
        return truck_id, load.load_id, first or 0
    if target.index is not None:
        raise EngineError("index_requires_load", fields=["target.load_id"])
    return _best_fit(ctx, draft, truck_id, stops)


def _best_fit(ctx: Any, draft: BoardDraft, truck_id: str, stops: List[Stop]) -> Tuple[str, str, int]:
    """K4.3: each not-started load in time order, then a new load; the first feasible
    candidate with the fewest added minutes wins (ties keep the earlier load)."""
    lane = draft.lanes[truck_id]
    best: Optional[Tuple[float, int, Lane]] = None
    candidates = [i for i, l in enumerate(lane.loads) if not load_started(lane, l.load_id, ctx)]
    for order_pos, pos in enumerate(candidates + [len(lane.loads)]):
        trial = lane.model_copy(deep=True)
        if pos == len(lane.loads):
            trial.loads.append(_new_load(ctx, trial))
        refresh_lane(trial, ctx)
        before_end = trial.loads[pos].planned_end if pos < len(lane.loads) else None
        base_minutes = (
            eta.minutes_between(trial.loads[pos].planned_start, before_end)
            if before_end is not None and trial.loads[pos].planned_start is not None
            else 0.0
        )
        for stop in stops:
            index = _best_index(ctx, truck_id, trial, pos, stop)
            trial.loads[pos].stops.insert(index, stop.model_copy(deep=True))
        refresh_lane(trial, ctx)
        load = trial.loads[pos]
        if not _feasible(ctx, truck_id, load):
            continue
        added = eta.minutes_between(load.planned_start, load.planned_end) - base_minutes
        key = (round(added, 6), order_pos)
        if best is None or key < (round(best[0], 6), best[1]):
            best = (added, order_pos, trial)
    if best is None:
        lane.loads.append(_new_load(ctx, lane))
        lane.loads[-1].stops.extend(stops)
        return truck_id, lane.loads[-1].load_id, 0
    chosen = best[2]
    # Keep the generated new-load id when the new load won.
    draft.lanes[truck_id] = chosen
    chosen_load = chosen.loads[(candidates + [len(lane.loads)])[best[1]]]
    first = next(i for i, s in enumerate(chosen_load.stops) if s.order_id == stops[0].order_id)
    return truck_id, chosen_load.load_id, first


def _dispatched_on_board(order: Optional[Dict[str, Any]], draft: BoardDraft) -> bool:
    """A dispatched order linked to one of this draft's published plans (goes to the shelf, R13.5)."""
    if not order or order.get("status") not in DISPATCHED_STATUSES:
        return False
    return (order.get("assigned_run_id") or "") in published_run_ids(draft)


def _require_expected(command: Any, touched: Iterable[str]) -> None:
    missing = [t for t in touched if t not in command.expected_lane_versions]
    if missing:
        raise EngineError("missing_expected_version", fields=["expected_lane_versions"])


# ---------------------------------------------------------------------------
# apply (K4.1)
# ---------------------------------------------------------------------------


def apply(draft: BoardDraft, command: Any, ctx: Any) -> ApplyResult:
    """Apply one command to a copy of ``draft``. The input draft is never mutated."""
    work = draft.model_copy(deep=True)
    rebuild_index(work)
    handler = _HANDLERS.get(type(command))
    if handler is None:
        raise EngineError("unsupported_command")
    result = handler(work, command, ctx)
    for truck_id in result.touched:
        lane = work.lanes.get(truck_id)
        if lane is not None:
            _drop_empty_loads(lane)
            refresh_lane(lane, ctx)
    rebuild_index(work)
    _check_limits(work, result.touched)
    result.draft = work
    return result


def _add_lane(draft: BoardDraft, cmd: AddLaneCommand, ctx: Any) -> ApplyResult:
    _require_expected(cmd, [cmd.truck_id])
    if cmd.truck_id in draft.lanes:
        raise EngineError("lane_exists")
    known = _ctx(ctx, "compartments", {})
    unavailable = _ctx(ctx, "unavailable", set())
    if cmd.truck_id not in known and "compartments" not in unavailable:
        raise EngineError("unknown_truck")
    if len(draft.lanes) >= MAX_LANES:
        raise EngineError("limit_exceeded")
    draft.lanes[cmd.truck_id] = Lane(truck_id=cmd.truck_id, version=0)
    return ApplyResult(draft=draft, touched=[cmd.truck_id])


def _remove_lane(draft: BoardDraft, cmd: RemoveLaneCommand, ctx: Any) -> ApplyResult:
    lane = _lane(draft, cmd.truck_id)
    _require_expected(cmd, [cmd.truck_id])
    if lane.publish.published_version is not None or lane.publish.plans:
        raise EngineError("lane_published")
    orders = _ctx(ctx, "orders", {})
    if any(_dispatched_on_board(orders.get(o), draft) for o in lane_order_ids(lane)):
        raise EngineError("lane_holds_dispatched_orders")
    del draft.lanes[cmd.truck_id]
    return ApplyResult(draft=draft, touched=[cmd.truck_id], removed_lanes=[cmd.truck_id])


def _pair_driver(draft: BoardDraft, cmd: PairDriverCommand, ctx: Any) -> ApplyResult:
    lane = _lane(draft, cmd.truck_id)
    touched = [cmd.truck_id]
    if cmd.driver_id is not None:
        drivers = _ctx(ctx, "drivers", {})
        unavailable = _ctx(ctx, "unavailable", set())
        if cmd.driver_id not in drivers and "drivers" not in unavailable:
            raise EngineError("unknown_driver")
        lane.driver_id = cmd.driver_id
        # One lane per driver per conflicting window (R6.2): the pairing moves.
        for other_id in double_booked(draft, cmd.truck_id):
            draft.lanes[other_id].driver_id = None
            touched.append(other_id)
    else:
        lane.driver_id = None
    _require_expected(cmd, touched)
    return ApplyResult(draft=draft, touched=touched)


def _assign_or_move(draft: BoardDraft, cmd: Any, ctx: Any, *, must_be_on_board: bool) -> ApplyResult:
    orders = _ctx(ctx, "orders", {})
    _lane(draft, cmd.truck_id)
    touched: List[str] = [cmd.truck_id]
    for order_id in cmd.order_ids:
        on_board = draft.order_index.get(order_id)
        if must_be_on_board and on_board is None:
            raise EngineError("order_not_on_board")
        if on_board is None and order_id not in orders:
            raise EngineError("unknown_order")
        if on_board is not None and on_board not in touched:
            touched.append(on_board)
    _require_expected(cmd, touched)
    stops: List[Stop] = []
    for order_id in cmd.order_ids:
        existing = _existing_stop(draft, order_id)
        _remove_order(draft, order_id)
        if existing is not None:
            if order_id in orders:
                existing.location = _to_point(_ctx(ctx, "order_locations", {}).get(order_id)) or existing.location
            stops.append(existing)
        else:
            stops.append(_make_stop(order_id, ctx))
    # An explicit index counts positions after the moved stops are taken out.
    placed = _place(ctx, draft, cmd.truck_id, stops, cmd.target)
    return ApplyResult(draft=draft, touched=touched, placed=placed)


def _existing_stop(draft: BoardDraft, order_id: str) -> Optional[Stop]:
    truck_id = draft.order_index.get(order_id)
    if truck_id is None:
        return None
    lane = draft.lanes[truck_id]
    for load in lane.loads:
        for stop in load.stops:
            if stop.order_id == order_id:
                return stop.model_copy(deep=True)
    return None  # on the shelf: a fresh stop is built from the order


def _assign_orders(draft: BoardDraft, cmd: AssignOrdersCommand, ctx: Any) -> ApplyResult:
    return _assign_or_move(draft, cmd, ctx, must_be_on_board=False)


def _move_stops(draft: BoardDraft, cmd: MoveStopsCommand, ctx: Any) -> ApplyResult:
    return _assign_or_move(draft, cmd, ctx, must_be_on_board=True)


def _unassign_orders(draft: BoardDraft, cmd: UnassignOrdersCommand, ctx: Any) -> ApplyResult:
    orders = _ctx(ctx, "orders", {})
    touched: List[str] = []
    for order_id in cmd.order_ids:
        truck_id = draft.order_index.get(order_id)
        if truck_id is None:
            raise EngineError("order_not_on_board")
        if truck_id not in touched:
            touched.append(truck_id)
    _require_expected(cmd, touched)
    for order_id in cmd.order_ids:
        truck_id = draft.order_index[order_id]
        lane = draft.lanes[truck_id]
        if _dispatched_on_board(orders.get(order_id), draft):
            if order_id in lane.shelf:
                raise EngineError("dispatched_order_cannot_unassign")
            _remove_order(draft, order_id)
            lane.shelf.append(order_id)  # R13.5: dispatched orders wait on the shelf
            draft.order_index[order_id] = truck_id
        else:
            _remove_order(draft, order_id)
    return ApplyResult(draft=draft, touched=touched)


def _move_load(draft: BoardDraft, cmd: MoveLoadCommand, ctx: Any) -> ApplyResult:
    source_id, source, pos = _find_load(draft, cmd.load_id)
    target = _lane(draft, cmd.truck_id)
    touched = [source_id] if source_id == cmd.truck_id else [source_id, cmd.truck_id]
    _require_expected(cmd, touched)
    load = source.loads.pop(pos)
    if cmd.index > len(target.loads):
        raise EngineError("index_out_of_range")
    if source_id != cmd.truck_id:
        load.allocation_overrides = {}
    target.loads.insert(cmd.index, load)
    return ApplyResult(draft=draft, touched=touched)


def _set_terminal(draft: BoardDraft, cmd: SetTerminalCommand, ctx: Any) -> ApplyResult:
    truck_id, lane, pos = _find_load(draft, cmd.load_id)
    _require_expected(cmd, [truck_id])
    terminals = getattr(ctx, "terminals", None)
    if cmd.terminal_id is not None and terminals is not None and cmd.terminal_id not in terminals:
        raise EngineError("unknown_terminal")
    lane.loads[pos].terminal_id = cmd.terminal_id
    return ApplyResult(draft=draft, touched=[truck_id])


def _set_allocation(draft: BoardDraft, cmd: SetAllocationCommand, ctx: Any) -> ApplyResult:
    truck_id, lane, pos = _find_load(draft, cmd.load_id)
    _require_expected(cmd, [truck_id])
    load = lane.loads[pos]
    if cmd.order_id not in {s.order_id for s in load.stops}:
        raise EngineError("unknown_order")
    if cmd.shares is None:
        load.allocation_overrides.pop(cmd.order_id, None)
        return ApplyResult(draft=draft, touched=[truck_id])
    by_id = {c.compartment_id: c for c in _compartments(ctx, truck_id)}
    if any(s.compartment_id not in by_id for s in cmd.shares):
        raise EngineError("unknown_compartment")
    if len({s.compartment_id for s in cmd.shares}) != len(cmd.shares):
        raise EngineError("invalid_shares")
    liters = order_liters(_ctx(ctx, "orders", {}).get(cmd.order_id))
    total = sum(float(s.liters) for s in cmd.shares)
    if liters is not None and total > liters * (1 + SHARE_TOLERANCE):
        raise EngineError("invalid_shares")
    load.allocation_overrides[cmd.order_id] = [CompartmentShare(**s.model_dump()) for s in cmd.shares]
    return ApplyResult(draft=draft, touched=[truck_id])


def _set_load_shift(draft: BoardDraft, cmd: SetLoadShiftCommand, ctx: Any) -> ApplyResult:
    truck_id, lane, pos = _find_load(draft, cmd.load_id)
    _require_expected(cmd, [truck_id])
    lane.loads[pos].shift_id = cmd.shift_id
    return ApplyResult(draft=draft, touched=[truck_id])


def _accept_suggestion(draft: BoardDraft, cmd: AcceptSuggestionCommand, ctx: Any) -> ApplyResult:
    """K9: one load per suggested plan, stops in route order, ``source: "suggestion"``."""
    suggestions = _ctx(ctx, "suggestions", {})
    suggestion = suggestions.get(cmd.suggestion_id)
    if suggestion is None:
        raise EngineError("unknown_suggestion")
    loads = list(suggestion.get("loads") or [])
    if cmd.load_ids is not None:
        wanted = set(cmd.load_ids)
        loads = [l for l in loads if l.get("load_key") in wanted]
        if not loads:
            raise EngineError("unknown_load")
    orders = _ctx(ctx, "orders", {})
    touched: List[str] = []
    for spec in loads:
        truck_id = spec.get("truck_id")
        if truck_id not in touched:
            touched.append(truck_id)
        for order_id in spec.get("order_ids") or []:
            if order_id not in orders and order_id not in draft.order_index:
                raise EngineError("unknown_order")
            source = draft.order_index.get(order_id)
            if source is not None and source not in touched:
                touched.append(source)
    _require_expected(cmd, touched)
    for spec in loads:
        truck_id = spec["truck_id"]
        if truck_id not in draft.lanes:  # E16: accepting creates the lane
            if len(draft.lanes) >= MAX_LANES:
                raise EngineError("limit_exceeded")
            draft.lanes[truck_id] = Lane(truck_id=truck_id, version=0)
        stops = []
        for order_id in spec.get("order_ids") or []:
            existing = _existing_stop(draft, order_id)
            _remove_order(draft, order_id)
            stops.append(existing or _make_stop(order_id, ctx))
        lane = draft.lanes[truck_id]
        lane.loads.append(
            _new_load(
                ctx,
                lane,
                terminal_id=spec.get("terminal_id"),
                source="suggestion",
                suggestion_id=cmd.suggestion_id,
                stops=stops,
            )
        )
    return ApplyResult(draft=draft, touched=touched)


def _discard(draft: BoardDraft, cmd: DiscardLaneChangesCommand, ctx: Any) -> ApplyResult:
    lane = _lane(draft, cmd.truck_id)
    _require_expected(cmd, [cmd.truck_id])
    published = lane.publish.published_content
    if lane.publish.state != "published" or published is None or content_hash(lane) == lane.publish.published_hash:
        raise EngineError("lane_not_modified")
    orders = _ctx(ctx, "orders", {})
    published_ids = {s.order_id for l in published.loads for s in l.stops} | set(published.shelf)
    for order_id in published_ids:
        holder = draft.order_index.get(order_id)
        if holder is not None and holder != cmd.truck_id:
            raise EngineError("order_on_other_lane")
    for order_id in lane_order_ids(lane):
        if order_id not in published_ids and _dispatched_on_board(orders.get(order_id), draft):
            raise EngineError("discard_moves_dispatched")
    restored = published.model_copy(deep=True)
    lane.driver_id = restored.driver_id
    lane.loads = restored.loads
    lane.shelf = restored.shelf
    return ApplyResult(draft=draft, touched=[cmd.truck_id])


_HANDLERS = {
    AddLaneCommand: _add_lane,
    RemoveLaneCommand: _remove_lane,
    PairDriverCommand: _pair_driver,
    AssignOrdersCommand: _assign_orders,
    MoveStopsCommand: _move_stops,
    UnassignOrdersCommand: _unassign_orders,
    MoveLoadCommand: _move_load,
    SetTerminalCommand: _set_terminal,
    SetAllocationCommand: _set_allocation,
    SetLoadShiftCommand: _set_load_shift,
    AcceptSuggestionCommand: _accept_suggestion,
    DiscardLaneChangesCommand: _discard,
}


def restore_lanes(draft: BoardDraft, contents: Dict[str, Optional[Dict[str, Any]]], ctx: Any) -> ApplyResult:
    """Undo/redo (K6): set each lane's content from a stored snapshot.

    ``None`` removes the lane (it was added by the target command). Lanes keep
    their versions and publish state; derived fields are recomputed. Raises
    :class:`EngineError` ``order_on_two_lanes`` when the restored content would
    put an order on two lanes.
    """
    work = draft.model_copy(deep=True)
    touched: List[str] = []
    removed: List[str] = []
    for truck_id, content in contents.items():
        touched.append(truck_id)
        if content is None:
            if truck_id in work.lanes:
                del work.lanes[truck_id]
                removed.append(truck_id)
            continue
        parsed = LaneContent.model_validate(content)
        lane = work.lanes.get(truck_id) or Lane(truck_id=truck_id, version=0)
        lane.driver_id = parsed.driver_id
        lane.loads = parsed.loads
        lane.shelf = parsed.shelf
        work.lanes[truck_id] = lane
        refresh_lane(lane, ctx)
    rebuild_index(work)
    _check_limits(work, touched)
    return ApplyResult(draft=work, touched=touched, removed_lanes=removed)


def touched_lanes_for(draft: BoardDraft, command: Any) -> List[str]:
    """Lanes a command will touch, from the stored draft (for the K4.2 pre-checks)."""
    trucks: List[str] = []

    def add(t: Optional[str]) -> None:
        if t and t not in trucks:
            trucks.append(t)

    for attr in ("truck_id",):
        add(getattr(command, attr, None))
    for order_id in getattr(command, "order_ids", None) or []:
        add(draft.order_index.get(order_id))
    load_id = getattr(command, "load_id", None)
    if load_id:
        for truck_id, lane in draft.lanes.items():
            if any(l.load_id == load_id for l in lane.loads):
                add(truck_id)
    for truck_id in getattr(command, "expected_lane_versions", {}) or {}:
        add(truck_id)
    return trucks


def all_orders(lanes: Sequence[Lane]) -> List[str]:
    out: List[str] = []
    for lane in lanes:
        out.extend(lane_order_ids(lane))
    return out
