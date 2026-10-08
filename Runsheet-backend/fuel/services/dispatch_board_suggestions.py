"""Dispatch Board agent suggestions (design K9; R16).

* **Read.** Agent ``mvp_load_plans`` (``must_not source=dispatch_board``;
  agent plans carry no ``source``) in status ``proposed`` or ``draft`` for the
  service day (``service_date`` when set, else ``created_at`` within the day in
  tenant time), on a truck of the tenant, plus the agent's ``mvp_routes`` for
  those plans and pending ``apply_loading_plan`` approvals joined by
  ``parameters.plan_id``. Plans are shown only while
  ``overlay.compartment_loading`` is ``active_gated`` or ``active_auto``; the
  route order and ETAs are used only while ``overlay.route_planning`` is
  (R16.5, Q6). Shadow output is never shown.
* **Id.** The plan id. Plans in ``draft.dismissed_suggestions`` are hidden.
* **Diff.** :func:`diff` is pure and maps the accept outcome onto the
  ``Agents/support/replan_diff_models.ReplanDiff`` vocabulary.
* **Why.** Templated from the priority list, tank forecasts and the plan's
  fill; no LLM text.
* **Reject.** Adds to ``dismissed_suggestions`` and, when a pending approval
  exists, calls ``ApprovalQueueService.reject``. A reject failure is reported;
  the dismissal stays.

Accepting a suggestion is an ordinary board command (``accept_suggestion``,
engine K9) and never approves the agent's approval entry (R16.7). This module
writes only board drafts (I4).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from Agents.support.replan_diff_models import (
    EtaShift,
    QuantityChange,
    ReassignedStop,
    ReorderedStop,
    ReplanDiff,
    StopRef,
)
from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services import dispatch_board_eta as eta
from fuel.services import dispatch_board_telemetry as tm
from fuel.services.dispatch_board_engine import GAL_TO_L
from fuel.services.dispatch_board_es_mappings import DISPATCH_BOARD_DRAFTS_INDEX
from fuel.services.dispatch_board_models import BoardDraft, Dismissal, draft_doc_id
from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

logger = logging.getLogger(__name__)

COMPARTMENT_FLAG = "overlay.compartment_loading"
ROUTE_FLAG = "overlay.route_planning"
ACTIVE_MODES = ("active_gated", "active_auto")
SUGGESTION_STATUSES = ("proposed", "draft")
LOADING_PLAN_TOOL = "apply_loading_plan"
#: Plans read per day (one per truck is kept).
PLAN_LIMIT = 200
READ_TIMEOUT_S = 2.0
#: Quantity differences below this many gallons are rounding, not a change.
QUANTITY_EPSILON_GAL = 0.5

_PLANS_INDEX = "mvp_load_plans"
_ROUTES_INDEX = "mvp_routes"
_FORECASTS_INDEX = "mvp_tank_forecasts"
_TRUCK_COMPARTMENTS_INDEX = "truck_compartments"

AGENT_NAMES = {
    "compartment_loading": "Compartment loading agent",
    "route_planning": "Route planning agent",
}


def _hits(resp: Any) -> List[Dict[str, Any]]:
    return [h.get("_source") or {} for h in ((resp or {}).get("hits") or {}).get("hits") or []]


def _parse_dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> Optional[str]:
    dt = _parse_dt(value)
    return dt.astimezone(timezone.utc).isoformat() if dt else None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def suggested_order_ids(plan: Dict[str, Any], route: Optional[Dict[str, Any]] = None) -> List[str]:
    """Orders of a suggested plan: route stop order first, then any plan-only order."""
    out: List[str] = []
    if route:
        stops = sorted(
            (s for s in route.get("stops") or [] if isinstance(s, dict)),
            key=lambda s: (s.get("sequence") is None, s.get("sequence") or 0),
        )
        for stop in stops:
            for order_id in stop.get("order_ids") or []:
                if order_id and order_id not in out:
                    out.append(order_id)
    for assignment in plan.get("assignments") or []:
        order_id = (assignment or {}).get("order_id")
        if order_id and order_id not in out:
            out.append(order_id)
    return out


def _route_etas(route: Optional[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for stop in (route or {}).get("stops") or []:
        when = _iso((stop or {}).get("eta"))
        for order_id in (stop or {}).get("order_ids") or []:
            if when and order_id not in out:
                out[order_id] = when
    return out


def _planned_gallons(plan: Dict[str, Any]) -> Dict[str, float]:
    liters: Dict[str, float] = {}
    for assignment in plan.get("assignments") or []:
        order_id = (assignment or {}).get("order_id")
        qty = _float((assignment or {}).get("quantity_liters"))
        if order_id and qty is not None:
            liters[order_id] = liters.get(order_id, 0.0) + qty
    return {o: round(l / GAL_TO_L, 1) for o, l in liters.items()}


def _lane_stops(lane_content: Any) -> List[Tuple[str, Any]]:
    if lane_content is None:
        return []
    return [(s.order_id, s) for load in lane_content.loads for s in load.stops]


def diff(
    lane_content: Optional[Any],
    plan: Dict[str, Any],
    route: Optional[Dict[str, Any]] = None,
    *,
    order_index: Optional[Dict[str, str]] = None,
    orders: Optional[Dict[str, Dict[str, Any]]] = None,
) -> ReplanDiff:
    """What accepting ``plan`` does to the lane of its truck (K9).

    ``lane_content`` is the truck's current lane (``Lane`` or ``LaneContent``,
    ``None`` when the truck has no lane). Accept appends one load with the
    suggested orders in route order and takes them off any other place, so:

    * ``added_stops``: suggested orders that are on no lane (from the tray);
    * ``reassigned_stops``: suggested orders on another truck's lane;
    * ``reordered_stops``: orders already on this lane whose position changes;
    * ``removed_stops``: orders this lane loses (accept never removes any, so
      this stays empty unless the vocabulary is reused for another producer);
    * ``quantity_changes``: requested gallons vs the plan's allocated gallons;
    * ``eta_shifts``: the lane's ETA vs the route's ETA for the same order.
    """
    truck_id = plan.get("truck_id") or ""
    order_index = order_index or {}
    orders = orders or {}
    suggested = suggested_order_ids(plan, route)
    suggested_set = set(suggested)
    before = _lane_stops(lane_content)
    before_ids = [o for o, _s in before]
    before_stop = dict(before)
    after_ids = [o for o in before_ids if o not in suggested_set] + suggested
    etas = _route_etas(route)
    planned = _planned_gallons(plan)

    def requested(order_id: str) -> Optional[float]:
        stop = before_stop.get(order_id)
        if stop is not None and stop.snapshot.gallons_requested is not None:
            return float(stop.snapshot.gallons_requested)
        return _float((orders.get(order_id) or {}).get("gallons_requested"))

    def product(order_id: str) -> Optional[str]:
        stop = before_stop.get(order_id)
        if stop is not None and stop.snapshot.product_code:
            return stop.snapshot.product_code
        return (orders.get(order_id) or {}).get("product_code")

    added: List[StopRef] = []
    reassigned: List[ReassignedStop] = []
    for index, order_id in enumerate(after_ids):
        if order_id not in suggested_set or order_id in before_stop:
            continue
        holder = order_index.get(order_id)
        if holder and holder != truck_id:
            reassigned.append(ReassignedStop(stop_id=order_id, from_truck_id=holder, to_truck_id=truck_id or "unknown"))
            continue
        gallons = planned.get(order_id, requested(order_id))
        added.append(
            StopRef(
                stop_id=order_id,
                index=index,
                gallons=gallons if gallons is not None and gallons >= 0 else None,
                product_code=product(order_id),
                eta=etas.get(order_id),
            )
        )
    removed = [
        StopRef(stop_id=o, index=i, gallons=requested(o), product_code=product(o))
        for i, o in enumerate(before_ids)
        if o not in after_ids
    ]
    reordered = [
        ReorderedStop(stop_id=o, before_index=i, after_index=after_ids.index(o))
        for i, o in enumerate(before_ids)
        if o in after_ids and after_ids.index(o) != i
    ]
    quantity: List[QuantityChange] = []
    for order_id in suggested:
        before_gal = requested(order_id)
        after_gal = planned.get(order_id)
        if before_gal is None or after_gal is None or abs(before_gal - after_gal) < QUANTITY_EPSILON_GAL:
            continue
        quantity.append(
            QuantityChange(stop_id=order_id, before_gallons=max(0.0, before_gal), after_gallons=max(0.0, after_gal), product_code=product(order_id))
        )
    shifts: List[EtaShift] = []
    for order_id in suggested:
        stop = before_stop.get(order_id)
        after_eta = etas.get(order_id)
        if stop is None or stop.eta is None or after_eta is None:
            continue
        before_dt = stop.eta if stop.eta.tzinfo else stop.eta.replace(tzinfo=timezone.utc)
        minutes = (_parse_dt(after_eta) - before_dt).total_seconds() / 60.0  # type: ignore[operator]
        if abs(minutes) < 1:
            continue
        shifts.append(EtaShift(stop_id=order_id, before_eta=before_dt.astimezone(timezone.utc).isoformat(), after_eta=after_eta, shift_minutes=round(minutes, 1)))
    return ReplanDiff(
        original_route_id=f"board:{truck_id or 'unknown'}",
        patched_route_id=(route or {}).get("route_id") or plan.get("plan_id") or "suggestion",
        added_stops=added,
        removed_stops=removed,
        reordered_stops=reordered,
        reassigned_stops=reassigned,
        quantity_changes=quantity,
        eta_shifts=shifts,
    )


def why(
    order_ids: Sequence[str],
    *,
    priorities: Dict[str, Tuple[float, Optional[str]]],
    runout_hours: Optional[float],
    window_end: Optional[datetime],
    fill_pct: Optional[float],
    tz: str,
) -> Dict[str, Any]:
    """Templated reason text (K9): priority, runout or window, fill."""
    best: Optional[Tuple[float, Optional[str]]] = None
    for order_id in order_ids:
        entry = priorities.get(order_id)
        if entry is not None and (best is None or entry[0] > best[0]):
            best = entry
    parts: List[str] = []
    if best is not None:
        bucket = (best[1] or "").replace("_", " ")
        parts.append(f"Priority {bucket} ({best[0]:.0f})" if bucket else f"Priority score {best[0]:.0f}")
    if runout_hours is not None:
        parts.append(f"Tank runs out in {runout_hours:.0f} h")
    elif window_end is not None:
        local = window_end.astimezone(eta.zone(tz))
        parts.append(f"Meets the window ending {local.strftime('%H:%M')}")
    if fill_pct is not None:
        parts.append(f"Fills {fill_pct:.0f}% of the truck")
    return {
        "text": ". ".join(parts) + ("." if parts else ""),
        "priority_score": best[0] if best else None,
        "priority_bucket": best[1] if best else None,
        "runout_hours": runout_hours,
        "window_end": window_end.astimezone(timezone.utc).isoformat() if window_end else None,
        "fill_pct": fill_pct,
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class _Raw:
    __slots__ = ("plan", "route", "order_ids", "route_active")

    def __init__(self, plan: Dict[str, Any], route: Optional[Dict[str, Any]], order_ids: List[str], route_active: bool) -> None:
        self.plan = plan
        self.route = route
        self.order_ids = order_ids
        self.route_active = route_active

    @property
    def plan_id(self) -> str:
        return self.plan["plan_id"]

    @property
    def truck_id(self) -> str:
        return self.plan["truck_id"]

    def load_spec(self) -> Dict[str, Any]:
        return {
            "load_key": self.plan_id,
            "truck_id": self.truck_id,
            "terminal_id": self.plan.get("terminal_id"),
            "order_ids": list(self.order_ids),
        }


class BoardSuggestionService:
    """Suggestion read, context for ``accept_suggestion`` and reject (K9)."""

    def __init__(
        self,
        *,
        es_service: Any,
        feature_flags: Any,
        board_service: Any,
        approval_queue: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._es = es_service
        self._flags = feature_flags
        self._board = board_service
        self._approvals = approval_queue
        self._clock = clock or getattr(board_service, "now", None) or (lambda: datetime.now(timezone.utc))

    # -- gating (R16.5) ----------------------------------------------------

    async def _mode(self, flag_key: str, tenant_id: str) -> str:
        if self._flags is None:
            return "disabled"
        try:
            return await self._flags.get_overlay_state(flag_key, tenant_id) or "disabled"
        except Exception as exc:
            logger.warning("dispatch board suggestion flag read failed: %s", type(exc).__name__)
            return "disabled"

    # -- read --------------------------------------------------------------

    async def _read(self, tenant_id: str, service_date: date, tz: str, dismissed: Iterable[str]) -> List[_Raw]:
        loading, routing = await asyncio.gather(self._mode(COMPARTMENT_FLAG, tenant_id), self._mode(ROUTE_FLAG, tenant_id))
        if loading not in ACTIVE_MODES:
            return []
        route_active = routing in ACTIVE_MODES
        day_start, _ = eta.shift_window(service_date, "all", tz)
        day_end = eta.local_at(service_date + timedelta(days=1), eta.SHIFT_TIMES["all"][0], tz)
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"status": list(SUGGESTION_STATUSES)}},
                    ],
                    "should": [
                        {"term": {"service_date": service_date.isoformat()}},
                        {"bool": {
                            "filter": [{"range": {"created_at": {
                                "gte": day_start.astimezone(timezone.utc).isoformat(),
                                "lt": day_end.astimezone(timezone.utc).isoformat(),
                            }}}],
                            "must_not": [{"exists": {"field": "service_date"}}],
                        }},
                    ],
                    "minimum_should_match": 1,
                    "must_not": [{"term": {"source": "dispatch_board"}}],
                }
            },
            "sort": [{"created_at": {"order": "desc"}}],
            "size": PLAN_LIMIT,
        }
        resp = await asyncio.wait_for(self._es.search_documents(_PLANS_INDEX, query, PLAN_LIMIT), timeout=READ_TIMEOUT_S)
        skip = set(dismissed)
        newest: Dict[str, Dict[str, Any]] = {}
        for plan in _hits(resp):
            # Defence in depth: the query already excludes these.
            if plan.get("tenant_id") != tenant_id or plan.get("source") == "dispatch_board":
                continue
            if plan.get("status") not in SUGGESTION_STATUSES or not plan.get("plan_id") or not plan.get("truck_id"):
                continue
            if plan["plan_id"] in skip:
                continue
            current = newest.get(plan["truck_id"])
            if current is None or str(plan.get("created_at") or "") > str(current.get("created_at") or ""):
                newest[plan["truck_id"]] = plan
        if not newest:
            return []
        trucks = await self._tenant_trucks(tenant_id, list(newest))
        plans = [p for t, p in newest.items() if t in trucks]
        routes = await self._routes(tenant_id, [p["plan_id"] for p in plans]) if route_active else {}
        out: List[_Raw] = []
        for plan in sorted(plans, key=lambda p: p["truck_id"]):
            route = routes.get(plan["plan_id"])
            order_ids = suggested_order_ids(plan, route)
            if order_ids:
                out.append(_Raw(plan, route, order_ids, route_active))
        return out

    async def _tenant_trucks(self, tenant_id: str, truck_ids: List[str]) -> Set[str]:
        query = {
            "query": {"bool": {"filter": [{"term": {"tenant_id": tenant_id}}, {"terms": {"truck_id": truck_ids}}]}},
            "_source": ["truck_id", "tenant_id"],
            "size": 1000,
        }
        resp = await asyncio.wait_for(self._es.search_documents(_TRUCK_COMPARTMENTS_INDEX, query, 1000), timeout=READ_TIMEOUT_S)
        return {h.get("truck_id") for h in _hits(resp) if h.get("tenant_id") == tenant_id and h.get("truck_id")}

    async def _routes(self, tenant_id: str, plan_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        if not plan_ids:
            return {}
        query = {
            "query": {
                "bool": {
                    "filter": [{"term": {"tenant_id": tenant_id}}, {"terms": {"plan_id": plan_ids}}],
                    "must_not": [{"term": {"source": "dispatch_board"}}],
                }
            },
            "size": 500,
        }
        resp = await asyncio.wait_for(self._es.search_documents(_ROUTES_INDEX, query, 500), timeout=READ_TIMEOUT_S)
        out: Dict[str, Dict[str, Any]] = {}
        for route in _hits(resp):
            if route.get("tenant_id") != tenant_id or route.get("source") == "dispatch_board":
                continue
            plan_id = route.get("plan_id")
            stamp = str(route.get("timestamp") or route.get("created_at") or "")
            current = out.get(plan_id)
            if current is None or stamp > str(current.get("timestamp") or current.get("created_at") or ""):
                out[plan_id] = route
        return out

    async def for_context(self, tenant_id: str, service_date: date, *, tz: str, dismissed: Iterable[str] = ()) -> Dict[str, Dict[str, Any]]:
        """``{plan_id: {"loads": [load spec]}}`` for ``accept_suggestion`` (engine K9)."""
        raws = await self._read(tenant_id, service_date, tz, dismissed)
        return {r.plan_id: {"loads": [r.load_spec()], "truck_id": r.truck_id} for r in raws}

    async def list_for_day(self, draft: BoardDraft) -> List[Dict[str, Any]]:
        """Suggestions for the snapshot (K5, K9): ghost loads with why text and diff."""
        tenant_id, tz = draft.tenant_id, draft.timezone
        raws = await self._read(tenant_id, draft.service_date, tz, draft.dismissed_suggestions)
        if not raws:
            return []
        all_orders = list(dict.fromkeys(o for r in raws for o in r.order_ids))
        orders, approvals, priorities = await asyncio.gather(
            self._safe(self._orders(tenant_id, all_orders), {}),
            self._safe(self._pending_approvals(tenant_id), {}),
            self._safe(self._priorities(tenant_id), {}),
        )
        tanks = [o.get("customer_tank_id") for o in orders.values() if o.get("customer_tank_id")]
        forecasts = await self._safe(self._runouts(tenant_id, tanks), {})
        out: List[Dict[str, Any]] = []
        for raw in raws:
            lane = draft.lanes.get(raw.truck_id)
            runouts = [forecasts[orders[o]["customer_tank_id"]] for o in raw.order_ids if o in orders and orders[o].get("customer_tank_id") in forecasts]
            ends = [e for e in (_parse_dt((orders.get(o) or {}).get("delivery_window_end")) for o in raw.order_ids) if e is not None]
            plan = raw.plan
            out.append(
                {
                    "suggestion_id": raw.plan_id,
                    "plan_id": raw.plan_id,
                    "truck_id": raw.truck_id,
                    "status": plan.get("status"),
                    "agent": "compartment_loading",
                    "agent_name": AGENT_NAMES["compartment_loading"],
                    "route_agent_name": AGENT_NAMES["route_planning"] if raw.route is not None else None,
                    "route_id": (raw.route or {}).get("route_id"),
                    "created_at": _iso(plan.get("created_at")),
                    "approval_action_id": approvals.get(raw.plan_id),
                    "loads": [raw.load_spec()],
                    "why": why(
                        raw.order_ids,
                        priorities=priorities,
                        runout_hours=min(runouts) if runouts else None,
                        window_end=min(ends) if ends else None,
                        fill_pct=_float(plan.get("total_utilization_pct")),
                        tz=tz,
                    ),
                    "diff": diff(lane, plan, raw.route, order_index=draft.order_index, orders=orders).model_dump(mode="json"),
                }
            )
        return out

    @staticmethod
    async def _safe(coro: Any, default: Any) -> Any:
        try:
            return await asyncio.wait_for(coro, timeout=READ_TIMEOUT_S)
        except Exception as exc:
            logger.warning("dispatch board suggestion detail read failed: %s", type(exc).__name__)
            return default

    async def _orders(self, tenant_id: str, order_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        if not order_ids:
            return {}
        query = {
            "query": {"bool": {"filter": [{"term": {"tenant_id": tenant_id}}, {"terms": {"order_id": order_ids}}]}},
            "size": len(order_ids),
        }
        resp = await self._es.search_documents(FUEL_ORDERS_CURRENT_INDEX, query, len(order_ids))
        return {o["order_id"]: o for o in _hits(resp) if o.get("tenant_id") == tenant_id and o.get("order_id")}

    async def _pending_approvals(self, tenant_id: str) -> Dict[str, str]:
        """``plan_id -> action_id`` for pending ``apply_loading_plan`` entries."""
        if self._approvals is None:
            return {}
        page = await self._approvals.list_pending(tenant_id, size=100)
        out: Dict[str, str] = {}
        for entry in (page or {}).get("items") or []:
            if entry.get("tool_name") != LOADING_PLAN_TOOL or entry.get("status", "pending") != "pending":
                continue
            if entry.get("tenant_id") not in (None, tenant_id):
                continue
            plan_id = (entry.get("parameters") or {}).get("plan_id")
            if plan_id and entry.get("action_id") and plan_id not in out:
                out[plan_id] = entry["action_id"]
        return out

    async def _priorities(self, tenant_id: str) -> Dict[str, Tuple[float, Optional[str]]]:
        reader = getattr(self._board, "_priorities", None)
        return await reader(tenant_id) if reader is not None else {}

    async def _runouts(self, tenant_id: str, tank_ids: List[str]) -> Dict[str, float]:
        """Newest ``hours_to_runout_p50`` per customer tank."""
        tank_ids = list(dict.fromkeys(tank_ids))
        if not tank_ids:
            return {}
        query = {
            "query": {"bool": {"filter": [{"term": {"tenant_id": tenant_id}}, {"terms": {"customer_tank_id": tank_ids}}]}},
            "sort": [{"created_at": {"order": "desc"}}],
            "_source": ["customer_tank_id", "hours_to_runout_p50", "created_at", "tenant_id"],
            "size": 500,
        }
        resp = await self._es.search_documents(_FORECASTS_INDEX, query, 500)
        out: Dict[str, float] = {}
        seen: Dict[str, str] = {}
        for doc in _hits(resp):
            tank = doc.get("customer_tank_id")
            hours = _float(doc.get("hours_to_runout_p50"))
            if doc.get("tenant_id") != tenant_id or not tank or hours is None:
                continue
            stamp = str(doc.get("created_at") or "")
            if tank not in seen or stamp > seen[tank]:
                seen[tank] = stamp
                out[tank] = hours
        return out

    # -- reject (R16.4) ----------------------------------------------------

    async def reject(
        self,
        *,
        tenant_id: str,
        user_id: str,
        service_date: date,
        plan_id: str,
        reason: Optional[str] = None,
        tz: Optional[str] = None,
    ) -> Dict[str, Any]:
        tz = tz or self._board.timezone_for(tenant_id)
        _today, past = self._board.check_service_date(service_date, tz)
        if past:
            raise AppException(
                ErrorCode.DISPATCH_BOARD_READ_ONLY,
                "Past service days are read-only.",
                status_code=409,
                details={"reason": "past_service_day"},
            )
        plan = await self._es.get_document(_PLANS_INDEX, plan_id)
        if not plan or plan.get("tenant_id") != tenant_id or plan.get("source") == "dispatch_board":
            raise AppException(ErrorCode.RESOURCE_NOT_FOUND, "Suggestion not found.", status_code=404)
        await self._dismiss(tenant_id, service_date, tz, plan_id, user_id)
        approval: Optional[Dict[str, Any]] = None
        action_id = None
        try:
            action_id = (await self._pending_approvals(tenant_id)).get(plan_id)
        except Exception as exc:
            logger.warning("dispatch board approval lookup failed: %s", type(exc).__name__)
            approval = {"action_id": None, "rejected": False, "reason": "approval_lookup_failed"}
        if action_id:
            approval = {"action_id": action_id, "rejected": True, "reason": None}
            try:
                await self._approvals.reject(action_id, user_id, reason or "", tenant_id=tenant_id)
            except Exception as exc:
                approval = {"action_id": action_id, "rejected": False, "reason": _reject_reason(exc)}
                logger.info("dispatch board approval reject refused: %s", type(exc).__name__)
        self._board.telemetry.metric(tm.SUGGESTION_COUNT, 1, tenant_id=tenant_id, action="reject")
        await self._board._broadcast(  # noqa: SLF001 - shared board fan-out
            tenant_id, service_date, "board_suggestions_changed", {"service_date": service_date.isoformat()}
        )
        return {"plan_id": plan_id, "dismissed": True, "approval": approval}

    async def _dismiss(self, tenant_id: str, service_date: date, tz: str, plan_id: str, user_id: str) -> None:
        now = self._clock()
        doc_id = draft_doc_id(tenant_id, service_date)

        def transform(current_doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            current = BoardDraft.model_validate(current_doc)
            if plan_id in current.dismissed_suggestions:
                return None
            current.dismissed_suggestions[plan_id] = Dismissal(actor_user_id=user_id, at=now)
            current.updated_at = now
            return current.model_dump(mode="json")

        try:
            existing = await self._es.get_document(DISPATCH_BOARD_DRAFTS_INDEX, doc_id)
            if not existing:
                base = BoardDraft(
                    tenant_id=tenant_id, service_date=service_date, timezone=tz, created_at=now, updated_at=now
                ).model_dump(mode="json")
                await self._es.create_document(DISPATCH_BOARD_DRAFTS_INDEX, doc_id, base)
            await self._es.atomic_update(DISPATCH_BOARD_DRAFTS_INDEX, doc_id, transform)
        except Exception as exc:
            logger.error("dispatch board suggestion dismissal failed: %s", type(exc).__name__)
            raise AppException(ErrorCode.ELASTICSEARCH_UNAVAILABLE, "The board is unavailable. Retry shortly.", status_code=503) from None


def _reject_reason(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "ApprovalExpiredError":
        return "approval_expired"
    if name == "ApprovalNotFoundError":
        return "approval_not_found"
    if isinstance(exc, ValueError):
        return "approval_not_rejectable"
    if isinstance(exc, RuntimeError):
        return "approval_changed"
    return "approval_reject_failed"


__all__ = [
    "BoardSuggestionService",
    "diff",
    "why",
    "suggested_order_ids",
    "COMPARTMENT_FLAG",
    "ROUTE_FLAG",
]
