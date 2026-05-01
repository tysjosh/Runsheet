"""
Fuel Distribution Pipeline Coordinator.

Orchestrates the A1→A2→A3→A4 pipeline sequence, assigns run_id,
tracks state, broadcasts progress via WebSocket, and implements
circuit-breaker behavior.

Also provides WebSocket event broadcasting helpers for pipeline
progress events (forecast_ready, priority_ready, loadplan_ready,
route_ready, replan_applied, replan_failed).

NOT an overlay agent — this is a service that triggers pipeline runs.

Validates: Requirements 6.1–6.6, 9.1–9.4
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from Agents.agent_ws_manager import AgentActivityWSManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebSocket event types for pipeline progress (Req 9.1–9.4)
# ---------------------------------------------------------------------------

WS_EVENT_FORECAST_READY = "forecast_ready"
WS_EVENT_PRIORITY_READY = "priority_ready"
WS_EVENT_LOADPLAN_READY = "loadplan_ready"
WS_EVENT_ROUTE_READY = "route_ready"
WS_EVENT_REPLAN_APPLIED = "replan_applied"
WS_EVENT_REPLAN_FAILED = "replan_failed"


async def broadcast_pipeline_event(
    ws_manager: AgentActivityWSManager,
    event_type: str,
    run_id: str,
    tenant_id: str,
    summary: Optional[Dict[str, Any]] = None,
) -> None:
    """Broadcast a pipeline progress event via WebSocket.

    Each event includes run_id, tenant_id, timestamp, and a summary
    payload appropriate to the event type (Req 9.3).

    Uses the existing AgentActivityWSManager pattern for connection
    management and tenant-scoped broadcasting (Req 9.4).

    Args:
        ws_manager: The AgentActivityWSManager instance.
        event_type: One of the WS_EVENT_* constants.
        run_id: The pipeline run identifier.
        tenant_id: The tenant identifier.
        summary: Optional summary payload for the event.
    """
    if ws_manager is None:
        return

    event_data = {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": summary or {},
    }

    try:
        await ws_manager.broadcast_event(event_type, event_data)
    except Exception as e:
        logger.warning(
            "Failed to broadcast pipeline event %s: %s",
            event_type,
            e,
        )


class PipelineState(str, Enum):
    """Pipeline run states (Req 6.4)."""
    PENDING = "pending"
    FORECASTING = "forecasting"
    PRIORITIZING = "prioritizing"
    LOADING = "loading"
    ROUTING = "routing"
    COMPLETE = "complete"
    FAILED = "failed"


# Agent stage ordering (Req 6.1)
PIPELINE_STAGES = [
    ("tank_forecasting", PipelineState.FORECASTING),
    ("delivery_prioritization", PipelineState.PRIORITIZING),
    ("compartment_loading", PipelineState.LOADING),
    ("route_planning", PipelineState.ROUTING),
]


# Map agent_id to the WS event broadcast after successful completion (Req 9.2)
_STAGE_WS_EVENTS: Dict[str, str] = {
    "tank_forecasting": WS_EVENT_FORECAST_READY,
    "delivery_prioritization": WS_EVENT_PRIORITY_READY,
    "compartment_loading": WS_EVENT_LOADPLAN_READY,
    "route_planning": WS_EVENT_ROUTE_READY,
}


class PipelineRun:
    """Tracks the state of a single pipeline execution."""

    def __init__(self, run_id: str, tenant_id: str) -> None:
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.state = PipelineState.PENDING
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        self.failed_agent: Optional[str] = None
        self.error_message: Optional[str] = None
        self.stage_results: Dict[str, Any] = {}

    def to_dict(self) -> Dict[str, Any]:
        """Serialize run status to a dict."""
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "state": self.state.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "failed_agent": self.failed_agent,
            "error_message": self.error_message,
        }


class FuelDistributionPipeline:
    """Orchestrates the A1→A2→A3→A4 pipeline sequence.

    Assigns run_id, triggers each agent in order, tracks state,
    broadcasts progress via WebSocket, and implements circuit-breaker
    behavior (Req 6.5).

    Supports periodic scheduling via start_periodic() (Req 6.3).

    Args:
        agents: Dict mapping agent_id to agent instances.
        ws_manager: AgentActivityWSManager for broadcasting events.
        signal_bus: SignalBus for inter-agent communication.
        schedule_interval_seconds: Periodic run interval (default 1800 = 30 min).
    """

    def __init__(
        self,
        agents: Dict[str, Any],
        ws_manager: AgentActivityWSManager,
        signal_bus: Any = None,
        schedule_interval_seconds: int = 1800,
    ) -> None:
        self._agents = agents
        self._ws_manager = ws_manager
        self._signal_bus = signal_bus
        self._runs: Dict[str, PipelineRun] = {}
        self._schedule_interval = schedule_interval_seconds
        self._periodic_task: Optional[asyncio.Task] = None
        self._periodic_tenant_id: Optional[str] = None

    async def run(self, tenant_id: str) -> str:
        """Execute a full pipeline run. Returns run_id.

        Triggers agents in sequence: A1→A2→A3→A4 (Req 6.1).
        Assigns a unique run_id (Req 6.2).
        Tracks state and broadcasts transitions (Req 6.4).
        Implements circuit-breaker on failure (Req 6.5).

        Args:
            tenant_id: The tenant to run the pipeline for.

        Returns:
            The run_id for this pipeline execution.
        """
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        pipeline_run = PipelineRun(run_id=run_id, tenant_id=tenant_id)
        pipeline_run.started_at = datetime.now(timezone.utc)
        self._runs[run_id] = pipeline_run

        logger.info(
            "FuelDistributionPipeline: starting run %s for tenant %s",
            run_id,
            tenant_id,
        )

        for agent_id, stage_state in PIPELINE_STAGES:
            agent = self._agents.get(agent_id)
            if agent is None:
                logger.warning(
                    "FuelDistributionPipeline: agent %s not registered, skipping",
                    agent_id,
                )
                continue

            # Transition state (Req 6.4)
            pipeline_run.state = stage_state
            await self._broadcast_state_transition(pipeline_run, agent_id)

            try:
                # Trigger the agent's evaluation cycle
                await agent.monitor_cycle()
                pipeline_run.stage_results[agent_id] = "completed"
                logger.info(
                    "FuelDistributionPipeline: agent %s completed for run %s",
                    agent_id,
                    run_id,
                )

                # Broadcast stage-specific WS event (Req 9.2)
                ws_event = _STAGE_WS_EVENTS.get(agent_id)
                if ws_event:
                    await broadcast_pipeline_event(
                        ws_manager=self._ws_manager,
                        event_type=ws_event,
                        run_id=run_id,
                        tenant_id=tenant_id,
                        summary={"agent_id": agent_id, "state": "completed"},
                    )
            except Exception as e:
                # Circuit-breaker: halt on agent failure (Req 6.5)
                pipeline_run.state = PipelineState.FAILED
                pipeline_run.failed_agent = agent_id
                pipeline_run.error_message = str(e)
                pipeline_run.completed_at = datetime.now(timezone.utc)

                logger.error(
                    "FuelDistributionPipeline: agent %s failed for run %s: %s",
                    agent_id,
                    run_id,
                    e,
                )

                await self._broadcast_state_transition(pipeline_run, agent_id)
                return run_id

        # All stages completed successfully
        pipeline_run.state = PipelineState.COMPLETE
        pipeline_run.completed_at = datetime.now(timezone.utc)
        await self._broadcast_state_transition(pipeline_run, "pipeline")

        logger.info(
            "FuelDistributionPipeline: run %s completed successfully",
            run_id,
        )

        return run_id

    async def get_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get pipeline run status.

        Args:
            run_id: The run_id to look up.

        Returns:
            Dict with run status, or None if run_id not found.
        """
        pipeline_run = self._runs.get(run_id)
        if pipeline_run is None:
            return None
        return pipeline_run.to_dict()

    # ------------------------------------------------------------------
    # Periodic scheduling (Req 6.3)
    # ------------------------------------------------------------------

    async def start_periodic(self, tenant_id: str) -> None:
        """Start periodic pipeline execution (Req 6.3).

        Runs the pipeline at the configured interval (default 30 min).
        Only one periodic schedule can be active at a time.

        Args:
            tenant_id: The tenant to run the pipeline for.
        """
        if self._periodic_task is not None and not self._periodic_task.done():
            logger.warning(
                "FuelDistributionPipeline: periodic schedule already active"
            )
            return

        self._periodic_tenant_id = tenant_id
        self._periodic_task = asyncio.create_task(
            self._periodic_loop(tenant_id)
        )
        logger.info(
            "FuelDistributionPipeline: started periodic schedule for tenant %s "
            "(interval=%ds)",
            tenant_id,
            self._schedule_interval,
        )

    async def stop_periodic(self) -> None:
        """Stop periodic pipeline execution."""
        if self._periodic_task is not None:
            self._periodic_task.cancel()
            try:
                await self._periodic_task
            except asyncio.CancelledError:
                pass
            self._periodic_task = None
            logger.info("FuelDistributionPipeline: stopped periodic schedule")

    async def _periodic_loop(self, tenant_id: str) -> None:
        """Internal loop for periodic pipeline execution."""
        while True:
            try:
                await asyncio.sleep(self._schedule_interval)
                run_id = await self.run(tenant_id)
                logger.info(
                    "FuelDistributionPipeline: periodic run completed: %s",
                    run_id,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "FuelDistributionPipeline: periodic run failed: %s", e
                )
                # Continue to next cycle (circuit-breaker retry — Req 6.5)

    async def _broadcast_state_transition(
        self,
        pipeline_run: PipelineRun,
        agent_id: str,
    ) -> None:
        """Broadcast pipeline state transition via WebSocket (Req 6.4).

        Uses AgentActivityWSManager.broadcast_event() to send
        pipeline_state_change events.
        """
        if self._ws_manager is None:
            return

        event_data = {
            "run_id": pipeline_run.run_id,
            "tenant_id": pipeline_run.tenant_id,
            "state": pipeline_run.state.value,
            "agent_id": agent_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if pipeline_run.state == PipelineState.FAILED:
            event_data["error"] = pipeline_run.error_message
            event_data["failed_agent"] = pipeline_run.failed_agent

        try:
            await self._ws_manager.broadcast_event(
                "pipeline_state_change", event_data
            )
        except Exception as e:
            logger.warning(
                "FuelDistributionPipeline: failed to broadcast state transition: %s",
                e,
            )
