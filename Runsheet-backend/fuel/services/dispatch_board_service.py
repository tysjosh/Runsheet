"""Dispatch Board service: snapshot, validate, commands, undo/redo, history (design K4-K6, K17).

Commands follow K4.2. Validation runs outside the row lock and the commit only
compares lane versions, so a concurrent change between validation and commit
makes the commit refuse instead of committing stale checks (I2). The
authoritative idempotency record is ``draft.applied_commands``, written in the
same ``atomic_update`` as the change (K4.5); the command log
(``dispatch_board_commands``) is append-only through ``create_document`` (K2.2).

This module never writes orders, plans, routes or executions (I4).
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services import dispatch_board_engine as engine
from fuel.services import dispatch_board_eta as eta
from fuel.services import dispatch_board_telemetry as tm
from fuel.services.dispatch_board_es_mappings import (
    DISPATCH_BOARD_COMMANDS_INDEX,
    DISPATCH_BOARD_DRAFTS_INDEX,
)
from fuel.services.dispatch_board_models import (
    APPLIED_COMMANDS_LIMIT,
    AcceptSuggestionCommand,
    AcknowledgeWarningCommand,
    AppliedCommand,
    BoardDraft,
    Check,
    CommandResponse,
    CompartmentView,
    DriverSummary,
    HistoryItem,
    HistoryPage,
    HistoryQuery,
    Lane,
    LanePublish,
    LaneView,
    MoveStopsCommand,
    PublishResult,
    ReapplyCommand,
    RevertCommand,
    Snapshot,
    SnapshotQuery,
    SuggestedDriver,
    TrayOrder,
    Trays,
    TrayTruck,
    ValidateBody,
    WarningAck,
    draft_doc_id,
    invalid,
    is_in_recovery,
)
from fuel.services.dispatch_validation import (
    DispatchValidationService,
    TTLCache,
    ValidationContext,
    lane_checks,
    parse_compartment_source,
    worst_outcome,
)
from fuel.services.driver_daily_reset import get_tenant_timezone
from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

logger = logging.getLogger(__name__)

#: Days a dispatcher may open (External input validation).
DAYS_BACK = 7
DAYS_AHEAD = 14
#: Order tray cap (K5).
TRAY_LIMIT = 1000
#: Tray statuses (K5).
TRAY_STATUSES = ("placed", "confirmed", "scheduled", "on_hold")
#: Checks older than this are recomputed by the snapshot read (K5).
STALE_CHECKS_S = 60.0
#: ``applied_commands`` entries older than this are pruned (K4.5).
APPLIED_TTL = timedelta(hours=24)
#: Replay branch: committed-log read retries (K4.2 step 11).
LOG_READ_RETRIES = 3
LOG_READ_DELAY_S = 0.1
#: Actor display-name cache (K17.1).
ACTOR_CACHE_TTL_S = 300.0
ACTOR_FALLBACK = "Another dispatcher"
PRIORITY_TIMEOUT_S = 2.0

_PRIORITIES_INDEX = "mvp_delivery_priorities"
_TRUCK_COMPARTMENTS_INDEX = "truck_compartments"


def _hits(resp: Any) -> List[Dict[str, Any]]:
    return [h.get("_source") or {} for h in ((resp or {}).get("hits") or {}).get("hits") or []]


def _total(resp: Any) -> int:
    total = ((resp or {}).get("hits") or {}).get("total")
    if isinstance(total, dict):
        return int(total.get("value") or 0)
    return int(total or 0)


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


async def _default_name_lookup(tenant_id: str, user_id: str) -> Optional[str]:
    """``SELECT email FROM auth_users WHERE st_user_id = :user_id AND tenant_id = :tenant_id`` (K17.1)."""
    from persistence.database import is_persistence_enabled, session_scope
    from sqlalchemy import text

    if not is_persistence_enabled():
        return None
    query = text("SELECT email FROM auth_users WHERE st_user_id = :user_id AND tenant_id = :tenant_id")
    async with session_scope() as db:
        row = (await db.execute(query, {"user_id": user_id, "tenant_id": tenant_id})).first()
    return row[0] if row is not None else None


class DispatchBoardService:
    """The board's read and command paths (K4.2, K5, K6)."""

    def __init__(
        self,
        *,
        es_service: Any,
        validation: DispatchValidationService,
        driver_repository: Any = None,
        ws_manager: Any = None,
        telemetry: Optional[tm.BoardTelemetry] = None,
        name_lookup: Optional[Callable[[str, str], Awaitable[Optional[str]]]] = None,
        suggestion_reader: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
        log_read_delay_s: float = LOG_READ_DELAY_S,
        new_id: Optional[Callable[[], str]] = None,
    ) -> None:
        self._es = es_service
        self.validation = validation
        self._drivers = driver_repository
        self._ws = ws_manager
        self.telemetry = telemetry or tm.BoardTelemetry()
        self._name_lookup = name_lookup or _default_name_lookup
        self._suggestions = suggestion_reader
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._log_delay = log_read_delay_s
        self._new_id = new_id
        self._actor_cache = TTLCache(ACTOR_CACHE_TTL_S)

    # -- small helpers -----------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    @staticmethod
    def timezone_for(tenant_id: str, settings: Any = None) -> str:
        """K2.5: the tenant zone (``America/Chicago`` until settings carry one)."""
        return get_tenant_timezone(tenant_id, settings)

    def check_service_date(self, service_date: date, tz: str) -> Tuple[date, bool]:
        """422 ``date_out_of_range`` outside [today - 7, today + 14]; returns (today, past)."""
        today = eta.today_in(tz, self.now())
        if service_date < today - timedelta(days=DAYS_BACK) or service_date > today + timedelta(days=DAYS_AHEAD):
            raise invalid("date_out_of_range", fields=["service_date"])
        return today, service_date < today

    async def resolve_actor_name(self, tenant_id: str, user_id: Optional[str]) -> str:
        """K17.1: the email local part, trimmed to 64; ``"Another dispatcher"`` otherwise."""
        if not user_id:
            return ACTOR_FALLBACK
        key = ("actor", tenant_id, user_id)
        hit, value = self._actor_cache.get(key)
        if hit:
            return value
        name = ACTOR_FALLBACK
        try:
            email = await self._name_lookup(tenant_id, user_id)
            if email and "@" in email:
                local = email.split("@", 1)[0].strip()[:64]
                name = local or ACTOR_FALLBACK
        except Exception as exc:
            logger.warning("dispatch board actor name lookup failed: %s", type(exc).__name__)
            return ACTOR_FALLBACK
        self._actor_cache.set(key, name)
        return name

    async def load_draft(self, tenant_id: str, service_date: date, tz: str) -> BoardDraft:
        doc = await self._es.get_document(DISPATCH_BOARD_DRAFTS_INDEX, draft_doc_id(tenant_id, service_date))
        if not doc or doc.get("tenant_id") != tenant_id:
            return BoardDraft(tenant_id=tenant_id, service_date=service_date, timezone=tz)
        return BoardDraft.model_validate(doc)

    def _lane_state(self, lane: Lane) -> str:
        publish = lane.publish
        if publish.attempt is not None and (publish.state == "failed" or is_in_recovery(lane, self.now())):
            return "recovering"
        if publish.state == "publishing":
            if publish.lease_until is not None and publish.lease_until < self.now():
                return "failed"
            return "publishing"
        if publish.state == "failed":
            return "failed"
        if publish.state == "published":
            return "modified" if engine.content_hash(lane) != publish.published_hash else "published"
        return "draft"

    def _presented_publish(self, lane: Lane) -> LanePublish:
        """K7.5: a ``publishing`` lane whose lease ran out reads as an interrupted failure."""
        publish = lane.publish
        if publish.state == "publishing" and publish.lease_until is not None and publish.lease_until < self.now():
            return publish.model_copy(
                update={
                    "last_result": PublishResult(
                        state="failed", reason="interrupted", writes_made="unknown", retryable=True,
                        publish_id=publish.attempt_id, at=publish.lease_until,
                    )
                }
            )
        return publish

    def lane_view(self, lane: Lane, ctx: Optional[ValidationContext] = None, *, suggested: Optional[SuggestedDriver] = None) -> LaneView:
        state = self._lane_state(lane)
        driver = None
        compartments: List[CompartmentView] = []
        if ctx is not None:
            if lane.driver_id and lane.driver_id in ctx.drivers:
                driver = self._driver_summary(ctx.drivers[lane.driver_id], ctx, paired_truck_id=lane.truck_id)
            states = ctx.compartment_states.get(lane.truck_id, {})
            for comp in ctx.compartments.get(lane.truck_id, []):
                st = states.get(comp.compartment_id, {})
                compartments.append(
                    CompartmentView(
                        compartment_id=comp.compartment_id,
                        position_index=comp.position_index,
                        capacity_l=comp.capacity_liters,
                        accepts_products=list(comp.allowed_product_codes or [g.value for g in comp.allowed_grades]),
                        state=st.get("state"),
                        last_loaded_product=st.get("last_loaded_product"),
                    )
                )
        return LaneView(
            truck_id=lane.truck_id,
            version=lane.version,
            driver_id=lane.driver_id,
            driver=driver,
            suggested_driver=suggested,
            compartments=compartments,
            loads=lane.loads,
            shelf=lane.shelf,
            checks=lane.checks,
            checks_computed_at=lane.checks_computed_at,
            checks_stale=lane.checks_stale,
            outcome=worst_outcome(lane.checks),
            publish=self._presented_publish(lane),
            state=state,
            modified=state == "modified",
            ever_published=lane.publish.published_version is not None or bool(lane.publish.plans),
        )

    def _driver_summary(self, doc: Dict[str, Any], ctx: Optional[ValidationContext], *, paired_truck_id: Optional[str] = None) -> DriverSummary:
        driver_id = doc.get("driver_id")
        qual = (ctx.qualification.get(driver_id) if ctx else None) or None
        hos = None
        if ctx is not None and ctx.is_today and driver_id in ctx.hos_advisories:
            adv = ctx.hos_advisories[driver_id] or {}
            hos = {
                "freshness_state": adv.get("freshness_state"),
                "remaining_drive_time": adv.get("remaining_drive_time"),
                "remaining_on_duty_window": adv.get("remaining_on_duty_window"),
                "cycle_hours": adv.get("cycle_hours"),
            }
        return DriverSummary(
            driver_id=driver_id,
            name=doc.get("driver_name"),
            status=doc.get("status"),
            assigned_truck_id=doc.get("assigned_truck_id"),
            cdl_class=doc.get("cdl_class"),
            hazmat_endorsement=doc.get("hazmat_endorsement"),
            eligible=(qual or {}).get("eligible") if qual else None,
            ineligible_reasons=list((qual or {}).get("reasons") or []) if qual else [],
            hos=hos,
            paired_truck_id=paired_truck_id,
        )

    async def _context_for(
        self,
        draft: BoardDraft,
        truck_ids: Iterable[str],
        *,
        extra_drivers: Iterable[str] = (),
        extra_orders: Iterable[str] = (),
        extra_terminals: Iterable[str] = (),
        fresh: bool = False,
        include_other_day: bool = True,
        suggestions: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> ValidationContext:
        trucks = list(dict.fromkeys(t for t in truck_ids if t))
        drivers: List[str] = list(extra_drivers)
        orders: List[str] = list(extra_orders)
        plans: List[str] = []
        terminals: List[str] = list(extra_terminals)
        for truck_id in trucks:
            lane = draft.lanes.get(truck_id)
            if lane is None:
                continue
            if lane.driver_id:
                drivers.append(lane.driver_id)
            if lane.publish.published_content and lane.publish.published_content.driver_id:
                drivers.append(lane.publish.published_content.driver_id)
            orders.extend(engine.lane_order_ids(lane))
            if lane.publish.published_content is not None:
                orders.extend(engine.lane_order_ids(Lane(truck_id=truck_id, version=0, **lane.publish.published_content.model_dump())))
            plans.extend(p.plan_id for p in lane.publish.plans.values())
            terminals.extend(l.terminal_id for l in lane.loads if l.terminal_id)
        ctx = await self.validation.build_context(
            draft.tenant_id,
            draft.service_date,
            truck_ids=trucks,
            driver_ids=drivers,
            order_ids=orders,
            plan_ids=plans,
            terminal_ids=terminals,
            fresh=fresh,
            timezone_name=draft.timezone,
            suggestions=suggestions,
            include_other_day=include_other_day,
        )
        if self._new_id is not None:
            ctx.new_id = self._new_id
        return ctx

    async def _broadcast(self, tenant_id: str, service_date: date, event_type: str, data: Dict[str, Any]) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.broadcast_board_event(tenant_id, service_date.isoformat(), event_type, data)
        except Exception as exc:
            logger.warning("dispatch board broadcast failed: %s", type(exc).__name__)

    # -- snapshot (K5) -----------------------------------------------------

    async def snapshot(
        self,
        tenant_id: str,
        service_date: date,
        *,
        mode: str,
        tz: str,
        lanes: Optional[Sequence[str]] = None,
        filters: Optional[SnapshotQuery] = None,
    ) -> Dict[str, Any]:
        started = _time.monotonic()
        today, past = self.check_service_date(service_date, tz)
        draft = await self.load_draft(tenant_id, service_date, tz)
        tz = draft.timezone
        lane_ids = [t for t in (lanes if lanes is not None else list(draft.lanes)) if t in draft.lanes]
        ctx = await self._context_for(draft, lane_ids)
        degraded: Set[str] = set(ctx.degraded_sources())

        # Stale re-validation with write-back only when the lane version is unchanged (K5).
        # Shadow recomputes for the response but saves nothing (review P1-3).
        if not past:
            await self._refresh_stale(draft, lane_ids, ctx, write_back=mode != "shadow")

        suggested: Dict[str, Optional[SuggestedDriver]] = {}
        trays = Trays()
        if lanes is None:
            trays, tray_degraded = await self._trays(draft, ctx, filters or SnapshotQuery(), tz, today)
            degraded |= tray_degraded
        # K9: suggestions on full reads of today and later (hidden on past days).
        suggestion_list: List[Dict[str, Any]] = []
        if lanes is None and not past and self._suggestions is not None:
            try:
                suggestion_list = await self._suggestions.list_for_day(draft)
            except Exception as exc:
                degraded.add("suggestions")
                logger.warning("dispatch board suggestion read failed: %s", type(exc).__name__)
        unpaired = [t for t in lane_ids if not draft.lanes[t].driver_id]
        suggested = await self._suggested_drivers(draft, unpaired)
        views = [self.lane_view(draft.lanes[t], ctx, suggested=suggested.get(t)) for t in lane_ids]
        snap = Snapshot(
            service_date=service_date,
            timezone=tz,
            mode=mode,  # type: ignore[arg-type]
            draft_version=draft.draft_version,
            read_only=mode == "shadow" or past,
            read_only_reason="past_service_day" if past else ("shadow" if mode == "shadow" else None),
            degraded_sources=sorted(degraded),
            lanes=views,
            trays=trays,
            suggestions=suggestion_list,
            acknowledged=draft.acknowledged,
        )
        self.telemetry.metric(tm.SNAPSHOT_MS, (_time.monotonic() - started) * 1000, tenant_id=tenant_id, endpoint="snapshot", outcome="ok")
        return snap.model_dump(mode="json")

    async def _refresh_stale(
        self, draft: BoardDraft, lane_ids: Sequence[str], ctx: ValidationContext, *, write_back: bool = True
    ) -> None:
        now = self.now()
        refreshed: Dict[str, Tuple[int, List[Check]]] = {}
        for truck_id in lane_ids:
            lane = draft.lanes[truck_id]
            age = (now - lane.checks_computed_at).total_seconds() if lane.checks_computed_at else None
            if not lane.checks_stale and age is not None and age <= STALE_CHECKS_S:
                continue
            checks = lane_checks(ctx, lane, draft=draft)
            lane.checks = checks
            lane.checks_computed_at = now
            lane.checks_stale = False
            refreshed[truck_id] = (lane.version, checks)
        if not write_back or not refreshed or draft.created_at is None and draft.draft_version == 0:
            return

        def transform(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            changed = False
            for truck_id, (version, checks) in refreshed.items():
                lane = (current.get("lanes") or {}).get(truck_id)
                if lane is None or lane.get("version") != version:
                    continue
                lane["checks"] = [c.model_dump(mode="json") for c in checks]
                lane["checks_computed_at"] = now.isoformat()
                lane["checks_stale"] = False
                changed = True
            return current if changed else None

        try:
            await self._es.atomic_update(DISPATCH_BOARD_DRAFTS_INDEX, draft_doc_id(draft.tenant_id, draft.service_date), transform)
        except Exception as exc:
            logger.debug("dispatch board check write-back skipped: %s", type(exc).__name__)

    async def _suggested_drivers(self, draft: BoardDraft, truck_ids: Sequence[str]) -> Dict[str, Optional[SuggestedDriver]]:
        """K5.2: exactly one active driver with ``assigned_truck_id`` == truck, not paired elsewhere."""
        if self._drivers is None or not truck_ids:
            return {}
        paired = {lane.driver_id for lane in draft.lanes.values() if lane.driver_id}

        async def one(truck_id: str) -> Tuple[str, Optional[SuggestedDriver]]:
            try:
                result = await self._drivers.search(draft.tenant_id, assigned_truck_id=truck_id, status="active", size=5)
            except Exception as exc:
                logger.warning("dispatch board suggested driver lookup failed: %s", type(exc).__name__)
                return truck_id, None
            drivers = [d.model_dump(mode="json") if hasattr(d, "model_dump") else d for d in (result or {}).get("drivers") or []]
            drivers = [d for d in drivers if d.get("tenant_id") == draft.tenant_id and d.get("status") == "active"]
            if len(drivers) != 1 or drivers[0].get("driver_id") in paired:
                return truck_id, None
            d = drivers[0]
            return truck_id, SuggestedDriver(driver_id=d["driver_id"], name=d.get("driver_name") or d["driver_id"])

        return dict(await asyncio.gather(*(one(t) for t in truck_ids)))

    def _tray_query(
        self, tenant_id: str, service_date: date, tz: str, filters: SnapshotQuery, exclude: Sequence[str] = ()
    ) -> Dict[str, Any]:
        """``exclude``: orders already on this draft. Leaving them out of the
        query keeps a full board from pushing unassigned orders past
        ``TRAY_LIMIT`` (N1: 1,500 stops a day)."""
        day_start, _ = eta.shift_window(service_date, "all", tz)
        day_end = eta.local_at(service_date + timedelta(days=1), eta.SHIFT_TIMES["all"][0], tz)
        start_iso = day_start.astimezone(timezone.utc).isoformat()
        end_iso = day_end.astimezone(timezone.utc).isoformat()
        filt: List[Dict[str, Any]] = [
            {"term": {"tenant_id": tenant_id}},
            {"terms": {"status": list(TRAY_STATUSES)}},
        ]
        should = [
            {"bool": {"filter": [
                {"range": {"delivery_window_start": {"lt": end_iso}}},
                {"range": {"delivery_window_end": {"gt": start_iso}}},
            ]}},
            {"bool": {"must_not": [{"exists": {"field": "delivery_window_start"}}]}},
        ]
        if filters.call_type:
            filt.append({"term": {"call_type": filters.call_type}})
        if filters.product:
            filt.append({"term": {"product_code": filters.product}})
        if filters.window == "overdue":
            filt.append({"range": {"delivery_window_end": {"lt": self.now().astimezone(timezone.utc).isoformat()}}})
        elif filters.window == "today":
            filt.append({"range": {"delivery_window_start": {"gte": start_iso, "lt": end_iso}}})
        elif filters.window == "later":
            filt.append({"range": {"delivery_window_start": {"gte": end_iso}}})
        query: Dict[str, Any] = {"filter": filt, "should": should, "minimum_should_match": 1}
        if exclude:
            query["must_not"] = [{"terms": {"order_id": list(exclude)}}]
        return {
            "query": {"bool": query},
            "sort": [{"delivery_window_start": {"order": "asc"}}, {"order_id": {"order": "asc"}}],
            "size": TRAY_LIMIT + 1,
        }

    async def _priorities(self, tenant_id: str) -> Dict[str, Tuple[float, Optional[str]]]:
        """Newest ``mvp_delivery_priorities`` doc of the last 24 h, joined by order id (K5)."""
        since = (self.now() - timedelta(hours=24)).astimezone(timezone.utc).isoformat()
        query = {
            "query": {"bool": {"filter": [{"term": {"tenant_id": tenant_id}}, {"range": {"created_at": {"gte": since}}}]}},
            "sort": [{"created_at": {"order": "desc"}}],
            "size": 1,
            "_source": ["priorities", "created_at", "tenant_id"],
        }
        resp = await asyncio.wait_for(self._es.search_documents(_PRIORITIES_INDEX, query, 1), timeout=PRIORITY_TIMEOUT_S)
        hits = _hits(resp)
        if not hits:
            return {}
        doc = hits[0]
        created = _parse_dt(doc.get("created_at"))
        if created is None or created < self.now() - timedelta(hours=24):
            return {}
        out: Dict[str, Tuple[float, Optional[str]]] = {}
        for entry in doc.get("priorities") or []:
            order_id = entry.get("order_id")
            score = entry.get("priority_score")
            if not order_id or score is None:
                continue
            try:
                score = float(score)
            except (TypeError, ValueError):
                continue
            if order_id not in out or score > out[order_id][0]:
                out[order_id] = (score, entry.get("priority_bucket"))
        return out

    async def _trays(
        self, draft: BoardDraft, ctx: ValidationContext, filters: SnapshotQuery, tz: str, today: date
    ) -> Tuple[Trays, Set[str]]:
        degraded: Set[str] = set()
        tenant_id = draft.tenant_id
        resp = await self._es.search_documents(
            FUEL_ORDERS_CURRENT_INDEX, self._tray_query(tenant_id, draft.service_date, tz, filters, sorted(draft.order_index)), TRAY_LIMIT + 1
        )
        raw = [o for o in _hits(resp) if o.get("tenant_id") == tenant_id]
        truncated = len(raw) > TRAY_LIMIT or _total(resp) > TRAY_LIMIT
        raw = raw[:TRAY_LIMIT]
        runs = engine.published_run_ids(draft)
        candidates = [
            o for o in raw
            if o.get("order_id") not in draft.order_index
            and (not o.get("assigned_run_id") or o.get("assigned_run_id") in runs)
        ]
        # K5.1: drop orders already on another day's draft.
        other_day: Dict[str, date] = {}
        ids = [o["order_id"] for o in candidates if o.get("order_id")]
        if ids:
            probe = ValidationContext(
                tenant_id=tenant_id, service_date=draft.service_date, timezone=tz, now=self.now(), today=today
            )
            try:
                await self.validation._fetch_other_day(probe, ids)  # noqa: SLF001 - shared K5.1 lookup
                other_day = probe.other_day
            except Exception as exc:
                degraded.add("other_day")
                logger.warning("dispatch board cross-day lookup failed: %s", type(exc).__name__)
        candidates = [o for o in candidates if o.get("order_id") not in other_day]
        priorities: Dict[str, Tuple[float, Optional[str]]] = {}
        try:
            priorities = await self._priorities(tenant_id)
        except Exception as exc:
            degraded.add("delivery_priorities")
            logger.warning("dispatch board priority read failed: %s", type(exc).__name__)
        orders: List[TrayOrder] = []
        for o in candidates:
            score, bucket = priorities.get(o["order_id"], (None, None))
            on_hold = o.get("status") == "on_hold"
            product = o.get("product_code") or ""
            orders.append(
                TrayOrder(
                    order_id=o["order_id"],
                    customer_id=o.get("customer_id"),
                    customer_name=o.get("customer_name"),
                    product_code=o.get("product_code"),
                    gallons_requested=o.get("gallons_requested"),
                    fill_to_full=bool(o.get("fill_to_full") or False),
                    delivery_window_start=_parse_dt(o.get("delivery_window_start")),
                    delivery_window_end=_parse_dt(o.get("delivery_window_end")),
                    call_type=o.get("call_type"),
                    status=o.get("status"),
                    priority_score=score,
                    priority_bucket=bucket,
                    draggable=not on_hold,
                    block_reason="on_hold" if on_hold else None,
                    missing_window=not (o.get("delivery_window_start") and o.get("delivery_window_end")),
                    dyed=str(product).upper() in {"OFF_ROAD_DIESEL", "DYED_DIESEL", "DYED_ULSD", "OFF_ROAD_ULSD"},
                )
            )
        far = datetime.max.replace(tzinfo=timezone.utc)
        orders.sort(
            key=lambda t: (
                t.priority_score is None,
                -(t.priority_score or 0.0),
                t.delivery_window_start is None,
                t.delivery_window_start or far,
                t.order_id,
            )
        )
        drivers, trucks = await asyncio.gather(self._driver_tray(draft), self._truck_tray(draft))
        return Trays(orders=orders, orders_truncated=truncated, drivers=drivers, trucks=trucks), degraded

    async def _driver_tray(self, draft: BoardDraft) -> List[DriverSummary]:
        if self._drivers is None:
            return []
        try:
            result = await self._drivers.search(draft.tenant_id, size=500)
        except Exception as exc:
            logger.warning("dispatch board driver tray failed: %s", type(exc).__name__)
            return []
        paired = {lane.driver_id: t for t, lane in draft.lanes.items() if lane.driver_id}
        out = []
        for d in (result or {}).get("drivers") or []:
            doc = d.model_dump(mode="json") if hasattr(d, "model_dump") else d
            if doc.get("tenant_id") != draft.tenant_id:
                continue
            out.append(self._driver_summary(doc, None, paired_truck_id=paired.get(doc.get("driver_id"))))
        return out

    async def _truck_tray(self, draft: BoardDraft) -> List[TrayTruck]:
        query = {"query": {"bool": {"filter": [{"term": {"tenant_id": draft.tenant_id}}]}}, "size": 1000}
        try:
            resp = await self._es.search_documents(_TRUCK_COMPARTMENTS_INDEX, query, 1000)
        except Exception as exc:
            logger.warning("dispatch board truck tray failed: %s", type(exc).__name__)
            return []
        grouped: Dict[str, List[Any]] = {}
        for source in _hits(resp):
            if source.get("tenant_id") not in (None, draft.tenant_id):
                continue
            comp, _state = parse_compartment_source(source, draft.tenant_id)
            if comp is not None:
                grouped.setdefault(comp.truck_id, []).append(comp)
        return [
            TrayTruck(truck_id=t, compartment_count=len(c), capacity_l=sum(x.capacity_liters for x in c))
            for t, c in sorted(grouped.items())
            if t not in draft.lanes
        ]

    # -- validate (K3.5) ---------------------------------------------------

    async def validate(self, tenant_id: str, service_date: date, body: ValidateBody, *, tz: str) -> Dict[str, Any]:
        started = _time.monotonic()
        self.check_service_date(service_date, tz)
        draft = await self.load_draft(tenant_id, service_date, tz)
        item = body.item
        extra_orders = item.ids if item.kind in ("order", "stop") else []
        extra_drivers = item.ids if item.kind == "driver" else []
        trucks = list(body.candidates)
        for order_id in extra_orders:
            if order_id in draft.order_index:
                trucks.append(draft.order_index[order_id])
        if item.kind == "load":
            for truck_id, lane in draft.lanes.items():
                if any(l.load_id == item.ids[0] for l in lane.loads):
                    trucks.append(truck_id)
        if item.kind == "driver":
            trucks.extend(t for t, lane in draft.lanes.items() if lane.driver_id in extra_drivers)
        ctx = await self._context_for(draft, trucks, extra_drivers=extra_drivers, extra_orders=extra_orders)
        results = await self.validation.validate_candidates(ctx, draft, item, body.candidates, body.position)
        kind = "position" if body.position is not None else "batch"
        self.telemetry.metric(tm.VALIDATE_MS, (_time.monotonic() - started) * 1000, tenant_id=tenant_id, endpoint="validate", kind=kind, outcome="ok")
        return {
            "results": {t: r.model_dump(mode="json") for t, r in results.items()},
            "degraded_sources": ctx.degraded_sources(),
        }

    # -- commands (K4.2) ---------------------------------------------------

    async def handle_command(
        self,
        *,
        tenant_id: str,
        user_id: str,
        service_date: date,
        command: Any,
        tz: str,
    ) -> Dict[str, Any]:
        """K4.2 steps 3-12. The route already ran steps 1-2 (guards, strict mode)."""
        started = _time.monotonic()
        ctype = command.type
        modality = command.input_modality
        try:
            result = await self._handle(tenant_id, user_id, service_date, command, tz)
        except AppException as exc:
            outcome = {
                ErrorCode.BOARD_LANE_CONFLICT: "conflict",
                ErrorCode.BOARD_COMMAND_BLOCKED: "blocked",
            }.get(exc.error_code, "error")
            if outcome == "conflict":
                self.telemetry.metric(tm.CONFLICT_COUNT, 1, tenant_id=tenant_id, type=ctype)
            if ctype in ("revert", "reapply"):
                self.telemetry.metric(tm.UNDO_COUNT, 1, tenant_id=tenant_id, type=ctype, result=outcome)
            self.telemetry.metric(tm.COMMAND_COUNT, 1, tenant_id=tenant_id, type=ctype, result=outcome, input_modality=modality)
            self.telemetry.metric(tm.COMMAND_MS, (_time.monotonic() - started) * 1000, tenant_id=tenant_id, type=ctype, result=outcome)
            raise
        if ctype in ("revert", "reapply"):
            self.telemetry.metric(tm.UNDO_COUNT, 1, tenant_id=tenant_id, type=ctype, result="committed")
        if isinstance(command, AcceptSuggestionCommand) and not result.get("already_applied"):
            action = "partial" if command.load_ids is not None else "accept"
            self.telemetry.metric(tm.SUGGESTION_COUNT, 1, tenant_id=tenant_id, action=action)
        self.telemetry.metric(tm.COMMAND_COUNT, 1, tenant_id=tenant_id, type=ctype, result="committed", input_modality=modality)
        self.telemetry.metric(tm.COMMAND_MS, (_time.monotonic() - started) * 1000, tenant_id=tenant_id, type=ctype, result="committed")
        return result

    async def _handle(
        self, tenant_id: str, user_id: str, service_date: date, command: Any, tz: str, *, scoped: bool = True
    ) -> Dict[str, Any]:
        _today, past = self.check_service_date(service_date, tz)
        if past:
            raise AppException(
                ErrorCode.DISPATCH_BOARD_READ_ONLY,
                "Past service days are read-only.",
                status_code=409,
                details={"reason": "past_service_day"},
            )
        payload = command.model_dump(mode="json")
        phash = engine.payload_hash(payload)
        cid = command.client_command_id
        log_id = f"{tenant_id}:{cid}"

        # Step 3: idempotency fast path.
        existing = await self._es.get_document(DISPATCH_BOARD_COMMANDS_INDEX, log_id)
        if existing and existing.get("tenant_id") == tenant_id:
            if existing.get("payload_hash") != phash:
                raise self._idempotency_conflict()
            logger.debug("dispatch board command replay: %s", cid)
            return existing.get("response") or {}

        # Step 4: read the draft; replay branch for a crash between commit and log.
        draft = await self.load_draft(tenant_id, service_date, tz)
        if cid in draft.applied_commands:
            return await self._replay(draft, command, phash, log_id)

        if isinstance(command, AcknowledgeWarningCommand):
            return await self._acknowledge(draft, command, user_id, phash, log_id, payload)

        # Step 5: version pre-check.
        self._version_precheck(draft, command)
        contents: Optional[Dict[str, Optional[Dict[str, Any]]]] = None
        target_log: Optional[Dict[str, Any]] = None
        if isinstance(command, (RevertCommand, ReapplyCommand)):
            target_log, contents = await self._undo_plan(draft, command, user_id)
        suggestions: Optional[Dict[str, Dict[str, Any]]] = None
        suggestion_orders: List[str] = []
        if isinstance(command, AcceptSuggestionCommand):
            suggestions = await self._suggestions_for(draft)
            touched, suggestion_orders = self._suggestion_scope(draft, command, suggestions)
        elif contents is not None:
            touched = list(contents)
        else:
            touched = self._touched_estimate(draft, command)

        # Step 6: publishing / recovery.
        self._publishing_check(draft, touched)

        # Step 7: the only I/O before the commit.
        ctx = await self._context_for(
            draft,
            touched,
            extra_drivers=[getattr(command, "driver_id", None) or ""],
            extra_orders=list(getattr(command, "order_ids", None) or []) + self._content_orders(contents) + suggestion_orders,
            extra_terminals=[getattr(command, "terminal_id", None) or ""],
            suggestions=suggestions,
        )
        if "orders" in ctx.unavailable:
            raise AppException(ErrorCode.ELASTICSEARCH_UNAVAILABLE, "The board is unavailable. Retry shortly.", status_code=503)

        # Step 8: engine.
        try:
            if contents is not None:
                applied = engine.restore_lanes(draft, contents, ctx)
            else:
                # K15: copy only the lanes the pre-checks expect to change.
                applied = engine.apply(draft, command, ctx, scope=touched if scoped else None)
        except engine.ScopeExceeded:
            # A lane outside the estimate changed and may be shared with
            # ``draft``: start over from a fresh read with a full copy.
            logger.info("dispatch board command left its lane estimate: type=%s", command.type)
            return await self._handle(tenant_id, user_id, service_date, command, tz, scoped=False)
        except engine.EngineError as exc:
            if contents is not None and exc.reason == "order_on_two_lanes":
                raise self._undo_stale("changed_by_other") from None
            logger.info("dispatch board command refused: type=%s reason=%s", command.type, exc.reason)
            raise invalid(exc.reason, fields=exc.fields or None) from None
        self._publishing_check(draft, applied.touched)

        # Step 9: validate every touched lane with the same context.
        checks: Dict[str, List[Check]] = {}
        for truck_id in applied.touched:
            lane = applied.draft.lanes.get(truck_id)
            if lane is not None:
                checks[truck_id] = lane_checks(ctx, lane, draft=applied.draft)
        blocked = {t: [c for c in cs if c.outcome == "block"] for t, cs in checks.items()}
        if any(blocked.values()) and self._move_back_adds_no_block(draft, applied, command, ctx, blocked):
            blocked = {}
        if any(blocked.values()):
            await self._log_refused(tenant_id, service_date, command, payload, phash, user_id, "blocked", draft, applied.touched, checks)
            first = next(c for cs in blocked.values() for c in cs)
            raise AppException(
                ErrorCode.BOARD_COMMAND_BLOCKED,
                "This change is blocked.",
                status_code=422,
                details={
                    "reason": first.reason_code,
                    "checks": {t: [c.model_dump(mode="json") for c in cs] for t, cs in checks.items()},
                },
            )

        # Steps 10-12.
        return await self._commit(
            draft, applied, command, payload, phash, user_id, checks, log_id, ctx, target_log=target_log
        )

    @staticmethod
    def _order_location(draft: BoardDraft, order_id: str) -> Optional[Tuple[str, Optional[str]]]:
        truck_id = draft.order_index.get(order_id)
        lane = draft.lanes.get(truck_id) if truck_id else None
        if lane is None:
            return None
        for load in lane.loads:
            if any(s.order_id == order_id for s in load.stops):
                return (truck_id, load.load_id)
        return (truck_id, None)

    def _move_back_adds_no_block(
        self,
        draft: BoardDraft,
        applied: Any,
        command: Any,
        ctx: ValidationContext,
        blocked: Dict[str, List[Check]],
    ) -> bool:
        """Freeze rule 11 (e) narrowing (Phase 1 review P1-2).

        A ``move_stops`` whose every order is pinned, was away from the load its
        ``assigned_run_id`` names, and now sits on that load commits as long as
        it adds no block the touched lanes didn't already have. Blocks that were
        there before (a lapsed driver qualification, an unavailable source)
        don't keep a started order from going home.
        """
        if not isinstance(command, MoveStopsCommand) or not command.order_ids:
            return False
        runs = engine.published_run_ids(applied.draft)
        for order_id in command.order_ids:
            order = ctx.orders.get(order_id)
            if not engine.is_pinned(order):
                return False
            home = runs.get((order or {}).get("assigned_run_id") or "")
            if home is None or self._order_location(draft, order_id) == home:
                return False
            if self._order_location(applied.draft, order_id) != home:
                return False

        def key(truck_id: str, c: Check) -> Tuple[str, str, str, str]:
            return (truck_id, c.check, c.reason_code, c.warning_id or "")

        before: Set[Tuple[str, str, str, str]] = set()
        for truck_id in applied.touched:
            lane = draft.lanes.get(truck_id)
            if lane is not None:
                before |= {key(truck_id, c) for c in lane_checks(ctx, lane, draft=draft) if c.outcome == "block"}
        after = {key(t, c) for t, cs in blocked.items() for c in cs}
        return after <= before

    def _content_orders(self, contents: Optional[Dict[str, Optional[Dict[str, Any]]]]) -> List[str]:
        out: List[str] = []
        for content in (contents or {}).values():
            if content:
                for load in content.get("loads") or []:
                    out.extend(s.get("order_id") for s in load.get("stops") or [])
                out.extend(content.get("shelf") or [])
        return out

    def _touched_estimate(self, draft: BoardDraft, command: Any) -> List[str]:
        touched = engine.touched_lanes_for(draft, command)
        driver_id = getattr(command, "driver_id", None)
        if driver_id:
            touched += [t for t, lane in draft.lanes.items() if lane.driver_id == driver_id and t not in touched]
        return touched

    def set_suggestion_reader(self, reader: Any) -> None:
        """Attach the K9 suggestion service (built after this service in bootstrap)."""
        self._suggestions = reader

    async def _suggestions_for(self, draft: BoardDraft) -> Dict[str, Dict[str, Any]]:
        """K9 suggestions for ``accept_suggestion`` (gated and minus dismissals)."""
        if self._suggestions is None:
            return {}
        try:
            return await self._suggestions.for_context(
                draft.tenant_id, draft.service_date, tz=draft.timezone, dismissed=draft.dismissed_suggestions
            )
        except Exception as exc:
            logger.warning("dispatch board suggestion read failed: %s", type(exc).__name__)
            raise AppException(ErrorCode.ELASTICSEARCH_UNAVAILABLE, "Suggestions are unavailable. Retry shortly.", status_code=503) from None

    @staticmethod
    def _suggestion_scope(
        draft: BoardDraft, command: AcceptSuggestionCommand, suggestions: Dict[str, Dict[str, Any]]
    ) -> Tuple[List[str], List[str]]:
        """Lanes and orders an ``accept_suggestion`` touches: its trucks and the lanes now holding its orders."""
        suggestion = suggestions.get(command.suggestion_id) or {}
        loads = list(suggestion.get("loads") or [])
        if command.load_ids is not None:
            wanted = set(command.load_ids)
            loads = [l for l in loads if l.get("load_key") in wanted]
        touched: List[str] = []
        orders: List[str] = []
        for spec in loads:
            truck_id = spec.get("truck_id")
            if truck_id and truck_id not in touched:
                touched.append(truck_id)
            for order_id in spec.get("order_ids") or []:
                orders.append(order_id)
                holder = draft.order_index.get(order_id)
                if holder and holder not in touched:
                    touched.append(holder)
        return touched, orders

    def _version_precheck(self, draft: BoardDraft, command: Any) -> None:
        stale = [
            t for t, v in command.expected_lane_versions.items()
            if (draft.lanes[t].version if t in draft.lanes else 0) != v
        ]
        if stale:
            raise self._conflict(draft, stale, "version_changed")

    def _publishing_check(self, draft: BoardDraft, touched: Iterable[str]) -> None:
        now = self.now()
        for truck_id in touched:
            lane = draft.lanes.get(truck_id)
            if lane is None:
                continue
            if is_in_recovery(lane, now) or (lane.publish.attempt is not None and lane.publish.state == "failed"):
                raise AppException(
                    ErrorCode.BOARD_PUBLISH_IN_PROGRESS,
                    "This truck is being published. Try again when it finishes.",
                    status_code=409,
                    details={"reason": "recovery_pending", "truck_id": truck_id},
                )
            if lane.publish.state == "publishing":
                raise AppException(
                    ErrorCode.BOARD_PUBLISH_IN_PROGRESS,
                    "This truck is being published. Try again when it finishes.",
                    status_code=409,
                    details={"reason": "publishing", "truck_id": truck_id},
                )

    def _conflict(self, draft: BoardDraft, truck_ids: Iterable[str], reason: str) -> AppException:
        lanes = [self.lane_view(draft.lanes[t]).model_dump(mode="json") for t in truck_ids if t in draft.lanes]
        logger.info("dispatch board lane conflict: reason=%s", reason)
        return AppException(
            ErrorCode.BOARD_LANE_CONFLICT,
            "Another change reached this truck first.",
            status_code=409,
            details={"reason": reason, "lanes": lanes, "missing_lanes": [t for t in truck_ids if t not in draft.lanes]},
        )

    @staticmethod
    def _idempotency_conflict() -> AppException:
        return AppException(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "This command id was already used for a different change.",
            status_code=409,
            details={"reason": "payload_mismatch"},
        )

    @staticmethod
    def _undo_stale(reason: str) -> AppException:
        return AppException(
            ErrorCode.BOARD_UNDO_STALE,
            "This change can't be undone any more.",
            status_code=409,
            details={"reason": reason},
        )

    def _prune_applied(self, applied: Dict[str, AppliedCommand]) -> Dict[str, AppliedCommand]:
        cutoff = self.now() - APPLIED_TTL
        kept = [(k, v) for k, v in applied.items() if v.at >= cutoff]
        kept.sort(key=lambda kv: kv[1].at)
        return dict(kept[-APPLIED_COMMANDS_LIMIT:])

    async def _commit(
        self,
        draft: BoardDraft,
        applied: engine.ApplyResult,
        command: Any,
        payload: Dict[str, Any],
        phash: str,
        user_id: str,
        checks: Dict[str, List[Check]],
        log_id: str,
        ctx: ValidationContext,
        *,
        target_log: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        tenant_id, service_date = draft.tenant_id, draft.service_date
        cid = command.client_command_id
        now = self.now()
        touched = applied.touched
        read_versions = {t: (draft.lanes[t].version if t in draft.lanes else 0) for t in touched}
        verdict: Dict[str, str] = {}

        def transform(current_doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            current = BoardDraft.model_validate(current_doc)
            if cid in current.applied_commands:
                verdict["reason"] = "already_applied"
                return None
            for truck_id in touched:
                stored = current.lanes.get(truck_id)
                if (stored.version if stored else 0) != read_versions[truck_id]:
                    verdict["reason"] = "version_changed"
                    return None
                if stored is not None and (stored.publish.state == "publishing" or is_in_recovery(stored, now) or (stored.publish.attempt is not None and stored.publish.state == "failed")):
                    verdict["reason"] = "publishing"
                    return None
            for truck_id in touched:
                new_lane = applied.draft.lanes.get(truck_id)
                if new_lane is None:
                    current.lanes.pop(truck_id, None)
                    continue
                lane = new_lane.model_copy(deep=True)
                lane.version = read_versions[truck_id] + 1
                lane.checks = checks.get(truck_id, [])
                lane.checks_computed_at = now
                lane.checks_stale = False
                stored = current.lanes.get(truck_id)
                if stored is not None:
                    lane.publish = stored.publish
                current.lanes[truck_id] = lane
            try:
                engine.rebuild_index(current)
            except engine.EngineError:
                verdict["reason"] = "cross_lane_rule"
                return None
            if engine.cross_lane_violation(current):
                verdict["reason"] = "cross_lane_rule"
                return None
            current.draft_version += 1
            current.applied_commands[cid] = AppliedCommand(draft_version=current.draft_version, payload_hash=phash, at=now)
            current.applied_commands = self._prune_applied(current.applied_commands)
            if current.created_at is None:
                current.created_at = now
            current.updated_at = now
            return current.model_dump(mode="json")

        doc_id = draft_doc_id(tenant_id, service_date)
        try:
            if draft.created_at is None and draft.draft_version == 0:
                # First command of the day: create the empty draft with
                # insert-if-absent, so concurrent first commands never race on
                # an upsert insert; the CAS below then runs on the stored row.
                base = BoardDraft(
                    tenant_id=tenant_id, service_date=service_date, timezone=draft.timezone, created_at=now, updated_at=now
                ).model_dump(mode="json")
                await self._es.create_document(DISPATCH_BOARD_DRAFTS_INDEX, doc_id, base)
            stored_doc, ok = await self._es.atomic_update(DISPATCH_BOARD_DRAFTS_INDEX, doc_id, transform)
        except AppException:
            raise
        except Exception as exc:
            logger.error("dispatch board draft commit failed: %s", type(exc).__name__)
            raise AppException(ErrorCode.ELASTICSEARCH_UNAVAILABLE, "The board is unavailable. Retry shortly.", status_code=503) from None
        if stored_doc is None and not ok:
            # The draft vanished between read and commit: treat as moved.
            raise self._conflict(draft, touched, "version_changed")
        stored = BoardDraft.model_validate(stored_doc)
        if not ok:
            reason = verdict.get("reason", "version_changed")
            if reason == "already_applied":
                return await self._replay(stored, command, phash, log_id)
            await self._log_refused(tenant_id, service_date, command, payload, phash, user_id, "conflict", stored, touched, checks)
            raise self._conflict(stored, touched, reason)

        views = [self.lane_view(stored.lanes[t], ctx) for t in touched if t in stored.lanes]
        response = CommandResponse(
            draft_version=stored.draft_version,
            lanes=views,
            checks={t: checks.get(t, []) for t in touched if t in stored.lanes},
        ).model_dump(mode="json")

        lanes_meta = [
            {
                "truck_id": t,
                "version_before": read_versions[t],
                "version_after": stored.lanes[t].version if t in stored.lanes else None,
                "hash_before": engine.content_hash(draft.lanes[t]) if t in draft.lanes else None,
                "hash_after": engine.content_hash(stored.lanes[t]) if t in stored.lanes else None,
            }
            for t in touched
        ]
        log_doc = await self._log_doc(
            draft, command, payload, phash, user_id, "committed", lanes_meta, checks,
            content_before={t: engine.lane_content_dump(draft.lanes.get(t)) for t in touched},
            content_after={t: engine.lane_content_dump(stored.lanes.get(t)) for t in touched},
            response=response,
        )
        if target_log is not None:
            log_doc["target_command_id"] = target_log.get("command_id")
        try:
            created = await self._es.create_document(DISPATCH_BOARD_COMMANDS_INDEX, log_id, log_doc)
        except Exception as exc:
            logger.error("dispatch board command log write failed after commit: %s", type(exc).__name__)
            degraded = dict(response)
            degraded["audit_degraded"] = True
            await self._after_commit(stored, command, user_id, views)
            return degraded
        if not created:
            existing = await self._es.get_document(DISPATCH_BOARD_COMMANDS_INDEX, log_id)
            if existing and existing.get("response"):
                return existing["response"]
        await self._after_commit(stored, command, user_id, views)
        return response

    async def _after_commit(self, stored: BoardDraft, command: Any, user_id: str, views: List[LaneView]) -> None:
        actor = await self.resolve_actor_name(stored.tenant_id, user_id)
        await self._broadcast(
            stored.tenant_id,
            stored.service_date,
            "board_lanes_updated",
            {
                "service_date": stored.service_date.isoformat(),
                "draft_version": stored.draft_version,
                "actor": {"user_id": user_id, "name": actor},
                "command_type": command.type,
                "lanes": [v.model_dump(mode="json") for v in views],
            },
        )

    async def _replay(self, draft: BoardDraft, command: Any, phash: str, log_id: str) -> Dict[str, Any]:
        """K4.2 step 11 replay branch."""
        entry = draft.applied_commands.get(command.client_command_id)
        if entry is not None and entry.payload_hash != phash:
            raise self._idempotency_conflict()
        for attempt in range(LOG_READ_RETRIES):
            existing = await self._es.get_document(DISPATCH_BOARD_COMMANDS_INDEX, log_id)
            if existing and existing.get("response"):
                return existing["response"]
            if attempt < LOG_READ_RETRIES - 1:
                await asyncio.sleep(self._log_delay)
        truck_ids = [t for t in command.expected_lane_versions if t in draft.lanes]
        truck_id = getattr(command, "truck_id", None)
        if truck_id and truck_id in draft.lanes and truck_id not in truck_ids:
            truck_ids.append(truck_id)
        return CommandResponse(
            draft_version=draft.draft_version,
            lanes=[self.lane_view(draft.lanes[t]) for t in truck_ids],
            checks={t: draft.lanes[t].checks for t in truck_ids},
            already_applied=True,
        ).model_dump(mode="json")

    async def _log_doc(
        self,
        draft: BoardDraft,
        command: Any,
        payload: Dict[str, Any],
        phash: str,
        user_id: str,
        result: str,
        lanes_meta: List[Dict[str, Any]],
        checks: Dict[str, List[Check]],
        *,
        content_before: Optional[Dict[str, Any]] = None,
        content_after: Optional[Dict[str, Any]] = None,
        response: Optional[Dict[str, Any]] = None,
        overrides: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        now = self.now().isoformat()
        doc: Dict[str, Any] = {
            "tenant_id": draft.tenant_id,
            "service_date": draft.service_date.isoformat(),
            "command_id": command.client_command_id,
            "type": command.type,
            "payload": payload,
            "payload_hash": phash,
            "actor_user_id": user_id,
            "actor_name": await self.resolve_actor_name(draft.tenant_id, user_id),
            "input_modality": command.input_modality,
            "result": result,
            "lanes": lanes_meta,
            "truck_ids": [m["truck_id"] for m in lanes_meta],
            "checks_summary": {
                t: {"outcome": worst_outcome(cs), "reason_codes": sorted({c.reason_code for c in cs if c.outcome in ("block", "warn")})}
                for t, cs in checks.items()
            },
            "overrides": overrides or [],
            "created_at": now,
            "updated_at": now,
        }
        if result == "committed":
            doc["response"] = response
            doc["content_before"] = content_before
            doc["content_after"] = content_after
        return doc

    async def _log_refused(
        self,
        tenant_id: str,
        service_date: date,
        command: Any,
        payload: Dict[str, Any],
        phash: str,
        user_id: str,
        result: str,
        draft: BoardDraft,
        touched: Sequence[str],
        checks: Dict[str, List[Check]],
    ) -> None:
        lanes_meta = [
            {"truck_id": t, "version_before": draft.lanes[t].version if t in draft.lanes else 0}
            for t in touched
        ]
        doc = await self._log_doc(draft, command, payload, phash, user_id, result, lanes_meta, checks)
        refused_id = f"{tenant_id}:{command.client_command_id}:refused:{uuid.uuid4().hex}"
        try:
            await self._es.create_document(DISPATCH_BOARD_COMMANDS_INDEX, refused_id, doc)
        except Exception as exc:
            logger.warning("dispatch board refused-command log failed: %s", type(exc).__name__)

    # -- acknowledgements (K4.4) -------------------------------------------

    async def _acknowledge(
        self,
        draft: BoardDraft,
        command: AcknowledgeWarningCommand,
        user_id: str,
        phash: str,
        log_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        lane = draft.lanes.get(command.truck_id)
        if lane is None:
            raise invalid("unknown_truck", fields=["truck_id"])
        warning = next((c for c in lane.checks if c.warning_id == command.warning_id and c.outcome == "warn"), None)
        if warning is None:
            raise invalid("warning_not_open", fields=["warning_id"])
        now = self.now()
        cid = command.client_command_id
        verdict: Dict[str, str] = {}

        def transform(current_doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            current = BoardDraft.model_validate(current_doc)
            if cid in current.applied_commands:
                verdict["reason"] = "already_applied"
                return None
            stored_lane = current.lanes.get(command.truck_id)
            if stored_lane is None or not any(c.warning_id == command.warning_id for c in stored_lane.checks):
                verdict["reason"] = "warning_not_open"
                return None
            current.acknowledged[command.warning_id] = WarningAck(
                reason=command.reason, actor_user_id=user_id, at=now, truck_id=command.truck_id
            )
            current.draft_version += 1
            current.applied_commands[cid] = AppliedCommand(draft_version=current.draft_version, payload_hash=phash, at=now)
            current.applied_commands = self._prune_applied(current.applied_commands)
            current.updated_at = now
            return current.model_dump(mode="json")

        stored_doc, ok = await self._es.atomic_update(
            DISPATCH_BOARD_DRAFTS_INDEX, draft_doc_id(draft.tenant_id, draft.service_date), transform
        )
        stored = BoardDraft.model_validate(stored_doc) if stored_doc else draft
        if not ok:
            if verdict.get("reason") == "already_applied":
                return await self._replay(stored, command, phash, log_id)
            raise invalid("warning_not_open", fields=["warning_id"])
        stored_lane = stored.lanes[command.truck_id]
        response = CommandResponse(
            draft_version=stored.draft_version,
            lanes=[self.lane_view(stored_lane)],
            checks={command.truck_id: stored_lane.checks},
        ).model_dump(mode="json")
        overrides = [{"warning_id": command.warning_id, "reason_code": warning.reason_code, "reason": command.reason}]
        log_doc = await self._log_doc(
            stored, command, payload, phash, user_id, "committed",
            [{"truck_id": command.truck_id, "version_before": stored_lane.version, "version_after": stored_lane.version,
              "hash_before": engine.content_hash(stored_lane), "hash_after": engine.content_hash(stored_lane)}],
            {command.truck_id: stored_lane.checks},
            content_before={}, content_after={}, response=response, overrides=overrides,
        )
        self.telemetry.metric(tm.OVERRIDE_COUNT, 1, tenant_id=draft.tenant_id, reason_code=warning.reason_code)
        try:
            await self._es.create_document(DISPATCH_BOARD_COMMANDS_INDEX, log_id, log_doc)
        except Exception as exc:
            logger.error("dispatch board command log write failed after commit: %s", type(exc).__name__)
            response = dict(response)
            response["audit_degraded"] = True
        return response

    # -- undo / redo (K6) --------------------------------------------------

    async def _undo_plan(
        self, draft: BoardDraft, command: Any, user_id: str
    ) -> Tuple[Dict[str, Any], Dict[str, Optional[Dict[str, Any]]]]:
        target = await self._es.get_document(DISPATCH_BOARD_COMMANDS_INDEX, f"{draft.tenant_id}:{command.target_command_id}")
        if not target or target.get("tenant_id") != draft.tenant_id or target.get("result") != "committed":
            raise self._undo_stale("not_found")
        if target.get("actor_user_id") != user_id:
            raise self._undo_stale("not_owner")
        if target.get("service_date") != draft.service_date.isoformat():
            raise self._undo_stale("changed_by_other")
        if target.get("type") == "acknowledge_warning" or not target.get("lanes"):
            raise invalid("not_undoable")
        is_revert = isinstance(command, RevertCommand)
        expect_key = "hash_after" if is_revert else "hash_before"
        contents_src = target.get("content_before" if is_revert else "content_after") or {}
        contents: Dict[str, Optional[Dict[str, Any]]] = {}
        now = self.now()
        for meta in target.get("lanes") or []:
            truck_id = meta.get("truck_id")
            lane = draft.lanes.get(truck_id)
            current_hash = engine.content_hash(lane) if lane is not None else None
            if current_hash != meta.get(expect_key):
                raise self._undo_stale("changed_by_other")
            if lane is not None:
                if lane.publish.state == "publishing" or is_in_recovery(lane, now):
                    raise self._undo_stale("published_since")
                published = lane.publish.published_version
                version_after = meta.get("version_after") or 0
                if published is not None and published >= version_after:
                    raise self._undo_stale("published_since")
            contents[truck_id] = contents_src.get(truck_id)
        return target, contents

    # -- history (R22.3) ---------------------------------------------------

    async def history(self, tenant_id: str, service_date: date, query: HistoryQuery, *, tz: str) -> Dict[str, Any]:
        self.check_service_date(service_date, tz)
        filt: List[Dict[str, Any]] = [
            {"term": {"tenant_id": tenant_id}},
            {"term": {"service_date": service_date.isoformat()}},
        ]
        if query.truck_id:
            filt.append({"term": {"truck_ids": query.truck_id}})
        body: Dict[str, Any] = {
            "query": {"bool": {"filter": filt}},
            "sort": [{"created_at": {"order": "desc"}}, {"command_id": {"order": "desc"}}],
            "size": query.size + 1,
            "_source": {"excludes": ["response", "content_before", "content_after", "payload"]},
        }
        if query.cursor:
            created_at, _, command_id = query.cursor.rpartition("_")
            if not created_at or not command_id:
                raise invalid("invalid_cursor", fields=["cursor"])
            body["search_after"] = [created_at, command_id]
        resp = await self._es.search_documents(DISPATCH_BOARD_COMMANDS_INDEX, body, query.size + 1)
        docs = [d for d in _hits(resp) if d.get("tenant_id") == tenant_id]
        page = docs[: query.size]
        items = [
            HistoryItem(
                command_id=d.get("command_id") or "",
                type=d.get("type") or "",
                result=d.get("result") or "",
                actor_user_id=d.get("actor_user_id"),
                actor_name=d.get("actor_name"),
                input_modality=d.get("input_modality"),
                created_at=_parse_dt(d.get("created_at")),
                lanes=list(d.get("lanes") or []),
                checks_summary=dict(d.get("checks_summary") or {}),
                overrides=list(d.get("overrides") or []),
            )
            for d in page
        ]
        next_cursor = None
        if len(docs) > query.size and page:
            last = page[-1]
            next_cursor = f"{last.get('created_at')}_{last.get('command_id')}"
        return HistoryPage(items=items, next_cursor=next_cursor).model_dump(mode="json")
