"""
Approval Queue Service for managing pending agent actions.

Manages the lifecycle of agent-proposed actions requiring human approval.
Entries are stored in the `agent_approval_queue` Elasticsearch index with
status tracking.

State machine (every tool):
    pending → approved → executed
    pending → rejected
    pending → expired

``apply_loading_plan`` approvals (loading-plan-executor design K7) add:
    approved   → executed | shadowed | failed | incomplete
                 | approved (stale reclaim) | rejected (legacy entries only)
    incomplete → approved (retry) | rejected (only while nothing was written)
    failed     → rejected

Every write is a compare-and-set (``_update_with_concurrency`` or an
``atomic_update`` transform), so concurrent approve/reject/expire calls
cannot overwrite each other. Broadcasts approval events via WebSocket on
every state change.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8
"""
import dataclasses
import time
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fuel.services.loading_plan_executor import (
    REASON_APPROVAL_RELEASED_DURING_EXECUTION,
    REASON_EXECUTION_IN_PROGRESS,
    REASON_EXPIRED_BEFORE_APPROVAL,
    REASON_INTERNAL_ERROR,
    REASON_LEGACY_APPROVAL,
    LoadingPlanExecutionResult,
)
from persistence.timestamps import parse_ts

logger = logging.getLogger(__name__)

# Valid status transitions for the approval lifecycle (K7 adds the loading rows).
VALID_TRANSITIONS = {
    "pending": {"approved", "rejected", "expired"},
    "approved": {
        "executed", "shadowed", "failed", "incomplete",
        "approved",   # stale reclaim
        "rejected",   # legacy loading entries only
    },
    "incomplete": {"approved", "rejected"},
    "failed": {"rejected"},
}

#: Per-truck loading-plan proposals from CompartmentLoadingAgent.
LOADING_PLAN_TOOL = "apply_loading_plan"
#: Loading approvals the supersede guard reads (K7, R5.2).
_LIVE_STATUSES = ("pending", "approved", "executed", "incomplete")
#: Modes in which an approved plan writes orders; only these expire competitors (R5.1).
EXECUTING_MODES = frozenset({"disabled", "active_gated", "active_auto"})
#: An ``approved`` loading entry whose attempt claimed less than this ago is running.
APPROVAL_LEASE_SECONDS = 120
#: Page size of the supersede guard's search (paged with ``search_after``).
_OVERLAP_PAGE_SIZE = 500

#: Route approvals queued once per (tenant, plan_id, truck_id) (N-new-2). Literal
#: names (importing route_planning_agent here would pull the overlay stack in);
#: a test pins them to APPLY_ROUTE_PLAN_TOOL / APPLY_ROUTE_PLAN_STORM_MODE_TOOL.
ROUTE_APPROVAL_TOOLS = frozenset({"apply_route_plan", "apply_route_plan_storm_mode"})
#: Fixed namespace for the deterministic route approval action ids.
_ROUTE_APPROVAL_NS = uuid.UUID("5d0c7a3e-8f41-4b9a-9c2e-1f6b2d7e4a90")


def _idempotent_action_id(request) -> Optional[str]:
    """Deterministic action id for a route approval, else ``None`` (N-new-2).

    Storm and non-storm share one key, so the first proposal for a plan and
    truck wins. ``plan_id`` is minted per loading run, so a later run never
    collides with an earlier entry.
    """
    if request.tool_name not in ROUTE_APPROVAL_TOOLS:
        return None
    params = request.parameters or {}
    plan_id = params.get("plan_id")
    truck_id = params.get("truck_id")
    if not plan_id or not truck_id:
        return None
    return str(
        uuid.uuid5(
            _ROUTE_APPROVAL_NS, f"route|{request.tenant_id}|{plan_id}|{truck_id}"
        )
    )


#: Executor outcome -> ``loading_plan_execution`` activity outcome (K9).
_ACTIVITY_OUTCOMES = {
    "applied": "executed",
    "replayed": "replayed",
    "shadow": "shadow",
    "failed": "failed",
    "incomplete": "incomplete",
    "in_progress": "incomplete",
}


def loading_plan_order_ids(parameters) -> set:
    """Order ids an ``apply_loading_plan`` approval would commit.

    ``parameters.order_ids`` when present, else the assignments' ``order_id``
    (proposals queued before ``order_ids`` existed).
    """
    if not isinstance(parameters, dict):
        return set()
    order_ids = parameters.get("order_ids")
    if not order_ids:
        order_ids = [
            a.get("order_id")
            for a in parameters.get("assignments") or []
            if isinstance(a, dict)
        ]
    return {o for o in order_ids if isinstance(o, str) and o}


def _holds_orders(entry: dict) -> bool:
    """Whether a loading approval holds its orders against other plans (K7).

    ``incomplete`` always holds (it may have written). ``approved`` and
    ``executed`` hold only when they carry ``execution_result.attempt_id``:
    entries without one predate the executor ("Unknown tool" / "ES not
    wired" results, or approved-but-never-run) and wrote nothing.
    """
    status = entry.get("status")
    if status == "incomplete":
        return True
    if status in ("approved", "executed"):
        return bool((entry.get("execution_result") or {}).get("attempt_id"))
    return False


def _safe_ts(value: Any) -> Optional[datetime]:
    try:
        return parse_ts(value)
    except (TypeError, ValueError):
        return None


def build_loading_plan_execution_entry(
    entry: dict,
    result: LoadingPlanExecutionResult,
    *,
    actor: str,
    duration_ms: int,
    executor_attempt_id: Optional[str] = None,
) -> dict:
    """The one ``loading_plan_execution`` activity entry per invocation (K9, R10.1).

    ``details.attempt_id`` is the approval's attempt; ``executor_attempt_id``
    is the executor's plan claim (``mvp_load_plans.execution_attempt_id``).
    """
    params = entry.get("parameters") or {}
    plan_id = result.plan_id or params.get("plan_id")
    return {
        "agent_id": entry.get("proposed_by") or "unknown",
        "action_type": "loading_plan_execution",
        "tool_name": LOADING_PLAN_TOOL,
        "parameters": {"plan_id": plan_id},
        "risk_level": entry.get("risk_level"),
        "outcome": _ACTIVITY_OUTCOMES.get(result.outcome, "failed"),
        "duration_ms": int(duration_ms),
        "tenant_id": entry.get("tenant_id"),
        "user_id": actor,
        "details": {
            "action_id": entry.get("action_id"),
            "plan_id": plan_id,
            "run_id": result.run_id,
            "truck_id": result.truck_id,
            "order_ids": list(result.order_ids),
            "applied_order_ids": list(result.applied_order_ids),
            "settled_order_ids": list(result.settled_order_ids),
            "reason": result.reason,
            "failures": [dict(f) for f in result.failures],
            "retryable": result.retryable,
            "writes_made": result.writes_made,
            "attempt_id": result.attempt_id,
            "executor_attempt_id": executor_attempt_id,
        },
    }


class LoadingPlanOverlapError(ValueError):
    """The plan shares an order with a holding approval; the entry stays pending.

    A ``ValueError`` so existing callers and the endpoint mapping are unchanged.
    """


class LoadingPlanExecutionError(Exception):
    """A loading approval did not end executed/shadowed (K10).

    ``entry`` is the approval as stored (or ``None``) and ``result`` the
    structured outcome; ``str(exc)`` is the fixed template text only.
    """

    def __init__(self, entry: Optional[dict], result: LoadingPlanExecutionResult):
        super().__init__(result.message)
        self.entry = entry
        self.result = result

    @classmethod
    def from_reason(
        cls,
        entry: Optional[dict],
        reason: str,
        *,
        retryable: bool,
        writes_made: bool = False,
    ) -> "LoadingPlanExecutionError":
        """legacy_approval, execution_in_progress, approval_released_during_execution."""
        plan_id = ((entry or {}).get("parameters") or {}).get("plan_id")
        return cls(
            entry,
            LoadingPlanExecutionResult.failure(
                plan_id, reason, retryable=retryable, writes_made=writes_made
            ),
        )

    @classmethod
    def from_stored(cls, entry: dict) -> "LoadingPlanExecutionError":
        """A failed/incomplete entry re-raised with the result it already records."""
        return cls(entry, LoadingPlanExecutionResult.from_dict(entry.get("execution_result") or {}))


class ApprovalNotFoundError(ValueError):
    """No approval with this id in the caller's tenant.

    A ``ValueError`` so existing callers keep working; the endpoints catch it
    first and answer 404 ``RESOURCE_NOT_FOUND`` instead of 400. A foreign
    tenant's entry is reported the same way, so existence never leaks.
    """

    def __init__(self, action_id: str):
        super().__init__(f"Approval entry {action_id} not found")
        self.action_id = action_id


class ApprovalExpiredError(Exception):
    """A decision arrived after the approval's ``expiry_time`` (R11.4, N7).

    Raised by :meth:`ApprovalQueueService.approve` and ``reject`` for any tool
    once ``expiry_time`` has passed or the entry is already ``expired``. Not a
    ``ValueError`` (loading-plan design K10, NIT 12): the endpoints catch it
    before their ``ValueError`` clause and answer 409 ``APPROVAL_EXPIRED``.
    """

    def __init__(self, action_id: str, expiry_time: Optional[str] = None):
        super().__init__(f"Approval {action_id} expired before a decision was made")
        self.action_id = action_id
        self.expiry_time = expiry_time


class ApprovalForbiddenError(Exception):
    """A loading plan approval needs a session user or an in-process agent actor (R7.4)."""

    def __init__(self, action_id: str):
        super().__init__("A signed-in user is required to approve a loading plan")
        self.action_id = action_id


def _status_for(result: LoadingPlanExecutionResult, writes_made: bool) -> str:
    """Approval status for an executor outcome and the sticky ``writes_made`` (K7 table)."""
    if result.outcome in ("applied", "replayed"):
        return "executed"
    if result.outcome == "shadow":
        return "incomplete" if writes_made else "shadowed"
    if result.outcome == "failed":
        return "incomplete" if (writes_made or result.retryable) else "failed"
    return "incomplete"  # incomplete, in_progress


class ApprovalQueueService:
    """Manages the lifecycle of pending agent actions requiring human approval.

    Stores approval entries in the `agent_approval_queue` ES index and
    broadcasts state-change events via WebSocket.

    Attributes:
        INDEX: The Elasticsearch index name for approval entries.
    """

    def __init__(
        self,
        es_service,
        ws_manager,
        activity_log_service,
        confirmation_protocol=None,
        feedback_service=None,
    ):
        """Initialise the service with its dependencies.

        Args:
            es_service: ElasticsearchService for persistence.
            ws_manager: WebSocket manager for broadcasting events.
            activity_log_service: Activity log for audit entries.
            confirmation_protocol: Optional back-reference used when
                executing an approved mutation.
            feedback_service: Optional FeedbackService; a rejection is
                recorded as a feedback signal (Req 12.1).
        """
        self._es = es_service
        self._ws = ws_manager
        self._activity_log = activity_log_service
        self._confirmation_protocol = confirmation_protocol
        self._feedback = feedback_service
        self.INDEX = "agent_approval_queue"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(self, request, risk_level, expiry_minutes: int = 60) -> str:
        """Create a pending approval entry.

        Args:
            request: A MutationRequest describing the proposed action.
            risk_level: The classified RiskLevel for the action.
            expiry_minutes: Minutes until the approval expires (default 60).

        Route approvals (:data:`ROUTE_APPROVAL_TOOLS`) are idempotent per
        (tenant, plan_id, truck_id) (N-new-2): the id is deterministic and the
        entry is written create-if-absent, so an existing entry in any status
        is returned as is, never reset and never re-broadcast.

        Returns:
            The action_id (UUID string).
        """
        idempotent_id = _idempotent_action_id(request)
        action_id = idempotent_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        risk_value = risk_level.value if hasattr(risk_level, "value") else str(risk_level)

        doc = {
            "action_id": action_id,
            "action_type": "mutation",
            "tool_name": request.tool_name,
            "parameters": request.parameters,
            "risk_level": risk_value,
            "proposed_by": request.agent_id,
            "proposed_at": now.isoformat(),
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "expiry_time": (now + timedelta(minutes=expiry_minutes)).isoformat(),
            "impact_summary": self._generate_impact_summary(request),
            "tenant_id": request.tenant_id,
        }

        if idempotent_id is not None:
            # Row-locked create-if-absent: the transform leaves an existing entry alone.
            _doc, created = await self._es.atomic_update(
                self.INDEX, action_id, lambda current: None, upsert=doc
            )
            if not created:
                params = request.parameters or {}
                logger.info(
                    "route approval for plan %s truck %s already queued as %s (tenant=%s)",
                    params.get("plan_id"), params.get("truck_id"), action_id,
                    request.tenant_id,
                )
                return action_id
        else:
            await self._es.index_document(self.INDEX, action_id, doc)

        # Broadcast creation event via WebSocket
        await self._broadcast("approval_created", doc)

        logger.info(
            f"Created approval entry {action_id} for {request.tool_name} "
            f"(risk={risk_value}, tenant={request.tenant_id})"
        )
        return action_id

    async def approve(
        self,
        action_id: str,
        reviewer_id: str,
        *,
        tenant_id: Optional[str] = None,
        session_user_id: Optional[str] = None,
        agent_actor: Optional[str] = None,
    ) -> dict:
        """Approve a pending action and execute it.

        ``tenant_id`` (always passed by the endpoint and the auto path) scopes
        the lookup: another tenant's entry is reported exactly like a missing
        one (R7.1).

        ``apply_loading_plan`` approvals follow the loading lifecycle
        (:meth:`_approve_loading_plan`, design K7). Their actor is
        ``session_user_id`` or the in-process ``agent_actor``; ``reviewer_id``
        is never used as the actor (R7.3, R7.4). Other tools keep the path
        below unchanged.

        Args:
            action_id: The UUID of the approval entry.
            reviewer_id: The ID of the user approving the action.
            tenant_id: The caller's verified tenant, when known.
            session_user_id: The verified session user (endpoint only).
            agent_actor: ``agent:<id>`` for the auto path (in-process only).

        Returns:
            The updated approval entry dict.

        Raises:
            ValueError: If the entry is missing, another tenant's, or not
                approvable (``LoadingPlanOverlapError`` is a subclass).
            ApprovalExpiredError: ``expiry_time`` has passed (any tool, N7;
                a past-due ``pending`` entry is marked ``expired`` first and
                never executed) or a non-loading entry is already ``expired``.
            LoadingPlanExecutionError, ApprovalForbiddenError: loading
                approvals only (K10).
        """
        entry = await self._get_entry(action_id)
        if tenant_id is not None and entry.get("tenant_id") != tenant_id:
            raise ApprovalNotFoundError(action_id)

        if entry.get("tool_name") == LOADING_PLAN_TOOL:
            # The loading path checks expiry itself, after its actor check and
            # with its own expired_before_approval record (R11.4). An entry
            # already ``expired`` stays a ValueError there: the overlap guard
            # expires superseded plans, and the auto path relies on it (K10).
            return await self._approve_loading_plan(
                entry, action_id, session_user_id=session_user_id, agent_actor=agent_actor
            )

        # N7: a decision after expiry_time is refused, never executed.
        await self._refuse_if_expired(entry, action_id)

        if entry["status"] != "pending":
            raise ValueError(
                f"Cannot approve action {action_id}: "
                f"current status is '{entry['status']}', expected 'pending'"
            )

        now = datetime.now(timezone.utc)
        update_fields = {
            "status": "approved",
            "reviewed_by": reviewer_id,
            "reviewed_at": now.isoformat(),
        }

        await self._update_with_concurrency(
            action_id, update_fields, expected_status="pending"
        )
        entry.update(update_fields)

        # Broadcast approval event
        await self._broadcast("approval_approved", entry)

        # Execute the mutation if confirmation protocol is wired
        execution_result = None
        if self._confirmation_protocol:
            try:
                execution_result = await self._execute_approved_action(entry)
                exec_update = {
                    "status": "executed",
                    "execution_result": {
                        "success": True,
                        "result": str(execution_result),
                    },
                }
                # No status guard: this records the result of the approval
                # this same call just made, so "did someone else change it" is
                # not the question — and re-reading only to assert a sequence
                # number taken microseconds earlier never protected anything.
                await self._update_with_concurrency(
                    action_id, exec_update, expected_status=None
                )
                entry.update(exec_update)
            except Exception as e:
                logger.exception("Failed to execute approved action %s", action_id)
                exec_update = {
                    "status": "executed",
                    "execution_result": {
                        "success": False,
                        "error": str(e),
                    },
                }
                await self._update_with_concurrency(
                    action_id, exec_update, expected_status=None
                )
                entry.update(exec_update)

        # Audit the approval (F7). No feedback signal: the design defines only
        # rejection / override / correction signals (Req 12.3).
        if self._activity_log:
            executed = entry.get("status") == "executed"
            try:
                await self._log_approved(entry, action_id, reviewer_id, executed)
            except Exception:
                # The approval (and any execution) already happened; a lost
                # audit write must not turn it into a 500.
                logger.warning(
                    "Failed to log approval of %s", action_id, exc_info=True
                )

        logger.info(
            f"Approved action {action_id} by {reviewer_id}"
        )
        return entry

    # ------------------------------------------------------------------
    # Loading-plan approvals (design K7, K9, K10)
    # ------------------------------------------------------------------

    async def _approve_loading_plan(
        self,
        entry: dict,
        action_id: str,
        *,
        session_user_id: Optional[str],
        agent_actor: Optional[str],
    ) -> dict:
        """K7 steps 3-7 for an ``apply_loading_plan`` approval."""
        actor = session_user_id or agent_actor
        if not actor:
            raise ApprovalForbiddenError(action_id)

        status = entry.get("status")
        stored = entry.get("execution_result") or {}
        now = datetime.now(timezone.utc)

        # 3. Short-circuits that start no attempt.
        if status in ("executed", "shadowed"):
            return entry  # R4.4: no executor call, no writes
        if status == "failed":
            raise LoadingPlanExecutionError.from_stored(entry)
        if status not in ("pending", "approved", "incomplete"):
            raise ValueError(
                f"Cannot approve action {action_id}: "
                f"current status is '{status}', expected 'pending'"
            )
        if status == "pending" and self._is_past_expiry(entry, action_id, now):
            await self._expire_lazily(
                entry, action_id, now,
                extra_fields={
                    "execution_result": {"reason": REASON_EXPIRED_BEFORE_APPROVAL},
                },
            )
            raise ApprovalExpiredError(action_id, entry.get("expiry_time"))
        if status == "approved":
            if not stored.get("attempt_id"):
                # Approved before the executor existed (OQ7): never run, may be rejected.
                raise LoadingPlanExecutionError.from_reason(
                    entry, REASON_LEGACY_APPROVAL, retryable=False
                )
            claimed_at = _safe_ts(stored.get("claimed_at"))
            if claimed_at is None:
                logger.warning(
                    "loading approval %s: attempt %s has no parseable claimed_at; treated as stale",
                    action_id, stored.get("attempt_id"),
                )
            elif claimed_at > now - timedelta(seconds=APPROVAL_LEASE_SECONDS):
                logger.info(
                    "loading approval %s: attempt %s still running", action_id, stored.get("attempt_id")
                )
                raise LoadingPlanExecutionError.from_reason(
                    entry, REASON_EXECUTION_IN_PROGRESS, retryable=True, writes_made=True
                )

        tenant_id = entry.get("tenant_id")
        params = entry.get("parameters") or {}
        plan_id = params.get("plan_id")
        seen_status = status
        seen_attempt = stored.get("attempt_id")  # None for pending
        started = time.monotonic()

        # 3a. One mode resolution per attempt; no hold when it is unusable.
        protocol = self._confirmation_protocol
        wired = protocol is not None and bool(protocol.has_loading_plan_executor())
        mode = None
        if wired:
            try:
                mode = await protocol.resolve_loading_mode(tenant_id)
            except Exception:
                logger.warning(
                    "loading approval %s: mode resolution raised for tenant=%s",
                    action_id, tenant_id, exc_info=True,
                )
                mode = None
        if not wired or mode is None:
            if not wired:
                logger.error(
                    "loading approval %s: LoadingPlanExecutor not wired (tenant=%s plan=%s); "
                    "executor_unavailable recorded",
                    action_id, tenant_id, plan_id,
                )
                result = LoadingPlanExecutionResult.unavailable(plan_id)
            else:
                logger.warning(
                    "loading approval %s: loading mode unresolvable (tenant=%s plan=%s)",
                    action_id, tenant_id, plan_id,
                )
                result = LoadingPlanExecutionResult.mode_unavailable(plan_id)
            return await self._record_unresolved(
                entry, action_id, result,
                seen_status=seen_status, seen_attempt=seen_attempt,
                actor=actor, agent_actor=agent_actor, started=started,
            )

        # 3b. Hold the orders and start the attempt.
        attempt_id = uuid.uuid4().hex
        claimed_iso = datetime.now(timezone.utc).isoformat()
        first_approval = seen_status == "pending"
        if seen_status == "pending":
            await self._check_loading_overlap(
                tenant_id,
                loading_plan_order_ids(params),
                action_id,
                expire_pending=mode in EXECUTING_MODES,
            )
            in_progress = {
                "state": "in_progress",
                "attempt_id": attempt_id,
                "claimed_at": claimed_iso,
                "actor_user_id": actor,
                "auto_executed": agent_actor is not None,
                "writes_made": False,
            }
            fields = {
                "status": "approved",
                "reviewed_by": actor,
                "reviewed_at": claimed_iso,
                "execution_result": in_progress,
            }
            await self._update_with_concurrency(action_id, fields, expected_status="pending")
            entry = {**entry, **fields}
            await self._broadcast("approval_approved", entry)
        else:
            reclaim = seen_status == "approved"

            def _start(current: dict) -> Optional[dict]:
                if current.get("status") != seen_status:
                    return None
                cur = current.get("execution_result") or {}
                if cur.get("attempt_id") != seen_attempt:
                    return None
                # Sticky writes_made comes from the stored record under the row
                # lock; a stalled attempt being reclaimed may have written.
                writes = True if reclaim else bool(cur.get("writes_made", True))
                return {
                    **current,
                    "status": "approved",
                    "execution_result": {
                        "state": "in_progress",
                        "attempt_id": attempt_id,
                        "claimed_at": claimed_iso,
                        "actor_user_id": actor,
                        "auto_executed": cur.get("auto_executed", agent_actor is not None),
                        "writes_made": writes,
                    },
                }

            doc, applied = await self._es.atomic_update(self.INDEX, action_id, _start)
            if not applied:
                if reclaim:
                    raise LoadingPlanExecutionError.from_reason(
                        entry, REASON_EXECUTION_IN_PROGRESS, retryable=True, writes_made=True
                    )
                raise RuntimeError(
                    f"Concurrent modification detected for action {action_id}. "
                    f"Another user may have already approved or rejected this action."
                )
            entry = doc
        prev = entry.get("execution_result") or {}

        # 4. Execute with the mode resolved above.
        from Agents.confirmation_protocol import MutationRequest

        request = MutationRequest(
            tool_name=entry["tool_name"],
            parameters=params,
            tenant_id=tenant_id,
            agent_id=entry.get("proposed_by") or "unknown",
        )
        try:
            result = await protocol.execute_loading_plan(
                request,
                mode=mode,
                actor_user_id=actor,
                action_id=action_id,
                approved_at=entry.get("reviewed_at"),
                approval_attempt_id=attempt_id,
            )
        except Exception:
            logger.exception(
                "loading approval: unexpected executor error tenant=%s plan=%s action=%s attempt=%s",
                tenant_id, plan_id, action_id, attempt_id,
            )
            result = dataclasses.replace(
                LoadingPlanExecutionResult.failure(
                    plan_id, REASON_INTERNAL_ERROR, retryable=True, writes_made=True
                ),
                outcome="incomplete",
            )

        # 5. Record, with sticky writes_made.
        if result.attempt_id is not None:
            # This call's own plan claim read authoritative state (K3).
            writes_made = bool(result.writes_made)
        else:
            writes_made = bool(
                result.writes_made or prev.get("writes_made") or result.outcome == "in_progress"
            )
        new_status = _status_for(result, writes_made)
        # D10: attempt_id stays the approval's id (_holds_orders and the CAS
        # guards read it); the executor's own id is recorded alongside.
        executor_attempt_id = result.attempt_id
        recorded = dataclasses.replace(result, writes_made=writes_made, attempt_id=attempt_id)
        execution_result = {
            **recorded.as_dict(),
            "approval_attempt_id": attempt_id,
            "executor_attempt_id": executor_attempt_id,
            "actor_user_id": actor,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "auto_executed": prev.get("auto_executed"),
        }

        def _record(current: dict) -> Optional[dict]:
            if current.get("status") != "approved":
                return None
            if (current.get("execution_result") or {}).get("attempt_id") != attempt_id:
                return None
            return {**current, "status": new_status, "execution_result": execution_result}

        doc, applied = await self._es.atomic_update(self.INDEX, action_id, _record)
        lost = not applied
        if lost:
            logger.warning(
                "loading approval %s: record for attempt %s lost (entry moved); re-reading",
                action_id, attempt_id,
            )
            try:
                entry = await self._get_entry(action_id)
            except ValueError:
                entry = doc or entry
        else:
            entry = doc

        # 6. Audit and broadcast (one loading_plan_execution entry per invocation).
        duration_ms = int((time.monotonic() - started) * 1000)
        await self._log_loading_execution(
            entry, recorded, actor, duration_ms, executor_attempt_id=executor_attempt_id
        )
        if first_approval and self._activity_log:
            try:
                await self._log_approved(
                    entry, action_id, actor,
                    executed=entry.get("status") == "executed",
                    execution_success=result.success,
                )
            except Exception:
                logger.warning("Failed to log approval of %s", action_id, exc_info=True)
        await self._broadcast("approval_execution_updated", entry)

        # 7. Return or raise.
        final = entry.get("status")
        if lost:
            if final in ("executed", "shadowed"):
                return entry
            if final in ("failed", "incomplete"):
                raise LoadingPlanExecutionError.from_stored(entry)
            if final == "approved":
                raise LoadingPlanExecutionError.from_reason(
                    entry, REASON_EXECUTION_IN_PROGRESS, retryable=True, writes_made=True
                )
            logger.error("approval %s released while attempt %s was running", action_id, attempt_id)
            raise LoadingPlanExecutionError.from_reason(
                entry, REASON_APPROVAL_RELEASED_DURING_EXECUTION, retryable=False, writes_made=True
            )
        logger.info(
            "loading approval %s: attempt %s (approval_attempt=%s executor_attempt=%s) "
            "by %s -> %s (outcome=%s reason=%s)",
            action_id, attempt_id, attempt_id, executor_attempt_id, actor, final,
            result.outcome, result.reason,
        )
        if final in ("executed", "shadowed"):
            return entry
        raise LoadingPlanExecutionError(entry, recorded)

    async def _record_unresolved(
        self,
        entry: dict,
        action_id: str,
        result: LoadingPlanExecutionResult,
        *,
        seen_status: str,
        seen_attempt: Optional[str],
        actor: str,
        agent_actor: Optional[str],
        started: float,
    ) -> dict:
        """K7 step 3a: record an unusable mode / unwired executor without a hold, then raise."""
        finished = datetime.now(timezone.utc).isoformat()

        def _transform(current: dict) -> Optional[dict]:
            if current.get("status") != seen_status:
                return None
            cur = current.get("execution_result") or {}
            if cur.get("attempt_id") != seen_attempt:
                return None  # another attempt finished in between: its record stands
            # missing => assume written (K7)
            sticky = False if seen_status == "pending" else bool(cur.get("writes_made", True))
            return {
                **current,
                "execution_result": {
                    **result.as_dict(),
                    "writes_made": sticky,
                    "attempt_id": cur.get("attempt_id"),
                    "auto_executed": cur.get("auto_executed", agent_actor is not None),
                    "actor_user_id": actor,
                    "finished_at": finished,
                },
            }

        stored = entry.get("execution_result") or {}
        if seen_status in ("pending", "incomplete"):
            doc, applied = await self._es.atomic_update(self.INDEX, action_id, _transform)
            if not applied:
                logger.info(
                    "loading approval %s: entry moved before the unresolved result was recorded",
                    action_id,
                )
                raise RuntimeError(f"Concurrent modification detected for action {action_id}")
            entry = doc
            stored = entry.get("execution_result") or {}
            recorded = dataclasses.replace(
                result,
                writes_made=bool(stored.get("writes_made")),
                attempt_id=stored.get("attempt_id"),
            )
        else:
            # A stale approved entry is left untouched: its in-progress record
            # must stay CAS-able by the next reclaim.
            recorded = dataclasses.replace(
                result,
                writes_made=bool(stored.get("writes_made", True)),
                attempt_id=stored.get("attempt_id"),
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        await self._log_loading_execution(entry, recorded, actor, duration_ms)
        await self._broadcast("approval_execution_updated", entry)
        raise LoadingPlanExecutionError(entry, recorded)

    # ------------------------------------------------------------------
    # Expiry (sweep and lazy, N7)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_past_expiry(entry: dict, action_id: str, now: datetime) -> bool:
        """``expiry_time <= now``. Missing or unparseable is not expired (logged)."""
        raw = entry.get("expiry_time")
        expiry = _safe_ts(raw)
        if expiry is None:
            if raw not in (None, ""):
                logger.warning(
                    "approval %s: unparseable expiry_time %r; treated as not expired",
                    action_id, raw,
                )
            else:
                logger.info("approval %s has no expiry_time", action_id)
            return False
        return expiry <= now

    async def _expire_entry(
        self,
        entry: dict,
        action_id: str,
        now: datetime,
        *,
        extra_fields: Optional[dict] = None,
    ) -> dict:
        """CAS ``pending -> expired``, then broadcast and audit it.

        The one body shared by :meth:`expire_stale` and the lazy check in
        approve/reject, so both produce the same ``approval_expired`` broadcast
        and activity entry F12's re-proposal relies on.

        Raises:
            RuntimeError: The entry was no longer ``pending``.
        """
        fields = {"status": "expired", "reviewed_at": now.isoformat()}
        fields.update(extra_fields or {})
        await self._update_with_concurrency(action_id, fields, expected_status="pending")
        entry.update(fields)
        await self._broadcast("approval_expired", entry)
        await self._log_expired(entry, action_id)
        return entry

    async def _expire_lazily(
        self,
        entry: dict,
        action_id: str,
        now: datetime,
        *,
        extra_fields: Optional[dict] = None,
    ) -> None:
        """Expire a past-due ``pending`` entry a decision has just reached.

        Losing the race to the sweep (or another reviewer's lazy expiry) is
        fine; any other move under us stays a concurrency conflict.
        """
        try:
            await self._expire_entry(entry, action_id, now, extra_fields=extra_fields)
        except RuntimeError:
            current = await self._get_entry(action_id)
            if current.get("status") != "expired":
                raise
            logger.info("approval %s was expired concurrently", action_id)

    async def _refuse_if_expired(
        self, entry: dict, action_id: str, *, already_expired_raises: bool = True
    ) -> None:
        """Raise :class:`ApprovalExpiredError` for an expired or past-due entry.

        A past-due ``pending`` entry is expired first (sweep side effects).
        ``already_expired_raises=False`` leaves an entry whose status is
        already ``expired`` to the caller's own status check.
        """
        status = entry.get("status")
        if status == "expired" and already_expired_raises:
            raise ApprovalExpiredError(action_id, entry.get("expiry_time"))
        now = datetime.now(timezone.utc)
        if status == "pending" and self._is_past_expiry(entry, action_id, now):
            await self._expire_lazily(entry, action_id, now)
            raise ApprovalExpiredError(action_id, entry.get("expiry_time"))

    async def _log_expired(self, entry: dict, action_id: str) -> None:
        if not self._activity_log:
            return
        await self._activity_log.log({
            "agent_id": entry.get("proposed_by", "unknown"),
            "action_type": "approval_expired",
            "tool_name": entry.get("tool_name"),
            "parameters": entry.get("parameters"),
            "risk_level": entry.get("risk_level"),
            "outcome": "expired",
            "duration_ms": 0,
            "tenant_id": entry.get("tenant_id"),
            "details": {"action_id": action_id},
        })

    async def _log_loading_execution(
        self,
        entry: dict,
        result: LoadingPlanExecutionResult,
        actor: str,
        duration_ms: int,
        *,
        executor_attempt_id: Optional[str] = None,
    ) -> None:
        """Write the K9 entry; a logging failure never changes the outcome (R10.3)."""
        if not self._activity_log:
            return
        try:
            await self._activity_log.log(
                build_loading_plan_execution_entry(
                    entry, result, actor=actor, duration_ms=duration_ms,
                    executor_attempt_id=executor_attempt_id,
                )
            )
        except Exception:
            logger.warning(
                "Failed to log loading plan execution for %s",
                entry.get("action_id"), exc_info=True,
            )

    @staticmethod
    def _loading_plan_order_ids(parameters) -> set:
        """Alias of :func:`loading_plan_order_ids` for existing callers."""
        return loading_plan_order_ids(parameters)

    async def _supersede_overlapping_loading_plans(
        self, entry: dict, action_id: str
    ) -> None:
        """Executing-mode guard for ``entry`` (``expire_pending=True``)."""
        await self._check_loading_overlap(
            entry.get("tenant_id"),
            loading_plan_order_ids(entry.get("parameters")),
            action_id,
            expire_pending=True,
        )

    async def _check_loading_overlap(
        self,
        tenant_id: Optional[str],
        order_ids: set,
        action_id: str,
        *,
        expire_pending: bool,
    ) -> None:
        """Make room for approving one loading plan (F10, K7, R5).

        Plans from one loading run are order-disjoint, so each truck's plan
        can be approved. Across runs they are not: a re-run while older plans
        are still pending proposes the same orders again. Approving one plan
        therefore:

        * refuses (:class:`LoadingPlanOverlapError`, this entry stays pending)
          if another plan that **holds** its orders shares an order with it.
          A plan holds (``_holds_orders``) while it is ``incomplete``, or
          ``approved``/``executed`` with ``execution_result.attempt_id``.
          ``failed`` and ``shadowed`` plans wrote nothing and do not hold
          (R5.3); legacy approved/executed entries without an ``attempt_id``
          predate the executor and do not hold either;
        * otherwise, when ``expire_pending`` (the attempt resolved an
          executing mode, R5.1), expires every pending plan sharing an order,
          recording ``execution_result.superseded_by``. ``expired`` is reused
          because the approval index is ``dynamic: strict`` and a system
          supersede must not look like a reviewer's rejection. Under
          ``shadow`` the guard is check-only, so a shadow approval never
          expires a competitor.

        A pending plan that moves before it can be expired is a conflict too:
        the caller retries rather than racing another reviewer (plain
        ``ValueError``).

        The executor (``fuel.services.loading_plan_executor``) commits an
        approved plan's orders to its truck and run and moves them to
        ``scheduled``; ``CompartmentLoadingAgent`` leaves committed orders out
        of the next run (R8), so the refusal no longer arises on every
        re-proposed order (closes the former "known dead end", R5.6).

        Supersede-then-fail is accepted (K7): when the guard expired
        competitors and the execution then ends ``failed``, they stay
        expired. Their orders are not held by anything, so the next loading
        run proposes them again.

        The search is narrowed to entries naming one of the orders (or
        carrying no ``order_ids``, checked through ``assignments``) and paged
        with ``search_after``; overlap is still computed here.
        """
        if not order_ids:
            return

        query: dict = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"tool_name": LOADING_PLAN_TOOL}},
                        {"terms": {"status": list(_LIVE_STATUSES)}},
                    ],
                    "should": [
                        {"terms": {"parameters.order_ids": sorted(order_ids)}},
                        {"bool": {"must_not": [{"exists": {"field": "parameters.order_ids"}}]}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            "sort": [{"proposed_at": {"order": "asc"}}, {"action_id": {"order": "asc"}}],
            "size": _OVERLAP_PAGE_SIZE,
        }
        overlapping = []
        while True:
            result = await self._es.search_documents(self.INDEX, query)
            hits = result.get("hits", {}).get("hits", [])
            for hit in hits:
                other = hit.get("_source") or {}
                other_id = other.get("action_id")
                if not other_id or other_id == action_id:
                    continue
                # Overlap is computed here rather than queried: ``parameters`` is a
                # dynamic object, not a nested field the store can filter on.
                shared = order_ids & loading_plan_order_ids(other.get("parameters"))
                if shared:
                    overlapping.append((other, shared))
            if len(hits) < _OVERLAP_PAGE_SIZE or not hits[-1].get("sort"):
                break
            query = {**query, "search_after": hits[-1]["sort"]}

        for other, shared in overlapping:
            if _holds_orders(other):
                raise LoadingPlanOverlapError(
                    f"Cannot approve action {action_id}: conflicts with "
                    f"approved loading plan {other['action_id']} for order(s) "
                    f"{', '.join(sorted(shared))}"
                )

        if not expire_pending:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        for other, shared in overlapping:
            if other.get("status") != "pending":
                continue
            other_id = other["action_id"]
            fields = {
                "status": "expired",
                "reviewed_at": now_iso,
                "execution_result": {
                    "superseded_by": action_id,
                    "reason": "conflicting_loading_plan",
                },
            }
            try:
                await self._update_with_concurrency(
                    other_id, fields, expected_status="pending"
                )
            except RuntimeError as exc:
                raise ValueError(
                    f"Cannot approve action {action_id}: overlapping loading "
                    f"plan {other_id} changed while approving; retry"
                ) from exc
            other.update(fields)
            await self._broadcast("approval_expired", other)
            if self._activity_log:
                try:
                    await self._activity_log.log({
                        "agent_id": other.get("proposed_by", "unknown"),
                        "action_type": "approval_superseded",
                        "tool_name": other.get("tool_name"),
                        "parameters": other.get("parameters"),
                        "risk_level": other.get("risk_level"),
                        "outcome": "expired",
                        "duration_ms": 0,
                        "tenant_id": other.get("tenant_id"),
                        "details": {
                            "action_id": other_id,
                            "superseded_by": action_id,
                            "order_ids": sorted(shared),
                        },
                    })
                except Exception:
                    logger.warning(
                        "Failed to log supersede of %s", other_id, exc_info=True
                    )
            logger.info(
                "Approval %s superseded loading plan %s (orders %s)",
                action_id, other_id, ", ".join(sorted(shared)),
            )

    async def _log_approved(
        self,
        entry: dict,
        action_id: str,
        reviewer_id: str,
        executed: bool,
        execution_success: Any = None,
    ) -> None:
        """Write the ``approval_approved`` audit entry for a reviewed action.

        Loading approvals pass ``execution_success=result.success`` so it is
        never true for a failed or shadow outcome (R10.2).
        """
        if execution_success is None and executed:
            execution_success = (entry.get("execution_result") or {}).get("success")
        await self._activity_log.log({
            "agent_id": entry.get("proposed_by", "unknown"),
            "action_type": "approval_approved",
            "tool_name": entry.get("tool_name"),
            "parameters": entry.get("parameters"),
            "risk_level": entry.get("risk_level"),
            "outcome": "approved",
            "duration_ms": 0,
            "tenant_id": entry.get("tenant_id"),
            "user_id": reviewer_id,
            "details": {
                "action_id": action_id,
                "executed": executed,
                "execution_success": execution_success,
            },
        })

    async def reject(
        self,
        action_id: str,
        reviewer_id: str,
        reason: str = "",
        *,
        tenant_id: Optional[str] = None,
    ) -> dict:
        """Reject an action and store a feedback signal.

        Any tool: ``pending``. ``apply_loading_plan`` also: ``failed``,
        ``incomplete`` whose sticky ``execution_result.writes_made`` is false
        (missing counts as true), and legacy ``approved`` entries without an
        ``attempt_id`` (K7). The write is an ``atomic_update`` that re-checks
        the same rule under the row lock.

        Args:
            action_id: The UUID of the approval entry.
            reviewer_id: The ID of the user rejecting the action.
            reason: Optional rejection reason.
            tenant_id: The caller's verified tenant; another tenant's entry
                is reported as not found (R7.1).

        Returns:
            The updated approval entry dict.

        Raises:
            ValueError: Missing, another tenant's, or in a non-rejectable status.
            ApprovalExpiredError: ``expiry_time`` has passed or the entry is
                already ``expired`` (N7); no feedback is recorded.
            RuntimeError: The entry moved (or may have written) under the lock.
        """
        entry = await self._get_entry(action_id)
        if tenant_id is not None and entry.get("tenant_id") != tenant_id:
            raise ApprovalNotFoundError(action_id)

        loading = entry.get("tool_name") == LOADING_PLAN_TOOL
        # N7: a rejection after expiry_time expires the entry instead. A
        # loading plan already ``expired`` (superseded) keeps its K10 400.
        await self._refuse_if_expired(
            entry, action_id, already_expired_raises=not loading
        )
        # Loading statuses whose rejectability depends on the stored record are
        # decided by the CAS below (RuntimeError -> 409 when refused).
        cas_decided = ("pending", "failed", "incomplete", "approved") if loading else ("pending",)
        if entry["status"] not in cas_decided:
            raise ValueError(
                f"Cannot reject action {action_id}: "
                f"current status is '{entry['status']}', expected 'pending'"
            )

        def _rejectable(doc: dict) -> bool:
            status = doc.get("status")
            if status == "pending":
                return True
            if not loading:
                return False
            stored = doc.get("execution_result") or {}
            if status == "failed":
                return True
            if status == "incomplete":
                return not bool(stored.get("writes_made", True))
            if status == "approved":
                return not stored.get("attempt_id")  # legacy only
            return False

        now = datetime.now(timezone.utc)
        update_fields = {
            "status": "rejected",
            "reviewed_by": reviewer_id,
            "reviewed_at": now.isoformat(),
            "rejection_reason": reason,
        }
        seen: dict = {}

        def _reject(current: dict) -> Optional[dict]:
            if not _rejectable(current):
                return None
            seen["status"] = current.get("status")
            return {**current, **update_fields}

        doc, applied = await self._es.atomic_update(self.INDEX, action_id, _reject)
        if not applied:
            logger.info("reject of %s refused: status or writes_made moved", action_id)
            raise RuntimeError(
                f"Concurrent modification detected for action {action_id}. "
                f"Another user may have already approved or rejected this action."
            )
        was_pending = seen.get("status") == "pending"
        entry = doc if isinstance(doc, dict) else {**entry, **update_fields}

        # Broadcast rejection event
        await self._broadcast("approval_rejected", entry)

        # Log rejection to activity log
        if self._activity_log:
            await self._activity_log.log({
                "agent_id": entry.get("proposed_by", "unknown"),
                "action_type": "approval_rejected",
                "tool_name": entry.get("tool_name"),
                "parameters": entry.get("parameters"),
                "risk_level": entry.get("risk_level"),
                "outcome": "rejected",
                "duration_ms": 0,
                "tenant_id": entry.get("tenant_id"),
                "user_id": reviewer_id,
                "details": {"reason": reason, "action_id": action_id},
            })

        # Store the rejection as a feedback signal so agents can learn from
        # it (F7, Req 12.1). Only a rejected proposal is feedback; dismissing a
        # failed/incomplete loading approval is not (K7). A feedback failure
        # never fails the reject.
        if self._feedback and was_pending:
            try:
                await self._feedback.record_rejection(
                    agent_id=entry.get("proposed_by", "unknown"),
                    action_type=entry.get("tool_name"),
                    original_proposal={
                        "action_id": action_id,
                        "tool_name": entry.get("tool_name"),
                        "parameters": entry.get("parameters"),
                        "risk_level": entry.get("risk_level"),
                        "impact_summary": entry.get("impact_summary"),
                    },
                    rejection_reason=reason,
                    user_action={},
                    tenant_id=entry.get("tenant_id"),
                    user_id=reviewer_id,
                )
            except Exception:
                logger.warning(
                    "Failed to record rejection feedback for %s",
                    action_id, exc_info=True,
                )

        logger.info(
            f"Rejected action {action_id} by {reviewer_id}: {reason}"
        )
        return entry

    async def expire_stale(self) -> int:
        """Mark expired approvals whose expiry_time has passed.

        Queries for entries with status="pending" and expiry_time < now,
        updates each to "expired" through a CAS guarded on ``pending`` (K7),
        and logs the expiry to the activity log. An entry that moved under
        the sweeper (approved, rejected or already expired) is skipped: not
        counted, not broadcast, not logged.

        Returns:
            The number of entries that were expired.
        """
        now = datetime.now(timezone.utc)
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"status": "pending"}},
                        {"range": {"expiry_time": {"lt": now.isoformat()}}},
                    ]
                }
            },
            "size": 500,
        }

        result = await self._es.search_documents(self.INDEX, query)
        hits = result.get("hits", {}).get("hits", [])
        expired_count = 0

        for hit in hits:
            entry = hit["_source"]
            action_id = entry["action_id"]
            try:
                try:
                    await self._expire_entry(entry, action_id, now)
                except RuntimeError:
                    logger.info("expire_stale: %s no longer pending; skipped", action_id)
                    continue

                expired_count += 1
            except Exception:
                logger.exception("Failed to expire action %s", action_id)

        if expired_count > 0:
            logger.info(f"Expired {expired_count} stale approval(s)")

        return expired_count

    async def list_pending(
        self,
        tenant_id: str,
        page: int = 1,
        size: int = 20,
        *,
        include_unresolved: bool = False,
    ) -> dict:
        """List pending approvals for a tenant, sorted by proposed_at descending.

        Args:
            tenant_id: The tenant to filter by.
            page: Page number (1-based).
            size: Number of entries per page.
            include_unresolved: Also list ``incomplete`` and ``failed``
                entries and ``approved`` loading plans (a stranded attempt
                stays visible and reclaimable, K7/K13).

        Returns:
            Dict with "items" (list of entries), "total" count, "page", and "size".
        """
        from_offset = (page - 1) * size
        if include_unresolved:
            status_clause: dict = {
                "bool": {
                    "should": [
                        {"terms": {"status": ["pending", "incomplete", "failed"]}},
                        {"bool": {"filter": [
                            {"term": {"status": "approved"}},
                            {"term": {"tool_name": LOADING_PLAN_TOOL}},
                        ]}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        else:
            status_clause = {"term": {"status": "pending"}}
        query = {
            "query": {
                "bool": {
                    "must": [
                        status_clause,
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "sort": [{"proposed_at": {"order": "desc"}}],
            "from": from_offset,
            "size": size,
        }

        result = await self._es.search_documents(self.INDEX, query)
        hits = result.get("hits", {}).get("hits", [])
        total_value = result.get("hits", {}).get("total", {})
        if isinstance(total_value, dict):
            total = total_value.get("value", 0)
        else:
            total = total_value

        items = [hit["_source"] for hit in hits]

        return {
            "items": items,
            "total": total,
            "page": page,
            "size": size,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _generate_impact_summary(self, request) -> str:
        """Generate a human-readable summary of the proposed action.

        Args:
            request: The MutationRequest to summarise.

        Returns:
            A short description of the proposed action.
        """
        tool = request.tool_name
        params = request.parameters

        summaries = {
            "cancel_job": lambda p: (
                f"Cancel job {p.get('job_id', 'unknown')}"
                f"{': ' + p['reason'] if p.get('reason') else ''}"
            ),
            "reassign_rider": lambda p: (
                f"Reassign shipment {p.get('shipment_id', 'unknown')} "
                f"to rider {p.get('new_rider_id', 'unknown')}"
            ),
            "assign_asset_to_job": lambda p: (
                f"Assign asset {p.get('asset_id', 'unknown')} "
                f"to job {p.get('job_id', 'unknown')}"
            ),
            "update_job_status": lambda p: (
                f"Update job {p.get('job_id', 'unknown')} "
                f"status to {p.get('new_status', 'unknown')}"
            ),
            "create_job": lambda p: (
                f"Create {p.get('job_type', '')} job "
                f"from {p.get('origin', 'unknown')} to {p.get('destination', 'unknown')}"
            ),
            "escalate_shipment": lambda p: (
                f"Escalate shipment {p.get('shipment_id', 'unknown')} "
                f"to priority {p.get('priority', 'unknown')}"
            ),
            "request_fuel_refill": lambda p: (
                f"Request {p.get('quantity_liters', '?')}L fuel refill "
                f"for station {p.get('station_id', 'unknown')}"
            ),
            "update_fuel_threshold": lambda p: (
                f"Update fuel threshold for station {p.get('station_id', 'unknown')} "
                f"to {p.get('threshold_pct', '?')}%"
            ),
            "apply_loading_plan": self._loading_plan_summary,
        }

        formatter = summaries.get(tool)
        if formatter:
            try:
                return formatter(params)
            except Exception:
                pass

        # Fallback for unknown tools
        return f"Execute {tool} with parameters: {params}"

    @staticmethod
    def _loading_plan_summary(params: dict) -> str:
        """``Load truck T1: 2 order(s), 7571 L, 84% utilization``.

        Orders are counted by ``order_id`` (``station_id`` for plans written
        before assignments carried one), so a split load counts once.
        """
        assignments = params.get("assignments") or []
        orders = {
            a.get("order_id") or a.get("station_id")
            for a in assignments
            if isinstance(a, dict)
        }
        orders.discard(None)
        liters = sum(
            float(a.get("quantity_liters") or 0)
            for a in assignments
            if isinstance(a, dict)
        )
        util = float(params.get("total_utilization_pct") or 0)
        return (
            f"Load truck {params.get('truck_id', 'unknown')}: "
            f"{len(orders)} order(s), {liters:.0f} L, {util:.0f}% utilization"
        )

    async def _get_entry(self, action_id: str) -> dict:
        """Fetch an approval entry.

        The ES ``_seq_no`` / ``_primary_term`` this used to return alongside the
        document are gone. They existed so the follow-up write could assert
        "nothing changed since I read", and the raw ``client.get`` needed to
        obtain them bypassed the document-store backend switch — after the cutover
        this read would have gone to Elasticsearch while the write went to
        Postgres, which is the worst possible split for a concurrency guard.

        The guard itself is preserved and is now stated in the terms it actually
        protects: see :meth:`_update_with_concurrency`.

        Raises:
            ValueError: If the entry is not found.
        """
        entry = await self._es.get_document(self.INDEX, action_id)
        if entry is None:
            raise ApprovalNotFoundError(action_id)
        return entry

    async def _update_with_concurrency(
        self, action_id: str, fields: dict, expected_status: Optional[str]
    ) -> None:
        """Update an approval entry, refusing if its status moved under us.

        What the ``if_seq_no`` assertion actually protected here was one thing:
        that no other reviewer had changed the entry between this caller's read
        and its write. Every caller reads, checks ``status``, then writes — so
        "the status is still what I read" IS the invariant, and asserting it
        directly is both backend-independent and easier to reason about than a
        sequence number.

        It is also *narrower in the right direction*: a sequence number changes on
        any write, including one that touched an unrelated field, so the old guard
        could reject a legitimate approval because something else had been
        appended to the entry.

        ``expected_status=None`` skips the check, for the follow-up writes that
        record an execution result immediately after the approval they belong to.

        Raises:
            RuntimeError: If another actor changed the status first.
        """
        def _apply(current: dict) -> Optional[dict]:
            if expected_status is not None and current.get("status") != expected_status:
                return None
            merged = dict(current)
            merged.update(fields)
            return merged

        _document, applied = await self._es.atomic_update(
            self.INDEX, action_id, _apply
        )
        if not applied:
            raise RuntimeError(
                f"Concurrent modification detected for action {action_id}. "
                f"Another user may have already approved or rejected this action."
            )

    async def _broadcast(self, event_type: str, data: dict) -> None:
        """Broadcast an approval event via WebSocket.

        Silently catches errors to avoid breaking the approval flow.

        Args:
            event_type: The event type string (e.g. "approval_created").
            data: The event payload.
        """
        if self._ws:
            try:
                await self._ws.broadcast_approval_event(event_type, data)
            except Exception as e:
                logger.warning(f"Failed to broadcast {event_type}: {e}")

    async def _execute_approved_action(self, entry: dict) -> str:
        """Execute an approved mutation via the confirmation protocol.

        Args:
            entry: The approval entry dict.

        Returns:
            The execution result string.
        """
        from Agents.confirmation_protocol import MutationRequest

        request = MutationRequest(
            tool_name=entry["tool_name"],
            parameters=entry["parameters"],
            tenant_id=entry["tenant_id"],
            agent_id=entry["proposed_by"],
        )
        result = await self._confirmation_protocol._execute_mutation(request)
        return result
