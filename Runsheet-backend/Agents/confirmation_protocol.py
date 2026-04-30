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
    ):
        self._risk_registry = risk_registry
        self._approval_queue = approval_queue_service
        self._autonomy = autonomy_config_service
        self._activity_log = activity_log_service
        self._validator = business_validator

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
        risk_level = await self._risk_registry.classify(request.tool_name)

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
            # 4b. Queue for approval
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

    async def _execute_mutation(self, request: MutationRequest) -> str:
        """Execute the actual mutation (ES write).

        This is a placeholder that returns a success string. The actual
        mutation execution logic will be wired in when mutation tools
        are integrated.

        Args:
            request: The mutation request to execute.

        Returns:
            A success message string.
        """
        logger.info(
            f"Executing mutation: {request.tool_name} "
            f"with params {request.parameters} "
            f"for tenant {request.tenant_id}"
        )
        return (
            f"Successfully executed {request.tool_name} "
            f"for tenant {request.tenant_id}"
        )
