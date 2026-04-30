"""
Unit tests for the Confirmation Protocol module.

Tests the MutationRequest and MutationResult dataclasses, the
ConfirmationProtocol class including process_mutation routing,
the _should_auto_execute routing matrix, business validation
rejection, and activity log / approval queue wiring.

Requirements: 1.4, 1.5, 1.6, 1.7, 1.8, 10.3
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.confirmation_protocol import (
    ConfirmationProtocol,
    MutationRequest,
    MutationResult,
)
from Agents.risk_registry import RiskLevel
from Agents.business_validator import ValidationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    tool_name: str = "update_fuel_threshold",
    parameters: dict = None,
    tenant_id: str = "t1",
    agent_id: str = "ai_agent",
    user_id: str = None,
    session_id: str = None,
) -> MutationRequest:
    """Create a MutationRequest with sensible defaults."""
    return MutationRequest(
        tool_name=tool_name,
        parameters=parameters or {},
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
    )


def _make_protocol(
    risk_level: RiskLevel = RiskLevel.LOW,
    autonomy_level: str = "full-auto",
    validation_valid: bool = True,
    validation_reason: str = None,
    approval_id: str = "approval-123",
) -> ConfirmationProtocol:
    """Create a ConfirmationProtocol with mocked dependencies."""
    risk_registry = MagicMock()
    risk_registry.classify = AsyncMock(return_value=risk_level)

    approval_queue = MagicMock()
    approval_queue.create = AsyncMock(return_value=approval_id)

    autonomy_config = MagicMock()
    autonomy_config.get_level = AsyncMock(return_value=autonomy_level)

    activity_log = MagicMock()
    activity_log.log_mutation = AsyncMock(return_value="log-123")

    business_validator = MagicMock()
    business_validator.validate = AsyncMock(
        return_value=ValidationResult(valid=validation_valid, reason=validation_reason)
    )

    return ConfirmationProtocol(
        risk_registry=risk_registry,
        approval_queue_service=approval_queue,
        autonomy_config_service=autonomy_config,
        activity_log_service=activity_log,
        business_validator=business_validator,
    )


# ---------------------------------------------------------------------------
# Tests: MutationRequest dataclass
# ---------------------------------------------------------------------------


class TestMutationRequest:
    def test_required_fields(self):
        req = MutationRequest(
            tool_name="cancel_job",
            parameters={"job_id": "JOB_1"},
            tenant_id="t1",
            agent_id="ai_agent",
        )
        assert req.tool_name == "cancel_job"
        assert req.parameters == {"job_id": "JOB_1"}
        assert req.tenant_id == "t1"
        assert req.agent_id == "ai_agent"

    def test_optional_fields_default_to_none(self):
        req = MutationRequest(
            tool_name="cancel_job",
            parameters={},
            tenant_id="t1",
            agent_id="ai_agent",
        )
        assert req.user_id is None
        assert req.session_id is None

    def test_optional_fields_can_be_set(self):
        req = MutationRequest(
            tool_name="cancel_job",
            parameters={},
            tenant_id="t1",
            agent_id="ai_agent",
            user_id="user-1",
            session_id="sess-1",
        )
        assert req.user_id == "user-1"
        assert req.session_id == "sess-1"

    def test_parameters_can_be_complex(self):
        params = {
            "job_id": "JOB_1",
            "cargo_manifest": [{"item": "fuel", "qty": 100}],
            "nested": {"key": "value"},
        }
        req = MutationRequest(
            tool_name="create_job",
            parameters=params,
            tenant_id="t1",
            agent_id="ai_agent",
        )
        assert req.parameters == params


# ---------------------------------------------------------------------------
# Tests: MutationResult dataclass
# ---------------------------------------------------------------------------


class TestMutationResult:
    def test_default_values(self):
        result = MutationResult(executed=False)
        assert result.executed is False
        assert result.approval_id is None
        assert result.result is None
        assert result.risk_level == "unknown"
        assert result.confirmation_method == "unknown"

    def test_executed_result(self):
        result = MutationResult(
            executed=True,
            result="Success",
            risk_level="low",
            confirmation_method="immediate",
        )
        assert result.executed is True
        assert result.result == "Success"
        assert result.risk_level == "low"
        assert result.confirmation_method == "immediate"

    def test_queued_result(self):
        result = MutationResult(
            executed=False,
            approval_id="approval-123",
            risk_level="high",
            confirmation_method="approval_queue",
        )
        assert result.executed is False
        assert result.approval_id == "approval-123"
        assert result.risk_level == "high"
        assert result.confirmation_method == "approval_queue"

    def test_rejected_result(self):
        result = MutationResult(
            executed=False,
            result="Validation failed: Job not found",
            risk_level="medium",
            confirmation_method="rejected",
        )
        assert result.executed is False
        assert result.confirmation_method == "rejected"
        assert "Validation failed" in result.result


# ---------------------------------------------------------------------------
# Tests: _should_auto_execute routing matrix
# ---------------------------------------------------------------------------


class TestShouldAutoExecute:
    """Tests for the routing matrix that maps (risk × autonomy) to decisions."""

    @pytest.fixture
    def protocol(self):
        return _make_protocol()

    # suggest-only: nothing auto-executes
    def test_suggest_only_low(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.LOW, "suggest-only") is False

    def test_suggest_only_medium(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.MEDIUM, "suggest-only") is False

    def test_suggest_only_high(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.HIGH, "suggest-only") is False

    # auto-low: only low auto-executes
    def test_auto_low_low(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.LOW, "auto-low") is True

    def test_auto_low_medium(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.MEDIUM, "auto-low") is False

    def test_auto_low_high(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.HIGH, "auto-low") is False

    # auto-medium: low + medium auto-execute
    def test_auto_medium_low(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.LOW, "auto-medium") is True

    def test_auto_medium_medium(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.MEDIUM, "auto-medium") is True

    def test_auto_medium_high(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.HIGH, "auto-medium") is False

    # full-auto: all auto-execute
    def test_full_auto_low(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.LOW, "full-auto") is True

    def test_full_auto_medium(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.MEDIUM, "full-auto") is True

    def test_full_auto_high(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.HIGH, "full-auto") is True

    # Unknown autonomy level: nothing auto-executes (safe default)
    def test_unknown_autonomy_level_low(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.LOW, "unknown-level") is False

    def test_unknown_autonomy_level_high(self, protocol):
        assert protocol._should_auto_execute(RiskLevel.HIGH, "unknown-level") is False


# ---------------------------------------------------------------------------
# Tests: process_mutation — immediate execution path
# ---------------------------------------------------------------------------


class TestProcessMutationImmediate:
    """Tests for mutations that auto-execute based on autonomy level."""

    async def test_low_risk_full_auto_executes_immediately(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.LOW, autonomy_level="full-auto"
        )
        request = _make_request(tool_name="update_fuel_threshold")
        result = await protocol.process_mutation(request)

        assert result.executed is True
        assert result.confirmation_method == "immediate"
        assert result.risk_level == "low"
        assert result.approval_id is None
        assert result.result is not None

    async def test_medium_risk_auto_medium_executes_immediately(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.MEDIUM, autonomy_level="auto-medium"
        )
        request = _make_request(tool_name="assign_asset_to_job")
        result = await protocol.process_mutation(request)

        assert result.executed is True
        assert result.confirmation_method == "immediate"
        assert result.risk_level == "medium"

    async def test_high_risk_full_auto_executes_immediately(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.HIGH, autonomy_level="full-auto"
        )
        request = _make_request(tool_name="cancel_job")
        result = await protocol.process_mutation(request)

        assert result.executed is True
        assert result.confirmation_method == "immediate"
        assert result.risk_level == "high"

    async def test_immediate_execution_logs_to_activity_log(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.LOW, autonomy_level="full-auto"
        )
        request = _make_request()
        await protocol.process_mutation(request)

        protocol._activity_log.log_mutation.assert_called_once()
        call_args = protocol._activity_log.log_mutation.call_args
        assert call_args[0][0] is request
        assert call_args[0][1] == RiskLevel.LOW
        assert call_args[0][2] == "immediate"
        # result string is the 4th arg
        assert call_args[0][3] is not None

    async def test_immediate_execution_does_not_create_approval(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.LOW, autonomy_level="full-auto"
        )
        request = _make_request()
        await protocol.process_mutation(request)

        protocol._approval_queue.create.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: process_mutation — approval queue path
# ---------------------------------------------------------------------------


class TestProcessMutationApprovalQueue:
    """Tests for mutations that are queued for approval."""

    async def test_high_risk_suggest_only_queues_for_approval(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.HIGH,
            autonomy_level="suggest-only",
            approval_id="approval-456",
        )
        request = _make_request(tool_name="cancel_job")
        result = await protocol.process_mutation(request)

        assert result.executed is False
        assert result.confirmation_method == "approval_queue"
        assert result.approval_id == "approval-456"
        assert result.risk_level == "high"

    async def test_medium_risk_auto_low_queues_for_approval(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.MEDIUM,
            autonomy_level="auto-low",
            approval_id="approval-789",
        )
        request = _make_request(tool_name="assign_asset_to_job")
        result = await protocol.process_mutation(request)

        assert result.executed is False
        assert result.confirmation_method == "approval_queue"
        assert result.approval_id == "approval-789"

    async def test_low_risk_suggest_only_queues_for_approval(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.LOW,
            autonomy_level="suggest-only",
            approval_id="approval-000",
        )
        request = _make_request(tool_name="update_fuel_threshold")
        result = await protocol.process_mutation(request)

        assert result.executed is False
        assert result.confirmation_method == "approval_queue"

    async def test_approval_queue_creates_entry(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.HIGH, autonomy_level="suggest-only"
        )
        request = _make_request(tool_name="cancel_job")
        await protocol.process_mutation(request)

        protocol._approval_queue.create.assert_called_once_with(
            request, RiskLevel.HIGH
        )

    async def test_approval_queue_logs_to_activity_log(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.HIGH, autonomy_level="suggest-only"
        )
        request = _make_request(tool_name="cancel_job")
        await protocol.process_mutation(request)

        protocol._activity_log.log_mutation.assert_called_once()
        call_args = protocol._activity_log.log_mutation.call_args
        assert call_args[0][2] == "approval_queue"
        assert call_args[0][3] is None  # No result yet


# ---------------------------------------------------------------------------
# Tests: process_mutation — validation rejection path
# ---------------------------------------------------------------------------


class TestProcessMutationValidationRejection:
    """Tests for mutations that fail business rule validation."""

    async def test_validation_failure_returns_rejected(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.MEDIUM,
            autonomy_level="full-auto",
            validation_valid=False,
            validation_reason="Job JOB_1 not found",
        )
        request = _make_request(tool_name="update_job_status")
        result = await protocol.process_mutation(request)

        assert result.executed is False
        assert result.confirmation_method == "rejected"
        assert result.risk_level == "medium"
        assert "Validation failed" in result.result
        assert "Job JOB_1 not found" in result.result

    async def test_validation_failure_does_not_execute(self):
        protocol = _make_protocol(
            validation_valid=False,
            validation_reason="Invalid transition",
        )
        request = _make_request()
        await protocol.process_mutation(request)

        # Should not call approval queue or activity log
        protocol._approval_queue.create.assert_not_called()
        protocol._activity_log.log_mutation.assert_not_called()

    async def test_validation_failure_does_not_queue(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.HIGH,
            autonomy_level="suggest-only",
            validation_valid=False,
            validation_reason="Already cancelled",
        )
        request = _make_request(tool_name="cancel_job")
        result = await protocol.process_mutation(request)

        assert result.executed is False
        assert result.approval_id is None
        assert result.confirmation_method == "rejected"
        protocol._approval_queue.create.assert_not_called()

    async def test_validation_failure_still_classifies_risk(self):
        """Risk level should be set even when validation fails."""
        protocol = _make_protocol(
            risk_level=RiskLevel.HIGH,
            validation_valid=False,
            validation_reason="Not found",
        )
        request = _make_request(tool_name="cancel_job")
        result = await protocol.process_mutation(request)

        assert result.risk_level == "high"


# ---------------------------------------------------------------------------
# Tests: process_mutation — dependency wiring
# ---------------------------------------------------------------------------


class TestProcessMutationWiring:
    """Tests that process_mutation correctly wires all dependencies."""

    async def test_calls_risk_registry_classify(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.LOW, autonomy_level="full-auto"
        )
        request = _make_request(tool_name="update_fuel_threshold")
        await protocol.process_mutation(request)

        protocol._risk_registry.classify.assert_called_once_with(
            "update_fuel_threshold"
        )

    async def test_calls_business_validator(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.LOW, autonomy_level="full-auto"
        )
        request = _make_request(
            tool_name="update_fuel_threshold",
            parameters={"station_id": "S-1", "threshold_pct": 25},
            tenant_id="t1",
        )
        await protocol.process_mutation(request)

        protocol._validator.validate.assert_called_once_with(
            "update_fuel_threshold",
            {"station_id": "S-1", "threshold_pct": 25},
            "t1",
        )

    async def test_calls_autonomy_config_get_level(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.LOW, autonomy_level="full-auto"
        )
        request = _make_request(tenant_id="tenant-abc")
        await protocol.process_mutation(request)

        protocol._autonomy.get_level.assert_called_once_with("tenant-abc")

    async def test_validation_runs_before_autonomy_check(self):
        """If validation fails, autonomy level should not be checked."""
        protocol = _make_protocol(
            validation_valid=False,
            validation_reason="Bad params",
        )
        request = _make_request()
        await protocol.process_mutation(request)

        protocol._validator.validate.assert_called_once()
        protocol._autonomy.get_level.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: _execute_mutation placeholder
# ---------------------------------------------------------------------------


class TestExecuteMutation:
    """Tests for the placeholder _execute_mutation method."""

    async def test_returns_success_string(self):
        protocol = _make_protocol()
        request = _make_request(
            tool_name="update_fuel_threshold", tenant_id="t1"
        )
        result = await protocol._execute_mutation(request)

        assert isinstance(result, str)
        assert "update_fuel_threshold" in result
        assert "t1" in result

    async def test_includes_tool_name_in_result(self):
        protocol = _make_protocol()
        request = _make_request(tool_name="cancel_job")
        result = await protocol._execute_mutation(request)

        assert "cancel_job" in result

    async def test_includes_tenant_id_in_result(self):
        protocol = _make_protocol()
        request = _make_request(tenant_id="tenant-xyz")
        result = await protocol._execute_mutation(request)

        assert "tenant-xyz" in result
