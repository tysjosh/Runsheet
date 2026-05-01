"""
AI agent mutation tools for scheduling, ops, and fuel operations.

Each tool creates a MutationRequest, routes it through the Confirmation
Protocol (risk classification → business validation → autonomy check →
execute or queue), and returns a formatted result string.

The module-level ``_confirmation_protocol`` and ``_es_service`` references
are wired at startup via ``configure_mutation_tools()``, following the same
pattern used by ``ops_search_tools.configure_ops_search_tools()``.

Validates:
- Requirement 1.1: Scheduling mutation tools
- Requirement 1.2: Ops mutation tools
- Requirement 1.3: Fuel mutation tools
- Requirement 1.8: Activity log integration (via Confirmation Protocol)
- Requirement 1.9: Business rule validation (via Confirmation Protocol)
- Requirement 1.10: Failure reporting with corrective suggestions
"""

import logging
import time
from strands import tool
from Agents.confirmation_protocol import MutationRequest
from .logging_wrapper import get_telemetry_service

logger = logging.getLogger(__name__)

# Module-level service references, wired at startup via configure_mutation_tools()
_confirmation_protocol = None
_es_service = None


def configure_mutation_tools(confirmation_protocol, es_service) -> None:
    """
    Wire the Confirmation Protocol and Elasticsearch service into this module.

    Called once during application startup (lifespan) so that the mutation
    tool functions can route through the confirmation protocol.

    Args:
        confirmation_protocol: The ConfirmationProtocol instance.
        es_service: The Elasticsearch service instance.
    """
    global _confirmation_protocol, _es_service
    _confirmation_protocol = confirmation_protocol
    _es_service = es_service
    logger.info("Mutation tools configured with ConfirmationProtocol and ES service")


def _get_protocol():
    """Return the configured ConfirmationProtocol or raise."""
    if _confirmation_protocol is None:
        raise RuntimeError(
            "Mutation tools not configured. "
            "Call configure_mutation_tools() during startup."
        )
    return _confirmation_protocol


def _log_tool_invocation(tool_name: str, input_params: dict, start_time: float,
                         success: bool, error: str = None):
    """Helper to log tool invocations with telemetry service."""
    duration_ms = (time.time() - start_time) * 1000
    telemetry = get_telemetry_service()
    if telemetry:
        telemetry.log_tool_invocation(
            tool_name=tool_name,
            input_params=input_params,
            duration_ms=duration_ms,
            success=success,
            error=error
        )
        telemetry.record_metric(
            name="tool_invocation_duration_ms",
            value=duration_ms,
            tags={"tool_name": tool_name, "success": str(success).lower()}
        )
        telemetry.record_metric(
            name="tool_invocation_count",
            value=1,
            tags={"tool_name": tool_name, "success": str(success).lower()}
        )


def _format_mutation_result(result) -> str:
    """Format a MutationResult into a human-readable response string.

    Args:
        result: A MutationResult from the confirmation protocol.

    Returns:
        A formatted string describing the outcome.
    """
    if result.confirmation_method == "rejected":
        return (
            f"❌ Action rejected: {result.result}\n"
            f"Please correct the parameters and try again."
        )
    elif result.executed:
        return f"✅ Action executed (risk: {result.risk_level}): {result.result}"
    else:
        return (
            f"⏳ Action queued for approval (risk: {result.risk_level}). "
            f"Approval ID: {result.approval_id}\n"
            f"A reviewer must approve this action before it is executed."
        )


# ---------------------------------------------------------------------------
# Scheduling Mutations (Requirement 1.1)
# ---------------------------------------------------------------------------

@tool
async def assign_asset_to_job(job_id: str, asset_id: str,
                              tenant_id: str = "dev-tenant") -> str:
    """
    Assign an asset to a job. Risk: medium.

    Routes through the Confirmation Protocol for risk classification,
    business validation, and autonomy-level checks before executing.

    Args:
        job_id: The job identifier to assign the asset to.
        asset_id: The asset identifier to assign.
        tenant_id: Tenant identifier for data scoping.

    Returns:
        Formatted result string indicating execution, queuing, or rejection.
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        protocol = _get_protocol()
        request = MutationRequest(
            tool_name="assign_asset_to_job",
            parameters={"job_id": job_id, "asset_id": asset_id},
            tenant_id=tenant_id,
            agent_id="ai_agent",
        )
        result = await protocol.process_mutation(request)
        success = result.confirmation_method != "rejected"
        return _format_mutation_result(result)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in assign_asset_to_job: {e}")
        return f"❌ Error assigning asset to job: {str(e)}"
    finally:
        _log_tool_invocation(
            "assign_asset_to_job",
            {"job_id": job_id, "asset_id": asset_id, "tenant_id": tenant_id},
            start_time, success, error_msg
        )


@tool
async def update_job_status(job_id: str, new_status: str, reason: str,
                            tenant_id: str = "dev-tenant") -> str:
    """
    Update job status with valid transition check. Risk: medium.

    Routes through the Confirmation Protocol for risk classification,
    business validation (including state machine transition check),
    and autonomy-level checks before executing.

    Args:
        job_id: The job identifier to update.
        new_status: The target status. Valid values depend on current status.
        reason: Reason for the status change.
        tenant_id: Tenant identifier for data scoping.

    Returns:
        Formatted result string indicating execution, queuing, or rejection.
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        protocol = _get_protocol()
        request = MutationRequest(
            tool_name="update_job_status",
            parameters={
                "job_id": job_id,
                "new_status": new_status,
                "reason": reason,
            },
            tenant_id=tenant_id,
            agent_id="ai_agent",
        )
        result = await protocol.process_mutation(request)
        success = result.confirmation_method != "rejected"
        return _format_mutation_result(result)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in update_job_status: {e}")
        return f"❌ Error updating job status: {str(e)}"
    finally:
        _log_tool_invocation(
            "update_job_status",
            {"job_id": job_id, "new_status": new_status, "reason": reason,
             "tenant_id": tenant_id},
            start_time, success, error_msg
        )


@tool
async def cancel_job(job_id: str, reason: str,
                     tenant_id: str = "dev-tenant") -> str:
    """
    Cancel a job. Risk: high.

    Routes through the Confirmation Protocol. High-risk actions require
    explicit approval unless the tenant autonomy level is full-auto.

    Args:
        job_id: The job identifier to cancel.
        reason: Reason for cancellation.
        tenant_id: Tenant identifier for data scoping.

    Returns:
        Formatted result string indicating execution, queuing, or rejection.
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        protocol = _get_protocol()
        request = MutationRequest(
            tool_name="cancel_job",
            parameters={"job_id": job_id, "reason": reason},
            tenant_id=tenant_id,
            agent_id="ai_agent",
        )
        result = await protocol.process_mutation(request)
        success = result.confirmation_method != "rejected"
        return _format_mutation_result(result)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in cancel_job: {e}")
        return f"❌ Error cancelling job: {str(e)}"
    finally:
        _log_tool_invocation(
            "cancel_job",
            {"job_id": job_id, "reason": reason, "tenant_id": tenant_id},
            start_time, success, error_msg
        )


@tool
async def create_job(job_type: str, origin: str, destination: str,
                     scheduled_time: str, asset_id: str = None,
                     cargo_manifest: list = None,
                     tenant_id: str = "dev-tenant") -> str:
    """
    Create a new logistics job. Risk: medium.

    Routes through the Confirmation Protocol for risk classification,
    business validation, and autonomy-level checks before executing.

    Args:
        job_type: Type of job. One of: cargo_transport, passenger_transport,
                  vessel_movement, airport_transfer, crane_booking.
        origin: Origin location for the job.
        destination: Destination location for the job.
        scheduled_time: Scheduled start time in ISO 8601 format.
        asset_id: Optional asset to assign to the job.
        cargo_manifest: Optional list of cargo items.
        tenant_id: Tenant identifier for data scoping.

    Returns:
        Formatted result string indicating execution, queuing, or rejection.
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        protocol = _get_protocol()
        parameters = {
            "job_type": job_type,
            "origin": origin,
            "destination": destination,
            "scheduled_time": scheduled_time,
        }
        if asset_id is not None:
            parameters["asset_id"] = asset_id
        if cargo_manifest is not None:
            parameters["cargo_manifest"] = cargo_manifest

        request = MutationRequest(
            tool_name="create_job",
            parameters=parameters,
            tenant_id=tenant_id,
            agent_id="ai_agent",
        )
        result = await protocol.process_mutation(request)
        success = result.confirmation_method != "rejected"
        return _format_mutation_result(result)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in create_job: {e}")
        return f"❌ Error creating job: {str(e)}"
    finally:
        _log_tool_invocation(
            "create_job",
            {"job_type": job_type, "origin": origin, "destination": destination,
             "scheduled_time": scheduled_time, "asset_id": asset_id,
             "cargo_manifest": cargo_manifest, "tenant_id": tenant_id},
            start_time, success, error_msg
        )


# ---------------------------------------------------------------------------
# Ops Mutations (Requirement 1.2)
# ---------------------------------------------------------------------------

@tool
async def reassign_rider(shipment_id: str, new_rider_id: str, reason: str,
                         tenant_id: str) -> str:
    """
    Reassign a shipment to a different rider. Risk: high.

    Routes through the Confirmation Protocol. High-risk actions require
    explicit approval unless the tenant autonomy level is full-auto.

    Args:
        shipment_id: The shipment identifier to reassign.
        new_rider_id: The new rider identifier.
        reason: Reason for the reassignment.
        tenant_id: Tenant identifier for data scoping.

    Returns:
        Formatted result string indicating execution, queuing, or rejection.
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        protocol = _get_protocol()
        request = MutationRequest(
            tool_name="reassign_rider",
            parameters={
                "shipment_id": shipment_id,
                "new_rider_id": new_rider_id,
                "reason": reason,
            },
            tenant_id=tenant_id,
            agent_id="ai_agent",
        )
        result = await protocol.process_mutation(request)
        success = result.confirmation_method != "rejected"
        return _format_mutation_result(result)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in reassign_rider: {e}")
        return f"❌ Error reassigning rider: {str(e)}"
    finally:
        _log_tool_invocation(
            "reassign_rider",
            {"shipment_id": shipment_id, "new_rider_id": new_rider_id,
             "reason": reason, "tenant_id": tenant_id},
            start_time, success, error_msg
        )


@tool
async def escalate_shipment(shipment_id: str, priority: str, reason: str,
                            tenant_id: str) -> str:
    """
    Escalate shipment priority. Risk: medium.

    Routes through the Confirmation Protocol for risk classification,
    business validation, and autonomy-level checks before executing.

    Args:
        shipment_id: The shipment identifier to escalate.
        priority: The new priority level (e.g., "high", "critical").
        reason: Reason for the escalation.
        tenant_id: Tenant identifier for data scoping.

    Returns:
        Formatted result string indicating execution, queuing, or rejection.
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        protocol = _get_protocol()
        request = MutationRequest(
            tool_name="escalate_shipment",
            parameters={
                "shipment_id": shipment_id,
                "priority": priority,
                "reason": reason,
            },
            tenant_id=tenant_id,
            agent_id="ai_agent",
        )
        result = await protocol.process_mutation(request)
        success = result.confirmation_method != "rejected"
        return _format_mutation_result(result)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in escalate_shipment: {e}")
        return f"❌ Error escalating shipment: {str(e)}"
    finally:
        _log_tool_invocation(
            "escalate_shipment",
            {"shipment_id": shipment_id, "priority": priority,
             "reason": reason, "tenant_id": tenant_id},
            start_time, success, error_msg
        )


# ---------------------------------------------------------------------------
# Fuel Mutations (Requirement 1.3)
# ---------------------------------------------------------------------------

@tool
async def request_fuel_refill(station_id: str, quantity_liters: float,
                              priority: str,
                              tenant_id: str = "dev-tenant") -> str:
    """
    Request a fuel refill for a station. Risk: medium.

    Routes through the Confirmation Protocol for risk classification,
    business validation (positive quantity, station existence),
    and autonomy-level checks before executing.

    Args:
        station_id: The fuel station identifier.
        quantity_liters: Amount of fuel to request in liters.
        priority: Refill priority (critical, high, medium, normal).
        tenant_id: Tenant identifier for data scoping.

    Returns:
        Formatted result string indicating execution, queuing, or rejection.
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        protocol = _get_protocol()
        request = MutationRequest(
            tool_name="request_fuel_refill",
            parameters={
                "station_id": station_id,
                "quantity_liters": quantity_liters,
                "priority": priority,
            },
            tenant_id=tenant_id,
            agent_id="ai_agent",
        )
        result = await protocol.process_mutation(request)
        success = result.confirmation_method != "rejected"
        return _format_mutation_result(result)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in request_fuel_refill: {e}")
        return f"❌ Error requesting fuel refill: {str(e)}"
    finally:
        _log_tool_invocation(
            "request_fuel_refill",
            {"station_id": station_id, "quantity_liters": quantity_liters,
             "priority": priority, "tenant_id": tenant_id},
            start_time, success, error_msg
        )


@tool
async def update_fuel_threshold(station_id: str, threshold_pct: float,
                                tenant_id: str = "dev-tenant") -> str:
    """
    Update fuel alert threshold. Risk: low.

    Low-risk action that may execute immediately depending on the tenant's
    autonomy level configuration.

    Args:
        station_id: The fuel station identifier.
        threshold_pct: New alert threshold as a percentage (0-100).
        tenant_id: Tenant identifier for data scoping.

    Returns:
        Formatted result string indicating execution, queuing, or rejection.
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        protocol = _get_protocol()
        request = MutationRequest(
            tool_name="update_fuel_threshold",
            parameters={
                "station_id": station_id,
                "threshold_pct": threshold_pct,
            },
            tenant_id=tenant_id,
            agent_id="ai_agent",
        )
        result = await protocol.process_mutation(request)
        success = result.confirmation_method != "rejected"
        return _format_mutation_result(result)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in update_fuel_threshold: {e}")
        return f"❌ Error updating fuel threshold: {str(e)}"
    finally:
        _log_tool_invocation(
            "update_fuel_threshold",
            {"station_id": station_id, "threshold_pct": threshold_pct,
             "tenant_id": tenant_id},
            start_time, success, error_msg
        )
