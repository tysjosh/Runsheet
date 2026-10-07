"""A publish world for the Dispatch Board Phase 2 tests (plan tasks 15-17).

The board ``Harness`` (fake validators over ``BoardStore``) plus the real
order stack over the same store: ``FuelOrderRepository`` (relink CAS, claim
CAS, guarded upsert), ``OrderService``, ``LoadingPlanExecutor``,
``FuelPlanDispatchService`` and ``PlanExecutionService``. Driver notifications
and work-cache invalidations go to spies that also write a marker into the
store's op log, so a test can check the order of writes, invalidations and
notifications on one timeline.
"""
from __future__ import annotations

import copy
import fnmatch
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import AsyncMock

from Agents.support.plan_execution_service import PlanExecutionService
from driver.services.work_service import DriverWorkService
from fuel.order_repository import FuelOrderRepository
from fuel.services.dispatch_board_models import PublishBody, PublishPreviewBody
from fuel.services.dispatch_board_publish import BoardPublishService, BoardRedispatchService
from fuel.services.loading_plan_executor import LoadingPlanExecutor
from fuel.services.order_service import OrderService
from fuel.services.plan_dispatch_service import FuelPlanDispatchService
from persistence.plan_execution_lock import PlanExecutionLock
from tests.unit._dispatch_board_fakes import ORDERS, T, TODAY, TZ, Harness
from tests.unit._loading_plan_fakes import fuel_order_doc

PLANS = "mvp_load_plans"
ROUTES = "mvp_routes"
EXECUTIONS = "mvp_plan_executions"
OPERATIONAL = {ORDERS, PLANS, ROUTES, EXECUTIONS}
REASON = "Checked with the driver."


def full_order(order_id: str, *, day: date = TODAY, status: str = "confirmed", **overrides: Any) -> Dict[str, Any]:
    """A valid stored ``FuelOrder`` for the service day (the executor reads the model)."""
    fields: Dict[str, Any] = {
        "customer_id": f"cust-{order_id}",
        "customer_tank_id": f"tank-{order_id}",
        "product_code": "DIESEL_2",
        "gallons_requested": 500.0,
        "delivery_window_start": f"{day.isoformat()}T13:00:00+00:00",
        "delivery_window_end": f"{day.isoformat()}T23:00:00+00:00",
        "ship_to_lat": 41.88,
        "ship_to_lon": -87.63,
    }
    fields.update(overrides)
    return fuel_order_doc(order_id, tenant_id=T, status=status, **fields)


class FakeRedis:
    """The driver work bundle cache: ``get``/``setex``/``delete``/``scan_iter``."""

    def __init__(self) -> None:
        self.values: Dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: Any) -> None:
        self.values[key] = value

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.values.pop(k, None) is not None)

    async def scan_iter(self, match: str = "*"):
        for key in list(self.values):
            if fnmatch.fnmatch(key, match):
                yield key


class SpyDriverWS:
    """Driver WS manager double: records every call and marks the op log."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.calls: List[tuple] = []
        self.fail_for: set = set()

    async def _record(self, kind: str, driver_id: str, payload: Dict[str, Any]) -> bool:
        self.calls.append((kind, driver_id, copy.deepcopy(payload)))
        self.store.ops.append(("notify", kind, driver_id, True))
        if driver_id in self.fail_for:
            raise ConnectionError("socket gone")
        return True

    async def send_assignment(self, driver_id: str, payload: Dict[str, Any]) -> bool:
        return await self._record("assignment", driver_id, payload)

    async def send_assignment_revoked(self, driver_id: str, payload: Dict[str, Any]) -> bool:
        return await self._record("assignment_revoked", driver_id, payload)

    async def send_new_route(self, driver_id: str, payload: Dict[str, Any]) -> bool:
        return await self._record("new_route", driver_id, payload)

    def of(self, kind: str) -> List[tuple]:
        return [c for c in self.calls if c[0] == kind]


class World:
    """Board + real order stack over one store."""

    def __init__(self, *, retry_delays: Sequence[float] = (0.0, 0.0, 0.0), forward_warning_interval_s: float = 0.0) -> None:
        self.h = Harness()
        self.store = self.h.store
        self.lock = PlanExecutionLock(use_postgres=False, timeout_seconds=2)
        self.repo = FuelOrderRepository(self.store)
        self.order_ws = AsyncMock()
        self.order_service = OrderService(order_repo=self.repo, ws_manager=self.order_ws, driver_counter_service=AsyncMock())
        #: Added to the executor's clock by ``advance`` so its own plan lease can expire too.
        self.skew = timedelta(0)
        self.executor = LoadingPlanExecutor(
            es_service=self.store, order_repository=self.repo, order_service=self.order_service, plan_lock=self.lock,
            clock=lambda: datetime.now(timezone.utc) + self.skew,
        )
        self.executions = PlanExecutionService(es_service=self.store)
        self.driver_ws = SpyDriverWS(self.store)
        self.dispatch = FuelPlanDispatchService(
            es_service=self.store,
            order_repository=self.repo,
            order_service=self.order_service,
            driver_repository=self.h.drivers,
            execution_service=self.executions,
            driver_ws_manager=self.driver_ws,
            plan_lock=self.lock,
        )
        self.invalidations: List[str] = []
        self.redis = FakeRedis()
        self.store.multi_search = self._multi_search  # type: ignore[attr-defined]
        self.work = DriverWorkService(es_service=self.store, order_repository=self.repo, redis_client=self.redis)
        self.redispatch = BoardRedispatchService(
            es_service=self.store,
            order_repository=self.repo,
            executor=self.executor,
            dispatch_service=self.dispatch,
            execution_service=self.executions,
            driver_ws_manager=self.driver_ws,
            work_cache_invalidator=self._invalidate,
            telemetry=self.h.service.telemetry,
            clock=lambda: self.h.now,
            retry_delays=retry_delays,
            forward_warning_interval_s=forward_warning_interval_s,
        )
        self.publish = BoardPublishService(
            es_service=self.store,
            board_service=self.h.service,
            executor=self.executor,
            dispatch_service=self.dispatch,
            redispatch_service=self.redispatch,
        )

    async def _invalidate(self, tenant_id: str, order_id: str) -> None:
        self.invalidations.append(order_id)
        self.store.ops.append(("invalidate", tenant_id, order_id, True))
        await self.work.invalidate(tenant_id, order_id)

    async def _multi_search(self, searches: List[Dict[str, Any]]) -> Dict[str, Any]:
        """``ElasticsearchService.multi_search`` over the fake store; ``a,b`` spans both."""
        responses = []
        for entry in searches:
            hits: List[Dict[str, Any]] = []
            for index in str(entry["index"]).split(","):
                resp = await self.store.search_documents(index, entry["query"], entry["query"].get("size", 100))
                hits.extend(resp["hits"]["hits"])
            responses.append({"hits": {"hits": hits, "total": {"value": len(hits)}}})
        return {"responses": responses}

    async def work_read(self, driver_id: str, order_id: str) -> Dict[str, Any]:
        """``GET /api/driver/work/{order_id}`` through the real ``DriverWorkService`` (cached)."""
        return (await self.work.get_work(T, driver_id, order_id))["data"]

    # -- seeding -------------------------------------------------------------

    def seed(self, *order_ids: str, **kw: Any) -> None:
        for order_id in order_ids:
            self.store.seed(ORDERS, order_id, full_order(order_id, **kw))

    async def lane(self, truck: str, *order_ids: str, driver_id: Optional[str] = None) -> None:
        await self.h.lane_with(truck, *order_ids, driver_id=driver_id)

    # -- publish -------------------------------------------------------------

    def refs(self, *trucks: str) -> List[Dict[str, Any]]:
        draft = self.h.draft()
        return [{"truck_id": t, "expected_version": draft.lanes[t].version if t in draft.lanes else 0} for t in trucks]

    async def dry(self, *trucks: str, reasons: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        body = PublishPreviewBody.model_validate({"dry_run": True, "lanes": self.refs(*trucks), "warning_reasons": reasons or {}})
        return await self.publish.handle(tenant_id=T, user_id="user-1", service_date=TODAY, body=body, tz=TZ)

    async def reasons_for(self, *trucks: str) -> Dict[str, str]:
        preview = await self.dry(*trucks)
        return {w["warning_id"]: REASON for w in preview["open_warnings"]}

    async def start(self, *trucks: str, cid: Optional[str] = None, reasons: Optional[Dict[str, str]] = None, ack: bool = True) -> Dict[str, Any]:
        if reasons is None and ack:
            reasons = await self.reasons_for(*trucks)
        body = PublishBody.model_validate(
            {"client_request_id": cid or str(uuid.uuid4()), "lanes": self.refs(*trucks), "warning_reasons": reasons or {}}
        )
        return await self.publish.handle(tenant_id=T, user_id="user-1", service_date=TODAY, body=body, tz=TZ)

    async def run(self, *trucks: str, **kw: Any) -> Dict[str, Any]:
        response = await self.start(*trucks, **kw)
        await self.publish.wait_idle()
        return response

    # -- reads ---------------------------------------------------------------

    def lane_doc(self, truck: str):
        return self.h.draft().lanes[truck]

    def order(self, order_id: str) -> Dict[str, Any]:
        return self.store.doc(ORDERS, order_id)

    def links(self, order_id: str) -> tuple:
        o = self.order(order_id)
        return (o.get("assigned_run_id"), o.get("assigned_asset_id"), o.get("assigned_driver_id"))

    def plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        return self.store.doc(PLANS, plan_id)

    def route(self, route_id: str) -> Optional[Dict[str, Any]]:
        return self.store.doc(ROUTES, route_id)

    def executions_of(self, plan_id: str) -> List[Dict[str, Any]]:
        return [copy.deepcopy(d) for d in self.store.docs[EXECUTIONS].values() if d.get("plan_id") == plan_id]

    def published(self, truck: str) -> Dict[str, Any]:
        """``load_id -> PublishedPlan`` of a lane."""
        return dict(self.lane_doc(truck).publish.plans)

    def plan_of(self, truck: str, index: int = 0):
        lane = self.lane_doc(truck)
        return lane.publish.plans[lane.loads[index].load_id]

    async def command(self, type_: str, *, lanes: Sequence[str], **fields: Any) -> Dict[str, Any]:
        return await self.h.run(type_, lanes=tuple(lanes), **fields)

    async def move(self, order_ids: Sequence[str], truck: str, *, lanes: Sequence[str], target: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self.command("move_stops", lanes=lanes, order_ids=list(order_ids), truck_id=truck, target=target or {})

    async def driver_start(self, order_id: str) -> Dict[str, Any]:
        """The driver marks an order ``in_transit`` through the guarded write (no gate)."""
        current = await self.repo.get_current(T, order_id)
        return await self.order_service.apply_status_transition(
            order=current, new_status="in_transit", actor_user_id="driver", guard_stored_state=True
        )

    def timeline(self) -> List[tuple]:
        return list(self.store.ops)

    def advance(self, seconds: float) -> None:
        """Move the board clock and the executor clock forward (lease expiry)."""
        self.h.now = self.h.now + timedelta(seconds=seconds)
        self.skew = self.skew + timedelta(seconds=seconds)
