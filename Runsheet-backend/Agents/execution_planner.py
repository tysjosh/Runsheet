"""
Execution Planner for multi-step plan generation and execution.

Generates and executes multi-step plans for complex requests. Plans are
structured as directed acyclic graphs (DAGs) of steps with dependency
tracking, error recovery, and rollback support.

Key behaviours:
  - ``validate_plan_dag`` verifies all depends_on references exist and
    detects cycles using Kahn's algorithm.
  - ``topological_sort`` returns steps in a valid execution order where
    no step runs before its dependencies.
  - ``execute_plan`` runs steps in topological order, passing outputs
    from completed steps as inputs to dependent steps. Failed steps are
    retried up to MAX_RECOVERY_ATTEMPTS times; if recovery fails,
    remaining dependent steps are marked SKIPPED.
  - ``rollback_plan`` rolls back completed steps in reverse completion
    order using their rollback_tool/rollback_params.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8
"""
import uuid
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class StepStatus(str, Enum):
    """Status of an individual plan step."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


@dataclass
class PlanStep:
    """A single step within an execution plan.

    Attributes:
        step_id: Unique integer identifier for this step.
        description: Human-readable description of what this step does.
        agent: The specialist agent responsible for executing this step.
        tool_name: The tool to invoke for this step.
        parameters: Parameters to pass to the tool.
        depends_on: List of step_ids that must complete before this step.
        rollback_tool: Optional tool name for rolling back this step.
        rollback_params: Optional parameters for the rollback tool.
        status: Current execution status of this step.
        result: The result string after execution (or error message).
        recovery_attempts: Number of recovery attempts made so far.
    """

    step_id: int
    description: str
    agent: str
    tool_name: str
    parameters: Dict[str, Any]
    depends_on: List[int] = field(default_factory=list)
    rollback_tool: Optional[str] = None
    rollback_params: Optional[Dict[str, Any]] = None
    status: StepStatus = StepStatus.PENDING
    result: Optional[str] = None
    recovery_attempts: int = 0


@dataclass
class ExecutionPlan:
    """A multi-step execution plan.

    Attributes:
        plan_id: Unique identifier for this plan.
        goal: The high-level goal this plan accomplishes.
        steps: Ordered list of plan steps.
        status: Overall plan status (pending, executing, completed,
            partial_failure, aborted).
    """

    plan_id: str
    goal: str
    steps: List[PlanStep]
    status: str = "pending"


# ---------------------------------------------------------------------------
# DAG validation and topological sort
# ---------------------------------------------------------------------------


def validate_plan_dag(steps: List[PlanStep]) -> None:
    """Validate that the plan steps form a valid DAG.

    Checks:
      1. All ``depends_on`` references point to existing step_ids.
      2. The dependency graph contains no cycles (Kahn's algorithm).

    Args:
        steps: The list of plan steps to validate.

    Raises:
        ValueError: If a depends_on reference is invalid or a cycle is
            detected.
    """
    step_ids = {s.step_id for s in steps}

    # 1. Verify all depends_on references exist
    for step in steps:
        for dep_id in step.depends_on:
            if dep_id not in step_ids:
                raise ValueError(
                    f"Step {step.step_id} depends on non-existent step {dep_id}"
                )

    # 2. Detect cycles using Kahn's algorithm
    in_degree: Dict[int, int] = {s.step_id: 0 for s in steps}
    adjacency: Dict[int, List[int]] = defaultdict(list)

    for step in steps:
        for dep_id in step.depends_on:
            adjacency[dep_id].append(step.step_id)
            in_degree[step.step_id] += 1

    queue: deque[int] = deque(
        sid for sid, deg in in_degree.items() if deg == 0
    )
    visited_count = 0

    while queue:
        node = queue.popleft()
        visited_count += 1
        for neighbour in adjacency[node]:
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    if visited_count != len(steps):
        raise ValueError("Cycle detected in plan step dependencies")


def topological_sort(steps: List[PlanStep]) -> List[PlanStep]:
    """Return steps in a valid topological execution order.

    Uses Kahn's algorithm to produce an ordering where no step appears
    before any of its dependencies.

    Args:
        steps: The list of plan steps (must form a valid DAG).

    Returns:
        A new list of PlanStep objects in dependency-safe execution order.

    Raises:
        ValueError: If the dependency graph is invalid (delegates to
            ``validate_plan_dag``).
    """
    validate_plan_dag(steps)

    step_map: Dict[int, PlanStep] = {s.step_id: s for s in steps}
    in_degree: Dict[int, int] = {s.step_id: 0 for s in steps}
    adjacency: Dict[int, List[int]] = defaultdict(list)

    for step in steps:
        for dep_id in step.depends_on:
            adjacency[dep_id].append(step.step_id)
            in_degree[step.step_id] += 1

    queue: deque[int] = deque(
        sid for sid, deg in in_degree.items() if deg == 0
    )
    ordered: List[PlanStep] = []

    while queue:
        node = queue.popleft()
        ordered.append(step_map[node])
        for neighbour in adjacency[node]:
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    return ordered


# ---------------------------------------------------------------------------
# Execution Planner
# ---------------------------------------------------------------------------


class ExecutionPlanner:
    """Generates and executes multi-step plans for complex requests.

    Attributes:
        MAX_RECOVERY_ATTEMPTS: Maximum number of recovery retries per
            failed step (default 2).
    """

    MAX_RECOVERY_ATTEMPTS = 2

    def __init__(self, activity_log_service, confirmation_protocol=None):
        """Initialise the planner with its dependencies.

        Args:
            activity_log_service: ActivityLogService for logging plan
                execution traces.
            confirmation_protocol: Optional ConfirmationProtocol for
                executing mutation steps.
        """
        self._activity_log = activity_log_service
        self._confirmation_protocol = confirmation_protocol

    # ------------------------------------------------------------------
    # Plan creation
    # ------------------------------------------------------------------

    async def create_plan(
        self, request: str, target_domains: list
    ) -> ExecutionPlan:
        """Create an execution plan from a request string and target domains.

        This is a placeholder that creates a single-step plan per target
        domain. Actual LLM-based decomposition will be wired later.

        Args:
            request: The user's natural language request.
            target_domains: List of specialist domain names to involve.

        Returns:
            An ExecutionPlan with steps for each target domain.
        """
        plan_id = str(uuid.uuid4())
        steps: List[PlanStep] = []

        for idx, domain in enumerate(target_domains):
            step = PlanStep(
                step_id=idx + 1,
                description=f"Execute {domain} subtask: {request}",
                agent=domain,
                tool_name=f"{domain}_query",
                parameters={"request": request},
            )
            steps.append(step)

        plan = ExecutionPlan(
            plan_id=plan_id,
            goal=request,
            steps=steps,
            status="pending",
        )

        # Log plan creation
        await self._activity_log.log({
            "agent_id": "execution_planner",
            "action_type": "plan",
            "tool_name": None,
            "parameters": None,
            "risk_level": None,
            "outcome": "success",
            "duration_ms": 0,
            "tenant_id": None,
            "user_id": None,
            "session_id": None,
            "details": {
                "event": "plan_created",
                "plan_id": plan_id,
                "goal": request,
                "step_count": len(steps),
                "target_domains": target_domains,
            },
        })

        return plan

    # ------------------------------------------------------------------
    # Plan execution
    # ------------------------------------------------------------------

    async def execute_plan(
        self, plan: ExecutionPlan, tenant_id: str
    ) -> ExecutionPlan:
        """Execute plan steps in dependency order with error recovery.

        For each step:
          1. Resolve parameters from dependent step outputs.
          2. Execute the step.
          3. On failure, retry up to MAX_RECOVERY_ATTEMPTS times.
          4. If recovery fails, mark dependent steps as SKIPPED.

        Args:
            plan: The execution plan to run.
            tenant_id: Tenant scope for the execution.

        Returns:
            The updated ExecutionPlan with step statuses and results.
        """
        plan.status = "executing"
        ordered_steps = topological_sort(plan.steps)

        # Track step results for parameter resolution
        step_results: Dict[int, str] = {}
        # Track completed steps in order for potential rollback
        completed_order: List[int] = []
        # Track which steps are failed (for skipping dependents)
        failed_steps: set = set()

        execution_start = time.monotonic()

        for step in ordered_steps:
            # Check if any dependency failed — skip this step
            if any(dep_id in failed_steps for dep_id in step.depends_on):
                step.status = StepStatus.SKIPPED
                step.result = "Skipped due to failed dependency"
                failed_steps.add(step.step_id)
                continue

            # Resolve parameters from dependent step outputs
            resolved_params = self._resolve_parameters(
                step.parameters, step.depends_on, step_results
            )

            # Execute with recovery
            success = await self._execute_step_with_recovery(
                step, resolved_params, tenant_id
            )

            if success:
                step_results[step.step_id] = step.result or ""
                completed_order.append(step.step_id)
            else:
                failed_steps.add(step.step_id)

        # Determine overall plan status
        execution_duration_ms = (time.monotonic() - execution_start) * 1000
        statuses = {s.status for s in plan.steps}

        if all(s.status == StepStatus.COMPLETED for s in plan.steps):
            plan.status = "completed"
        elif StepStatus.FAILED in statuses:
            if StepStatus.COMPLETED in statuses:
                plan.status = "partial_failure"
            else:
                plan.status = "aborted"
        else:
            plan.status = "completed"

        # Log execution trace
        await self._activity_log.log({
            "agent_id": "execution_planner",
            "action_type": "plan",
            "tool_name": None,
            "parameters": None,
            "risk_level": None,
            "outcome": plan.status,
            "duration_ms": execution_duration_ms,
            "tenant_id": tenant_id,
            "user_id": None,
            "session_id": None,
            "details": {
                "event": "plan_executed",
                "plan_id": plan.plan_id,
                "goal": plan.goal,
                "step_results": [
                    {
                        "step_id": s.step_id,
                        "status": s.status.value,
                        "result": s.result,
                        "recovery_attempts": s.recovery_attempts,
                    }
                    for s in plan.steps
                ],
            },
        })

        return plan

    async def _execute_step_with_recovery(
        self,
        step: PlanStep,
        resolved_params: Dict[str, Any],
        tenant_id: str,
    ) -> bool:
        """Execute a single step with up to MAX_RECOVERY_ATTEMPTS retries.

        Args:
            step: The plan step to execute.
            resolved_params: Parameters with dependency outputs resolved.
            tenant_id: Tenant scope.

        Returns:
            True if the step completed successfully, False otherwise.
        """
        step.status = StepStatus.RUNNING

        while step.recovery_attempts <= self.MAX_RECOVERY_ATTEMPTS:
            try:
                result = await self._execute_step(
                    step, resolved_params, tenant_id
                )
                step.status = StepStatus.COMPLETED
                step.result = result
                return True
            except Exception as e:
                step.recovery_attempts += 1
                logger.warning(
                    f"Step {step.step_id} failed (attempt "
                    f"{step.recovery_attempts}/{self.MAX_RECOVERY_ATTEMPTS}): {e}"
                )
                if step.recovery_attempts > self.MAX_RECOVERY_ATTEMPTS:
                    step.status = StepStatus.FAILED
                    step.result = f"Failed after {self.MAX_RECOVERY_ATTEMPTS} recovery attempts: {e}"
                    return False

        # Should not reach here, but guard against it
        step.status = StepStatus.FAILED
        step.result = "Exhausted recovery attempts"
        return False

    async def _execute_step(
        self,
        step: PlanStep,
        resolved_params: Dict[str, Any],
        tenant_id: str,
    ) -> str:
        """Execute a single plan step.

        If a confirmation_protocol is available, routes through it.
        Otherwise returns a placeholder success message.

        Args:
            step: The plan step to execute.
            resolved_params: Resolved parameters for the step.
            tenant_id: Tenant scope.

        Returns:
            The execution result string.
        """
        if self._confirmation_protocol:
            from Agents.confirmation_protocol import MutationRequest

            request = MutationRequest(
                tool_name=step.tool_name,
                parameters=resolved_params,
                tenant_id=tenant_id,
                agent_id=step.agent,
            )
            mutation_result = await self._confirmation_protocol.process_mutation(
                request
            )
            if mutation_result.executed:
                return mutation_result.result or "Executed successfully"
            elif mutation_result.approval_id:
                return f"Queued for approval: {mutation_result.approval_id}"
            else:
                raise RuntimeError(
                    mutation_result.result or "Mutation failed"
                )

        # Placeholder execution when no confirmation protocol is wired
        return f"Step {step.step_id} ({step.tool_name}) executed successfully"

    def _resolve_parameters(
        self,
        parameters: Dict[str, Any],
        depends_on: List[int],
        step_results: Dict[int, str],
    ) -> Dict[str, Any]:
        """Resolve step parameters by injecting dependent step outputs.

        Adds a ``_dependency_outputs`` key to the parameters dict
        containing the results of all dependency steps.

        Args:
            parameters: The original step parameters.
            depends_on: List of dependency step_ids.
            step_results: Map of step_id to result string.

        Returns:
            A new dict with the original parameters plus dependency outputs.
        """
        resolved = dict(parameters)
        if depends_on:
            resolved["_dependency_outputs"] = {
                dep_id: step_results.get(dep_id, "")
                for dep_id in depends_on
            }
        return resolved

    # ------------------------------------------------------------------
    # Plan rollback
    # ------------------------------------------------------------------

    async def rollback_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        """Rollback completed steps in reverse completion order.

        Only steps with status COMPLETED and a defined rollback_tool are
        rolled back. Steps are processed in reverse of the order they
        were completed (last completed first).

        Args:
            plan: The execution plan to roll back.

        Returns:
            The updated ExecutionPlan with rolled-back step statuses.
        """
        rollback_start = time.monotonic()

        # Collect completed steps and reverse them
        completed_steps = [
            s for s in plan.steps if s.status == StepStatus.COMPLETED
        ]
        # Reverse so last-completed is rolled back first
        completed_steps.reverse()

        for step in completed_steps:
            if step.rollback_tool:
                try:
                    rollback_params = step.rollback_params or {}
                    if self._confirmation_protocol:
                        from Agents.confirmation_protocol import MutationRequest

                        request = MutationRequest(
                            tool_name=step.rollback_tool,
                            parameters=rollback_params,
                            tenant_id="default",
                            agent_id=step.agent,
                        )
                        await self._confirmation_protocol.process_mutation(
                            request
                        )
                    step.status = StepStatus.ROLLED_BACK
                    step.result = f"Rolled back via {step.rollback_tool}"
                except Exception as e:
                    logger.error(
                        f"Rollback failed for step {step.step_id}: {e}"
                    )
                    step.result = f"Rollback failed: {e}"
            else:
                # No rollback tool defined — mark as rolled back anyway
                step.status = StepStatus.ROLLED_BACK
                step.result = "Rolled back (no rollback tool defined)"

        plan.status = "rolled_back"

        rollback_duration_ms = (time.monotonic() - rollback_start) * 1000

        # Log rollback
        await self._activity_log.log({
            "agent_id": "execution_planner",
            "action_type": "plan",
            "tool_name": None,
            "parameters": None,
            "risk_level": None,
            "outcome": "rolled_back",
            "duration_ms": rollback_duration_ms,
            "tenant_id": None,
            "user_id": None,
            "session_id": None,
            "details": {
                "event": "plan_rolled_back",
                "plan_id": plan.plan_id,
                "goal": plan.goal,
                "rolled_back_steps": [
                    s.step_id
                    for s in completed_steps
                    if s.status == StepStatus.ROLLED_BACK
                ],
            },
        })

        return plan
