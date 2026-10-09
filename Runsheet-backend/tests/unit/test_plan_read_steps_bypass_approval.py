"""A read must never become a dispatcher approval request.

``ExecutionPlanner.create_plan`` synthesises one step per target domain with an
invented tool name — ``fleet_query``, ``fuel_query``, ``ops_query``. None exist
in ``DEFAULT_RISK_REGISTRY``, and ``RiskRegistry.classify`` returns HIGH for
unknown tools *by design* (safe for mutations, wrong for reads). Routing those
steps through the ConfirmationProtocol therefore meant:

* every step of every "complex request" returned ``"Queued for approval: …"``,
* which is not an exception, so ``_execute_complex_request``'s fallback to
  simple execution never fired and the user got approval ids instead of an
  answer,
* the dispatcher's queue filled with HIGH-risk approvals for reads,
* and approving one hit ``"Unknown tool … no mutation executed"`` because no
  handler is registered for those names.

Read-only steps now go to the specialist that owns the domain.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.execution_planner import ExecutionPlanner, PlanStep


class TestGeneratedStepsAreMarkedReadOnly:
    @pytest.mark.asyncio
    async def test_create_plan_marks_query_steps_read_only(self):
        planner = ExecutionPlanner(activity_log_service=AsyncMock())

        plan = await planner.create_plan("show fleet and fuel", ["fleet", "fuel"])

        assert [s.tool_name for s in plan.steps] == ["fleet_query", "fuel_query"]
        assert all(s.read_only for s in plan.steps), (
            "unmarked steps go through the mutation path, where their invented "
            "tool names classify HIGH and land in the approval queue"
        )

    @pytest.mark.asyncio
    async def test_the_invented_tool_names_really_do_classify_high(self):
        """Pins the premise: this is why routing reads there was harmful."""
        from Agents.risk_registry import RiskLevel, RiskRegistry

        registry = RiskRegistry()
        for name in ("fleet_query", "fuel_query", "ops_query"):
            assert await registry.classify(name) is RiskLevel.HIGH

    def test_a_step_is_a_mutation_unless_it_says_otherwise(self):
        """Default must stay False so real mutations keep their approval gate."""
        step = PlanStep(
            step_id=1, description="d", agent="fleet", tool_name="cancel_job",
            parameters={},
        )
        assert step.read_only is False


class TestReadStepsBypassTheConfirmationProtocol:
    def _planner(self, executor=None):
        confirmation = MagicMock()
        confirmation.process_mutation = AsyncMock()
        planner = ExecutionPlanner(
            activity_log_service=AsyncMock(), confirmation_protocol=confirmation
        )
        if executor is not None:
            planner.set_read_step_executor(executor)
        return planner, confirmation

    def _read_step(self):
        return PlanStep(
            step_id=1,
            description="Execute fleet subtask: how many trucks?",
            agent="fleet",
            tool_name="fleet_query",
            parameters={"request": "how many trucks?"},
            read_only=True,
        )

    @pytest.mark.asyncio
    async def test_a_read_step_never_reaches_the_confirmation_protocol(self):
        executor = AsyncMock(return_value="12 trucks")
        planner, confirmation = self._planner(executor)

        result = await planner._execute_step(
            self._read_step(), {"request": "how many trucks?"}, "tenant-a"
        )

        assert result == "12 trucks"
        assert confirmation.process_mutation.await_count == 0, (
            "a read reached the approval path — the dispatcher queue will fill "
            "with HIGH-risk approvals for questions"
        )

    @pytest.mark.asyncio
    async def test_a_mutation_step_still_goes_through_approval(self):
        """The fix must not open a hole around the confirmation gate."""
        planner, confirmation = self._planner(AsyncMock())
        confirmation.process_mutation.return_value = MagicMock(
            executed=True, approval_id=None, result="done"
        )
        step = PlanStep(
            step_id=1, description="cancel", agent="fleet",
            tool_name="cancel_job", parameters={}, read_only=False,
        )

        await planner._execute_step(step, {}, "tenant-a")

        assert confirmation.process_mutation.await_count == 1

    @pytest.mark.asyncio
    async def test_an_unwired_executor_raises_rather_than_queueing(self):
        """Failing loudly lets the orchestrator fall back to simple execution.

        Returning a cheerful string, or falling through to the mutation path,
        would both hide the misconfiguration.
        """
        planner, confirmation = self._planner(None)

        with pytest.raises(RuntimeError):
            await planner._execute_step(self._read_step(), {}, "tenant-a")

        assert confirmation.process_mutation.await_count == 0

    @pytest.mark.asyncio
    async def test_the_executor_receives_the_resolved_request(self):
        executor = AsyncMock(return_value="ok")
        planner, _ = self._planner(executor)
        step = self._read_step()

        await planner._execute_step(step, {"request": "resolved text"}, "tenant-a")

        called_step, called_params, called_tenant = executor.await_args.args
        assert called_step is step
        assert called_params == {"request": "resolved text"}
        assert called_tenant == "tenant-a"


class TestOrchestratorWiresTheExecutor:
    def test_the_orchestrator_injects_a_read_executor(self):
        from Agents.orchestrator import AgentOrchestrator

        planner = MagicMock()
        AgentOrchestrator(
            specialists={"fleet": MagicMock()},
            execution_planner=planner,
            activity_log_service=MagicMock(),
        )

        assert planner.set_read_step_executor.call_count == 1

    @pytest.mark.asyncio
    async def test_the_executor_delegates_to_the_domain_specialist(self):
        from Agents.orchestrator import AgentOrchestrator

        fleet = MagicMock()
        fleet.handle = AsyncMock(return_value="12 trucks")
        orch = AgentOrchestrator(
            specialists={"fleet": fleet},
            execution_planner=MagicMock(),
            activity_log_service=MagicMock(),
        )
        step = PlanStep(
            step_id=1, description="d", agent="fleet",
            tool_name="fleet_query", parameters={}, read_only=True,
        )

        result = await orch._execute_read_step(
            step, {"request": "how many trucks?"}, "tenant-a"
        )

        assert result == "12 trucks"
        fleet.handle.assert_awaited_once_with(
            "how many trucks?", {"tenant_id": "tenant-a"}
        )

    @pytest.mark.asyncio
    async def test_an_unknown_domain_raises_instead_of_claiming_success(self):
        from Agents.orchestrator import AgentOrchestrator

        orch = AgentOrchestrator(
            specialists={},
            execution_planner=MagicMock(),
            activity_log_service=MagicMock(),
        )
        step = PlanStep(
            step_id=1, description="d", agent="nope",
            tool_name="nope_query", parameters={}, read_only=True,
        )

        with pytest.raises(RuntimeError):
            await orch._execute_read_step(step, {}, "tenant-a")

    def test_a_planner_without_the_setter_does_not_break_construction(self):
        """Older/stubbed planners are used widely in tests."""
        from Agents.orchestrator import AgentOrchestrator

        class _OldPlanner:
            pass

        AgentOrchestrator(
            specialists={},
            execution_planner=_OldPlanner(),
            activity_log_service=MagicMock(),
        )
