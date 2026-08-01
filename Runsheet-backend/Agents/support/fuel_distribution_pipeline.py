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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from Agents.agent_ws_manager import AgentActivityWSManager
from Agents.overlay.data_contracts import InterventionProposal
from Agents.support.fuel_distribution_models import DeliveryPriorityList

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline stage data model (Req 1.1, 2.1)
# ---------------------------------------------------------------------------


@dataclass
class PipelineStageResult:
    """Captures the output of each pipeline stage for injection into the next stage.

    Validates: Requirements 1.1, 2.1
    """

    agent_id: str
    signals_consumed: List[Any] = field(default_factory=list)
    proposals_generated: List[Any] = field(default_factory=list)
    published_messages: List[Any] = field(default_factory=list)


class PipelinePublishCapture:
    """Async context manager that captures messages published to SignalBus during a stage.

    Monkey-patches SignalBus.publish to intercept messages while still calling
    the original publish method. This allows the pipeline to know what each
    stage published without modifying SignalBus internals.

    Validates: Requirements 1.1, 2.1

    Args:
        signal_bus: The SignalBus instance to intercept publish calls on.
    """

    def __init__(self, signal_bus) -> None:
        self._signal_bus = signal_bus
        self._captured: List[Any] = []
        self._original_publish = signal_bus.publish

    async def __aenter__(self) -> "PipelinePublishCapture":
        async def capturing_publish(message) -> int:
            self._captured.append(message)
            return await self._original_publish(message)

        self._signal_bus.publish = capturing_publish
        return self

    async def __aexit__(self, *args) -> None:
        self._signal_bus.publish = self._original_publish

    @property
    def captured(self) -> List[Any]:
        """Return a copy of all captured messages."""
        return list(self._captured)


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

        # Reset the write circuit breaker before pipeline execution to ensure
        # plan persistence isn't blocked by unrelated background write failures
        first_agent = self._agents.get(PIPELINE_STAGES[0][0])
        if first_agent and hasattr(first_agent, '_es') and hasattr(first_agent._es, '_circuit_breaker'):
            first_agent._es._circuit_breaker.reset()

        # Seed the first agent's signal buffer with a trigger signal so it
        # has input to process. Without this, the TankForecastingAgent's
        # monitor_cycle() returns immediately when _signal_buffer is empty.
        first_agent_id = PIPELINE_STAGES[0][0]
        first_agent = self._agents.get(first_agent_id)
        if first_agent is not None and hasattr(first_agent, "_signal_buffer"):
            from Agents.overlay.data_contracts import RiskSignal, Severity

            trigger_signal = RiskSignal(
                source_agent="pipeline_trigger",
                entity_id=f"pipeline_run_{run_id}",
                entity_type="pipeline_trigger",
                severity=Severity.MEDIUM,
                confidence=1.0,
                ttl_seconds=3600,
                context={"trigger": "pipeline_run", "run_id": run_id},
                tenant_id=tenant_id,
            )
            first_agent._signal_buffer.append(trigger_signal)
            logger.info(
                "FuelDistributionPipeline: seeded %s signal buffer with trigger signal",
                first_agent_id,
            )

        for stage_idx, (agent_id, stage_state) in enumerate(PIPELINE_STAGES):
            agent = self._agents.get(agent_id)
            if agent is None:
                logger.warning(
                    "FuelDistributionPipeline: agent %s not registered, skipping",
                    agent_id,
                )
                continue

            # Determine the next agent for buffer injection (Req 1.2)
            next_agent = None
            if stage_idx + 1 < len(PIPELINE_STAGES):
                next_agent_id = PIPELINE_STAGES[stage_idx + 1][0]
                next_agent = self._agents.get(next_agent_id)

            # Transition state (Req 6.4)
            pipeline_run.state = stage_state
            await self._broadcast_state_transition(pipeline_run, agent_id)

            try:
                # Inject run_id into agent if it supports it
                agent._current_run_id = run_id

                # Override agent mode to active_auto during pipeline execution
                # so that feature flags don't block agent evaluation
                if hasattr(agent, '_pipeline_mode_override'):
                    agent._pipeline_mode_override = "active_auto"
                else:
                    agent._pipeline_mode_override = "active_auto"

                # Capture published messages during monitor_cycle (Req 1.1, 2.1)
                captured_messages: List[Any] = []
                if self._signal_bus is not None:
                    async with PipelinePublishCapture(self._signal_bus) as capture:
                        await agent.monitor_cycle()
                        captured_messages = capture.captured
                else:
                    await agent.monitor_cycle()

                # Inject captured output into next agent's buffer (Req 1.2, 1.3)
                await self._capture_and_inject(
                    current_agent_id=agent_id,
                    captured_messages=captured_messages,
                    next_agent=next_agent,
                    tenant_id=tenant_id,
                )

                pipeline_run.stage_results[agent_id] = "completed"
                # Clear pipeline mode override after stage completes
                agent._pipeline_mode_override = None
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

    # ------------------------------------------------------------------
    # Direct buffer injection (Req 1.1, 1.2, 1.3, 1.5)
    # ------------------------------------------------------------------

    # Stage-to-buffer mapping: names the typed buffer on the *next* agent that
    # receives the captured messages from the *current* stage.
    #
    # ``tank_forecasting`` is deliberately absent, and that absence is the fix
    # for a silent pipeline break. ``DeliveryPrioritizationAgent`` owns no typed
    # buffer: it reads forecasts back out of ``mvp_tank_forecasts``, which
    # ``TankForecastingAgent`` persists (step 9) *before* it publishes them
    # (step 10), so within one synchronous run the read is complete and
    # race-free. What the stage needs from its predecessor is a cycle trigger,
    # not an in-memory payload.
    #
    # Mapping it to ``_forecast_buffer`` — an attribute a refactor removed from
    # that agent — meant the ``hasattr`` guard below skipped the injection and
    # returned *before* the trigger was seeded. Prioritization therefore never
    # ran, loading and routing had empty buffers, and the run still reported
    # ``complete``. Any stage not named here is triggered with no payload.
    _STAGE_BUFFER_MAP: Dict[str, str] = {
        "delivery_prioritization": "_priority_buffer",
        "compartment_loading": "_proposal_buffer",
    }

    # The payload type each typed buffer holds, mirroring the isinstance check
    # in the receiving agent's own ``_on_signal``.
    #
    # A stage publishes more than its payload: ``monitor_cycle`` also routes the
    # ``InterventionProposal`` that ``evaluate()`` returns, so
    # ``delivery_prioritization`` emits a ``DeliveryPriorityList`` *and* a
    # proposal. Injecting both unfiltered put the proposal last in
    # ``_priority_buffer``, and ``CompartmentLoadingAgent.evaluate`` reads
    # ``priority_lists[-1]`` — so the loading stage treated a proposal as its
    # priority list, built no delivery requests, and returned empty while the
    # run reported ``complete``. The SignalBus path never had this problem
    # because ``_on_signal`` type-checks before buffering; only the pipeline's
    # direct injection skipped that check.
    #
    # ``test_pipeline_payload_types_match_agent_routing`` pins each entry
    # against the receiving agent's ``_on_signal``, so the two cannot drift.
    _STAGE_PAYLOAD_TYPES: Dict[str, type] = {
        "delivery_prioritization": DeliveryPriorityList,
        "compartment_loading": InterventionProposal,
    }

    async def _capture_and_inject(
        self,
        current_agent_id: str,
        captured_messages: List[Any],
        next_agent: Optional[Any],
        tenant_id: str,
    ) -> None:
        """Hand one stage's output to the next stage, and always trigger it.

        Two separate things happen here, and conflating them is what previously
        broke the pipeline:

        * **Payload transfer.** A stage whose successor consumes a typed buffer
          (``delivery_prioritization`` → ``_priority_buffer``,
          ``compartment_loading`` → ``_proposal_buffer``) has its published
          messages appended to that buffer. This bypasses the SignalBus
          subscription mechanism, which is what makes a synchronous run work —
          and because it bypasses ``_on_signal``, it has to reproduce that
          method's type filter itself via ``_STAGE_PAYLOAD_TYPES``. A stage
          publishes its payload *and* the ``InterventionProposal`` its
          ``evaluate()`` returned; appending both left the proposal last in a
          buffer whose reader takes ``[-1]``.
        * **Cycle trigger.** ``OverlayAgentBase.monitor_cycle`` collects
          ``_signal_buffer`` and returns early when it is empty — *before* it
          groups by tenant and calls ``evaluate()``. Every successor therefore
          needs one signal here whether or not it also receives a payload, so
          the trigger is now unconditional. It previously sat behind two early
          returns, so a stage that published nothing, or one whose mapped buffer
          was missing, left the remainder of the pipeline dead while the run
          still reported ``complete``.

        A stage publishing nothing is a data condition, not a fault:
        prioritization can still score will-call orders from delivery windows
        with no forecast, and the loading and routing stages return an empty
        proposal list when their buffer is empty. The successor is triggered
        regardless so that outcome is reached honestly rather than by
        short-circuit.

        Args:
            current_agent_id: The agent_id of the stage that just completed.
            captured_messages: Messages captured from SignalBus.publish()
                during the stage's monitor_cycle().
            next_agent: The next agent in the pipeline sequence, or ``None``
                if this is the last stage.
            tenant_id: The run's tenant, taken from the pipeline run rather than
                inferred from message contents. A stage that published nothing
                must still trigger its successor under the right tenant —
                ``_group_by_tenant`` drops any signal without a ``tenant_id``,
                and the previous ``"unknown"`` fallback silently produced a
                tenant with no orders.

        Raises:
            RuntimeError: When a mapped typed buffer, or ``_signal_buffer``, is
                absent from the next agent. That is a wiring fault rather than a
                data condition, so the circuit breaker must fail the run instead
                of reporting success on a stage that cannot run.

        Validates: Requirements 1.1, 1.2, 1.3, 1.5
        """
        if next_agent is None:
            return

        target_buffer_name = self._STAGE_BUFFER_MAP.get(current_agent_id)
        payload: List[Any] = []

        if target_buffer_name is not None:
            if not hasattr(next_agent, target_buffer_name):
                raise RuntimeError(
                    "FuelDistributionPipeline wiring fault: stage "
                    f"'{current_agent_id}' is mapped to buffer "
                    f"'{target_buffer_name}', but "
                    f"{type(next_agent).__name__} has no such attribute. "
                    "Either the agent's buffer was renamed or removed, or "
                    "_STAGE_BUFFER_MAP is stale."
                )

            expected_type = self._STAGE_PAYLOAD_TYPES.get(current_agent_id)
            if expected_type is None:
                raise RuntimeError(
                    "FuelDistributionPipeline wiring fault: stage "
                    f"'{current_agent_id}' is mapped to buffer "
                    f"'{target_buffer_name}' but declares no payload type in "
                    "_STAGE_PAYLOAD_TYPES, so injected messages cannot be "
                    "filtered the way the receiving agent's _on_signal "
                    "filters them."
                )

            payload = [
                message
                for message in captured_messages
                if isinstance(message, expected_type)
            ]
            if payload:
                getattr(next_agent, target_buffer_name).extend(payload)
                logger.info(
                    "FuelDistributionPipeline: injected %d %s message(s) from "
                    "stage '%s' into %s.%s (%d other message(s) not routed "
                    "to this buffer)",
                    len(payload),
                    expected_type.__name__,
                    current_agent_id,
                    getattr(next_agent, "agent_id", "unknown"),
                    target_buffer_name,
                    len(captured_messages) - len(payload),
                )
            elif captured_messages:
                # Published something, but nothing the successor can consume.
                # Not fatal — the successor returns an empty result on an empty
                # buffer — but it is never expected, so it must be visible.
                logger.warning(
                    "FuelDistributionPipeline: stage '%s' published %d "
                    "message(s), none of them %s, so %s.%s received nothing",
                    current_agent_id,
                    len(captured_messages),
                    expected_type.__name__,
                    getattr(next_agent, "agent_id", "unknown"),
                    target_buffer_name,
                )

        if not captured_messages:
            logger.warning(
                "FuelDistributionPipeline: stage '%s' published nothing; "
                "triggering the next stage with no payload",
                current_agent_id,
            )

        # Skip only when the payload itself already landed in _signal_buffer,
        # which is the one case where monitor_cycle cannot short-circuit.
        if not (target_buffer_name == "_signal_buffer" and payload):
            self._seed_cycle_trigger(
                next_agent=next_agent,
                current_agent_id=current_agent_id,
                tenant_id=tenant_id,
            )

    @staticmethod
    def _seed_cycle_trigger(
        *,
        next_agent: Any,
        current_agent_id: str,
        tenant_id: str,
    ) -> None:
        """Append the signal that lets the next stage's ``evaluate()`` run.

        ``monitor_cycle`` drains ``_signal_buffer`` first and returns
        ``([], [])`` when it is empty, so a stage that reads only a typed buffer
        would never be evaluated without this. The signal carries the run's
        tenant because ``_group_by_tenant`` skips anything without one and
        ``evaluate()`` is invoked once per tenant it finds.

        Raises:
            RuntimeError: When the next agent has no ``_signal_buffer``. Every
                ``OverlayAgentBase`` subclass does; an agent that does not
                cannot be triggered at all, and skipping it silently is the
                defect this method exists to prevent.
        """
        if not hasattr(next_agent, "_signal_buffer"):
            raise RuntimeError(
                "FuelDistributionPipeline wiring fault: "
                f"{type(next_agent).__name__} has no _signal_buffer, so the "
                f"stage after '{current_agent_id}' cannot be triggered."
            )

        from Agents.overlay.data_contracts import RiskSignal, Severity

        next_agent._signal_buffer.append(
            RiskSignal(
                source_agent="pipeline_injection",
                entity_id=f"pipeline_stage_{current_agent_id}",
                entity_type="pipeline_trigger",
                severity=Severity.LOW,
                confidence=1.0,
                ttl_seconds=3600,
                context={
                    "trigger": "pipeline_injection",
                    "source_stage": current_agent_id,
                },
                tenant_id=tenant_id,
            )
        )

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
