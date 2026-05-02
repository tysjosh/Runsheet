"""
Unit tests for the Agent Orchestrator module.

Tests the AgentOrchestrator class: ROUTING_TABLE, _classify_intent,
_is_complex_request, route (simple and complex), fallback to reporting,
result synthesis, and plan formatting.

Requirements: 7.6, 7.7, 7.8
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.orchestrator import AgentOrchestrator
from Agents.execution_planner import ExecutionPlan, PlanStep, StepStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_activity_log() -> MagicMock:
    """Create a mock ActivityLogService."""
    log = MagicMock()
    log.log = AsyncMock(return_value="log-id-1")
    return log


def _make_specialist(response: str = "specialist response") -> MagicMock:
    """Create a mock specialist agent with an async handle method."""
    agent = MagicMock()
    agent.handle = AsyncMock(return_value=response)
    return agent


def _make_planner(
    plan_status: str = "completed",
    step_results: list = None,
) -> MagicMock:
    """Create a mock ExecutionPlanner."""
    planner = MagicMock()

    steps = []
    if step_results:
        for i, (status, result) in enumerate(step_results):
            step = PlanStep(
                step_id=i + 1,
                description=f"Step {i + 1}",
                agent="test",
                tool_name="test_tool",
                parameters={},
                status=status,
                result=result,
            )
            steps.append(step)
    else:
        steps = [
            PlanStep(
                step_id=1,
                description="Execute task",
                agent="test",
                tool_name="test_tool",
                parameters={},
                status=StepStatus.COMPLETED,
                result="Done",
            )
        ]

    plan = ExecutionPlan(
        plan_id="plan-1",
        goal="Test goal",
        steps=steps,
        status=plan_status,
    )

    planner.create_plan = AsyncMock(return_value=plan)
    planner.execute_plan = AsyncMock(return_value=plan)
    return planner


def _make_orchestrator(
    specialists: dict = None,
    planner: MagicMock = None,
    activity_log: MagicMock = None,
) -> AgentOrchestrator:
    """Create an AgentOrchestrator with mocked dependencies."""
    if specialists is None:
        specialists = {
            "fleet": _make_specialist("Fleet data retrieved"),
            "scheduling": _make_specialist("Scheduling info"),
            "fuel": _make_specialist("Fuel status"),
            "ops": _make_specialist("Ops intelligence"),
            "reporting": _make_specialist("Report generated"),
        }
    if planner is None:
        planner = _make_planner()
    if activity_log is None:
        activity_log = _make_activity_log()

    return AgentOrchestrator(
        specialists=specialists,
        execution_planner=planner,
        activity_log_service=activity_log,
    )


# ---------------------------------------------------------------------------
# Tests: ROUTING_TABLE
# ---------------------------------------------------------------------------


class TestRoutingTable:
    """Tests for the ROUTING_TABLE class attribute."""

    def test_routing_table_has_all_domains(self):
        expected_domains = {"fleet", "scheduling", "fuel", "ops", "reporting"}
        assert set(AgentOrchestrator.ROUTING_TABLE.keys()) == expected_domains

    def test_fleet_keywords(self):
        keywords = AgentOrchestrator.ROUTING_TABLE["fleet"]
        assert "truck" in keywords
        assert "vehicle" in keywords
        assert "vessel" in keywords
        assert "equipment" in keywords
        assert "container" in keywords
        assert "asset" in keywords
        assert "location" in keywords
        assert "fleet" in keywords

    def test_scheduling_keywords(self):
        keywords = AgentOrchestrator.ROUTING_TABLE["scheduling"]
        assert "job" in keywords
        assert "schedule" in keywords
        assert "dispatch" in keywords
        assert "assign" in keywords
        assert "cancel" in keywords
        assert "delay" in keywords
        assert "cargo" in keywords

    def test_fuel_keywords(self):
        keywords = AgentOrchestrator.ROUTING_TABLE["fuel"]
        assert "fuel" in keywords
        assert "refill" in keywords
        assert "station" in keywords
        assert "diesel" in keywords
        assert "petrol" in keywords
        assert "consumption" in keywords

    def test_ops_keywords(self):
        keywords = AgentOrchestrator.ROUTING_TABLE["ops"]
        assert "shipment" in keywords
        assert "rider" in keywords
        assert "sla" in keywords
        assert "delivery" in keywords
        assert "ops" in keywords
        assert "breach" in keywords

    def test_reporting_keywords(self):
        keywords = AgentOrchestrator.ROUTING_TABLE["reporting"]
        assert "report" in keywords
        assert "analysis" in keywords
        assert "summary" in keywords
        assert "overview" in keywords
        assert "performance" in keywords
        assert "productivity" in keywords

    def test_all_keywords_are_lowercase(self):
        for domain, keywords in AgentOrchestrator.ROUTING_TABLE.items():
            for kw in keywords:
                assert kw == kw.lower(), f"Keyword '{kw}' in '{domain}' is not lowercase"


# ---------------------------------------------------------------------------
# Tests: _classify_intent
# ---------------------------------------------------------------------------


class TestClassifyIntent:
    """Tests for keyword-based intent classification."""

    def test_single_fleet_keyword(self):
        orch = _make_orchestrator()
        result = orch._classify_intent_keywords("Show me all trucks")
        assert "fleet" in result

    def test_single_scheduling_keyword(self):
        orch = _make_orchestrator()
        result = orch._classify_intent_keywords("List all active jobs")
        assert "scheduling" in result

    def test_single_fuel_keyword(self):
        orch = _make_orchestrator()
        result = orch._classify_intent_keywords("Check fuel levels")
        assert "fuel" in result

    def test_single_ops_keyword(self):
        orch = _make_orchestrator()
        result = orch._classify_intent_keywords("Track this shipment")
        assert "ops" in result

    def test_single_reporting_keyword(self):
        orch = _make_orchestrator()
        result = orch._classify_intent_keywords("Generate a report")
        assert "reporting" in result

    def test_multiple_domains_matched(self):
        orch = _make_orchestrator()
        result = orch._classify_intent_keywords("Show truck fuel consumption")
        assert "fleet" in result
        assert "fuel" in result

    def test_case_insensitive(self):
        orch = _make_orchestrator()
        result = orch._classify_intent_keywords("SHOW ME ALL TRUCKS")
        assert "fleet" in result

    def test_no_match_returns_empty(self):
        orch = _make_orchestrator()
        result = orch._classify_intent_keywords("hello how are you")
        assert result == []

    def test_partial_keyword_match(self):
        orch = _make_orchestrator()
        # "trucks" contains "truck"
        result = orch._classify_intent_keywords("Where are the trucks?")
        assert "fleet" in result

    def test_multiple_keywords_same_domain(self):
        orch = _make_orchestrator()
        result = orch._classify_intent_keywords("Find vehicle at location")
        assert "fleet" in result
        # Should only appear once
        assert result.count("fleet") == 1

    def test_all_domains_matched(self):
        orch = _make_orchestrator()
        result = orch._classify_intent_keywords(
            "Show truck job fuel shipment report"
        )
        assert len(result) == 5


# ---------------------------------------------------------------------------
# Tests: _is_complex_request
# ---------------------------------------------------------------------------


class TestIsComplexRequest:
    """Tests for multi-step request detection."""

    def test_simple_single_domain_not_complex(self):
        orch = _make_orchestrator()
        assert orch._is_complex_request("Show me all trucks") is False

    def test_conjunction_with_single_domain_not_complex(self):
        orch = _make_orchestrator()
        # "and" present but only one domain (fleet)
        assert orch._is_complex_request("Show trucks and vehicles") is False

    def test_multi_domain_without_conjunction_not_complex(self):
        orch = _make_orchestrator()
        # Multiple domains but no conjunction indicator
        assert orch._is_complex_request("truck fuel status") is False

    def test_conjunction_with_multi_domain_is_complex(self):
        orch = _make_orchestrator()
        assert orch._is_complex_request(
            "Check truck status and show fuel levels"
        ) is True

    def test_then_indicator_with_multi_domain(self):
        orch = _make_orchestrator()
        assert orch._is_complex_request(
            "Find available trucks then schedule a job"
        ) is True

    def test_also_indicator_with_multi_domain(self):
        orch = _make_orchestrator()
        assert orch._is_complex_request(
            "Check shipment status, also show the report"
        ) is True

    def test_no_match_not_complex(self):
        orch = _make_orchestrator()
        assert orch._is_complex_request("hello world") is False

    def test_case_insensitive_indicators(self):
        orch = _make_orchestrator()
        assert orch._is_complex_request(
            "Check TRUCK status AND show FUEL levels"
        ) is True


# ---------------------------------------------------------------------------
# Tests: route — simple requests
# ---------------------------------------------------------------------------


class TestRouteSimple:
    """Tests for simple (non-complex) request routing."""

    async def test_routes_to_single_specialist(self):
        fleet_agent = _make_specialist("Fleet data")
        orch = _make_orchestrator(
            specialists={"fleet": fleet_agent, "reporting": _make_specialist()}
        )

        result = await orch.route("Show me all trucks", "tenant-1")

        fleet_agent.handle.assert_called_once()
        assert result == "Fleet data"

    async def test_routes_to_multiple_specialists(self):
        fleet_agent = _make_specialist("Fleet data")
        fuel_agent = _make_specialist("Fuel data")
        orch = _make_orchestrator(
            specialists={
                "fleet": fleet_agent,
                "fuel": fuel_agent,
                "reporting": _make_specialist(),
            }
        )

        # "truck fuel" matches fleet + fuel but no conjunction → simple
        result = await orch.route("truck fuel status", "tenant-1")

        fleet_agent.handle.assert_called_once()
        fuel_agent.handle.assert_called_once()
        assert "Fleet data" in result
        assert "Fuel data" in result

    async def test_fallback_to_reporting_on_no_match(self):
        reporting_agent = _make_specialist("General report")
        orch = _make_orchestrator(
            specialists={"reporting": reporting_agent}
        )

        result = await orch.route("hello how are you", "tenant-1")

        reporting_agent.handle.assert_called_once()
        assert result == "General report"

    async def test_passes_tenant_id_in_context(self):
        fleet_agent = _make_specialist("OK")
        orch = _make_orchestrator(
            specialists={"fleet": fleet_agent, "reporting": _make_specialist()}
        )

        await orch.route("Show trucks", "tenant-42", session_id="sess-1")

        call_args = fleet_agent.handle.call_args
        context = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("context")
        assert context["tenant_id"] == "tenant-42"
        assert context["session_id"] == "sess-1"

    async def test_handles_specialist_error_gracefully(self):
        fleet_agent = _make_specialist()
        fleet_agent.handle = AsyncMock(side_effect=RuntimeError("Agent crashed"))
        orch = _make_orchestrator(
            specialists={"fleet": fleet_agent, "reporting": _make_specialist()}
        )

        result = await orch.route("Show trucks", "tenant-1")

        assert "Error" in result

    async def test_missing_specialist_skipped(self):
        # Only reporting agent available, but message matches fleet
        orch = _make_orchestrator(
            specialists={"reporting": _make_specialist("Fallback")}
        )

        # "truck" matches fleet, but fleet specialist not in dict
        result = await orch.route("Show trucks", "tenant-1")

        # No fleet specialist → empty results → fallback message
        assert result == "I wasn't able to find relevant information for your request."

    async def test_logs_routing_decision(self):
        activity_log = _make_activity_log()
        orch = _make_orchestrator(activity_log=activity_log)

        await orch.route("Show trucks", "tenant-1")

        # Should log at least twice: intent_classified + routing_completed
        assert activity_log.log.call_count >= 2

        # Check the first log call (intent classification)
        first_call = activity_log.log.call_args_list[0][0][0]
        assert first_call["details"]["event"] == "intent_classified"
        assert "fleet" in first_call["details"]["targets"]


# ---------------------------------------------------------------------------
# Tests: route — complex requests
# ---------------------------------------------------------------------------


class TestRouteComplex:
    """Tests for complex (multi-step) request routing via ExecutionPlanner."""

    async def test_complex_request_uses_planner(self):
        planner = _make_planner()
        orch = _make_orchestrator(planner=planner)

        result = await orch.route(
            "Check truck status and show fuel levels", "tenant-1"
        )

        planner.create_plan.assert_called_once()
        planner.execute_plan.assert_called_once()
        assert "Plan:" in result

    async def test_complex_request_passes_targets_to_planner(self):
        planner = _make_planner()
        orch = _make_orchestrator(planner=planner)

        await orch.route(
            "Check truck status and show fuel levels", "tenant-1"
        )

        create_call = planner.create_plan.call_args
        targets = create_call[0][1]
        assert "fleet" in targets
        assert "fuel" in targets

    async def test_complex_request_falls_back_on_planner_error(self):
        planner = _make_planner()
        planner.create_plan = AsyncMock(side_effect=RuntimeError("Planner failed"))

        fleet_agent = _make_specialist("Fleet fallback")
        fuel_agent = _make_specialist("Fuel fallback")
        orch = _make_orchestrator(
            specialists={
                "fleet": fleet_agent,
                "fuel": fuel_agent,
                "reporting": _make_specialist(),
            },
            planner=planner,
        )

        result = await orch.route(
            "Check truck status and show fuel levels", "tenant-1"
        )

        # Should fall back to simple execution
        fleet_agent.handle.assert_called_once()
        fuel_agent.handle.assert_called_once()
        assert "Fleet fallback" in result
        assert "Fuel fallback" in result


# ---------------------------------------------------------------------------
# Tests: _synthesize
# ---------------------------------------------------------------------------


class TestSynthesize:
    """Tests for result synthesis."""

    def test_single_result_returned_directly(self):
        orch = _make_orchestrator()
        assert orch._synthesize(["Hello"]) == "Hello"

    def test_multiple_results_joined(self):
        orch = _make_orchestrator()
        result = orch._synthesize(["Part 1", "Part 2"])
        assert "Part 1" in result
        assert "Part 2" in result
        assert "\n\n" in result

    def test_empty_results_fallback_message(self):
        orch = _make_orchestrator()
        result = orch._synthesize([])
        assert "wasn't able to find" in result


# ---------------------------------------------------------------------------
# Tests: _format_plan_result
# ---------------------------------------------------------------------------


class TestFormatPlanResult:
    """Tests for plan result formatting."""

    def test_completed_plan_format(self):
        orch = _make_orchestrator()
        plan = ExecutionPlan(
            plan_id="p1",
            goal="Test goal",
            steps=[
                PlanStep(
                    step_id=1,
                    description="Do something",
                    agent="fleet",
                    tool_name="search",
                    parameters={},
                    status=StepStatus.COMPLETED,
                    result="Found 5 trucks",
                ),
            ],
            status="completed",
        )

        result = orch._format_plan_result(plan)

        assert "Test goal" in result
        assert "completed" in result
        assert "✅" in result
        assert "Do something" in result
        assert "Found 5 trucks" in result

    def test_partial_failure_format(self):
        orch = _make_orchestrator()
        plan = ExecutionPlan(
            plan_id="p1",
            goal="Multi-step",
            steps=[
                PlanStep(
                    step_id=1,
                    description="Step 1",
                    agent="fleet",
                    tool_name="t1",
                    parameters={},
                    status=StepStatus.COMPLETED,
                    result="OK",
                ),
                PlanStep(
                    step_id=2,
                    description="Step 2",
                    agent="fuel",
                    tool_name="t2",
                    parameters={},
                    status=StepStatus.FAILED,
                    result="Error occurred",
                ),
            ],
            status="partial_failure",
        )

        result = orch._format_plan_result(plan)

        assert "✅" in result
        assert "❌" in result
        assert "partial_failure" in result

    def test_skipped_step_format(self):
        orch = _make_orchestrator()
        plan = ExecutionPlan(
            plan_id="p1",
            goal="Test",
            steps=[
                PlanStep(
                    step_id=1,
                    description="Skipped step",
                    agent="test",
                    tool_name="t1",
                    parameters={},
                    status=StepStatus.SKIPPED,
                    result="Skipped due to dependency",
                ),
            ],
            status="completed",
        )

        result = orch._format_plan_result(plan)

        assert "⏭️" in result
        assert "Skipped step" in result
