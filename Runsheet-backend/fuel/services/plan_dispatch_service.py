"""Dispatcher-approved fuel plan delivery to the assigned driver's handset.

This service is the missing bridge between the planning indices and the
canonical FuelOrder lifecycle.  A plan is not considered dispatched until:

* its truck resolves to exactly one active driver;
* every route stop resolves to an exact FuelOrder (new plans carry
  ``order_ids``; a deliberately strict fallback supports unambiguous old
  plans);
* every order is linked to the driver, truck, and planning run;
* every order reaches ``dispatched`` through :class:`OrderService`, which is
  what triggers the push subscriber and the rest of the order side effects;
* execution records exist and the route/plan projections are marked
  dispatched; and
* a realtime invalidation is sent to an open driver app.

The operation is idempotent.  Retrying an already-dispatched plan neither
duplicates status events nor execution records.

Serialization and ownership (loading-plan-executor design K5 and FREEZE
rules 1-2).  Everything from the first order claim through the plan write runs
under the tenant's :data:`~persistence.plan_execution_lock.PLAN_EXECUTION_LOCK`,
the same lock the loading-plan executor holds, so the two never claim the same
orders concurrently.  Each call claims its orders with one ``claim_id``; on any
failure it releases exactly the links that ``claim_id`` wrote and that have not
reached ``dispatched``, whatever a concurrent driver assignment or edit did to
the order meanwhile.  Orders that were already linked to this run (executor
applied) are never released.

Residuals:

* A guarded-transition refusal inside the order loop (a concurrent cancel or
  driver assignment of order *k* between its claim and its transition) raises
  409 ``order_changed_concurrently`` after orders *1..k-1* were dispatched, as
  any mid-loop failure does.  Orders *k..n* are released back to unlinked.
  Closing that window needs a transaction across orders (backlog).
* A pod kill or task cancellation after the claims and before the order loop
  finishes leaves this call's claims in place.  ``asyncio.CancelledError`` is a
  ``BaseException``, so the ``except Exception`` release does not run; awaiting
  store calls while being cancelled is unreliable.  A dispatch retry heals it
  unless an order's driver changed meanwhile: every claim then returns
  ``already_linked`` and the loop runs.  A ``reject_plan`` of that plan strands
  the links; there is no release path in this change (OQ10).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence
from uuid import uuid4

from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.order_repository import (
    OrderChangedConcurrentlyError,
    OrderWriteDiscardedError,
    _link,
)
from fuel.order_state_machine import assert_window_present_for_transition
from fuel.services.order_id_generator import mint_event_id
from persistence.plan_execution_lock import (
    PLAN_EXECUTION_LOCK,
    PlanExecutionLockTimeout,
)
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

MVP_LOAD_PLANS_INDEX = "mvp_load_plans"
MVP_ROUTES_INDEX = "mvp_routes"
MVP_PLAN_EXECUTIONS_INDEX = "mvp_plan_executions"

_DISPATCHABLE_STATUSES = {"confirmed", "scheduled"}
_ACTIVE_STATUSES = {"dispatched", "in_transit"}

#: 409 message per ``AssignmentClaim.reason`` (design K5, pass-4 NIT 3).
_CLAIM_REFUSAL_MESSAGES = {
    "order_committed_elsewhere": "An order in the plan is committed to another run",
    "order_changed_since_plan": "An order in the plan changed since it was loaded; reload and retry",
    "order_not_found": "The plan references orders that no longer exist",
}


@dataclass(frozen=True)
class PlanDispatchResult:
    plan_id: str
    run_id: str
    driver_id: str
    truck_id: str
    route_ids: List[str]
    execution_ids: List[str]
    order_ids: List[str]
    newly_dispatched: int
    already_dispatched: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "driver_id": self.driver_id,
            "truck_id": self.truck_id,
            "route_ids": list(self.route_ids),
            "execution_ids": list(self.execution_ids),
            "order_ids": list(self.order_ids),
            "newly_dispatched": self.newly_dispatched,
            "already_dispatched": self.already_dispatched,
        }


class FuelPlanDispatchService:
    """Turn one approved loading plan into authenticated driver work."""

    def __init__(
        self,
        *,
        es_service: Any,
        order_repository: Any,
        order_service: Any,
        driver_repository: Any,
        execution_service: Any,
        driver_ws_manager: Optional[Any] = None,
        clock=utcnow,
        plan_lock: Any = PLAN_EXECUTION_LOCK,
    ) -> None:
        required = {
            "es_service": es_service,
            "order_repository": order_repository,
            "order_service": order_service,
            "driver_repository": driver_repository,
            "execution_service": execution_service,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(
                "FuelPlanDispatchService missing dependencies: "
                + ", ".join(sorted(missing))
            )
        self._es = es_service
        self._order_repository = order_repository
        self._order_service = order_service
        self._driver_repository = driver_repository
        self._execution_service = execution_service
        self._driver_ws_manager = driver_ws_manager
        self._clock = clock
        self._plan_lock = plan_lock

    async def dispatch(
        self,
        *,
        tenant_id: str,
        plan_doc: Mapping[str, Any],
        actor_user_id: str,
    ) -> PlanDispatchResult:
        """Validate, link, transition, and notify one loading plan."""
        plan_id = str(plan_doc.get("plan_id") or "").strip()
        truck_id = str(plan_doc.get("truck_id") or "").strip()
        if not plan_id or not truck_id:
            raise self._conflict(
                "The plan is missing the identifiers required for dispatch",
                reason="plan_identity_incomplete",
                plan_id=plan_id or None,
                truck_id=truck_id or None,
            )

        routes = await self._fetch_routes(tenant_id, plan_id, truck_id)
        if not routes:
            raise self._conflict(
                "The plan has no route to send to a driver",
                reason="plan_route_missing",
                plan_id=plan_id,
                truck_id=truck_id,
            )

        driver = await self._resolve_driver(tenant_id, truck_id)
        driver_id = str(driver.get("driver_id") or "")
        run_id = self._run_id(plan_doc, routes, plan_id)
        orders = await self._resolve_orders(tenant_id, plan_doc, routes)

        # Fail before the first write.  Elasticsearch cannot transact across
        # all of these projections, so complete preflight validation is what
        # prevents a known-invalid order late in the list from creating a
        # partially dispatched run.
        for order in orders:
            self._validate_order(
                order,
                tenant_id=tenant_id,
                driver_id=driver_id,
                truck_id=truck_id,
                run_id=run_id,
            )

        # Route identity is a deterministic refusal, so it comes before any
        # claim (K5 preflight step 2).
        for route in routes:
            if not str(route.get("route_id") or "").strip():
                raise self._conflict(
                    "A route in this plan has no route_id",
                    reason="route_identity_incomplete",
                    plan_id=plan_id,
                )

        execution_ids: List[str] = []
        route_ids: List[str] = []
        now = self._clock().isoformat()
        newly_dispatched = 0
        already_dispatched = 0
        order_ids: List[str] = []
        claim_id = uuid4().hex
        lock_held = False
        try:
            async with self._plan_lock.hold(tenant_id):
                lock_held = True
                # Orders this call linked and has not dispatched yet.
                fresh: List[str] = []
                try:
                    # Claim every order (K5 preflight step 3).  The preflight
                    # above may have read a stale projection; the claim reads
                    # the authoritative document under its row lock.
                    for order in orders:
                        if order.get("status") in _ACTIVE_STATUSES:
                            continue
                        order_id = str(order["order_id"])
                        claim = await self._order_repository.claim_assignment(
                            tenant_id,
                            order_id,
                            run_id=run_id,
                            asset_id=truck_id,
                            expected_status=order["status"],
                            claim_id=claim_id,
                        )
                        if claim.outcome == "refused":
                            reason = claim.reason or "order_committed_elsewhere"
                            logger.warning(
                                "dispatch: claim refused for order=%s plan=%s "
                                "run=%s reason=%s",
                                order_id,
                                plan_id,
                                run_id,
                                reason,
                            )
                            raise self._conflict(
                                _CLAIM_REFUSAL_MESSAGES.get(
                                    reason,
                                    _CLAIM_REFUSAL_MESSAGES["order_committed_elsewhere"],
                                ),
                                reason=reason,
                                order_id=order_id,
                            )
                        if claim.outcome == "linked":
                            fresh.append(order_id)
                        # The authoritative document from here on; re-check it
                        # (pass-5 finding 2).
                        order.clear()
                        order.update(claim.order)
                        self._validate_order(
                            order,
                            tenant_id=tenant_id,
                            driver_id=driver_id,
                            truck_id=truck_id,
                            run_id=run_id,
                        )

                    for route in routes:
                        route_id = str(route.get("route_id") or "").strip()
                        route_ids.append(route_id)
                        await self._es.update_document(
                            MVP_ROUTES_INDEX,
                            route_id,
                            {
                                "run_id": run_id,
                                "status": "dispatched",
                                "updated_at": now,
                            },
                        )
                        execution = await self._ensure_execution(
                            tenant_id=tenant_id,
                            plan_id=plan_id,
                            route=route,
                        )
                        execution_ids.append(str(execution["execution_id"]))

                    for order in orders:
                        order_id = str(order["order_id"])
                        order_ids.append(order_id)
                        if order.get("status") in _ACTIVE_STATUSES:
                            already_dispatched += 1
                            continue

                        # The driver travels on the in-memory document into
                        # the canonical transition write, next to the links
                        # the claim already put there.  The dispatched
                        # subscriber therefore sees the driver id.
                        order["assigned_driver_id"] = driver_id
                        await self._append_assignment_event(
                            tenant_id=tenant_id,
                            order=order,
                            actor_user_id=actor_user_id,
                            driver_id=driver_id,
                            truck_id=truck_id,
                            run_id=run_id,
                            plan_id=plan_id,
                        )

                        try:
                            if order.get("status") == "confirmed":
                                await self._order_service.apply_status_transition(
                                    order=order,
                                    new_status="scheduled",
                                    reason="dispatcher_plan_approved",
                                    actor_user_id=actor_user_id,
                                    guard_stored_state=True,
                                )
                            await self._order_service.apply_status_transition(
                                order=order,
                                new_status="dispatched",
                                reason="dispatcher_plan_approved",
                                actor_user_id=actor_user_id,
                                guard_stored_state=True,
                            )
                        except (OrderChangedConcurrentlyError, OrderWriteDiscardedError):
                            logger.warning(
                                "dispatch: order=%s changed while plan=%s was "
                                "being dispatched",
                                order_id,
                                plan_id,
                            )
                            raise self._conflict(
                                "An order in the plan changed while it was being dispatched",
                                reason="order_changed_concurrently",
                                order_id=order_id,
                            ) from None
                        if order_id in fresh:
                            fresh.remove(order_id)
                        newly_dispatched += 1

                    await self._es.update_document(
                        MVP_LOAD_PLANS_INDEX,
                        plan_id,
                        {
                            "run_id": run_id,
                            "status": "dispatched",
                            "approved_by": actor_user_id,
                            "approved_at": now,
                            "updated_at": now,
                        },
                    )
                except Exception:
                    # Release by ownership (FREEZE rule 2): only links this
                    # call's claim wrote and that never reached dispatched.
                    for order_id in fresh:
                        try:
                            await self._order_repository.release_assignment(
                                tenant_id,
                                order_id,
                                run_id=run_id,
                                asset_id=truck_id,
                                claim_id=claim_id,
                            )
                        except Exception:
                            logger.exception(
                                "dispatch: releasing claim on %s failed "
                                "(plan=%s run=%s)",
                                order_id,
                                plan_id,
                                run_id,
                            )
                    raise
        except PlanExecutionLockTimeout:
            if lock_held:
                raise
            raise self._conflict(
                "Another plan is being applied or dispatched for this tenant; retry",
                reason="plan_execution_busy",
                plan_id=plan_id,
            ) from None

        if self._driver_ws_manager is not None:
            try:
                await self._driver_ws_manager.send_assignment(
                    driver_id,
                    {
                        "plan_id": plan_id,
                        "run_id": run_id,
                        "truck_id": truck_id,
                        "route_ids": route_ids,
                        "order_ids": order_ids,
                    },
                )
            except Exception as exc:  # notification failure never rolls back work
                logger.warning(
                    "Realtime assignment failed for plan=%s driver=%s: %s",
                    plan_id,
                    driver_id,
                    exc,
                )

        return PlanDispatchResult(
            plan_id=plan_id,
            run_id=run_id,
            driver_id=driver_id,
            truck_id=truck_id,
            route_ids=route_ids,
            execution_ids=execution_ids,
            order_ids=order_ids,
            newly_dispatched=newly_dispatched,
            already_dispatched=already_dispatched,
        )

    async def _fetch_routes(
        self, tenant_id: str, plan_id: str, truck_id: str
    ) -> List[Dict[str, Any]]:
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"plan_id": plan_id}},
                        {"term": {"truck_id": truck_id}},
                    ]
                }
            },
            "size": 100,
        }
        response = await self._es.search_documents(MVP_ROUTES_INDEX, query, 100)
        return [
            dict(hit.get("_source") or {})
            for hit in (response.get("hits", {}).get("hits", []) or [])
            if (hit.get("_source") or {}).get("tenant_id") == tenant_id
        ]

    async def _resolve_driver(
        self, tenant_id: str, truck_id: str
    ) -> Dict[str, Any]:
        result = await self._driver_repository.search(
            tenant_id,
            assigned_truck_id=truck_id,
            page=1,
            size=10,
        )
        drivers = [
            self._as_dict(driver)
            for driver in (result.get("drivers") or [])
            if self._as_dict(driver).get("status") == "active"
        ]
        if len(drivers) != 1:
            reason = (
                "no_active_driver_for_truck"
                if not drivers
                else "multiple_active_drivers_for_truck"
            )
            raise AppException(
                error_code=ErrorCode.DRIVER_UNAVAILABLE,
                message=(
                    "Exactly one active driver must be assigned to the plan's "
                    "truck before dispatch"
                ),
                status_code=409,
                details={
                    "reason": reason,
                    "truck_id": truck_id,
                    "active_driver_count": len(drivers),
                },
            )
        return drivers[0]

    async def _resolve_orders(
        self,
        tenant_id: str,
        plan_doc: Mapping[str, Any],
        routes: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        assignments = list(plan_doc.get("assignments") or [])
        exact_ids = {
            str(assignment.get("order_id"))
            for assignment in assignments
            if assignment.get("order_id")
        }
        station_ids = {
            str(assignment.get("station_id"))
            for assignment in assignments
            if assignment.get("station_id")
        }
        for route in routes:
            for stop in route.get("stops") or []:
                exact_ids.update(
                    str(order_id)
                    for order_id in (stop.get("order_ids") or [])
                    if order_id
                )
                if stop.get("station_id"):
                    station_ids.add(str(stop["station_id"]))

        all_orders = [
            self._as_dict(order)
            for order in await self._order_repository.list_for_tenant(
                tenant_id, size=2000
            )
        ]
        by_order_id = {
            str(order.get("order_id")): order
            for order in all_orders
            if order.get("order_id")
        }

        if exact_ids:
            missing = sorted(exact_ids - set(by_order_id))
            if missing:
                raise self._conflict(
                    "The plan references orders that no longer exist",
                    reason="plan_order_missing",
                    missing_order_ids=missing,
                )
            selected = [by_order_id[order_id] for order_id in sorted(exact_ids)]
        else:
            # Backward-compatible support for pre-order-id plans is intentionally
            # strict.  An identifier that maps to more than one eligible order
            # is unsafe to auto-dispatch; the operator must regenerate the plan
            # so it carries exact order ids.
            selected_by_id: Dict[str, Dict[str, Any]] = {}
            for station_id in sorted(station_ids):
                candidates = [
                    order
                    for order in all_orders
                    if order.get("status") in (_DISPATCHABLE_STATUSES | _ACTIVE_STATUSES)
                    and station_id
                    in {
                        str(order.get("order_id") or ""),
                        str(order.get("customer_tank_id") or ""),
                        str(order.get("customer_id") or ""),
                    }
                ]
                if len(candidates) > 1:
                    raise self._conflict(
                        "An older plan stop maps to multiple fuel orders; "
                        "regenerate the plan before dispatch",
                        reason="plan_order_ambiguous",
                        station_id=station_id,
                        candidate_order_ids=sorted(
                            str(order["order_id"]) for order in candidates
                        ),
                    )
                if candidates:
                    selected_by_id[str(candidates[0]["order_id"])] = candidates[0]
            selected = [selected_by_id[key] for key in sorted(selected_by_id)]

        if not selected:
            raise self._conflict(
                "The plan does not resolve to any dispatchable fuel orders",
                reason="plan_has_no_orders",
                station_ids=sorted(station_ids),
            )
        return selected

    def _validate_order(
        self,
        order: Mapping[str, Any],
        *,
        tenant_id: str,
        driver_id: str,
        truck_id: str,
        run_id: str,
    ) -> None:
        order_id = str(order.get("order_id") or "")
        if order.get("tenant_id") != tenant_id:
            raise self._conflict(
                "The plan contains an order from another tenant",
                reason="plan_order_tenant_mismatch",
                order_id=order_id,
            )
        status = str(order.get("status") or "")
        if status not in (_DISPATCHABLE_STATUSES | _ACTIVE_STATUSES):
            raise self._conflict(
                "An order in the plan is not ready for dispatch",
                reason="order_not_dispatchable",
                order_id=order_id,
                status=status,
            )
        existing_driver = order.get("assigned_driver_id")
        existing_asset = order.get("assigned_asset_id")
        existing_run = order.get("assigned_run_id")
        if status in _ACTIVE_STATUSES and (
            existing_driver != driver_id
            or existing_asset != truck_id
            or existing_run != run_id
        ):
            raise self._conflict(
                "An active order is already assigned to another run",
                reason="order_assignment_conflict",
                order_id=order_id,
                assigned_driver_id=existing_driver,
                assigned_asset_id=existing_asset,
                assigned_run_id=existing_run,
            )
        if status in _DISPATCHABLE_STATUSES and (
            _link(existing_run) not in (None, run_id)
            or _link(existing_asset) not in (None, truck_id)
        ):
            # K5 preflight step 1: committed to another run or truck (for
            # example by the loading-plan executor for another plan).
            raise self._conflict(
                _CLAIM_REFUSAL_MESSAGES["order_committed_elsewhere"],
                reason="order_committed_elsewhere",
                order_id=order_id,
            )
        if status in _DISPATCHABLE_STATUSES and existing_driver not in (
            None,
            "",
            driver_id,
        ):
            raise self._conflict(
                "An order is already assigned to another driver",
                reason="order_driver_conflict",
                order_id=order_id,
                assigned_driver_id=existing_driver,
            )
        if status == "confirmed":
            assert_window_present_for_transition(dict(order), "scheduled")
        if status in _DISPATCHABLE_STATUSES:
            assert_window_present_for_transition(dict(order), "dispatched")

    async def _append_assignment_event(
        self,
        *,
        tenant_id: str,
        order: Mapping[str, Any],
        actor_user_id: str,
        driver_id: str,
        truck_id: str,
        run_id: str,
        plan_id: str,
    ) -> None:
        now = self._clock()
        await self._order_repository.append_event(
            tenant_id,
            {
                "event_id": mint_event_id(),
                "order_id": order["order_id"],
                "tenant_id": tenant_id,
                "event_type": "order_assigned",
                "event_payload": {
                    "driver_id": driver_id,
                    "asset_id": truck_id,
                    "run_id": run_id,
                    "plan_id": plan_id,
                    "actor_user_id": actor_user_id,
                },
                "event_timestamp": now,
                "ingested_at": now,
                "source_schema_version": order.get(
                    "source_schema_version", "1.0"
                ),
                "trace_id": order.get("trace_id", ""),
            },
        )

    async def _ensure_execution(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        route: Mapping[str, Any],
    ) -> Dict[str, Any]:
        route_id = str(route["route_id"])
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"plan_id": plan_id}},
                        {"term": {"route_id": route_id}},
                    ]
                }
            },
            "size": 1,
        }
        response = await self._es.search_documents(
            MVP_PLAN_EXECUTIONS_INDEX, query, 1
        )
        hits = response.get("hits", {}).get("hits", []) or []
        if hits:
            return dict(hits[0].get("_source") or {})
        return await self._execution_service.create_execution(
            plan_id=plan_id,
            route_id=route_id,
            tenant_id=tenant_id,
            stops=list(route.get("stops") or []),
        )

    @staticmethod
    def _run_id(
        plan_doc: Mapping[str, Any],
        routes: Sequence[Mapping[str, Any]],
        plan_id: str,
    ) -> str:
        if plan_doc.get("run_id"):
            return str(plan_doc["run_id"])
        for route in routes:
            if route.get("run_id"):
                return str(route["run_id"])
        # A plan id is stable and unique, and is a safe run key for plans
        # created before the pipeline began stamping run_id.
        return plan_id

    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return dict(dump(mode="python"))
        return dict(value)

    @staticmethod
    def _conflict(message: str, **details: Any) -> AppException:
        return AppException(
            error_code=ErrorCode.INVALID_STATUS_TRANSITION,
            message=message,
            status_code=409,
            details=details,
        )


__all__ = ["FuelPlanDispatchService", "PlanDispatchResult"]
