"""
Confirmation Protocol for mutation tools.

Central decision engine that determines whether a mutation executes
immediately, waits for brief confirmation, or queues for approval based
on the action's risk level and the tenant's autonomy configuration.

The routing matrix maps (risk_level × autonomy_level) to an execute/queue
decision:
  - suggest-only: all actions queue for approval
  - auto-low: low-risk auto-executes, medium/high queue
  - auto-medium: low+medium auto-execute, high queues
  - full-auto: all actions auto-execute with audit logging

Requirements: 1.4, 1.5, 1.6, 1.7, 1.8, 10.3
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class MutationRequest:
    """Represents a request to execute a mutation tool.

    Attributes:
        tool_name: The mutation tool to invoke.
        parameters: Tool invocation parameters.
        tenant_id: Tenant scope for the mutation.
        agent_id: The agent proposing the mutation.
        user_id: Optional user who triggered the mutation.
        session_id: Optional session context.
    """
    tool_name: str
    parameters: Dict[str, Any]
    tenant_id: str
    agent_id: str
    user_id: Optional[str] = None
    session_id: Optional[str] = None


@dataclass
class MutationResult:
    """Result of processing a mutation through the confirmation protocol.

    Attributes:
        executed: Whether the mutation was executed immediately.
        approval_id: ID of the approval queue entry (if queued).
        result: Execution result string (if executed) or error message.
        risk_level: The classified risk level of the mutation.
        confirmation_method: How the mutation was handled:
            "immediate" - auto-executed based on autonomy level
            "approval_queue" - queued for human approval
            "rejected" - failed business rule validation
    """
    executed: bool
    approval_id: Optional[str] = None
    result: Optional[str] = None
    risk_level: str = "unknown"
    confirmation_method: str = "unknown"


class ConfirmationProtocol:
    """Routes mutations through risk classification and autonomy level checks.

    Wires together the risk registry, business validator, autonomy config,
    approval queue, and activity log to determine the correct handling for
    each mutation request.
    """

    def __init__(
        self,
        risk_registry,
        approval_queue_service,
        autonomy_config_service,
        activity_log_service,
        business_validator,
        es_service=None,
        notification_service=None,
    ):
        self._risk_registry = risk_registry
        self._approval_queue = approval_queue_service
        self._autonomy = autonomy_config_service
        self._activity_log = activity_log_service
        self._validator = business_validator
        self._es = es_service
        self._notification_service = notification_service

    async def process_mutation(self, request: MutationRequest) -> MutationResult:
        """Route a mutation through risk classification and autonomy level checks.

        Steps:
            1. Classify the risk level of the tool
            2. Validate business rules
            3. Check tenant autonomy level against risk
            4. Execute immediately or queue for approval

        Args:
            request: The mutation request to process.

        Returns:
            MutationResult indicating whether the action was executed or queued.
        """
        # 1. Classify risk
        risk_level = await self._risk_registry.classify(
            request.tool_name, tenant_id=request.tenant_id
        )

        # 2. Validate business rules
        validation = await self._validator.validate(
            request.tool_name, request.parameters, request.tenant_id
        )
        if not validation.valid:
            return MutationResult(
                executed=False,
                risk_level=risk_level.value,
                result=f"Validation failed: {validation.reason}",
                confirmation_method="rejected",
            )

        # 3. Check autonomy level
        autonomy = await self._autonomy.get_level(request.tenant_id)
        should_auto_execute = self._should_auto_execute(risk_level, autonomy)

        if should_auto_execute:
            # 4a. Execute immediately
            result = await self._execute_mutation(request)
            await self._activity_log.log_mutation(
                request, risk_level, "immediate", result
            )
            return MutationResult(
                executed=True,
                risk_level=risk_level.value,
                result=result,
                confirmation_method="immediate",
            )
        else:
            # 4b. Check for existing pending action (deduplication)
            try:
                existing = await self._find_pending_duplicate(request)
                if existing:
                    return MutationResult(
                        executed=False,
                        approval_id=existing,
                        risk_level=risk_level.value,
                        confirmation_method="already_queued",
                    )
            except Exception:
                pass  # If dedup check fails, proceed to queue normally

            # 4c. Queue for approval
            approval_id = await self._approval_queue.create(request, risk_level)
            await self._activity_log.log_mutation(
                request, risk_level, "approval_queue", None
            )
            return MutationResult(
                executed=False,
                approval_id=approval_id,
                risk_level=risk_level.value,
                confirmation_method="approval_queue",
            )

    def _should_auto_execute(self, risk_level, autonomy_level: str) -> bool:
        """Determine whether a mutation should auto-execute based on the routing matrix.

        Args:
            risk_level: The classified RiskLevel of the mutation.
            autonomy_level: The tenant's autonomy level string.

        Returns:
            True if the mutation should execute immediately, False if it
            should be queued for approval.
        """
        matrix = {
            "suggest-only": set(),
            "auto-low": {"low"},
            "auto-medium": {"low", "medium"},
            "full-auto": {"low", "medium", "high"},
        }
        return risk_level.value in matrix.get(autonomy_level, set())

    async def _find_pending_duplicate(self, request: MutationRequest):
        """Check if an identical pending action already exists in the approval queue.

        Returns the action_id if a duplicate is found, None otherwise.
        """
        es = self._approval_queue._es
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"status": "pending"}},
                        {"term": {"tool_name": request.tool_name}},
                        {"term": {"proposed_by": request.agent_id}},
                        {"term": {"tenant_id": request.tenant_id}},
                    ]
                }
            },
            "size": 1,
        }
        resp = await es.search_documents("agent_approval_queue", query, size=1)
        hits = resp.get("hits", {}).get("hits", [])
        if hits:
            return hits[0]["_source"].get("action_id")
        return None

    async def _execute_mutation(self, request: MutationRequest) -> str:
        """Execute the actual mutation via Elasticsearch.

        Dispatches the mutation to the appropriate ES index based on
        tool_name. Falls back to a no-op log if no ES service is wired.

        Args:
            request: The mutation request to execute.

        Returns:
            A result message string.
        """
        tool_name = request.tool_name
        params = request.parameters
        tenant_id = request.tenant_id

        # Handle send_customer_notification before the ES check — this
        # branch delegates to NotificationService rather than writing to
        # ES directly.
        if tool_name == "send_customer_notification":
            if self._notification_service is None:
                logger.warning(
                    "ConfirmationProtocol: notification_service not wired, "
                    "cannot execute send_customer_notification for tenant %s",
                    tenant_id,
                )
                return (
                    "Notification dispatch failed: notification_service not configured"
                )

            try:
                notifications = await self._notification_service.notify_event(
                    event_type=params.get("notification_type", "order_status_update"),
                    event_data={
                        "customer_id": params.get("customer_id"),
                        "job_id": params.get("delivery_id"),
                        "channel_override": params.get("channel"),
                        "template_override": params.get("message_template"),
                        "proposal_id": params.get("proposal_id"),
                        **params.get("context", {}),
                    },
                    tenant_id=tenant_id,
                )
                if notifications:
                    notification_ids = [n["notification_id"] for n in notifications]
                    return (
                        f"Dispatched {len(notifications)} notification(s): "
                        f"{','.join(notification_ids)}"
                    )
                return "Notification dispatch failed: no notifications created"
            except Exception as e:
                logger.error(
                    "ConfirmationProtocol: failed to execute %s for tenant %s: %s",
                    tool_name,
                    tenant_id,
                    e,
                )
                return f"Failed to execute {tool_name}: {e}"

        if self._es is None:
            logger.warning(
                "ConfirmationProtocol: no ES service wired, mutation %s "
                "logged but not persisted for tenant %s",
                request.tool_name,
                request.tenant_id,
            )
            return (
                f"Mutation {request.tool_name} approved but ES not wired "
                f"for tenant {request.tenant_id}"
            )

        # Dispatch to tool-specific ES writes
        try:
            if tool_name == "update_job_status":
                await self._es.update_document(
                    "jobs",
                    params["job_id"],
                    {"status": params["new_status"], "tenant_id": tenant_id},
                )
            elif tool_name == "assign_asset_to_job":
                await self._es.update_document(
                    "jobs",
                    params["job_id"],
                    {"assigned_asset_id": params["asset_id"], "tenant_id": tenant_id},
                )
            elif tool_name == "cancel_job":
                await self._es.update_document(
                    "jobs",
                    params["job_id"],
                    {"status": "cancelled", "cancel_reason": params.get("reason", ""), "tenant_id": tenant_id},
                )
            elif tool_name == "create_job":
                import uuid
                job_id = f"JOB_{uuid.uuid4().hex[:8].upper()}"
                await self._es.index_document(
                    "jobs",
                    job_id,
                    {**params, "job_id": job_id, "status": "scheduled", "tenant_id": tenant_id},
                )
            elif tool_name == "reassign_rider":
                await self._es.update_document(
                    "shipments_current",
                    params["shipment_id"],
                    {"rider_id": params["new_rider_id"], "tenant_id": tenant_id},
                )
            elif tool_name == "escalate_shipment":
                await self._es.update_document(
                    "shipments_current",
                    params["shipment_id"],
                    {"priority": params.get("priority", "high"), "tenant_id": tenant_id},
                )
            elif tool_name == "request_fuel_refill":
                import uuid
                refill_id = f"REFILL_{uuid.uuid4().hex[:8].upper()}"
                await self._es.index_document(
                    "fuel_events",
                    refill_id,
                    {
                        "event_type": "refill_request",
                        "station_id": params["station_id"],
                        "quantity_liters": params.get("quantity_liters", 0),
                        "status": "requested",
                        "tenant_id": tenant_id,
                    },
                )
            elif tool_name == "update_fuel_threshold":
                await self._es.update_document(
                    "fuel_stations",
                    params["station_id"],
                    {"threshold_pct": params["threshold_pct"], "tenant_id": tenant_id},
                )
            else:
                logger.warning(
                    "ConfirmationProtocol: unknown tool %s, no ES write performed",
                    tool_name,
                )
                return f"Unknown tool {tool_name} — no mutation executed"

            logger.info(
                "ConfirmationProtocol: executed %s for tenant %s",
                tool_name,
                tenant_id,
            )
            return f"Successfully executed {tool_name} for tenant {tenant_id}"

        except Exception as e:
            logger.error(
                "ConfirmationProtocol: failed to execute %s for tenant %s: %s",
                tool_name,
                tenant_id,
                e,
            )
            return f"Failed to execute {tool_name}: {e}"
