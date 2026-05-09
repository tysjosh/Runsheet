"""
Order-domain AI mutation tools for the fuel order intake pipeline.

Provides tenant-scoped mutation tools that route through the internal
OrderService and DriverRepository (not live HTTP calls). Each tool
registers its risk classification with ConfirmationProtocol:

- ``update_order_status``: MEDIUM risk
- ``assign_driver_to_order``: MEDIUM risk
- ``cancel_order``: HIGH risk

In ``autonomy_level = "suggest-only"`` tenants, every mutation tool
returns a suggestion string rather than executing.

Validates:
- Requirement 6.2: Mutation tools with ConfirmationProtocol routing
- Design §10 — AI tools (mutation)
"""

import json
import logging
import time
from typing import Optional

from strands import tool

from Agents.autonomy_config_service import AutonomyConfigService
from Agents.confirmation_protocol import ConfirmationProtocol, MutationRequest
from Agents.risk_registry import RiskLevel, RiskRegistry

from ._tenant_context import get_current_tenant
from .logging_wrapper import get_telemetry_service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Risk classifications for order mutation tools (Design §10)
# ---------------------------------------------------------------------------

ORDER_MUTATION_RISK_CLASSIFICATIONS: dict[str, RiskLevel] = {
    "update_order_status": RiskLevel.MEDIUM,
    "assign_driver_to_order": RiskLevel.MEDIUM,
    "cancel_order": RiskLevel.HIGH,
}

# ---------------------------------------------------------------------------
# Module-level service references (wired at bootstrap)
# ---------------------------------------------------------------------------

_order_service = None
_order_repo = None
_driver_repo = None
_confirmation_protocol: Optional[ConfirmationProtocol] = None
_autonomy_config_service: Optional[AutonomyConfigService] = None


def configure_order_mutation_tools(
    *,
    order_service,
    order_repo,
    driver_repo,
    confirmation_protocol: ConfirmationProtocol,
    autonomy_config_service: AutonomyConfigService,
) -> None:
    """Wire service dependencies for order mutation tools.

    Called during application bootstrap to inject the internal service
    instances. Tools route through these services directly (not HTTP).

    Args:
        order_service: The OrderService instance for status transitions.
        order_repo: The FuelOrderRepository instance for order lookups.
        driver_repo: The DriverRepository instance for driver lookups.
        confirmation_protocol: The ConfirmationProtocol for risk routing.
        autonomy_config_service: The AutonomyConfigService for tenant
            autonomy level checks.
    """
    global _order_service, _order_repo, _driver_repo
    global _confirmation_protocol, _autonomy_config_service

    _order_service = order_service
    _order_repo = order_repo
    _driver_repo = driver_repo
    _confirmation_protocol = confirmation_protocol
    _autonomy_config_service = autonomy_config_service


def _get_order_service():
    """Return the configured OrderService or raise."""
    if _order_service is None:
        raise RuntimeError(
            "Order mutation tools not configured. "
            "Call configure_order_mutation_tools() during startup."
        )
    return _order_service


def _get_order_repo():
    """Return the configured FuelOrderRepository or raise."""
    if _order_repo is None:
        raise RuntimeError(
            "Order mutation tools not configured. "
            "Call configure_order_mutation_tools() during startup."
        )
    return _order_repo


def _get_driver_repo():
    """Return the configured DriverRepository or raise."""
    if _driver_repo is None:
        raise RuntimeError(
            "Order mutation tools not configured. "
            "Call configure_order_mutation_tools() during startup."
        )
    return _driver_repo


def _get_confirmation_protocol() -> ConfirmationProtocol:
    """Return the configured ConfirmationProtocol or raise."""
    if _confirmation_protocol is None:
        raise RuntimeError(
            "Order mutation tools not configured. "
            "Call configure_order_mutation_tools() during startup."
        )
    return _confirmation_protocol


def _get_autonomy_config_service() -> AutonomyConfigService:
    """Return the configured AutonomyConfigService or raise."""
    if _autonomy_config_service is None:
        raise RuntimeError(
            "Order mutation tools not configured. "
            "Call configure_order_mutation_tools() during startup."
        )
    return _autonomy_config_service


# ---------------------------------------------------------------------------
# Telemetry helper
# ---------------------------------------------------------------------------


def _log_tool_invocation(
    tool_name: str,
    input_params: dict,
    start_time: float,
    success: bool,
    error: str = None,
):
    """Helper to log tool invocations with telemetry service."""
    duration_ms = (time.time() - start_time) * 1000
    telemetry = get_telemetry_service()
    if telemetry:
        telemetry.log_tool_invocation(
            tool_name=tool_name,
            input_params=input_params,
            duration_ms=duration_ms,
            success=success,
            error=error,
        )
        telemetry.record_metric(
            name="tool_invocation_duration_ms",
            value=duration_ms,
            tags={"tool_name": tool_name, "success": str(success).lower()},
        )
        telemetry.record_metric(
            name="tool_invocation_count",
            value=1,
            tags={"tool_name": tool_name, "success": str(success).lower()},
        )


# ---------------------------------------------------------------------------
# Mutation tools
# ---------------------------------------------------------------------------


@tool
async def update_order_status(
    order_id: str,
    new_status: str,
    reason: Optional[str] = None,
    notes: Optional[str] = None,
) -> str:
    """
    Update the status of a fuel order.

    Routes through ConfirmationProtocol with MEDIUM risk classification.
    In suggest-only tenants, returns a suggestion string without executing.

    Tenant-scoped — raises RuntimeError if no tenant is bound.

    Args:
        order_id: The order ID to update.
        new_status: The target status (confirmed, scheduled, dispatched,
                    in_transit, delivered, failed, on_hold).
        reason: Optional reason for the status change.
        notes: Optional notes to attach to the transition event.

    Returns:
        JSON string with the mutation result for the AI agent to interpret.
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()

    params = {
        "order_id": order_id,
        "new_status": new_status,
        "reason": reason,
        "notes": notes,
    }

    try:
        logger.info(
            "AI tool invocation: tool=update_order_status tenant_id=%s params=%s",
            tenant_id,
            json.dumps(params, default=str),
        )

        # Check autonomy level — suggest-only returns suggestion without executing
        autonomy_svc = _get_autonomy_config_service()
        autonomy_level = await autonomy_svc.get_level(tenant_id)

        if autonomy_level == "suggest-only":
            suggestion = (
                f"SUGGESTION: Update order {order_id} status to '{new_status}'"
            )
            if reason:
                suggestion += f" (reason: {reason})"
            if notes:
                suggestion += f" [notes: {notes}]"
            suggestion += (
                ". This action requires dispatcher approval. "
                "Tenant autonomy level is 'suggest-only'."
            )
            success = True
            return json.dumps({
                "tool": "update_order_status",
                "action": "suggestion",
                "suggestion": suggestion,
                "order_id": order_id,
                "proposed_status": new_status,
                "autonomy_level": autonomy_level,
            })

        # Route through ConfirmationProtocol
        cp = _get_confirmation_protocol()
        mutation_request = MutationRequest(
            tool_name="update_order_status",
            parameters={
                "order_id": order_id,
                "new_status": new_status,
                "reason": reason,
                "notes": notes,
                "tenant_id": tenant_id,
            },
            tenant_id=tenant_id,
            agent_id="ops_intelligence_agent",
        )

        mutation_result = await cp.process_mutation(mutation_request)

        if mutation_result.executed:
            # Execute via internal OrderService (not HTTP)
            order_repo = _get_order_repo()
            order_svc = _get_order_service()

            order = await order_repo.get(tenant_id, order_id)
            if order is None:
                success = True
                return json.dumps({
                    "tool": "update_order_status",
                    "error": f"Order {order_id} not found for tenant {tenant_id}",
                })

            order_dict = order.model_dump(mode="python")
            updated = await order_svc.apply_status_transition(
                order=order_dict,
                new_status=new_status,
                reason=reason,
                notes=notes,
                actor_user_id=None,
            )

            success = True
            return json.dumps({
                "tool": "update_order_status",
                "action": "executed",
                "order_id": order_id,
                "old_status": order.status,
                "new_status": updated["status"],
                "risk_level": mutation_result.risk_level,
                "confirmation_method": mutation_result.confirmation_method,
            })
        else:
            # Queued for approval
            success = True
            return json.dumps({
                "tool": "update_order_status",
                "action": "queued_for_approval",
                "order_id": order_id,
                "proposed_status": new_status,
                "approval_id": mutation_result.approval_id,
                "risk_level": mutation_result.risk_level,
                "confirmation_method": mutation_result.confirmation_method,
            })

    except Exception as e:
        error_msg = str(e)
        logger.error("update_order_status failed: %s", e)
        return json.dumps({"tool": "update_order_status", "error": str(e)})
    finally:
        _log_tool_invocation(
            "update_order_status", params, start_time, success, error_msg
        )


@tool
async def assign_driver_to_order(
    order_id: str,
    driver_id: str,
    reason: Optional[str] = None,
) -> str:
    """
    Assign a driver to a fuel order.

    Routes through ConfirmationProtocol with MEDIUM risk classification.
    In suggest-only tenants, returns a suggestion string without executing.
    Validates driver availability before assignment — rejects off_duty or
    inactive drivers.

    Tenant-scoped — raises RuntimeError if no tenant is bound.

    Args:
        order_id: The order ID to assign a driver to.
        driver_id: The driver ID to assign.
        reason: Optional reason for the assignment.

    Returns:
        JSON string with the mutation result for the AI agent to interpret.
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()

    params = {
        "order_id": order_id,
        "driver_id": driver_id,
        "reason": reason,
    }

    try:
        logger.info(
            "AI tool invocation: tool=assign_driver_to_order tenant_id=%s params=%s",
            tenant_id,
            json.dumps(params, default=str),
        )

        # Check autonomy level — suggest-only returns suggestion without executing
        autonomy_svc = _get_autonomy_config_service()
        autonomy_level = await autonomy_svc.get_level(tenant_id)

        if autonomy_level == "suggest-only":
            suggestion = (
                f"SUGGESTION: Assign driver {driver_id} to order {order_id}"
            )
            if reason:
                suggestion += f" (reason: {reason})"
            suggestion += (
                ". This action requires dispatcher approval. "
                "Tenant autonomy level is 'suggest-only'."
            )
            success = True
            return json.dumps({
                "tool": "assign_driver_to_order",
                "action": "suggestion",
                "suggestion": suggestion,
                "order_id": order_id,
                "proposed_driver_id": driver_id,
                "autonomy_level": autonomy_level,
            })

        # Route through ConfirmationProtocol
        cp = _get_confirmation_protocol()
        mutation_request = MutationRequest(
            tool_name="assign_driver_to_order",
            parameters={
                "order_id": order_id,
                "driver_id": driver_id,
                "reason": reason,
                "tenant_id": tenant_id,
            },
            tenant_id=tenant_id,
            agent_id="ops_intelligence_agent",
        )

        mutation_result = await cp.process_mutation(mutation_request)

        if mutation_result.executed:
            # Execute via internal services (not HTTP)
            order_repo = _get_order_repo()
            driver_repo = _get_driver_repo()

            # Validate order exists
            order = await order_repo.get(tenant_id, order_id)
            if order is None:
                success = True
                return json.dumps({
                    "tool": "assign_driver_to_order",
                    "error": f"Order {order_id} not found for tenant {tenant_id}",
                })

            # Validate driver exists and is available
            driver = await driver_repo.get(tenant_id, driver_id)
            if driver is None:
                success = True
                return json.dumps({
                    "tool": "assign_driver_to_order",
                    "error": f"Driver {driver_id} not found for tenant {tenant_id}",
                })

            if driver.status in ("off_duty", "inactive"):
                success = True
                return json.dumps({
                    "tool": "assign_driver_to_order",
                    "error": (
                        f"Driver {driver_id} is {driver.status} and cannot "
                        f"be assigned to orders."
                    ),
                    "error_code": "driver_unavailable",
                })

            # Perform the assignment via the order repository
            order_dict = order.model_dump(mode="python")
            order_dict["assigned_driver_id"] = driver_id

            from services.time_utils import utcnow

            now = utcnow()
            order_dict["updated_at"] = now
            order_dict["last_event_timestamp"] = now

            await order_repo.upsert_with_last_event_timestamp(
                tenant_id, order_dict
            )

            # Increment driver active order count
            try:
                await driver_repo.increment_counters(
                    tenant_id=tenant_id,
                    driver_id=driver_id,
                    delta_active=1,
                    delta_completed=0,
                )
            except Exception as exc:
                logger.warning(
                    "Driver counter increment failed for driver=%s: %s",
                    driver_id,
                    exc,
                )

            success = True
            return json.dumps({
                "tool": "assign_driver_to_order",
                "action": "executed",
                "order_id": order_id,
                "driver_id": driver_id,
                "risk_level": mutation_result.risk_level,
                "confirmation_method": mutation_result.confirmation_method,
            })
        else:
            # Queued for approval
            success = True
            return json.dumps({
                "tool": "assign_driver_to_order",
                "action": "queued_for_approval",
                "order_id": order_id,
                "proposed_driver_id": driver_id,
                "approval_id": mutation_result.approval_id,
                "risk_level": mutation_result.risk_level,
                "confirmation_method": mutation_result.confirmation_method,
            })

    except Exception as e:
        error_msg = str(e)
        logger.error("assign_driver_to_order failed: %s", e)
        return json.dumps({"tool": "assign_driver_to_order", "error": str(e)})
    finally:
        _log_tool_invocation(
            "assign_driver_to_order", params, start_time, success, error_msg
        )


@tool
async def cancel_order(
    order_id: str,
    reason: str,
    notes: Optional[str] = None,
) -> str:
    """
    Cancel a fuel order.

    Routes through ConfirmationProtocol with HIGH risk classification.
    Always requires explicit dispatcher approval in most autonomy levels.
    In suggest-only tenants, returns a suggestion string without executing.

    Tenant-scoped — raises RuntimeError if no tenant is bound.

    Args:
        order_id: The order ID to cancel.
        reason: The cancellation reason (required).
        notes: Optional additional notes.

    Returns:
        JSON string with the mutation result for the AI agent to interpret.
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()

    params = {
        "order_id": order_id,
        "reason": reason,
        "notes": notes,
    }

    try:
        logger.info(
            "AI tool invocation: tool=cancel_order tenant_id=%s params=%s",
            tenant_id,
            json.dumps(params, default=str),
        )

        # Check autonomy level — suggest-only returns suggestion without executing
        autonomy_svc = _get_autonomy_config_service()
        autonomy_level = await autonomy_svc.get_level(tenant_id)

        if autonomy_level == "suggest-only":
            suggestion = (
                f"SUGGESTION: Cancel order {order_id} "
                f"(reason: {reason})"
            )
            if notes:
                suggestion += f" [notes: {notes}]"
            suggestion += (
                ". This action requires dispatcher approval. "
                "Tenant autonomy level is 'suggest-only'."
            )
            success = True
            return json.dumps({
                "tool": "cancel_order",
                "action": "suggestion",
                "suggestion": suggestion,
                "order_id": order_id,
                "proposed_reason": reason,
                "autonomy_level": autonomy_level,
            })

        # Route through ConfirmationProtocol
        cp = _get_confirmation_protocol()
        mutation_request = MutationRequest(
            tool_name="cancel_order",
            parameters={
                "order_id": order_id,
                "reason": reason,
                "notes": notes,
                "tenant_id": tenant_id,
            },
            tenant_id=tenant_id,
            agent_id="ops_intelligence_agent",
        )

        mutation_result = await cp.process_mutation(mutation_request)

        if mutation_result.executed:
            # Execute via internal OrderService (not HTTP)
            order_repo = _get_order_repo()
            order_svc = _get_order_service()

            order = await order_repo.get(tenant_id, order_id)
            if order is None:
                success = True
                return json.dumps({
                    "tool": "cancel_order",
                    "error": f"Order {order_id} not found for tenant {tenant_id}",
                })

            order_dict = order.model_dump(mode="python")
            updated = await order_svc.apply_status_transition(
                order=order_dict,
                new_status="cancelled",
                reason=reason,
                notes=notes,
                actor_user_id=None,
            )

            success = True
            return json.dumps({
                "tool": "cancel_order",
                "action": "executed",
                "order_id": order_id,
                "old_status": order.status,
                "new_status": updated["status"],
                "reason": reason,
                "risk_level": mutation_result.risk_level,
                "confirmation_method": mutation_result.confirmation_method,
            })
        else:
            # Queued for approval
            success = True
            return json.dumps({
                "tool": "cancel_order",
                "action": "queued_for_approval",
                "order_id": order_id,
                "proposed_reason": reason,
                "approval_id": mutation_result.approval_id,
                "risk_level": mutation_result.risk_level,
                "confirmation_method": mutation_result.confirmation_method,
            })

    except Exception as e:
        error_msg = str(e)
        logger.error("cancel_order failed: %s", e)
        return json.dumps({"tool": "cancel_order", "error": str(e)})
    finally:
        _log_tool_invocation(
            "cancel_order", params, start_time, success, error_msg
        )
