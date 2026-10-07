"""
Unit tests for the Confirmation Protocol module.

Tests the MutationRequest and MutationResult dataclasses, the
ConfirmationProtocol class including process_mutation routing,
the _should_auto_execute routing matrix, business validation
rejection, and activity log / approval queue wiring.

Requirements: 1.4, 1.5, 1.6, 1.7, 1.8, 10.3
"""
import logging as _logging

import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.confirmation_protocol import (
    ConfirmationProtocol,
    MutationExecutionError,
    MutationRequest,
    MutationResult,
)
from Agents.risk_registry import RiskLevel
from Agents.business_validator import ValidationResult
from Agents.approval_queue_service import (
    ApprovalForbiddenError,
    LoadingPlanExecutionError,
    LoadingPlanOverlapError,
)
from fuel.services.loading_plan_executor import (
    MESSAGE_TEMPLATES,
    LoadingPlanExecutionResult,
)
from tests.unit._loading_plan_fakes import (
    APPROVALS,
    ORDERS,
    ApprovalHarness,
    FakeFeatureFlagService,
    order_fixture,
)


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
    notification_service=None,
    job_service=None,
    es_service=None,
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
        notification_service=notification_service,
        job_service=job_service,
        es_service=es_service,
    )


def _refill_request(**overrides) -> MutationRequest:
    """A tool that writes one document when an ES service is wired."""
    overrides.setdefault("parameters", {"station_id": "ST-1", "quantity_liters": 100})
    return _make_request(tool_name="request_fuel_refill", **overrides)


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
            risk_level=RiskLevel.LOW, autonomy_level="full-auto",
            es_service=AsyncMock(),
        )
        request = _refill_request()
        result = await protocol.process_mutation(request)

        assert result.executed is True
        assert result.confirmation_method == "immediate"
        assert result.risk_level == "low"
        assert result.approval_id is None
        assert result.result is not None

    async def test_medium_risk_auto_medium_executes_immediately(self):
        # Job tools run through JobService (OI-15).
        protocol = _make_protocol(
            risk_level=RiskLevel.MEDIUM, autonomy_level="auto-medium",
            job_service=AsyncMock(),
        )
        request = _make_request(
            tool_name="assign_asset_to_job",
            parameters={"job_id": "JOB_1", "asset_id": "T-1"},
        )
        result = await protocol.process_mutation(request)

        assert result.executed is True
        assert result.confirmation_method == "immediate"
        assert result.risk_level == "medium"

    async def test_high_risk_full_auto_executes_immediately(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.HIGH, autonomy_level="full-auto",
            job_service=AsyncMock(),
        )
        request = _make_request(tool_name="cancel_job", parameters={"job_id": "JOB_1"})
        result = await protocol.process_mutation(request)

        assert result.executed is True
        assert result.confirmation_method == "immediate"
        assert result.risk_level == "high"

    async def test_immediate_execution_logs_to_activity_log(self):
        protocol = _make_protocol(
            risk_level=RiskLevel.LOW, autonomy_level="full-auto",
            es_service=AsyncMock(),
        )
        request = _refill_request()
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
            "update_fuel_threshold", tenant_id="t1"
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
        protocol = _make_protocol(es_service=AsyncMock())
        request = _refill_request(tenant_id="t1")
        result = await protocol._execute_mutation(request)

        assert isinstance(result, str)
        assert "request_fuel_refill" in result
        assert "t1" in result

    async def test_includes_tool_name_in_result(self):
        protocol = _make_protocol(job_service=AsyncMock())
        request = _make_request(tool_name="cancel_job", parameters={"job_id": "JOB_1"})
        result = await protocol._execute_mutation(request)

        assert "cancel_job" in result

    async def test_includes_tenant_id_in_result(self):
        protocol = _make_protocol(es_service=AsyncMock())
        request = _refill_request(tenant_id="tenant-xyz")
        result = await protocol._execute_mutation(request)

        assert "tenant-xyz" in result


# ---------------------------------------------------------------------------
# Tests: _execute_mutation — send_customer_notification branch
# ---------------------------------------------------------------------------


class TestExecuteMutationSendCustomerNotification:
    """Tests for the send_customer_notification branch in _execute_mutation.

    Requirements: 1.1, 1.2, 1.3, 1.4
    """

    def _make_notification_request(self, **overrides) -> MutationRequest:
        """Create a MutationRequest for send_customer_notification."""
        params = {
            "delivery_id": "JOB_ABC123",
            "notification_type": "delay_alert",
            "channel": "sms",
            "message_template": "Your delivery is delayed.",
            "customer_id": "CUST_001",
            "proposal_id": "PROP_001",
            "context": {},
        }
        params.update(overrides)
        return _make_request(
            tool_name="send_customer_notification",
            parameters=params,
            tenant_id="t1",
        )

    async def test_success_returns_notification_ids(self):
        """Req 1.4: Successful dispatch includes notification_id(s) in result."""
        mock_ns = AsyncMock()
        mock_ns.notify_event = AsyncMock(
            return_value=[
                {"notification_id": "notif-001"},
                {"notification_id": "notif-002"},
            ]
        )
        protocol = _make_protocol(notification_service=mock_ns)
        request = self._make_notification_request()

        result = await protocol._execute_mutation(request)

        assert "notif-001" in result
        assert "notif-002" in result
        assert "Dispatched 2 notification(s)" in result

    async def test_invokes_notify_event_with_correct_params(self):
        """Req 1.1, 1.2: Parameters forwarded correctly to NotificationService."""
        mock_ns = AsyncMock()
        mock_ns.notify_event = AsyncMock(
            return_value=[{"notification_id": "notif-001"}]
        )
        protocol = _make_protocol(notification_service=mock_ns)
        request = self._make_notification_request(
            delivery_id="JOB_XYZ",
            notification_type="eta_change",
            channel="email",
            message_template="ETA updated.",
            customer_id="CUST_002",
            proposal_id="PROP_002",
        )

        await protocol._execute_mutation(request)

        mock_ns.notify_event.assert_called_once()
        call_kwargs = mock_ns.notify_event.call_args[1]
        assert call_kwargs["event_type"] == "eta_change"
        assert call_kwargs["tenant_id"] == "t1"
        event_data = call_kwargs["event_data"]
        assert event_data["customer_id"] == "CUST_002"
        assert event_data["job_id"] == "JOB_XYZ"
        assert event_data["channel_override"] == "email"
        assert event_data["template_override"] == "ETA updated."
        assert event_data["proposal_id"] == "PROP_002"

    async def test_empty_notifications_returns_failure_message(self):
        """Req 1.3: Empty notification list returns failure details."""
        mock_ns = AsyncMock()
        mock_ns.notify_event = AsyncMock(return_value=[])
        protocol = _make_protocol(notification_service=mock_ns)
        request = self._make_notification_request()

        with pytest.raises(MutationExecutionError) as exc:
            await protocol._execute_mutation(request)
        result = str(exc.value)

        assert "failed" in result.lower()
        assert "no notifications created" in result.lower()

    async def test_none_notifications_returns_failure_message(self):
        """Req 1.3: None return from notify_event returns failure details."""
        mock_ns = AsyncMock()
        mock_ns.notify_event = AsyncMock(return_value=None)
        protocol = _make_protocol(notification_service=mock_ns)
        request = self._make_notification_request()

        with pytest.raises(MutationExecutionError) as exc:
            await protocol._execute_mutation(request)

        assert "failed" in str(exc.value).lower()

    async def test_notify_event_exception_returns_failure(self):
        """Req 1.3: Exception from NotificationService returns failure details."""
        mock_ns = AsyncMock()
        mock_ns.notify_event = AsyncMock(
            side_effect=Exception("ES connection timeout")
        )
        protocol = _make_protocol(notification_service=mock_ns)
        request = self._make_notification_request()

        with pytest.raises(MutationExecutionError) as exc:
            await protocol._execute_mutation(request)
        result = str(exc.value)

        assert "Failed to execute" in result
        assert "ES connection timeout" in result

    async def test_no_notification_service_returns_failure(self):
        """When notification_service is not wired, returns failure message."""
        protocol = _make_protocol(notification_service=None)
        request = self._make_notification_request()

        with pytest.raises(MutationExecutionError) as exc:
            await protocol._execute_mutation(request)
        result = str(exc.value)

        assert "failed" in result.lower()
        assert "not configured" in result.lower()

    async def test_default_notification_type_is_order_status_update(self):
        """Req 1.2: Defaults to order_status_update when notification_type missing."""
        mock_ns = AsyncMock()
        mock_ns.notify_event = AsyncMock(
            return_value=[{"notification_id": "notif-001"}]
        )
        protocol = _make_protocol(notification_service=mock_ns)
        # Omit notification_type from parameters
        request = _make_request(
            tool_name="send_customer_notification",
            parameters={
                "delivery_id": "JOB_1",
                "customer_id": "CUST_1",
                "context": {},
            },
            tenant_id="t1",
        )

        await protocol._execute_mutation(request)

        call_kwargs = mock_ns.notify_event.call_args[1]
        assert call_kwargs["event_type"] == "order_status_update"

    async def test_context_params_merged_into_event_data(self):
        """Req 1.2: Extra context parameters are merged into event_data."""
        mock_ns = AsyncMock()
        mock_ns.notify_event = AsyncMock(
            return_value=[{"notification_id": "notif-001"}]
        )
        protocol = _make_protocol(notification_service=mock_ns)
        request = self._make_notification_request(
            context={"extra_key": "extra_value", "another": 42}
        )

        await protocol._execute_mutation(request)

        event_data = mock_ns.notify_event.call_args[1]["event_data"]
        assert event_data["extra_key"] == "extra_value"
        assert event_data["another"] == 42

    async def test_single_notification_returns_single_id(self):
        """Req 1.4: Single notification returns single notification_id."""
        mock_ns = AsyncMock()
        mock_ns.notify_event = AsyncMock(
            return_value=[{"notification_id": "notif-solo"}]
        )
        protocol = _make_protocol(notification_service=mock_ns)
        request = self._make_notification_request()

        result = await protocol._execute_mutation(request)

        assert "Dispatched 1 notification(s)" in result
        assert "notif-solo" in result

    async def test_end_to_end_via_process_mutation(self):
        """Req 1.1: Full flow through process_mutation returns executed=True with notification_ids."""
        mock_ns = AsyncMock()
        mock_ns.notify_event = AsyncMock(
            return_value=[{"notification_id": "notif-e2e"}]
        )
        protocol = _make_protocol(
            risk_level=RiskLevel.LOW,
            autonomy_level="full-auto",
            notification_service=mock_ns,
        )
        request = self._make_notification_request()

        result = await protocol.process_mutation(request)

        assert result.executed is True
        assert result.confirmation_method == "immediate"
        assert "notif-e2e" in result.result


# ---------------------------------------------------------------------------
# Tests: approval-queue deduplication (_find_pending_duplicate)
# ---------------------------------------------------------------------------


def _pending_hit(action_id: str, tool_name: str, parameters: dict):
    """Build an ES hit shaped like an agent_approval_queue pending entry."""
    return {
        "_source": {
            "action_id": action_id,
            "tool_name": tool_name,
            "status": "pending",
            "parameters": parameters,
        }
    }


def _queueing_protocol(pending_hits: list):
    """A protocol in suggest-only mode (so mutations queue) whose approval
    queue's ES returns ``pending_hits`` for the dedup lookup."""
    protocol = _make_protocol(
        risk_level=RiskLevel.MEDIUM, autonomy_level="suggest-only"
    )
    protocol._approval_queue._es = MagicMock()
    protocol._approval_queue._es.search_documents = AsyncMock(
        return_value={"hits": {"hits": pending_hits}}
    )
    return protocol


class TestApprovalQueueDeduplication:
    """Dedup keys on the *target identity*, not the full parameter set."""

    async def test_same_station_is_deduped_despite_different_quantity(self):
        """A pending refill for FS-011 suppresses a new FS-011 refill even when
        the recomputed quantity differs — no duplicate pending request."""
        protocol = _queueing_protocol(
            [
                _pending_hit(
                    "existing-1",
                    "request_fuel_refill",
                    {"station_id": "FS-011", "quantity_liters": 16800.0,
                     "priority": "medium"},
                )
            ]
        )
        request = _make_request(
            tool_name="request_fuel_refill",
            parameters={"station_id": "FS-011", "quantity_liters": 17250.0,
                        "priority": "high"},
            agent_id="fuel_management_agent",
        )

        result = await protocol.process_mutation(request)

        assert result.confirmation_method == "already_queued"
        assert result.approval_id == "existing-1"
        protocol._approval_queue.create.assert_not_called()

    async def test_different_station_still_queues(self):
        """A pending refill for FS-011 must NOT suppress a refill for FS-014."""
        protocol = _queueing_protocol(
            [
                _pending_hit(
                    "existing-1",
                    "request_fuel_refill",
                    {"station_id": "FS-011", "quantity_liters": 16800.0},
                )
            ]
        )
        request = _make_request(
            tool_name="request_fuel_refill",
            parameters={"station_id": "FS-014", "quantity_liters": 14200.0},
            agent_id="fuel_management_agent",
        )

        result = await protocol.process_mutation(request)

        assert result.confirmation_method == "approval_queue"
        protocol._approval_queue.create.assert_awaited_once()

    async def test_no_pending_entries_queues_normally(self):
        """With nothing pending, the proposal is queued (create called)."""
        protocol = _queueing_protocol([])
        request = _make_request(
            tool_name="request_fuel_refill",
            parameters={"station_id": "FS-003", "quantity_liters": 30600.0},
            agent_id="fuel_management_agent",
        )

        result = await protocol.process_mutation(request)

        assert result.confirmation_method == "approval_queue"
        protocol._approval_queue.create.assert_awaited_once()

    async def test_unregistered_tool_falls_back_to_full_param_match(self):
        """Tools without a registered identity dedup on exact parameters."""
        protocol = _queueing_protocol(
            [_pending_hit("existing-1", "mystery_tool", {"a": 1, "b": 2})]
        )
        # Identical params → duplicate.
        dup = await protocol.process_mutation(
            _make_request(tool_name="mystery_tool", parameters={"a": 1, "b": 2})
        )
        assert dup.confirmation_method == "already_queued"

        # Different params → not a duplicate.
        protocol._approval_queue.create.reset_mock()
        fresh = await protocol.process_mutation(
            _make_request(tool_name="mystery_tool", parameters={"a": 1, "b": 99})
        )
        assert fresh.confirmation_method == "approval_queue"


# ---------------------------------------------------------------------------
# T-U13: apply_loading_plan auto path and refusal (loading-plan-executor K1)
# ---------------------------------------------------------------------------

_T = "tenant-1"
_AGENT = "compartment_loading"


def _loading_harness(*order_ids, autonomy="full-auto", **kwargs):
    return ApprovalHarness(
        [order_fixture(o, tenant_id=_T) for o in order_ids], autonomy=autonomy, **kwargs
    )


def _loading_request(h, seed_id, order_ids):
    """A proposal for a seeded plan, as CompartmentLoadingAgent routes it."""
    entry = h.add_plan(seed_id, order_ids)
    h.store.remove(APPROVALS, seed_id)
    return MutationRequest(
        tool_name="apply_loading_plan",
        parameters=entry["parameters"],
        tenant_id=_T,
        agent_id=_AGENT,
    )


def _created(h):
    """The approval entries process_mutation created (one per call)."""
    return list(h.store.docs[APPROVALS].values())


class TestLoadingPlanAutoPath:
    async def test_full_auto_success_runs_through_the_approval_lifecycle(self):
        h = _loading_harness("o1", "o2")
        approve_calls = []
        real_approve = h.svc.approve

        async def spy(*args, **kwargs):
            approve_calls.append(kwargs)
            return await real_approve(*args, **kwargs)

        h.svc.approve = spy
        result = await h.protocol.process_mutation(_loading_request(h, "seed", ["o1", "o2"]))

        assert result.executed is True and result.confirmation_method == "immediate"
        assert "Unknown tool" not in (result.result or "")
        (entry,) = _created(h)
        assert entry["status"] == "executed"
        assert entry["reviewed_by"] == "agent:compartment_loading"
        assert entry["execution_result"]["auto_executed"] is True
        assert result.approval_id == entry["action_id"]
        assert h.ws.types() == ["approval_created", "approval_approved", "approval_execution_updated"]
        assert approve_calls == [{
            "reviewer_id": "agent:compartment_loading", "tenant_id": _T,
            "session_user_id": None, "agent_actor": "agent:compartment_loading",
        }]
        assert len(h.execute_calls) == 1
        assert h.activity.mutations == [("immediate", result.result, "success")]
        assert h.order("o1")["status"] == "scheduled"

    async def test_fault_leaves_incomplete_auto_entry_a_dispatcher_can_finish(self):
        h = _loading_harness("o1", "o2")
        h.store.fail_on("atomic_update", ORDERS, "o2", nth=2)
        result = await h.protocol.process_mutation(_loading_request(h, "seed", ["o1", "o2"]))
        assert result.executed is False and result.confirmation_method == "immediate"
        (entry,) = _created(h)
        assert entry["status"] == "incomplete"
        assert entry["execution_result"]["auto_executed"] is True
        assert h.activity.mutations == [("immediate", None, "pending_approval")]
        done = await h.approve(entry["action_id"], user="dispatcher-1")
        assert done["status"] == "executed"
        assert done["execution_result"]["auto_executed"] is True

    async def test_overlap_with_a_holder_queues_without_executing(self):
        h = _loading_harness("o1", "o2")
        h.add_plan("HOLD", ["o1"], status="executed", execution_result={"attempt_id": "a0"})
        result = await h.protocol.process_mutation(_loading_request(h, "seed", ["o1", "o2"]))
        assert result.executed is False and result.confirmation_method == "approval_queue"
        assert result.result == "Queued for approval: overlaps a plan already being applied"
        assert h.store.doc(APPROVALS, result.approval_id)["status"] == "pending"
        assert h.execute_calls == []
        assert h.activity.mutations == [("approval_queue", None, "pending_approval")]

    async def test_shadow_tenant_records_shadowed(self):
        h = _loading_harness("o1", ff=FakeFeatureFlagService("shadow"))
        result = await h.protocol.process_mutation(_loading_request(h, "seed", ["o1"]))
        assert result.executed is False and result.confirmation_method == "immediate"
        assert h.store.doc(APPROVALS, result.approval_id)["status"] == "shadowed"
        assert h.activity.mutations == [("immediate", None, "pending_approval")]

    async def test_unresolvable_mode_leaves_created_entry_pending(self):
        h = _loading_harness("o1", ff=FakeFeatureFlagService(raises=ConnectionError("down")))
        result = await h.protocol.process_mutation(_loading_request(h, "seed", ["o1"]))
        assert result.executed is False and result.confirmation_method == "approval_queue"
        assert result.result == MESSAGE_TEMPLATES["mode_unavailable"].format(plan_id="plan-seed")
        assert h.store.doc(APPROVALS, result.approval_id)["status"] == "pending"
        assert h.activity.mutations == [("approval_queue", None, "pending_approval")]

    async def test_unwired_executor_message_comes_from_the_template(self):
        h = _loading_harness("o1", wired=False)
        result = await h.protocol.process_mutation(_loading_request(h, "seed", ["o1"]))
        assert result.confirmation_method == "approval_queue"
        assert result.result == MESSAGE_TEMPLATES["executor_unavailable"].format(plan_id="plan-seed")

    async def test_non_overlap_value_error_is_left_for_review(self):
        h = _loading_harness("o1")
        h.svc.approve = AsyncMock(side_effect=ValueError(
            "Cannot approve action X: overlapping loading plan Y changed while approving; retry"
        ))
        result = await h.protocol.process_mutation(_loading_request(h, "seed", ["o1"]))
        assert result.executed is False and result.confirmation_method == "approval_queue"
        assert result.result == "Queued for approval: the plan could not be approved automatically"
        assert h.activity.mutations == [("approval_queue", None, "pending_approval")]

    async def test_overlap_error_gives_the_overlap_text(self):
        h = _loading_harness("o1")
        h.svc.approve = AsyncMock(side_effect=LoadingPlanOverlapError("conflict"))
        result = await h.protocol.process_mutation(_loading_request(h, "seed", ["o1"]))
        assert result.result == "Queued for approval: overlaps a plan already being applied"

    async def test_in_progress_error_message_is_a_template(self):
        h = _loading_harness("o1")

        async def busy(action_id, **kwargs):
            raise LoadingPlanExecutionError.from_reason(
                h.store.doc(APPROVALS, action_id), "execution_in_progress",
                retryable=True, writes_made=True,
            )

        h.svc.approve = busy
        result = await h.protocol.process_mutation(_loading_request(h, "seed", ["o1"]))
        assert result.result == MESSAGE_TEMPLATES["execution_in_progress"].format(plan_id="plan-seed")
        assert result.confirmation_method == "approval_queue"  # the entry is still pending

    async def test_full_auto_never_calls_execute_mutation_for_the_tool(self):
        h = _loading_harness("o1")
        h.protocol._execute_mutation = AsyncMock(side_effect=AssertionError("must not run"))
        result = await h.protocol.process_mutation(_loading_request(h, "seed", ["o1"]))
        assert result.executed is True
        h.protocol._execute_mutation.assert_not_called()

    async def test_suggest_only_queues(self):
        h = _loading_harness("o1", autonomy="suggest-only")
        result = await h.protocol.process_mutation(_loading_request(h, "seed", ["o1"]))
        assert result.executed is False and result.confirmation_method == "approval_queue"
        assert h.store.doc(APPROVALS, result.approval_id)["status"] == "pending"
        assert h.execute_calls == []

    async def test_approve_without_session_or_agent_actor_is_forbidden(self):
        h = _loading_harness("o1")
        h.add_plan("A", ["o1"])
        with pytest.raises(ApprovalForbiddenError):
            await h.svc.approve("A", reviewer_id="x", tenant_id=_T, session_user_id=None, agent_actor=None)


class TestExecuteMutationRefusesLoadingPlans:
    async def test_direct_call_returns_the_refusal_and_writes_nothing(self, caplog):
        h = _loading_harness("o1")
        request = _loading_request(h, "seed", ["o1"])
        h.svc.create = AsyncMock()
        mark = h.mark()
        with caplog.at_level(_logging.ERROR, logger="Agents.confirmation_protocol"):
            with pytest.raises(MutationExecutionError) as exc:
                await h.protocol._execute_mutation(request)
        text = str(exc.value)
        assert text == "apply_loading_plan runs only through the approval queue; no mutation executed"
        assert "Unknown tool" not in text and "Successfully" not in text
        h.svc.create.assert_not_called()
        assert h.execute_calls == []
        assert h.writes_since(mark) == []
        errors = [r for r in caplog.records if r.levelno == _logging.ERROR]
        assert len(errors) == 1

    async def test_refusal_holds_without_an_es_service(self):
        protocol = _make_protocol()
        with pytest.raises(MutationExecutionError) as exc:
            await protocol._execute_mutation(_make_request(tool_name="apply_loading_plan"))
        text = str(exc.value)
        assert "approval queue" in text and "not wired" not in text


class TestLoadingPlanExecutorRegistry:
    async def test_unwired_protocol(self):
        protocol = _make_protocol()
        assert protocol.has_loading_plan_executor() is False
        assert await protocol.resolve_loading_mode("t1") is None
        result = await protocol.execute_loading_plan(
            _make_request(tool_name="apply_loading_plan", parameters={"plan_id": "P1"}),
            mode="active_gated", actor_user_id="u", action_id="a", approved_at=None,
        )
        assert result.reason == "executor_unavailable" and result.retryable is True
        assert result.writes_made is False

    async def test_execute_loading_plan_passes_parameters_and_mode(self):
        protocol = _make_protocol()
        executor = MagicMock()
        executor.resolve_mode = AsyncMock(return_value="shadow")
        executor.execute = AsyncMock(return_value="result")
        protocol.set_loading_plan_executor(executor)
        assert protocol.has_loading_plan_executor() is True
        assert await protocol.resolve_loading_mode("t1") == "shadow"
        params = {
            "plan_id": "P1", "truck_id": "T1", "order_ids": ["b", "a"],
            "order_snapshots": {"a": {"gallons_requested": 1.0}},
        }
        out = await protocol.execute_loading_plan(
            _make_request(tool_name="apply_loading_plan", parameters=params),
            mode="active_gated", actor_user_id="u1", action_id="act", approved_at="2026-07-29T00:00:00Z",
        )
        assert out == "result"
        executor.execute.assert_awaited_once_with(
            tenant_id="t1", plan_id="P1", expected_order_ids=["a", "b"], expected_truck_id="T1",
            order_snapshots={"a": {"gallons_requested": 1.0}}, actor_user_id="u1",
            action_id="act", approved_at="2026-07-29T00:00:00Z", mode="active_gated",
            approval_attempt_id=None,
        )

    async def test_execute_loading_plan_forwards_the_approval_attempt_id(self):
        protocol = _make_protocol()
        executor = MagicMock()
        executor.execute = AsyncMock(return_value="result")
        protocol.set_loading_plan_executor(executor)
        await protocol.execute_loading_plan(
            _make_request(tool_name="apply_loading_plan", parameters={"plan_id": "P1"}),
            mode="active_gated", actor_user_id="u1", action_id="act", approved_at=None,
            approval_attempt_id="appr-1",
        )
        assert executor.execute.await_args.kwargs["approval_attempt_id"] == "appr-1"


class TestLoadingPlanExecutionErrorShape:
    def test_from_stored_on_a_partial_record(self):
        exc = LoadingPlanExecutionError.from_stored({"parameters": {"plan_id": "P1"}, "execution_result": {}})
        assert exc.result.writes_made is True
        assert exc.result.message == MESSAGE_TEMPLATES["internal_error"].format(plan_id="")

    def test_from_reason_uses_the_template(self):
        exc = LoadingPlanExecutionError.from_reason(
            {"parameters": {"plan_id": "P1"}, "status": "approved"}, "legacy_approval", retryable=False
        )
        assert str(exc) == exc.result.message == MESSAGE_TEMPLATES["legacy_approval"].format(plan_id="P1")
        assert exc.entry["status"] == "approved"

    @pytest.mark.parametrize("d", [{}, {"reason": None}, {"reason": "no_such_reason"}])
    def test_from_dict_never_raises(self, d):
        result = LoadingPlanExecutionResult.from_dict(d)
        assert result.message == MESSAGE_TEMPLATES["internal_error"].format(plan_id="")
        assert result.writes_made is True

    def test_failure_with_unknown_reason(self):
        result = LoadingPlanExecutionResult.failure("P1", "no_such_reason", retryable=True, writes_made=False)
        assert result.message == MESSAGE_TEMPLATES["internal_error"].format(plan_id="P1")
