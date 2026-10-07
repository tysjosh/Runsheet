"""Agent job mutations go through JobService (OI-15).

``ConfirmationProtocol._execute_mutation`` wrote the four job tools straight
to the legacy ``jobs`` index (the UI and ``JobService`` read ``jobs_current``),
skipped ``VALID_TRANSITIONS``, and turned any failure into a returned string
that both the immediate path and the approval queue reported as success.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.confirmation_protocol import (
    ConfirmationProtocol,
    MutationExecutionError,
    MutationRequest,
)
from Agents.risk_registry import RiskLevel
from errors.codes import ErrorCode
from errors.exceptions import AppException
from scheduling.models import CreateJob, StatusTransition
from tests.unit.test_approval_queue_service import _make_service


class _FakeJobService:
    def __init__(self, raises: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._raises = raises

    async def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(job_id="JOB_42")

    async def create_job(self, data, tenant_id, actor_id=None):
        return await self._record("create_job", data, tenant_id, actor_id=actor_id)

    async def assign_asset(self, job_id, asset_id, tenant_id, actor_id=None):
        return await self._record(
            "assign_asset", job_id, asset_id, tenant_id, actor_id=actor_id
        )

    async def transition_status(self, job_id, transition, tenant_id, actor_id=None):
        return await self._record(
            "transition_status", job_id, transition, tenant_id, actor_id=actor_id
        )


def _fake_es() -> MagicMock:
    es = MagicMock()
    es.update_document = AsyncMock()
    es.index_document = AsyncMock()
    return es


def _protocol(job_service, es=None, autonomy="full-auto") -> ConfirmationProtocol:
    risk_registry = MagicMock()
    risk_registry.classify = AsyncMock(return_value=RiskLevel.HIGH)
    autonomy_config = MagicMock()
    autonomy_config.get_level = AsyncMock(return_value=autonomy)
    activity_log = MagicMock()
    activity_log.log_mutation = AsyncMock(return_value="log-1")
    validator = MagicMock()
    validator.validate = AsyncMock(return_value=SimpleNamespace(valid=True, reason=None))
    approval_queue = MagicMock()
    approval_queue.create = AsyncMock(return_value="approval-1")
    return ConfirmationProtocol(
        risk_registry=risk_registry,
        approval_queue_service=approval_queue,
        autonomy_config_service=autonomy_config,
        activity_log_service=activity_log,
        business_validator=validator,
        es_service=es if es is not None else _fake_es(),
        job_service=job_service,
    )


def _request(tool_name: str, parameters: dict) -> MutationRequest:
    return MutationRequest(
        tool_name=tool_name, parameters=parameters, tenant_id="t1", agent_id="ai_agent"
    )


_CASES = {
    "create_job": {
        "job_type": "fuel_delivery",
        "origin": "Depot A",
        "destination": "Station B",
        "scheduled_time": "2026-10-08T09:00:00Z",
        "asset_id": "T-1",
    },
    "assign_asset_to_job": {"job_id": "JOB_1", "asset_id": "T-1"},
    "update_job_status": {"job_id": "JOB_1", "new_status": "in_progress", "reason": "go"},
    "cancel_job": {"job_id": "JOB_1", "reason": "customer called"},
}


def _assert_no_jobs_index_write(es: MagicMock) -> None:
    for call in es.update_document.call_args_list + es.index_document.call_args_list:
        assert call.args[0] != "jobs", call


@pytest.mark.parametrize("tool_name", sorted(_CASES))
async def test_each_job_tool_calls_job_service_and_never_writes_jobs(tool_name):
    jobs = _FakeJobService()
    es = _fake_es()
    protocol = _protocol(jobs, es)

    result = await protocol.process_mutation(_request(tool_name, dict(_CASES[tool_name])))

    assert result.executed is True, result
    assert len(jobs.calls) == 1
    name, args, kwargs = jobs.calls[0]
    assert kwargs["actor_id"] == "agent:ai_agent"
    if tool_name == "create_job":
        assert name == "create_job"
        data, tenant = args
        assert isinstance(data, CreateJob)
        assert (data.origin, data.destination, data.asset_assigned) == (
            "Depot A", "Station B", "T-1",
        )
        assert tenant == "t1"
        assert "JOB_42" in result.result
    elif tool_name == "assign_asset_to_job":
        assert name == "assign_asset"
        assert args == ("JOB_1", "T-1", "t1")
    elif tool_name == "update_job_status":
        assert name == "transition_status"
        job_id, transition, tenant = args
        assert (job_id, tenant) == ("JOB_1", "t1")
        assert transition == StatusTransition(status="in_progress")
    else:
        assert name == "transition_status"
        job_id, transition, tenant = args
        assert transition == StatusTransition(status="cancelled")
        assert "customer called" in result.result
    _assert_no_jobs_index_write(es)


async def test_failed_status_carries_the_reason_as_failure_reason():
    jobs = _FakeJobService()
    protocol = _protocol(jobs)
    await protocol.process_mutation(
        _request(
            "update_job_status",
            {"job_id": "JOB_1", "new_status": "failed", "reason": "truck broke down"},
        )
    )
    _, (_, transition, _), _ = jobs.calls[0]
    assert transition.status == "failed"
    assert transition.failure_reason == "truck broke down"


async def test_invalid_transition_is_reported_as_not_executed():
    jobs = _FakeJobService(
        raises=AppException(
            ErrorCode.INVALID_STATUS_TRANSITION,
            "Cannot transition from 'completed' to 'in_progress'",
            status_code=409,
        )
    )
    es = _fake_es()
    protocol = _protocol(jobs, es)

    result = await protocol.process_mutation(
        _request("update_job_status", dict(_CASES["update_job_status"]))
    )

    assert result.executed is False
    assert result.confirmation_method == "immediate"
    assert "Cannot transition from 'completed' to 'in_progress'" in result.result
    # The attempt is logged with no result, so it can't read as a success.
    call = protocol._activity_log.log_mutation.call_args
    assert call.args[2] == "immediate"
    assert call.args[3] is None
    _assert_no_jobs_index_write(es)


@pytest.mark.parametrize("tool_name", sorted(_CASES))
async def test_without_job_service_each_tool_is_refused(tool_name):
    es = _fake_es()
    protocol = _protocol(None, es)

    result = await protocol.process_mutation(_request(tool_name, dict(_CASES[tool_name])))

    assert result.executed is False
    assert "Job tools are disabled: JobService is not wired" in result.result
    es.update_document.assert_not_called()
    es.index_document.assert_not_called()


async def test_execute_mutation_raises_for_job_tools_without_job_service():
    protocol = _protocol(None)
    with pytest.raises(MutationExecutionError, match="disabled"):
        await protocol._execute_mutation(_request("cancel_job", {"job_id": "JOB_1"}))


async def test_approved_cancel_job_that_job_service_refuses_records_failure():
    jobs = _FakeJobService(
        raises=AppException(
            ErrorCode.INVALID_STATUS_TRANSITION,
            "Cannot transition from 'completed' to 'cancelled'",
            status_code=409,
        )
    )
    protocol = _protocol(jobs)
    entry = {
        "action_id": "action-1",
        "action_type": "mutation",
        "tool_name": "cancel_job",
        "parameters": {"job_id": "JOB_1", "reason": "duplicate"},
        "risk_level": "high",
        "proposed_by": "ai_agent",
        "proposed_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "reviewed_by": None,
        "reviewed_at": None,
        "expiry_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "impact_summary": "Cancel job JOB_1",
        "tenant_id": "t1",
    }
    service = _make_service(get_response=entry, confirmation_protocol=protocol)

    result = await service.approve("action-1", "reviewer-1")

    assert result["execution_result"]["success"] is False
    assert "Cannot transition from 'completed' to 'cancelled'" in result["execution_result"]["error"]
    assert jobs.calls[0][0] == "transition_status"


class TestChatFormatting:
    """The chat reply for a refused immediate mutation must not say "queued"."""

    def test_immediate_not_executed_renders_as_not_executed(self):
        from Agents.confirmation_protocol import MutationResult
        from Agents.tools.mutation_tools import _format_mutation_result

        text = _format_mutation_result(
            MutationResult(
                executed=False,
                result="Job tools are disabled: JobService is not wired",
                risk_level="medium",
                confirmation_method="immediate",
            )
        )
        assert text.startswith("❌ Action not executed (risk: medium)")
        assert "JobService is not wired" in text
        assert "queued" not in text
        assert "Approval ID" not in text

    def test_queued_result_still_renders_as_queued(self):
        from Agents.confirmation_protocol import MutationResult
        from Agents.tools.mutation_tools import _format_mutation_result

        text = _format_mutation_result(
            MutationResult(
                executed=False,
                approval_id="action-9",
                risk_level="high",
                confirmation_method="approval_queue",
            )
        )
        assert text.startswith("⏳ Action queued for approval (risk: high)")
        assert "Approval ID: action-9" in text
