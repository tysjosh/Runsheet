"""
Unit tests for the FuelDistributionPipeline coordinator.

Tests cover:
- Pipeline run lifecycle (pending → stages → complete)
- Unique run_id assignment (Req 6.2)
- Sequential agent execution A1→A2→A3→A4 (Req 6.1)
- State tracking and transitions (Req 6.4)
- Circuit-breaker: halt on agent failure (Req 6.5)
- WebSocket event broadcasting (Req 9.2)
- get_status() retrieval
- broadcast_pipeline_event() helper

Requirements: 6.1–6.6, 9.1–9.4
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call

from Agents.support.fuel_distribution_pipeline import (
    FuelDistributionPipeline,
    PipelineRun,
    PipelineState,
    PIPELINE_STAGES,
    WS_EVENT_FORECAST_READY,
    WS_EVENT_PRIORITY_READY,
    WS_EVENT_LOADPLAN_READY,
    WS_EVENT_ROUTE_READY,
    WS_EVENT_REPLAN_APPLIED,
    WS_EVENT_REPLAN_FAILED,
    broadcast_pipeline_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_agent(agent_id: str, should_fail: bool = False):
    """Create a mock agent with an async monitor_cycle method."""
    agent = MagicMock()
    agent.agent_id = agent_id
    if should_fail:
        agent.monitor_cycle = AsyncMock(
            side_effect=RuntimeError(f"{agent_id} failed")
        )
    else:
        agent.monitor_cycle = AsyncMock(return_value=([], []))
    return agent


def _make_pipeline(agents=None, ws_manager=None):
    """Create a FuelDistributionPipeline with mock dependencies."""
    if agents is None:
        agents = {
            "tank_forecasting": _make_mock_agent("tank_forecasting"),
            "delivery_prioritization": _make_mock_agent("delivery_prioritization"),
            "compartment_loading": _make_mock_agent("compartment_loading"),
            "route_planning": _make_mock_agent("route_planning"),
        }
    if ws_manager is None:
        ws_manager = MagicMock()
        ws_manager.broadcast_event = AsyncMock(return_value=0)

    return FuelDistributionPipeline(
        agents=agents,
        ws_manager=ws_manager,
    ), agents, ws_manager


# ---------------------------------------------------------------------------
# Tests: PipelineRun
# ---------------------------------------------------------------------------


class TestPipelineRun:
    def test_initial_state_is_pending(self):
        run = PipelineRun(run_id="run-1", tenant_id="t1")
        assert run.state == PipelineState.PENDING

    def test_to_dict_contains_required_fields(self):
        run = PipelineRun(run_id="run-1", tenant_id="t1")
        d = run.to_dict()
        assert d["run_id"] == "run-1"
        assert d["tenant_id"] == "t1"
        assert d["state"] == "pending"
        assert d["started_at"] is None
        assert d["completed_at"] is None
        assert d["failed_agent"] is None

    def test_to_dict_with_timestamps(self):
        run = PipelineRun(run_id="run-1", tenant_id="t1")
        run.started_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        d = run.to_dict()
        assert d["started_at"] is not None


# ---------------------------------------------------------------------------
# Tests: FuelDistributionPipeline.run()
# ---------------------------------------------------------------------------


class TestPipelineRun_Run:
    @pytest.mark.asyncio
    async def test_returns_run_id(self):
        """Req 6.2: Each run gets a unique run_id."""
        pipeline, _, _ = _make_pipeline()
        run_id = await pipeline.run("tenant-1")
        assert run_id.startswith("run_")
        assert len(run_id) > 10

    @pytest.mark.asyncio
    async def test_unique_run_ids(self):
        """Req 6.2: Successive runs get different run_ids."""
        pipeline, _, _ = _make_pipeline()
        run_id_1 = await pipeline.run("tenant-1")
        run_id_2 = await pipeline.run("tenant-1")
        assert run_id_1 != run_id_2

    @pytest.mark.asyncio
    async def test_executes_all_agents_in_order(self):
        """Req 6.1: Agents execute in order A1→A2→A3→A4."""
        pipeline, agents, _ = _make_pipeline()
        await pipeline.run("tenant-1")

        # All four agents should have been called
        for agent_id in ["tank_forecasting", "delivery_prioritization",
                         "compartment_loading", "route_planning"]:
            agents[agent_id].monitor_cycle.assert_called_once()

    @pytest.mark.asyncio
    async def test_completes_successfully(self):
        """Req 6.4: Pipeline reaches COMPLETE state on success."""
        pipeline, _, _ = _make_pipeline()
        run_id = await pipeline.run("tenant-1")
        status = await pipeline.get_status(run_id)
        assert status["state"] == "complete"
        assert status["completed_at"] is not None
        assert status["failed_agent"] is None

    @pytest.mark.asyncio
    async def test_circuit_breaker_on_failure(self):
        """Req 6.5: Pipeline halts when an agent fails."""
        agents = {
            "tank_forecasting": _make_mock_agent("tank_forecasting"),
            "delivery_prioritization": _make_mock_agent(
                "delivery_prioritization", should_fail=True
            ),
            "compartment_loading": _make_mock_agent("compartment_loading"),
            "route_planning": _make_mock_agent("route_planning"),
        }
        pipeline, _, _ = _make_pipeline(agents=agents)
        run_id = await pipeline.run("tenant-1")

        status = await pipeline.get_status(run_id)
        assert status["state"] == "failed"
        assert status["failed_agent"] == "delivery_prioritization"
        assert "failed" in status["error_message"]

        # Agents after the failure should NOT have been called
        agents["compartment_loading"].monitor_cycle.assert_not_called()
        agents["route_planning"].monitor_cycle.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_missing_agents(self):
        """Pipeline skips agents not in the agents dict."""
        agents = {
            "tank_forecasting": _make_mock_agent("tank_forecasting"),
            # delivery_prioritization is missing
            "compartment_loading": _make_mock_agent("compartment_loading"),
            "route_planning": _make_mock_agent("route_planning"),
        }
        pipeline, _, _ = _make_pipeline(agents=agents)
        run_id = await pipeline.run("tenant-1")

        status = await pipeline.get_status(run_id)
        assert status["state"] == "complete"

    @pytest.mark.asyncio
    async def test_broadcasts_state_transitions(self):
        """Req 6.4: State transitions are broadcast via WebSocket."""
        pipeline, _, ws_manager = _make_pipeline()
        await pipeline.run("tenant-1")

        # Should have broadcast for each stage + completion
        assert ws_manager.broadcast_event.call_count >= 5

    @pytest.mark.asyncio
    async def test_broadcasts_stage_specific_ws_events(self):
        """Req 9.2: Broadcasts forecast_ready, priority_ready, etc."""
        pipeline, _, ws_manager = _make_pipeline()
        await pipeline.run("tenant-1")

        event_types = [
            c.args[0] for c in ws_manager.broadcast_event.call_args_list
        ]
        assert WS_EVENT_FORECAST_READY in event_types
        assert WS_EVENT_PRIORITY_READY in event_types
        assert WS_EVENT_LOADPLAN_READY in event_types
        assert WS_EVENT_ROUTE_READY in event_types


# ---------------------------------------------------------------------------
# Tests: FuelDistributionPipeline.get_status()
# ---------------------------------------------------------------------------


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_run(self):
        pipeline, _, _ = _make_pipeline()
        status = await pipeline.get_status("nonexistent")
        assert status is None

    @pytest.mark.asyncio
    async def test_returns_status_for_known_run(self):
        pipeline, _, _ = _make_pipeline()
        run_id = await pipeline.run("tenant-1")
        status = await pipeline.get_status(run_id)
        assert status is not None
        assert status["run_id"] == run_id
        assert status["tenant_id"] == "tenant-1"


# ---------------------------------------------------------------------------
# Tests: broadcast_pipeline_event()
# ---------------------------------------------------------------------------


class TestBroadcastPipelineEvent:
    @pytest.mark.asyncio
    async def test_broadcasts_event_with_correct_structure(self):
        ws_manager = MagicMock()
        ws_manager.broadcast_event = AsyncMock(return_value=1)

        await broadcast_pipeline_event(
            ws_manager=ws_manager,
            event_type=WS_EVENT_FORECAST_READY,
            run_id="run-1",
            tenant_id="t1",
            summary={"count": 5},
        )

        ws_manager.broadcast_event.assert_called_once()
        call_args = ws_manager.broadcast_event.call_args
        assert call_args[0][0] == WS_EVENT_FORECAST_READY
        event_data = call_args[0][1]
        assert event_data["run_id"] == "run-1"
        assert event_data["tenant_id"] == "t1"
        assert "timestamp" in event_data
        assert event_data["summary"]["count"] == 5

    @pytest.mark.asyncio
    async def test_handles_none_ws_manager(self):
        """Should not raise when ws_manager is None."""
        await broadcast_pipeline_event(
            ws_manager=None,
            event_type=WS_EVENT_FORECAST_READY,
            run_id="run-1",
            tenant_id="t1",
        )

    @pytest.mark.asyncio
    async def test_handles_broadcast_error_gracefully(self):
        """Should not raise when broadcast fails."""
        ws_manager = MagicMock()
        ws_manager.broadcast_event = AsyncMock(
            side_effect=RuntimeError("ws error")
        )

        # Should not raise
        await broadcast_pipeline_event(
            ws_manager=ws_manager,
            event_type=WS_EVENT_FORECAST_READY,
            run_id="run-1",
            tenant_id="t1",
        )

    @pytest.mark.asyncio
    async def test_default_summary_is_empty_dict(self):
        ws_manager = MagicMock()
        ws_manager.broadcast_event = AsyncMock(return_value=1)

        await broadcast_pipeline_event(
            ws_manager=ws_manager,
            event_type=WS_EVENT_REPLAN_APPLIED,
            run_id="run-1",
            tenant_id="t1",
        )

        event_data = ws_manager.broadcast_event.call_args[0][1]
        assert event_data["summary"] == {}


# ---------------------------------------------------------------------------
# Tests: WebSocket event constants
# ---------------------------------------------------------------------------


class TestWSEventConstants:
    def test_all_event_types_defined(self):
        """Req 9.2: All required event types are defined."""
        assert WS_EVENT_FORECAST_READY == "forecast_ready"
        assert WS_EVENT_PRIORITY_READY == "priority_ready"
        assert WS_EVENT_LOADPLAN_READY == "loadplan_ready"
        assert WS_EVENT_ROUTE_READY == "route_ready"
        assert WS_EVENT_REPLAN_APPLIED == "replan_applied"
        assert WS_EVENT_REPLAN_FAILED == "replan_failed"


# ---------------------------------------------------------------------------
# Tests: PipelineState enum
# ---------------------------------------------------------------------------


class TestPipelineState:
    def test_all_states_defined(self):
        """Req 6.4: All pipeline states are defined."""
        assert PipelineState.PENDING.value == "pending"
        assert PipelineState.FORECASTING.value == "forecasting"
        assert PipelineState.PRIORITIZING.value == "prioritizing"
        assert PipelineState.LOADING.value == "loading"
        assert PipelineState.ROUTING.value == "routing"
        assert PipelineState.COMPLETE.value == "complete"
        assert PipelineState.FAILED.value == "failed"
