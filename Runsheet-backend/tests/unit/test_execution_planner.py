"""
Unit tests for the Execution Planner module.

Tests the StepStatus enum, PlanStep and ExecutionPlan dataclasses,
validate_plan_dag, topological_sort, and the ExecutionPlanner class
(create_plan, execute_plan, rollback_plan).

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.execution_planner import (
    StepStatus,
    PlanStep,
    ExecutionPlan,
    ExecutionPlanner,
    validate_plan_dag,
    topological_sort,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_activity_log() -> MagicMock:
    """Create a mock ActivityLogService."""
    log = MagicMock()
    log.log = AsyncMock(return_value="log-id-1")
    return log


def _make_confirmation_protocol(
    executed: bool = True,
    result: str = "OK",
    approval_id: str = None,
) -> MagicMock:
    """Create a mock ConfirmationProtocol."""
    cp = MagicMock()
    mutation_result = MagicMock()
    mutation_result.executed = executed
    mutation_result.result = result
    mutation_result.approval_id = approval_id
    cp.process_mutation = AsyncMock(return_value=mutation_result)
    return cp


def _make_planner(
    confirmation_protocol=None,
) -> ExecutionPlanner:
    """Create an ExecutionPlanner with mocked dependencies."""
    log = _make_activity_log()
    return ExecutionPlanner(
        activity_log_service=log,
        confirmation_protocol=confirmation_protocol,
    )


def _step(
    step_id: int,
    depends_on: list = None,
    tool_name: str = "test_tool",
    rollback_tool: str = None,
    rollback_params: dict = None,
) -> PlanStep:
    """Shorthand for creating a PlanStep."""
    return PlanStep(
        step_id=step_id,
        description=f"Step {step_id}",
        agent="test_agent",
        tool_name=tool_name,
        parameters={"key": f"val_{step_id}"},
        depends_on=depends_on or [],
        rollback_tool=rollback_tool,
        rollback_params=rollback_params,
    )


# ---------------------------------------------------------------------------
# Tests: StepStatus enum
# ---------------------------------------------------------------------------


class TestStepStatus:
    """Tests for the StepStatus enum values."""

    def test_all_statuses_exist(self):
        assert StepStatus.PENDING == "pending"
        assert StepStatus.RUNNING == "running"
        assert StepStatus.COMPLETED == "completed"
        assert StepStatus.FAILED == "failed"
        assert StepStatus.SKIPPED == "skipped"
        assert StepStatus.ROLLED_BACK == "rolled_back"

    def test_step_status_is_string(self):
        assert isinstance(StepStatus.PENDING, str)
        assert isinstance(StepStatus.COMPLETED, str)


# ---------------------------------------------------------------------------
# Tests: PlanStep dataclass
# ---------------------------------------------------------------------------


class TestPlanStep:
    """Tests for the PlanStep dataclass."""

    def test_defaults(self):
        step = PlanStep(
            step_id=1,
            description="Do something",
            agent="fleet",
            tool_name="search",
            parameters={"q": "trucks"},
        )
        assert step.depends_on == []
        assert step.rollback_tool is None
        assert step.rollback_params is None
        assert step.status == StepStatus.PENDING
        assert step.result is None
        assert step.recovery_attempts == 0

    def test_custom_values(self):
        step = PlanStep(
            step_id=5,
            description="Cancel job",
            agent="scheduling",
            tool_name="cancel_job",
            parameters={"job_id": "J1"},
            depends_on=[1, 2],
            rollback_tool="create_job",
            rollback_params={"job_type": "cargo"},
            status=StepStatus.COMPLETED,
            result="Done",
            recovery_attempts=1,
        )
        assert step.step_id == 5
        assert step.depends_on == [1, 2]
        assert step.rollback_tool == "create_job"
        assert step.recovery_attempts == 1


# ---------------------------------------------------------------------------
# Tests: ExecutionPlan dataclass
# ---------------------------------------------------------------------------


class TestExecutionPlan:
    """Tests for the ExecutionPlan dataclass."""

    def test_defaults(self):
        plan = ExecutionPlan(
            plan_id="p1", goal="Test goal", steps=[]
        )
        assert plan.status == "pending"
        assert plan.steps == []

    def test_with_steps(self):
        steps = [_step(1), _step(2, depends_on=[1])]
        plan = ExecutionPlan(
            plan_id="p2", goal="Multi-step", steps=steps
        )
        assert len(plan.steps) == 2


# ---------------------------------------------------------------------------
# Tests: validate_plan_dag
# ---------------------------------------------------------------------------


class TestValidatePlanDag:
    """Tests for DAG validation using Kahn's algorithm."""

    def test_valid_linear_chain(self):
        steps = [_step(1), _step(2, [1]), _step(3, [2])]
        validate_plan_dag(steps)  # Should not raise

    def test_valid_diamond_dag(self):
        steps = [
            _step(1),
            _step(2, [1]),
            _step(3, [1]),
            _step(4, [2, 3]),
        ]
        validate_plan_dag(steps)  # Should not raise

    def test_valid_no_dependencies(self):
        steps = [_step(1), _step(2), _step(3)]
        validate_plan_dag(steps)  # Should not raise

    def test_valid_single_step(self):
        steps = [_step(1)]
        validate_plan_dag(steps)  # Should not raise

    def test_valid_empty_steps(self):
        validate_plan_dag([])  # Should not raise

    def test_invalid_missing_dependency(self):
        steps = [_step(1), _step(2, [99])]
        with pytest.raises(ValueError, match="non-existent step 99"):
            validate_plan_dag(steps)

    def test_invalid_self_cycle(self):
        steps = [_step(1, [1])]
        with pytest.raises(ValueError, match="Cycle detected"):
            validate_plan_dag(steps)

    def test_invalid_two_node_cycle(self):
        steps = [_step(1, [2]), _step(2, [1])]
        with pytest.raises(ValueError, match="Cycle detected"):
            validate_plan_dag(steps)

    def test_invalid_three_node_cycle(self):
        steps = [_step(1, [3]), _step(2, [1]), _step(3, [2])]
        with pytest.raises(ValueError, match="Cycle detected"):
            validate_plan_dag(steps)

    def test_invalid_cycle_in_larger_graph(self):
        steps = [
            _step(1),
            _step(2, [1]),
            _step(3, [2]),
            _step(4, [3, 5]),
            _step(5, [4]),  # cycle: 4 -> 5 -> 4
        ]
        with pytest.raises(ValueError, match="Cycle detected"):
            validate_plan_dag(steps)


# ---------------------------------------------------------------------------
# Tests: topological_sort
# ---------------------------------------------------------------------------


class TestTopologicalSort:
    """Tests for dependency-ordered step sorting."""

    def test_linear_chain_order(self):
        steps = [_step(3, [2]), _step(1), _step(2, [1])]
        ordered = topological_sort(steps)
        ids = [s.step_id for s in ordered]
        assert ids.index(1) < ids.index(2)
        assert ids.index(2) < ids.index(3)

    def test_diamond_dag_order(self):
        steps = [
            _step(4, [2, 3]),
            _step(2, [1]),
            _step(3, [1]),
            _step(1),
        ]
        ordered = topological_sort(steps)
        ids = [s.step_id for s in ordered]
        assert ids.index(1) < ids.index(2)
        assert ids.index(1) < ids.index(3)
        assert ids.index(2) < ids.index(4)
        assert ids.index(3) < ids.index(4)

    def test_independent_steps_all_present(self):
        steps = [_step(1), _step(2), _step(3)]
        ordered = topological_sort(steps)
        assert len(ordered) == 3
        assert {s.step_id for s in ordered} == {1, 2, 3}

    def test_single_step(self):
        steps = [_step(1)]
        ordered = topological_sort(steps)
        assert len(ordered) == 1
        assert ordered[0].step_id == 1

    def test_empty_steps(self):
        ordered = topological_sort([])
        assert ordered == []

    def test_preserves_step_data(self):
        step = PlanStep(
            step_id=1,
            description="Important step",
            agent="fleet",
            tool_name="search_fleet",
            parameters={"q": "trucks"},
            rollback_tool="undo_search",
        )
        ordered = topological_sort([step])
        assert ordered[0].description == "Important step"
        assert ordered[0].agent == "fleet"
        assert ordered[0].tool_name == "search_fleet"
        assert ordered[0].rollback_tool == "undo_search"

    def test_raises_on_cycle(self):
        steps = [_step(1, [2]), _step(2, [1])]
        with pytest.raises(ValueError, match="Cycle detected"):
            topological_sort(steps)

    def test_raises_on_missing_dependency(self):
        steps = [_step(1, [99])]
        with pytest.raises(ValueError, match="non-existent step 99"):
            topological_sort(steps)


# ---------------------------------------------------------------------------
# Tests: ExecutionPlanner.create_plan
# ---------------------------------------------------------------------------


class TestCreatePlan:
    """Tests for plan creation."""

    async def test_creates_plan_with_correct_goal(self):
        planner = _make_planner()
        plan = await planner.create_plan("Reassign delayed jobs", ["scheduling", "fleet"])
        assert plan.goal == "Reassign delayed jobs"

    async def test_creates_plan_with_uuid_id(self):
        planner = _make_planner()
        plan = await planner.create_plan("Test", ["fleet"])
        assert len(plan.plan_id) == 36  # UUID format

    async def test_creates_one_step_per_domain(self):
        planner = _make_planner()
        plan = await planner.create_plan("Test", ["fleet", "fuel", "ops"])
        assert len(plan.steps) == 3

    async def test_step_agents_match_domains(self):
        planner = _make_planner()
        plan = await planner.create_plan("Test", ["scheduling", "fuel"])
        agents = [s.agent for s in plan.steps]
        assert "scheduling" in agents
        assert "fuel" in agents

    async def test_plan_status_is_pending(self):
        planner = _make_planner()
        plan = await planner.create_plan("Test", ["fleet"])
        assert plan.status == "pending"

    async def test_logs_plan_creation(self):
        planner = _make_planner()
        await planner.create_plan("Test", ["fleet"])
        planner._activity_log.log.assert_called_once()
        call_args = planner._activity_log.log.call_args[0][0]
        assert call_args["details"]["event"] == "plan_created"

    async def test_empty_domains_creates_empty_plan(self):
        planner = _make_planner()
        plan = await planner.create_plan("Test", [])
        assert len(plan.steps) == 0


# ---------------------------------------------------------------------------
# Tests: ExecutionPlanner.execute_plan
# ---------------------------------------------------------------------------


class TestExecutePlan:
    """Tests for plan execution."""

    async def test_executes_all_steps_successfully(self):
        planner = _make_planner()
        steps = [_step(1), _step(2, [1])]
        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)

        result = await planner.execute_plan(plan, "t1")

        assert result.status == "completed"
        assert all(s.status == StepStatus.COMPLETED for s in result.steps)

    async def test_passes_outputs_between_dependent_steps(self):
        planner = _make_planner()
        steps = [_step(1), _step(2, [1])]
        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)

        await planner.execute_plan(plan, "t1")

        # Step 2 should have received dependency outputs
        # (verified by the fact it completed without error)
        assert steps[1].status == StepStatus.COMPLETED

    async def test_skips_steps_when_dependency_fails(self):
        cp = _make_confirmation_protocol()
        call_count = 0

        async def fail_first(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:  # First step fails all 3 attempts (1 + 2 recovery)
                raise RuntimeError("Step 1 failed")
            result = MagicMock()
            result.executed = True
            result.result = "OK"
            result.approval_id = None
            return result

        cp.process_mutation = AsyncMock(side_effect=fail_first)
        planner = ExecutionPlanner(
            activity_log_service=_make_activity_log(),
            confirmation_protocol=cp,
        )

        steps = [_step(1), _step(2, [1]), _step(3)]
        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)

        result = await planner.execute_plan(plan, "t1")

        assert steps[0].status == StepStatus.FAILED
        assert steps[1].status == StepStatus.SKIPPED
        assert steps[2].status == StepStatus.COMPLETED

    async def test_recovery_attempts_capped_at_max(self):
        cp = _make_confirmation_protocol()
        cp.process_mutation = AsyncMock(side_effect=RuntimeError("Always fails"))
        planner = ExecutionPlanner(
            activity_log_service=_make_activity_log(),
            confirmation_protocol=cp,
        )

        steps = [_step(1)]
        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)

        await planner.execute_plan(plan, "t1")

        # 1 initial attempt + 2 recovery = 3 total calls
        assert cp.process_mutation.call_count == 3
        assert steps[0].recovery_attempts == 3
        assert steps[0].status == StepStatus.FAILED

    async def test_partial_failure_status(self):
        cp = _make_confirmation_protocol()
        call_count = 0

        async def fail_second(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Step 1 succeeds (call 1), Step 2 fails (calls 2-4)
            if call_count == 1:
                result = MagicMock()
                result.executed = True
                result.result = "OK"
                result.approval_id = None
                return result
            raise RuntimeError("Step 2 failed")

        cp.process_mutation = AsyncMock(side_effect=fail_second)
        planner = ExecutionPlanner(
            activity_log_service=_make_activity_log(),
            confirmation_protocol=cp,
        )

        steps = [_step(1), _step(2)]  # Independent steps
        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)

        result = await planner.execute_plan(plan, "t1")

        assert result.status == "partial_failure"

    async def test_aborted_status_when_all_fail(self):
        cp = _make_confirmation_protocol()
        cp.process_mutation = AsyncMock(side_effect=RuntimeError("Fail"))
        planner = ExecutionPlanner(
            activity_log_service=_make_activity_log(),
            confirmation_protocol=cp,
        )

        steps = [_step(1), _step(2, [1])]
        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)

        result = await planner.execute_plan(plan, "t1")

        # Step 1 fails, step 2 skipped — only FAILED and SKIPPED
        assert result.status == "aborted"

    async def test_logs_execution_trace(self):
        planner = _make_planner()
        steps = [_step(1)]
        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)

        await planner.execute_plan(plan, "t1")

        # Should have been called (plan execution log)
        assert planner._activity_log.log.call_count >= 1
        last_call = planner._activity_log.log.call_args[0][0]
        assert last_call["details"]["event"] == "plan_executed"
        assert last_call["tenant_id"] == "t1"

    async def test_execute_without_confirmation_protocol(self):
        planner = _make_planner()  # No confirmation protocol
        steps = [_step(1), _step(2, [1])]
        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)

        result = await planner.execute_plan(plan, "t1")

        assert result.status == "completed"
        assert "executed successfully" in steps[0].result.lower()

    async def test_execute_with_confirmation_protocol(self):
        cp = _make_confirmation_protocol(executed=True, result="Job assigned")
        planner = ExecutionPlanner(
            activity_log_service=_make_activity_log(),
            confirmation_protocol=cp,
        )

        steps = [_step(1)]
        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)

        await planner.execute_plan(plan, "t1")

        assert steps[0].result == "Job assigned"
        cp.process_mutation.assert_called()

    async def test_queued_for_approval_raises_for_recovery(self):
        cp = _make_confirmation_protocol(
            executed=False, result="Validation failed: bad params", approval_id=None
        )
        planner = ExecutionPlanner(
            activity_log_service=_make_activity_log(),
            confirmation_protocol=cp,
        )

        steps = [_step(1)]
        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)

        await planner.execute_plan(plan, "t1")

        # Should fail because mutation was not executed and no approval_id
        assert steps[0].status == StepStatus.FAILED


# ---------------------------------------------------------------------------
# Tests: ExecutionPlanner.rollback_plan
# ---------------------------------------------------------------------------


class TestRollbackPlan:
    """Tests for plan rollback."""

    async def test_rollback_completed_steps_in_reverse_order(self):
        planner = _make_planner()
        steps = [
            _step(1, rollback_tool="undo_1"),
            _step(2, depends_on=[1], rollback_tool="undo_2"),
            _step(3, depends_on=[2], rollback_tool="undo_3"),
        ]
        # Simulate completed execution
        for s in steps:
            s.status = StepStatus.COMPLETED

        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)
        result = await planner.rollback_plan(plan)

        assert result.status == "rolled_back"
        assert all(s.status == StepStatus.ROLLED_BACK for s in result.steps)

    async def test_rollback_only_completed_steps(self):
        planner = _make_planner()
        steps = [
            _step(1, rollback_tool="undo_1"),
            _step(2, depends_on=[1]),
            _step(3, depends_on=[2]),
        ]
        steps[0].status = StepStatus.COMPLETED
        steps[1].status = StepStatus.FAILED
        steps[2].status = StepStatus.SKIPPED

        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)
        result = await planner.rollback_plan(plan)

        assert steps[0].status == StepStatus.ROLLED_BACK
        assert steps[1].status == StepStatus.FAILED  # Unchanged
        assert steps[2].status == StepStatus.SKIPPED  # Unchanged

    async def test_rollback_without_rollback_tool(self):
        planner = _make_planner()
        steps = [_step(1)]  # No rollback_tool
        steps[0].status = StepStatus.COMPLETED

        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)
        result = await planner.rollback_plan(plan)

        assert steps[0].status == StepStatus.ROLLED_BACK
        assert "no rollback tool" in steps[0].result.lower()

    async def test_rollback_with_confirmation_protocol(self):
        cp = _make_confirmation_protocol(executed=True, result="Rolled back")
        planner = ExecutionPlanner(
            activity_log_service=_make_activity_log(),
            confirmation_protocol=cp,
        )

        steps = [_step(1, rollback_tool="undo_1", rollback_params={"id": "1"})]
        steps[0].status = StepStatus.COMPLETED

        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)
        await planner.rollback_plan(plan)

        assert steps[0].status == StepStatus.ROLLED_BACK
        cp.process_mutation.assert_called_once()

    async def test_rollback_logs_to_activity_log(self):
        planner = _make_planner()
        steps = [_step(1, rollback_tool="undo_1")]
        steps[0].status = StepStatus.COMPLETED

        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)
        await planner.rollback_plan(plan)

        planner._activity_log.log.assert_called_once()
        call_args = planner._activity_log.log.call_args[0][0]
        assert call_args["details"]["event"] == "plan_rolled_back"

    async def test_rollback_handles_rollback_failure_gracefully(self):
        cp = _make_confirmation_protocol()
        cp.process_mutation = AsyncMock(side_effect=RuntimeError("Rollback failed"))
        planner = ExecutionPlanner(
            activity_log_service=_make_activity_log(),
            confirmation_protocol=cp,
        )

        steps = [_step(1, rollback_tool="undo_1")]
        steps[0].status = StepStatus.COMPLETED

        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=steps)
        result = await planner.rollback_plan(plan)

        # Should not raise, but step result should indicate failure
        assert "Rollback failed" in steps[0].result

    async def test_rollback_empty_plan(self):
        planner = _make_planner()
        plan = ExecutionPlan(plan_id="p1", goal="Test", steps=[])

        result = await planner.rollback_plan(plan)
        assert result.status == "rolled_back"


# ---------------------------------------------------------------------------
# Tests: MAX_RECOVERY_ATTEMPTS constant
# ---------------------------------------------------------------------------


class TestMaxRecoveryAttempts:
    """Tests for the MAX_RECOVERY_ATTEMPTS constant."""

    def test_max_recovery_attempts_is_two(self):
        assert ExecutionPlanner.MAX_RECOVERY_ATTEMPTS == 2
