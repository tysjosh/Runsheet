"""Apply an approved compartment loading plan to its fuel orders.

``LoadingPlanExecutor.execute`` commits a plan's orders to the plan's truck and
run and moves them to ``scheduled`` through
:meth:`fuel.services.order_service.OrderService.apply_status_transition`. It
never dispatches (loading-plan-executor design K2, R2, R9): it only requests
``confirmed`` and ``scheduled``, never touches ``assigned_driver_id`` and
imports no notification, dispatch, plan-execution, driver-WS or jobs module
(pinned by ``tests/unit/test_loading_plan_executor_side_effects.py``).

Sequence (design K4, plan P2):

1. Mode: the caller's ``mode`` or :meth:`LoadingPlanExecutor.resolve_mode`
   (strict flag read, K8). Unresolvable -> ``failed/mode_unavailable``.
2. Plan read + tenant check (a foreign plan is ``plan_not_found``; nothing of
   it is returned).
3. ``shadow``: read-only classification, no claim and no write (R6.1).
4. Fast replay of a ``succeeded`` plan (R4.3).
5-9. Under the tenant's :data:`~persistence.plan_execution_lock.PLAN_EXECUTION_LOCK`
   (design freeze rule 1): the plan claim with a lease (``atomic_update`` on
   ``mvp_load_plans``), preflight on the claimed document (K3), the commit
   (K2), the projection check (K6) and the finalize write guarded on this
   attempt's ``execution_attempt_id``.

Serialization (design freeze). MVP plan dispatch takes the same per-tenant lock
from its first order claim to its plan write, so the executor and dispatch
never claim the same orders concurrently. The executor passes its plan
``attempt_id`` as the order ``claim_id`` and never releases a claim: a partly
applied plan is resumed, not undone (K6).

Residual windows (documented, not closed here):

* **Lease duplicate (K4).** If an attempt stalls past ``lease_seconds`` while
  still running, a reclaimer can run concurrently with it on another process
  (the in-process lock only serializes one process; the Postgres advisory lock
  serializes across processes when the store is Postgres). Both can append
  ``order_assigned`` for the same order because the event dedupe is
  check-then-append. The guarded transition still lets only one of them move
  the status, so the duplicate is limited to one extra ``order_assigned``.
* **Unguarded writers (K5a).** ``assign_driver_to_order`` and other full-order
  upserts outside ``OrderService`` do not take the lock. The executor's guarded
  writes refuse rather than overwrite them (``order_changed_concurrently``);
  the reverse direction is the existing behaviour of those writers.
* **Crash windows (K6).** A guarded upsert can succeed and the following
  ``append_event`` fail (or the pod die): the order is correct but lacks its
  ``order_confirmed`` / ``order_scheduled`` event; the retry classifies it as
  applied and writes nothing (R4.2). A crash after the plan claim and before
  the first order write leaves the plan ``in_progress``; every later attempt
  treats it as possibly written (``prior_writes``), so an edited order makes
  the plan ``incomplete`` until it is cancelled or restored.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from fuel.order_repository import (
    OrderChangedConcurrentlyError,
    OrderWriteDiscardedError,
    _link,
)
from fuel.order_state_machine import (
    TERMINAL_STATUSES,
    assert_window_present_for_transition,
)
from fuel.services.fuel_product_catalog import UnknownFuelProductError, canonicalize
from fuel.services.order_id_generator import mint_event_id
from persistence.plan_execution_lock import (
    PLAN_EXECUTION_LOCK,
    PlanExecutionLockTimeout,
)
from persistence.timestamps import parse_ts
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

#: Overlay flag that gates the loading executor (D7).
LOADING_FLAG_KEY = "overlay.compartment_loading"

MVP_LOAD_PLANS_INDEX = "mvp_load_plans"

#: Modes ``resolve_mode`` accepts (the overlay states).
_VALID_MODES = frozenset({"disabled", "shadow", "active_gated", "active_auto"})

Outcome = Literal["applied", "replayed", "shadow", "failed", "incomplete", "in_progress"]
_OUTCOMES = frozenset({"applied", "replayed", "shadow", "failed", "incomplete", "in_progress"})

# ---------------------------------------------------------------------------
# Reasons (K3, K4, K6, K7, K10)
# ---------------------------------------------------------------------------

REASON_PLAN_NOT_FOUND = "plan_not_found"
REASON_PLAN_REJECTED = "plan_rejected"
REASON_PLAN_IDENTITY_INCOMPLETE = "plan_identity_incomplete"
REASON_ASSIGNMENT_WITHOUT_ORDER = "assignment_without_order"
REASON_PLAN_CHANGED = "plan_changed"
REASON_ORDER_NOT_FOUND = "order_not_found"
REASON_ORDER_NOT_LOADABLE = "order_not_loadable"
REASON_ORDER_COMMITTED_ELSEWHERE = "order_committed_elsewhere"
REASON_ORDER_CHANGED_SINCE_PLAN = "order_changed_since_plan"
REASON_MISSING_DELIVERY_WINDOW = "missing_delivery_window"
REASON_MODE_UNAVAILABLE = "mode_unavailable"
REASON_EXECUTOR_UNAVAILABLE = "executor_unavailable"
REASON_EXECUTION_IN_PROGRESS = "execution_in_progress"
REASON_WRITE_FAILED = "write_failed"
REASON_ORDER_CHANGED_CONCURRENTLY = "order_changed_concurrently"
REASON_ORDER_WRITE_DISCARDED = "order_write_discarded"
REASON_PROJECTION_LAG = "projection_lag"
REASON_PROJECTION_MIRROR_DISABLED = "projection_mirror_disabled"
REASON_INTERNAL_ERROR = "internal_error"
REASON_SHADOW_MODE = "shadow_mode"
REASON_LEGACY_APPROVAL = "legacy_approval"
REASON_APPROVAL_RELEASED_DURING_EXECUTION = "approval_released_during_execution"
REASON_EXPIRED_BEFORE_APPROVAL = "expired_before_approval"

#: Fixed text per reason; only ``{plan_id}`` is substituted (R3.14, R11.5).
#: Exception text never reaches a message.
MESSAGE_TEMPLATES: Dict[str, str] = {
    REASON_PLAN_NOT_FOUND: "Loading plan {plan_id} was not found.",
    REASON_PLAN_REJECTED: "Loading plan {plan_id} was rejected and cannot be applied.",
    REASON_PLAN_IDENTITY_INCOMPLETE: "Loading plan {plan_id} is missing its plan or truck identifier.",
    REASON_ASSIGNMENT_WITHOUT_ORDER: "Loading plan {plan_id} has a compartment assignment without an order.",
    REASON_PLAN_CHANGED: "Loading plan {plan_id} changed since it was proposed; review the new proposal.",
    REASON_ORDER_NOT_FOUND: "Loading plan {plan_id} was not applied: an order in it no longer exists.",
    REASON_ORDER_NOT_LOADABLE: "Loading plan {plan_id} was not applied: an order in it can no longer be loaded.",
    REASON_ORDER_COMMITTED_ELSEWHERE: "Loading plan {plan_id} was not applied: an order in it is committed to another run.",
    REASON_ORDER_CHANGED_SINCE_PLAN: "Loading plan {plan_id} was not applied: an order changed since the plan was proposed.",
    REASON_MISSING_DELIVERY_WINDOW: "Loading plan {plan_id} was not applied: an order has no delivery window.",
    REASON_MODE_UNAVAILABLE: "Loading plan {plan_id} was not applied: the loading mode could not be read. Retry shortly.",
    REASON_EXECUTOR_UNAVAILABLE: "Loading plan {plan_id} was not applied: the loading executor is not available. Retry later.",
    REASON_EXECUTION_IN_PROGRESS: "Loading plan {plan_id} is already being applied. Retry shortly.",
    REASON_WRITE_FAILED: "Loading plan {plan_id} is partly applied: a write failed. Retry to finish it.",
    REASON_ORDER_CHANGED_CONCURRENTLY: "Loading plan {plan_id} is partly applied: an order changed while it was being applied. Retry to finish it.",
    REASON_ORDER_WRITE_DISCARDED: "Loading plan {plan_id} is partly applied: an order write was superseded. Retry to finish it.",
    REASON_PROJECTION_LAG: "Loading plan {plan_id} is applied but not yet visible everywhere. Retry to confirm it.",
    REASON_PROJECTION_MIRROR_DISABLED: "Loading plan {plan_id} is applied but the order read model is not being updated. Contact support.",
    REASON_INTERNAL_ERROR: "Loading plan {plan_id} could not be applied because of an internal error. Retry later.",
    REASON_SHADOW_MODE: "Loading plan {plan_id} was evaluated in shadow mode; no orders were changed.",
    REASON_LEGACY_APPROVAL: "Loading plan {plan_id} was approved before execution tracking existed; reject it and re-run planning.",
    REASON_APPROVAL_RELEASED_DURING_EXECUTION: "Loading plan {plan_id} approval was released while it was being applied.",
    REASON_EXPIRED_BEFORE_APPROVAL: "Loading plan {plan_id} expired before it was approved.",
}

#: Outcome texts (ids and counts only).
APPLIED_TEMPLATE = "Loading plan {plan_id} applied: {applied} of {total} orders committed to truck {truck_id}."
REPLAYED_TEMPLATE = "Loading plan {plan_id} was already applied; nothing changed."
#: K3 decision text for a preflight refusal after prior writes.
INCOMPLETE_BLOCKED_TEMPLATE = (
    "Loading plan {plan_id} is partly applied ({k} of {n} orders). Order {order_id} "
    "changed since it was proposed; cancel it or restore it, then retry."
)

#: Statuses an order linked to this run counts as applied in (R4.2).
_APPLIED_STATUSES = frozenset({"scheduled", "dispatched", "in_transit", "delivered"})
#: Linked orders in these statuses are settled (skipped) for this plan.
_LINKED_SETTLED_STATUSES = frozenset({"cancelled", "failed"})
_LOADABLE_STATUSES = frozenset({"placed", "confirmed", "scheduled"})
_STEPS = {"placed": ("confirmed", "scheduled"), "confirmed": ("scheduled",), "scheduled": ("link",)}
_SNAPSHOT_FIELDS = ("customer_tank_id", "gallons_requested", "fill_to_full")


def _template(reason: Optional[str], plan_id: Optional[str]) -> str:
    """Fixed text for a reason; never raises. REASON_INTERNAL_ERROR == "internal_error"."""
    fallback = MESSAGE_TEMPLATES[REASON_INTERNAL_ERROR]
    tpl = MESSAGE_TEMPLATES.get(reason or "", fallback)
    try:
        return tpl.format(plan_id=plan_id or "")
    except (KeyError, IndexError, ValueError):  # a template that needs more than {plan_id} (NIT 5)
        return fallback.format(plan_id=plan_id or "")


def _str_list(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v) for v in value if v is not None]


def _dict_list(value: Any) -> List[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(v) for v in value if isinstance(v, Mapping)]


def _opt_str(value: Any) -> Optional[str]:
    return None if value is None or value == "" else str(value)


# ---------------------------------------------------------------------------
# Result contract (K4, K10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadingPlanExecutionResult:
    """Structured outcome of one ``execute`` call (design K4)."""

    outcome: Outcome
    success: bool                  # True only for applied / replayed
    replay: bool                   # True only for replayed
    plan_id: Optional[str]
    run_id: Optional[str]
    truck_id: Optional[str]
    order_ids: List[str] = field(default_factory=list)          # every plan order, sorted
    applied_order_ids: List[str] = field(default_factory=list)  # this or an earlier attempt
    settled_order_ids: List[str] = field(default_factory=list)  # terminal, skipped
    pending_order_ids: List[str] = field(default_factory=list)  # non-empty only for incomplete
    would_apply: List[dict] = field(default_factory=list)       # shadow only
    failures: List[dict] = field(default_factory=list)          # {order_id, reason, status?, field?}
    reason: Optional[str] = None
    retryable: bool = False
    writes_made: bool = False      # sticky across attempts (K3)
    message: str = ""
    attempt_id: Optional[str] = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def unavailable(cls, plan_id: Optional[str]) -> "LoadingPlanExecutionResult":
        return cls.failure(plan_id, REASON_EXECUTOR_UNAVAILABLE, retryable=True)

    @classmethod
    def mode_unavailable(cls, plan_id: Optional[str]) -> "LoadingPlanExecutionResult":
        return cls.failure(plan_id, REASON_MODE_UNAVAILABLE, retryable=True)

    @classmethod
    def failure(
        cls,
        plan_id: Optional[str],
        reason: str,
        *,
        retryable: bool,
        writes_made: bool = False,
    ) -> "LoadingPlanExecutionResult":
        return cls(
            outcome="failed",
            success=False,
            replay=False,
            plan_id=plan_id,
            run_id=None,
            truck_id=None,
            reason=reason,
            retryable=retryable,
            writes_made=writes_made,
            message=_template(reason, plan_id),
            attempt_id=None,
        )

    @classmethod
    def from_dict(cls, d: Any) -> "LoadingPlanExecutionResult":
        """Rebuild a stored ``execution_result``; never raises.

        Unknown keys are dropped and missing ones defaulted: ``outcome="failed"``,
        lists empty, ``writes_made=True`` when absent (a legacy or partial record
        may have written), ``message`` from the reason's template.
        """
        d = d if isinstance(d, Mapping) else {}
        plan_id = _opt_str(d.get("plan_id"))
        reason = _opt_str(d.get("reason"))
        outcome = d.get("outcome")
        message = d.get("message")
        return cls(
            outcome=outcome if outcome in _OUTCOMES else "failed",
            success=bool(d.get("success", False)),
            replay=bool(d.get("replay", False)),
            plan_id=plan_id,
            run_id=_opt_str(d.get("run_id")),
            truck_id=_opt_str(d.get("truck_id")),
            order_ids=_str_list(d.get("order_ids")),
            applied_order_ids=_str_list(d.get("applied_order_ids")),
            settled_order_ids=_str_list(d.get("settled_order_ids")),
            pending_order_ids=_str_list(d.get("pending_order_ids")),
            would_apply=_dict_list(d.get("would_apply")),
            failures=_dict_list(d.get("failures")),
            reason=reason,
            retryable=bool(d.get("retryable", False)),
            writes_made=bool(d.get("writes_made", True)),
            message=message if isinstance(message, str) and message else _template(reason, plan_id),
            attempt_id=_opt_str(d.get("attempt_id")),
        )


# ---------------------------------------------------------------------------
# Preflight state
# ---------------------------------------------------------------------------


@dataclass
class _Preflight:
    order_ids: List[str] = field(default_factory=list)
    applied: List[str] = field(default_factory=list)
    settled: List[str] = field(default_factory=list)
    #: order_id -> (preflight status, steps, authoritative order)
    to_apply: Dict[str, Tuple[str, Tuple[str, ...], Dict[str, Any]]] = field(default_factory=dict)
    failures: List[dict] = field(default_factory=list)
    plan_failure: Optional[str] = None
    prior_writes: bool = False


def _canon(code: Any) -> Optional[str]:
    if code is None or code == "":
        return None
    try:
        return canonicalize(code)
    except (UnknownFuelProductError, TypeError):
        return str(code).strip().upper()


def _gallons_equal(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return math.isclose(float(a), float(b), abs_tol=1e-6)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class LoadingPlanExecutor:
    """Commit an approved loading plan's orders to its truck and run (design K2-K6)."""

    def __init__(
        self,
        *,
        es_service: Any,
        order_repository: Any,
        order_service: Any,
        feature_flag_service: Optional[Any] = None,
        lease_seconds: float = 120,
        plan_lock: Any = PLAN_EXECUTION_LOCK,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("es_service", es_service),
                ("order_repository", order_repository),
                ("order_service", order_service),
            )
            if value is None
        ]
        if missing:
            raise ValueError("LoadingPlanExecutor missing dependencies: " + ", ".join(missing))
        self._es = es_service
        self._repo = order_repository
        self._order_service = order_service
        self._ff = feature_flag_service
        self._lease = float(lease_seconds)
        self._plan_lock = plan_lock
        self._clock = clock

    # ------------------------------------------------------------------
    # Mode (K8)
    # ------------------------------------------------------------------

    async def resolve_mode(self, tenant_id: str) -> Optional[str]:
        """The tenant's loading overlay mode, or ``None`` when it cannot be read.

        Uses the strict flag read only (never ``get_overlay_state_or_none``,
        never an agent's ``_pipeline_mode_override``; R6.4). A Redis error,
        a disconnected client or an unknown value is ``None`` (fail closed).
        """
        try:
            from config.settings import get_settings

            state = None
            if self._ff is not None:
                state = await self._ff.get_overlay_state_strict(LOADING_FLAG_KEY, tenant_id)
            mode = state or get_settings().overlay_default_mode
        except Exception as exc:
            logger.warning(
                "loading plan: overlay mode unreadable for tenant=%s (%s)",
                tenant_id,
                type(exc).__name__,
            )
            return None
        if mode not in _VALID_MODES:
            logger.warning("loading plan: invalid overlay mode %r for tenant=%s", mode, tenant_id)
            return None
        return mode

    # ------------------------------------------------------------------
    # Execute (K4)
    # ------------------------------------------------------------------

    async def execute(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        expected_order_ids: Sequence[str],
        expected_truck_id: Optional[str],
        order_snapshots: Optional[Mapping[str, Mapping[str, Any]]],
        actor_user_id: str,
        action_id: Optional[str],
        approved_at: Optional[str],
        mode: Optional[str] = None,
        approval_attempt_id: Optional[str] = None,
    ) -> LoadingPlanExecutionResult:
        snapshots = dict(order_snapshots or {})
        expected_ids = sorted({str(o) for o in (expected_order_ids or []) if o})

        # 1. Mode.
        if mode is None:
            mode = await self.resolve_mode(tenant_id)
        if mode not in _VALID_MODES:
            return LoadingPlanExecutionResult.mode_unavailable(plan_id)

        # 2. Plan read + tenant.
        try:
            plan = await self._es.get_document(MVP_LOAD_PLANS_INDEX, plan_id)
        except Exception:
            logger.exception(
                "loading plan: plan read failed tenant=%s plan=%s action=%s",
                tenant_id, plan_id, action_id,
            )
            return LoadingPlanExecutionResult.failure(plan_id, REASON_INTERNAL_ERROR, retryable=True)
        if not plan or plan.get("tenant_id") != tenant_id:
            logger.warning("loading plan: plan_not_found tenant=%s plan=%s", tenant_id, plan_id)
            return LoadingPlanExecutionResult.failure(plan_id, REASON_PLAN_NOT_FOUND, retryable=False)

        # 3. Shadow: classification on this read, no claim, no writes.
        if mode == "shadow":
            return await self._shadow(
                tenant_id, plan_id, plan, expected_ids, expected_truck_id, snapshots
            )

        # 4. Fast replay.
        if plan.get("execution_status") == "succeeded":
            return self._replay(plan, plan_id)

        # 5-9 under the per-tenant lock (P2).
        lock_held = False
        try:
            async with self._plan_lock.hold(tenant_id):
                lock_held = True
                return await self._execute_locked(
                    tenant_id=tenant_id,
                    plan_id=plan_id,
                    expected_ids=expected_ids,
                    expected_truck_id=expected_truck_id,
                    snapshots=snapshots,
                    actor_user_id=actor_user_id,
                    action_id=action_id,
                    approved_at=approved_at,
                    mode=mode,
                    approval_attempt_id=approval_attempt_id,
                )
        except PlanExecutionLockTimeout:
            if lock_held:
                raise
            logger.info(
                "loading plan: tenant lock busy tenant=%s plan=%s action=%s",
                tenant_id, plan_id, action_id,
            )
            return self._in_progress(plan_id)

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    async def _shadow(
        self,
        tenant_id: str,
        plan_id: str,
        plan: Dict[str, Any],
        expected_ids: List[str],
        expected_truck_id: Optional[str],
        snapshots: Dict[str, Mapping[str, Any]],
    ) -> LoadingPlanExecutionResult:
        run_id = str(plan.get("run_id") or "").strip() or plan_id
        pre = await self._preflight(
            tenant_id, plan, plan_id, run_id, expected_ids, expected_truck_id,
            snapshots, plan.get("execution_status"),
        )
        failures = list(pre.failures)
        if pre.plan_failure:
            failures = [{"order_id": None, "reason": pre.plan_failure}]
        would_apply = [
            {
                "order_id": oid,
                "from_status": status,
                "target_status": "scheduled",
                "steps": list(steps),
            }
            for oid, (status, steps, _order) in sorted(pre.to_apply.items())
        ]
        logger.info(
            "loading plan: shadow tenant=%s plan=%s would_apply=%d failures=%d",
            tenant_id, plan_id, len(would_apply), len(failures),
        )
        return LoadingPlanExecutionResult(
            outcome="shadow",
            success=False,
            replay=False,
            plan_id=plan_id,
            run_id=run_id,
            truck_id=_opt_str(plan.get("truck_id")),
            order_ids=list(pre.order_ids),
            applied_order_ids=sorted(pre.applied),
            settled_order_ids=sorted(pre.settled),
            would_apply=would_apply,
            failures=failures,
            reason=REASON_SHADOW_MODE,
            retryable=pre.prior_writes,
            writes_made=pre.prior_writes,
            message=_template(REASON_SHADOW_MODE, plan_id),
            attempt_id=None,
        )

    @staticmethod
    def _replay(plan: Mapping[str, Any], plan_id: str) -> LoadingPlanExecutionResult:
        stored = LoadingPlanExecutionResult.from_dict(plan.get("execution_result") or {})
        return dataclasses.replace(
            stored,
            outcome="replayed",
            success=True,
            replay=True,
            plan_id=plan_id,
            reason=None,
            retryable=False,
            failures=[],
            pending_order_ids=[],
            message=REPLAYED_TEMPLATE.format(plan_id=plan_id),
        )

    @staticmethod
    def _in_progress(plan_id: str) -> LoadingPlanExecutionResult:
        return LoadingPlanExecutionResult(
            outcome="in_progress",
            success=False,
            replay=False,
            plan_id=plan_id,
            run_id=None,
            truck_id=None,
            reason=REASON_EXECUTION_IN_PROGRESS,
            retryable=True,
            writes_made=False,
            message=_template(REASON_EXECUTION_IN_PROGRESS, plan_id),
            attempt_id=None,
        )

    async def _execute_locked(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        expected_ids: List[str],
        expected_truck_id: Optional[str],
        snapshots: Dict[str, Mapping[str, Any]],
        actor_user_id: str,
        action_id: Optional[str],
        approved_at: Optional[str],
        mode: str,
        approval_attempt_id: Optional[str] = None,
    ) -> LoadingPlanExecutionResult:
        # 5. Claim the plan with a lease.
        now = self._clock()
        attempt_id = uuid4().hex
        seen: Dict[str, Any] = {"previous": None, "garbage_lease": False}

        def claim(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if current.get("tenant_id") != tenant_id:
                return None
            status = current.get("execution_status")
            if status == "succeeded":
                return None
            if status == "in_progress":
                try:
                    claimed_at = parse_ts(current.get("execution_claimed_at"))
                except (TypeError, ValueError):
                    claimed_at = None
                if claimed_at is None:
                    # Refusing would leave the plan unclaimable forever; the
                    # order layer still guards every write.
                    seen["garbage_lease"] = True
                elif claimed_at > now - timedelta(seconds=self._lease):
                    return None
            seen["previous"] = status
            updated = {
                **current,
                "execution_status": "in_progress",
                "execution_attempt_id": attempt_id,
                "execution_claimed_at": now.isoformat(),
                "execution_action_id": action_id,
            }
            if not str(current.get("run_id") or "").strip():
                updated["run_id"] = plan_id  # R2.1: dispatch's _run_id reads plan.run_id first
            return updated

        try:
            doc, applied = await self._es.atomic_update(MVP_LOAD_PLANS_INDEX, plan_id, claim)
        except Exception:
            logger.exception(
                "loading plan: plan claim failed tenant=%s plan=%s action=%s attempt=%s",
                tenant_id, plan_id, action_id, attempt_id,
            )
            return LoadingPlanExecutionResult.failure(plan_id, REASON_INTERNAL_ERROR, retryable=True)
        if seen["garbage_lease"] and applied:
            logger.warning(
                "loading plan: in_progress plan %s had no parseable execution_claimed_at; reclaimed",
                plan_id,
            )
        if not applied:
            if not doc or doc.get("tenant_id") != tenant_id:
                return LoadingPlanExecutionResult.failure(plan_id, REASON_PLAN_NOT_FOUND, retryable=False)
            if doc.get("execution_status") == "succeeded":
                return self._replay(doc, plan_id)
            logger.info(
                "loading plan: lease held tenant=%s plan=%s action=%s", tenant_id, plan_id, action_id
            )
            return self._in_progress(plan_id)

        plan = doc
        run_id = str(plan.get("run_id") or plan_id)
        truck_id = str(plan.get("truck_id") or "").strip()
        logger.info(
            "loading plan: start tenant=%s plan=%s attempt=%s approval_attempt=%s mode=%s action=%s",
            tenant_id, plan_id, attempt_id, approval_attempt_id, mode, action_id,
        )
        try:
            result, skipped = await self._run_claimed(
                tenant_id=tenant_id,
                plan_id=plan_id,
                plan=plan,
                run_id=run_id,
                truck_id=truck_id,
                expected_ids=expected_ids,
                expected_truck_id=expected_truck_id,
                snapshots=snapshots,
                actor_user_id=actor_user_id,
                action_id=action_id,
                attempt_id=attempt_id,
                previous_status=seen["previous"],
            )
        except Exception:
            # A bug or a store fault outside the per-order handling. The plan
            # claim stays in_progress until its lease runs out; the retry
            # resumes from authoritative state.
            logger.exception(
                "loading plan: unexpected error tenant=%s plan=%s action=%s attempt=%s "
                "approval_attempt=%s",
                tenant_id, plan_id, action_id, attempt_id, approval_attempt_id,
            )
            return LoadingPlanExecutionResult(
                outcome="incomplete",
                success=False,
                replay=False,
                plan_id=plan_id,
                run_id=run_id,
                truck_id=truck_id or None,
                reason=REASON_INTERNAL_ERROR,
                retryable=True,
                writes_made=True,
                message=_template(REASON_INTERNAL_ERROR, plan_id),
                attempt_id=attempt_id,
            )

        # 9. Finalize, guarded on this attempt.
        await self._finalize(
            tenant_id=tenant_id,
            plan_id=plan_id,
            attempt_id=attempt_id,
            result=result,
            actor_user_id=actor_user_id,
            approved_at=approved_at,
        )
        logger.info(
            "loading plan: finish tenant=%s plan=%s attempt=%s approval_attempt=%s outcome=%s "
            "applied=%d skipped=%d settled=%d",
            tenant_id, plan_id, attempt_id, approval_attempt_id, result.outcome,
            len(result.applied_order_ids),
            skipped, len(result.settled_order_ids),
        )
        return result

    async def _run_claimed(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        plan: Dict[str, Any],
        run_id: str,
        truck_id: str,
        expected_ids: List[str],
        expected_truck_id: Optional[str],
        snapshots: Dict[str, Mapping[str, Any]],
        actor_user_id: str,
        action_id: Optional[str],
        attempt_id: str,
        previous_status: Optional[str],
    ) -> Tuple[LoadingPlanExecutionResult, int]:
        """Steps 6-8; returns the result and the count of skipped (applied or settled) orders."""
        # 6. Preflight on the claimed document.
        pre = await self._preflight(
            tenant_id, plan, plan_id, run_id, expected_ids, expected_truck_id,
            snapshots, previous_status,
        )
        result = await self._commit(
            pre,
            tenant_id=tenant_id,
            plan_id=plan_id,
            plan=plan,
            run_id=run_id,
            truck_id=truck_id,
            actor_user_id=actor_user_id,
            action_id=action_id,
            attempt_id=attempt_id,
        )
        return result, len(pre.applied) + len(pre.settled)

    async def _commit(
        self,
        pre: _Preflight,
        *,
        tenant_id: str,
        plan_id: str,
        plan: Dict[str, Any],
        run_id: str,
        truck_id: str,
        actor_user_id: str,
        action_id: Optional[str],
        attempt_id: str,
    ) -> LoadingPlanExecutionResult:
        def build(
            outcome: Outcome,
            *,
            reason: Optional[str],
            retryable: bool,
            writes_made: bool,
            applied: List[str],
            failures: List[dict],
            message: str,
        ) -> LoadingPlanExecutionResult:
            settled = sorted(pre.settled)
            applied_sorted = sorted(set(applied))
            pending = (
                [o for o in pre.order_ids if o not in applied_sorted and o not in settled]
                if outcome == "incomplete"
                else []
            )
            return LoadingPlanExecutionResult(
                outcome=outcome,
                success=outcome == "applied",
                replay=False,
                plan_id=plan_id,
                run_id=run_id,
                truck_id=truck_id or None,
                order_ids=list(pre.order_ids),
                applied_order_ids=applied_sorted,
                settled_order_ids=settled,
                pending_order_ids=pending,
                failures=failures,
                reason=reason,
                retryable=retryable,
                writes_made=writes_made,
                message=message,
                attempt_id=attempt_id,
            )

        # Decision when any failure exists (R3.13): zero writes either way.
        if pre.plan_failure or pre.failures:
            reason = pre.plan_failure or pre.failures[0]["reason"]
            logger.warning(
                "loading plan: preflight refused tenant=%s plan=%s attempt=%s reason=%s failures=%s",
                tenant_id, plan_id, attempt_id, reason,
                [(f.get("order_id"), f.get("reason")) for f in pre.failures],
            )
            if not pre.prior_writes:
                return build(
                    "failed", reason=reason, retryable=False, writes_made=False,
                    applied=pre.applied, failures=pre.failures,
                    message=_template(reason, plan_id),
                )
            blocker = pre.failures[0].get("order_id") if pre.failures else None
            message = (
                INCOMPLETE_BLOCKED_TEMPLATE.format(
                    plan_id=plan_id, k=len(pre.applied), n=len(pre.order_ids), order_id=blocker,
                )
                if blocker
                else _template(reason, plan_id)
            )
            return build(
                "incomplete", reason=reason, retryable=True, writes_made=True,
                applied=pre.applied, failures=pre.failures, message=message,
            )

        # 7. Commit (K2), orders sorted by order_id.
        applied = list(pre.applied)
        wrote = False
        for order_id in sorted(pre.to_apply):
            status, steps, seen_order = pre.to_apply[order_id]
            try:
                # The preflight read's timestamp rides into the claim CAS so a
                # same-status edit (quantity, tank, product) landing between
                # preflight and this claim is refused, not applied (R3.11).
                claim = await self._repo.claim_assignment(
                    tenant_id,
                    order_id,
                    run_id=run_id,
                    asset_id=truck_id,
                    expected_status=status,
                    claim_id=attempt_id,
                    expected_last_event_timestamp=seen_order.get("last_event_timestamp"),
                )
            except Exception:
                logger.exception(
                    "loading plan: order claim failed tenant=%s plan=%s action=%s attempt=%s order=%s",
                    tenant_id, plan_id, action_id, attempt_id, order_id,
                )
                # A raised claim is ambiguous: the row may have committed
                # before the error reached us. Release by ownership (FREEZE
                # rule 2); a no-op when the claim never landed. If the
                # release raises too, the link state is unknown, so hold the
                # plan as incomplete with writes_made (both rejects refused)
                # until a retry classifies the order.
                released = await self._release_raised_claim(
                    tenant_id, order_id, run_id=run_id, truck_id=truck_id,
                    attempt_id=attempt_id, plan_id=plan_id,
                )
                return self._write_failure(
                    build, REASON_WRITE_FAILED, order_id, applied,
                    first_write=released and not wrote and not pre.prior_writes,
                    raised=True, plan_id=plan_id,
                )
            if claim.outcome == "refused":
                reason = claim.reason or REASON_ORDER_COMMITTED_ELSEWHERE
                logger.warning(
                    "loading plan: order claim refused tenant=%s plan=%s attempt=%s order=%s reason=%s",
                    tenant_id, plan_id, attempt_id, order_id, reason,
                )
                return self._write_failure(
                    build, reason, order_id, applied,
                    first_write=not wrote and not pre.prior_writes, raised=False,
                    plan_id=plan_id,
                )
            if claim.outcome == "linked":
                wrote = True
            order = dict(claim.order)
            try:
                if await self._append_assignment_event_once(
                    tenant_id=tenant_id,
                    order=order,
                    plan=plan,
                    plan_id=plan_id,
                    run_id=run_id,
                    truck_id=truck_id,
                    actor_user_id=actor_user_id,
                    action_id=action_id,
                    attempt_id=attempt_id,
                ):
                    wrote = True
                extra = {
                    "plan_id": plan_id,
                    "run_id": run_id,
                    "action_id": action_id,
                    "attempt_id": attempt_id,
                }
                for step in steps:
                    if step == "link":
                        await self._link_only(tenant_id, order)
                    else:
                        await self._order_service.apply_status_transition(
                            order=order,
                            new_status=step,
                            reason="loading_plan_approved",
                            actor_user_id=actor_user_id,
                            event_payload_extra=extra,
                            guard_stored_state=True,
                        )
                    wrote = True
            except OrderChangedConcurrentlyError:
                logger.warning(
                    "loading plan: order changed concurrently tenant=%s plan=%s attempt=%s order=%s",
                    tenant_id, plan_id, attempt_id, order_id,
                )
                return self._write_failure(
                    build, REASON_ORDER_CHANGED_CONCURRENTLY, order_id, applied,
                    first_write=False, raised=False, plan_id=plan_id,
                )
            except OrderWriteDiscardedError:
                logger.warning(
                    "loading plan: order write discarded tenant=%s plan=%s attempt=%s order=%s",
                    tenant_id, plan_id, attempt_id, order_id,
                )
                return self._write_failure(
                    build, REASON_ORDER_WRITE_DISCARDED, order_id, applied,
                    first_write=False, raised=False, plan_id=plan_id,
                )
            except Exception:
                logger.exception(
                    "loading plan: order write failed tenant=%s plan=%s action=%s attempt=%s order=%s",
                    tenant_id, plan_id, action_id, attempt_id, order_id,
                )
                return self._write_failure(
                    build, REASON_WRITE_FAILED, order_id, applied,
                    first_write=False, raised=False, plan_id=plan_id,
                )
            applied.append(order_id)

        writes_made = pre.prior_writes or wrote

        # 8. Projection check (K6).
        projection_reason = await self._projection_check(tenant_id, run_id, sorted(set(applied)))
        if projection_reason is not None:
            return build(
                "incomplete", reason=projection_reason, retryable=True, writes_made=True,
                applied=applied, failures=[], message=_template(projection_reason, plan_id),
            )

        return build(
            "applied", reason=None, retryable=False, writes_made=writes_made,
            applied=applied, failures=[],
            message=APPLIED_TEMPLATE.format(
                plan_id=plan_id,
                applied=len(set(applied)),
                total=len(pre.order_ids),
                truck_id=truck_id,
            ),
        )

    async def _release_raised_claim(
        self,
        tenant_id: str,
        order_id: str,
        *,
        run_id: str,
        truck_id: str,
        attempt_id: str,
        plan_id: str,
    ) -> bool:
        """Release this attempt's link on ``order_id``; ``False`` if that raised."""
        try:
            await self._repo.release_assignment(
                tenant_id, order_id, run_id=run_id, asset_id=truck_id, claim_id=attempt_id,
            )
        except Exception:
            logger.exception(
                "loading plan: releasing raised claim failed tenant=%s plan=%s attempt=%s order=%s",
                tenant_id, plan_id, attempt_id, order_id,
            )
            return False
        return True

    @staticmethod
    def _write_failure(
        build: Callable[..., LoadingPlanExecutionResult],
        reason: str,
        order_id: str,
        applied: List[str],
        *,
        first_write: bool,
        raised: bool,
        plan_id: str,
    ) -> LoadingPlanExecutionResult:
        """K6 failure rules, including the no-prior-writes exception."""
        failures = [{"order_id": order_id, "reason": reason}]
        if first_write:
            # Nothing written in this plan yet: a refused claim is a clean
            # failure; a raised one may be transient.
            return build(
                "failed", reason=reason, retryable=raised, writes_made=False,
                applied=applied, failures=failures, message=_template(reason, plan_id),
            )
        return build(
            "incomplete", reason=reason, retryable=True, writes_made=True,
            applied=applied, failures=failures, message=_template(reason, plan_id),
        )

    # ------------------------------------------------------------------
    # Preflight and classification (K3)
    # ------------------------------------------------------------------

    async def _preflight(
        self,
        tenant_id: str,
        plan: Mapping[str, Any],
        plan_id: str,
        run_id: str,
        expected_ids: List[str],
        expected_truck_id: Optional[str],
        snapshots: Mapping[str, Mapping[str, Any]],
        previous_status: Optional[str],
    ) -> _Preflight:
        pre = _Preflight()
        assignments = [a for a in (plan.get("assignments") or []) if isinstance(a, Mapping)]
        plan_order_ids = sorted({str(a["order_id"]) for a in assignments if a.get("order_id")})
        pre.order_ids = plan_order_ids
        truck_id = str(plan.get("truck_id") or "").strip()

        # Reads only. Orders come from es_documents (get_current), never the
        # hybrid projection, so a lagging projection cannot re-transition.
        orders: Dict[str, Optional[Dict[str, Any]]] = {}
        for order_id in plan_order_ids:
            orders[order_id] = await self._repo.get_current(tenant_id, order_id)

        def mine(order: Mapping[str, Any]) -> bool:
            return (
                _link(order.get("assigned_run_id")) == run_id
                and _link(order.get("assigned_asset_id")) == truck_id
            )

        # Sticky writes_made (K3): from the reads alone.
        pre.prior_writes = previous_status in ("incomplete", "in_progress") or (
            bool(truck_id) and any(o is not None and mine(o) for o in orders.values())
        )

        # Plan-level checks; the first failure ends preflight.
        if plan.get("status") == "rejected":
            pre.plan_failure = REASON_PLAN_REJECTED
        elif not str(plan.get("plan_id") or "").strip() or not truck_id:
            pre.plan_failure = REASON_PLAN_IDENTITY_INCOMPLETE
        elif any(not a.get("order_id") for a in assignments):
            pre.plan_failure = REASON_ASSIGNMENT_WITHOUT_ORDER
        elif plan_order_ids != expected_ids or (
            expected_truck_id and str(expected_truck_id) != truck_id
        ):
            pre.plan_failure = REASON_PLAN_CHANGED
        if pre.plan_failure:
            return pre

        by_order: Dict[str, List[Mapping[str, Any]]] = {}
        for a in assignments:
            by_order.setdefault(str(a["order_id"]), []).append(a)

        # Once prior_writes is true, R4.7 governs; a terminal plan order is
        # resolved for this plan and is settled, which is how R3.7's
        # already-applied-by-this-plan is read on a resumed plan.
        for order_id in plan_order_ids:
            order = orders[order_id]
            if order is None:
                pre.failures.append({"order_id": order_id, "reason": REASON_ORDER_NOT_FOUND})
                continue
            status = order.get("status")
            run = _link(order.get("assigned_run_id"))
            asset = _link(order.get("assigned_asset_id"))
            linked_here = run == run_id and asset == truck_id
            if status in _APPLIED_STATUSES and linked_here:
                pre.applied.append(order_id)
                continue
            if status in _LINKED_SETTLED_STATUSES and linked_here:
                pre.settled.append(order_id)
                continue
            if status in TERMINAL_STATUSES and run is None and asset is None and pre.prior_writes:
                pre.settled.append(order_id)
                continue
            if run not in (None, run_id) or asset not in (None, truck_id):
                pre.failures.append({"order_id": order_id, "reason": REASON_ORDER_COMMITTED_ELSEWHERE})
                continue
            if status not in _LOADABLE_STATUSES:
                pre.failures.append(
                    {"order_id": order_id, "reason": REASON_ORDER_NOT_LOADABLE, "status": status}
                )
                continue
            changed = self._changed_field(order, by_order.get(order_id, []), snapshots.get(order_id))
            if changed:
                pre.failures.append(
                    {"order_id": order_id, "reason": REASON_ORDER_CHANGED_SINCE_PLAN, "field": changed}
                )
                continue
            if status in ("placed", "confirmed"):
                try:
                    assert_window_present_for_transition(order, "scheduled")
                except Exception:
                    pre.failures.append({"order_id": order_id, "reason": REASON_MISSING_DELIVERY_WINDOW})
                    continue
            pre.to_apply[order_id] = (status, _STEPS[status], order)
        return pre

    @staticmethod
    def _changed_field(
        order: Mapping[str, Any],
        assignments: Sequence[Mapping[str, Any]],
        snapshot: Optional[Mapping[str, Any]],
    ) -> Optional[str]:
        order_product = _canon(order.get("product_code"))
        customer = order.get("customer_id") or None
        for a in assignments:
            planned = a.get("product_code") or a.get("fuel_grade")
            if planned and _canon(planned) != order_product:
                return "product_code"
            if (a.get("station_id") or None) != customer:
                return "customer_id"
        if isinstance(snapshot, Mapping):
            for name in _SNAPSHOT_FIELDS:
                if name not in snapshot:
                    continue
                expected, actual = snapshot.get(name), order.get(name)
                if name == "gallons_requested":
                    if not _gallons_equal(expected, actual):
                        return name
                elif name == "fill_to_full":
                    if bool(expected) != bool(actual):
                        return name
                elif (expected or None) != (actual or None):
                    return name
        return None

    # ------------------------------------------------------------------
    # Commit helpers (K2)
    # ------------------------------------------------------------------

    async def _append_assignment_event_once(
        self,
        *,
        tenant_id: str,
        order: Mapping[str, Any],
        plan: Mapping[str, Any],
        plan_id: str,
        run_id: str,
        truck_id: str,
        actor_user_id: str,
        action_id: Optional[str],
        attempt_id: str,
    ) -> bool:
        """Append ``order_assigned`` unless this plan already recorded it (resume dedupe)."""
        order_id = str(order["order_id"])
        events = await self._repo.get_events_for_order(tenant_id, order_id)
        for event in events:
            payload = getattr(event, "event_payload", None) or {}
            if getattr(event, "event_type", None) == "order_assigned" and payload.get("plan_id") == plan_id:
                return False
        allocations = [
            {
                "compartment_id": a.get("compartment_id"),
                "product_code": a.get("product_code") or a.get("fuel_grade"),
                "quantity_liters": a.get("quantity_liters"),
            }
            for a in (plan.get("assignments") or [])
            if isinstance(a, Mapping) and str(a.get("order_id") or "") == order_id
        ]
        now = self._clock()
        await self._repo.append_event(
            tenant_id,
            {
                "event_id": mint_event_id(),
                "order_id": order_id,
                "tenant_id": tenant_id,
                "event_type": "order_assigned",
                "event_payload": {
                    "plan_id": plan_id,
                    "run_id": run_id,
                    "asset_id": truck_id,
                    "actor_user_id": actor_user_id,
                    "action_id": action_id,
                    "attempt_id": attempt_id,
                    "source": "loading_plan_executor",
                    "allocations": allocations,
                },
                "event_timestamp": now,
                "ingested_at": now,
                "source_schema_version": order.get("source_schema_version", "1.0"),
                "trace_id": order.get("trace_id", ""),
            },
        )
        return True

    async def _link_only(self, tenant_id: str, order: Dict[str, Any]) -> None:
        """R2.4: a scheduled, unlinked order gets its links with no status change."""
        prior_ts = order.get("last_event_timestamp")
        now = self._clock()
        order["updated_at"] = now
        order["last_event_timestamp"] = now
        stored = await self._repo.upsert_with_last_event_timestamp(
            tenant_id,
            order,
            expected_status="scheduled",
            expected_last_event_timestamp=prior_ts,
        )
        order["last_event_timestamp"] = stored["last_event_timestamp"]
        order["updated_at"] = stored["updated_at"]

    # ------------------------------------------------------------------
    # Projection check (K6) and finalize (K4 step 9)
    # ------------------------------------------------------------------

    async def _projection_check(
        self, tenant_id: str, run_id: str, applied: List[str]
    ) -> Optional[str]:
        from commerce.services import commerce_persistence_bridge as bridge

        if not applied or not bridge.read_from_postgres():
            return None

        async def visible(order_id: str) -> bool:
            model = await self._repo.get(tenant_id, order_id)
            return (
                model is not None
                and _link(getattr(model, "assigned_run_id", None)) == run_id
                and getattr(model, "status", None) in _APPLIED_STATUSES
            )

        for order_id in applied:
            if await visible(order_id):
                continue
            if not bridge.dual_write_enabled():
                logger.error(
                    "loading plan: order read model is cut over to Postgres but dual-write "
                    "is off; order=%s tenant=%s is not visible (projection_mirror_disabled)",
                    order_id, tenant_id,
                )
                return REASON_PROJECTION_MIRROR_DISABLED
            current = await self._repo.get_current(tenant_id, order_id)
            if current is not None:
                await bridge.mirror_current_state_upsert("fuel_order", current)
            if not await visible(order_id):
                logger.warning(
                    "loading plan: projection still lags for order=%s tenant=%s", order_id, tenant_id
                )
                return REASON_PROJECTION_LAG
        return None

    async def _finalize(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        attempt_id: str,
        result: LoadingPlanExecutionResult,
        actor_user_id: str,
        approved_at: Optional[str],
    ) -> None:
        final_status = {
            "applied": "succeeded",
            "incomplete": "incomplete",
            "failed": "failed",
        }.get(result.outcome, "incomplete")
        now_iso = self._clock().isoformat()
        stored_result = result.as_dict()

        def finalize(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if current.get("tenant_id") != tenant_id:
                return None
            if current.get("execution_attempt_id") != attempt_id:
                return None
            updated = {
                **current,
                "execution_status": final_status,
                "execution_result": stored_result,
                "updated_at": now_iso,
            }
            if final_status == "succeeded":
                updated["applied_by"] = actor_user_id
                updated["applied_at"] = now_iso
                updated["execution_approved_at"] = approved_at or now_iso
                # NIT 2: only when an order was applied and the plan is still
                # a proposal (a dispatched plan keeps its status).
                if result.applied_order_ids and current.get("status") in ("draft", "proposed"):
                    updated["status"] = "scheduled"
            return updated

        try:
            _doc, applied = await self._es.atomic_update(MVP_LOAD_PLANS_INDEX, plan_id, finalize)
        except Exception:
            logger.exception(
                "loading plan: finalize failed tenant=%s plan=%s attempt=%s", tenant_id, plan_id, attempt_id
            )
            return
        if not applied:
            logger.warning(
                "loading plan: finalize lost to a reclaimer tenant=%s plan=%s attempt=%s",
                tenant_id, plan_id, attempt_id,
            )


__all__ = [
    "APPLIED_TEMPLATE",
    "INCOMPLETE_BLOCKED_TEMPLATE",
    "LOADING_FLAG_KEY",
    "LoadingPlanExecutionResult",
    "LoadingPlanExecutor",
    "MESSAGE_TEMPLATES",
    "Outcome",
    "REASON_APPROVAL_RELEASED_DURING_EXECUTION",
    "REASON_ASSIGNMENT_WITHOUT_ORDER",
    "REASON_EXECUTION_IN_PROGRESS",
    "REASON_EXECUTOR_UNAVAILABLE",
    "REASON_EXPIRED_BEFORE_APPROVAL",
    "REASON_INTERNAL_ERROR",
    "REASON_LEGACY_APPROVAL",
    "REASON_MISSING_DELIVERY_WINDOW",
    "REASON_MODE_UNAVAILABLE",
    "REASON_ORDER_CHANGED_CONCURRENTLY",
    "REASON_ORDER_CHANGED_SINCE_PLAN",
    "REASON_ORDER_COMMITTED_ELSEWHERE",
    "REASON_ORDER_NOT_FOUND",
    "REASON_ORDER_NOT_LOADABLE",
    "REASON_ORDER_WRITE_DISCARDED",
    "REASON_PLAN_CHANGED",
    "REASON_PLAN_IDENTITY_INCOMPLETE",
    "REASON_PLAN_NOT_FOUND",
    "REASON_PLAN_REJECTED",
    "REASON_PROJECTION_LAG",
    "REASON_PROJECTION_MIRROR_DISABLED",
    "REASON_SHADOW_MODE",
    "REASON_WRITE_FAILED",
    "REPLAYED_TEMPLATE",
]
