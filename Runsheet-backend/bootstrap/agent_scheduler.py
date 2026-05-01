"""
Autonomous Agent Scheduler with restart policies and health reporting.

Replaces bare ``asyncio.create_task`` calls in the lifespan function
with a managed lifecycle framework.

Requirements: 7.1–7.7, 8.1–8.6
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from Agents.autonomous.base_agent import AutonomousAgentBase

logger = logging.getLogger(__name__)


class RestartPolicy(Enum):
    """Restart policy for autonomous agents."""
    ALWAYS = "always"          # Restart on any exit
    ON_FAILURE = "on_failure"  # Restart only on unhandled exception
    NEVER = "never"            # Do not restart


# SLO constants (Req 8.1, 8.2)
SLO_MAX_RESTART_SECONDS = 5
SLO_MAX_CONSECUTIVE_FAILURES = 3
SLO_MIN_UPTIME_PCT = 99.0
SLO_RESTART_WINDOW_SECONDS = 300  # 5-minute window for max restarts
SLO_MAX_CYCLE_DURATION_SECONDS = 5
SLO_SCHEDULE_DRIFT_PCT = 10

# Rolling window for uptime SLO calculation (Req 8.4)
SLO_UPTIME_WINDOW_SECONDS = 86400  # 24 hours


@dataclass
class UptimeRecord:
    """A single uptime/downtime interval for an agent."""
    start: datetime
    end: Optional[datetime] = None
    is_up: bool = True


@dataclass
class AgentState:
    """Runtime state for a managed agent."""
    agent: AutonomousAgentBase
    policy: RestartPolicy
    status: str = "stopped"  # running | stopped | restarting | failed
    task: Optional[asyncio.Task] = None
    started_at: Optional[datetime] = None
    restart_count: int = 0
    restart_timestamps: List[datetime] = field(default_factory=list)
    last_error: Optional[str] = None
    total_uptime_seconds: float = 0.0
    # SLO compliance tracking (Req 8.3, 8.4)
    uptime_records: List[UptimeRecord] = field(default_factory=list)


class AgentScheduler:
    """Manages the lifecycle of all autonomous background agents.

    Provides:
    - Configurable restart policies (always, on_failure, never)
    - Bounded restart attempts (max 3 within 5-minute window)
    - Health reporting per agent
    - Graceful shutdown with configurable timeout
    - SLO compliance tracking

    Args:
        telemetry_service: For emitting alerts on SLO violations.
        activity_log_service: For recording restart events.
        shutdown_timeout: Seconds to wait for graceful stop (default 10).
    """

    def __init__(
        self,
        telemetry_service=None,
        activity_log_service=None,
        shutdown_timeout: float = 10.0,
    ) -> None:
        self._agents: Dict[str, AgentState] = {}
        self._telemetry = telemetry_service
        self._activity_log = activity_log_service
        self._shutdown_timeout = shutdown_timeout

    def register(
        self,
        agent: AutonomousAgentBase,
        policy: RestartPolicy = RestartPolicy.ON_FAILURE,
    ) -> None:
        """Register an agent with a restart policy."""
        self._agents[agent.agent_id] = AgentState(agent=agent, policy=policy)

    async def start_all(self) -> None:
        """Start all registered agents."""
        for agent_id, state in self._agents.items():
            await self._start_agent(state)

    async def stop_all(self) -> None:
        """Stop all agents gracefully within the shutdown timeout."""
        tasks = []
        for state in self._agents.values():
            tasks.append(self._stop_agent(state))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _start_agent(self, state: AgentState) -> None:
        """Start a single agent and create a monitoring task."""
        await state.agent.start()
        state.status = "running"
        state.started_at = datetime.now(timezone.utc)
        self._record_uptime_start(state)
        state.task = asyncio.create_task(
            self._monitor_agent(state),
            name=f"scheduler-{state.agent.agent_id}",
        )

    async def _stop_agent(self, state: AgentState) -> None:
        """Stop a single agent with timeout. Force-cancel if timeout exceeded."""
        try:
            await asyncio.wait_for(
                state.agent.stop(), timeout=self._shutdown_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Agent %s did not stop within %ss, force-cancelling",
                state.agent.agent_id, self._shutdown_timeout,
            )
            if state.agent._task and not state.agent._task.done():
                state.agent._task.cancel()
        except Exception as exc:
            logger.error(
                "Error stopping agent %s: %s",
                state.agent.agent_id, exc,
            )

        # Cancel the monitoring task
        if state.task and not state.task.done():
            state.task.cancel()
            try:
                await state.task
            except asyncio.CancelledError:
                pass

        # Accumulate uptime
        if state.started_at and state.status == "running":
            elapsed = (datetime.now(timezone.utc) - state.started_at).total_seconds()
            state.total_uptime_seconds += elapsed

        # Close the current uptime/downtime record
        self._close_current_record(state)

        state.status = "stopped"

    async def _monitor_agent(self, state: AgentState) -> None:
        """Watch the agent's internal task and restart on failure."""
        while state.status == "running":
            if state.agent._task is None or not state.agent._task.done():
                await asyncio.sleep(1)
                continue

            # Task exited — check for exception
            exc = None
            try:
                exc = state.agent._task.exception()
            except asyncio.CancelledError:
                state.status = "stopped"
                return

            # Accumulate uptime before restart
            if state.started_at:
                elapsed = (datetime.now(timezone.utc) - state.started_at).total_seconds()
                state.total_uptime_seconds += elapsed

            # Normal exit with ON_FAILURE or NEVER policy — don't restart
            if exc is None and state.policy != RestartPolicy.ALWAYS:
                state.status = "stopped"
                self._close_current_record(state)
                return

            # Restart logic
            if not self._can_restart(state):
                state.status = "failed"
                state.last_error = str(exc) if exc else "max restarts exceeded"
                self._record_downtime_start(state)
                logger.critical(
                    "Agent %s marked as FAILED: %s",
                    state.agent.agent_id, state.last_error,
                )
                if self._telemetry:
                    try:
                        self._telemetry.emit_alert(
                            "agent_failed",
                            agent_id=state.agent.agent_id,
                            error=state.last_error,
                        )
                    except Exception:
                        logger.error(
                            "Failed to emit telemetry alert for agent %s",
                            state.agent.agent_id,
                        )
                # Check uptime SLO on failure
                self._check_and_alert_uptime_slo(state)
                return

            state.status = "restarting"
            # Record downtime during restart
            self._record_downtime_start(state)
            state.restart_count += 1
            state.restart_timestamps.append(datetime.now(timezone.utc))
            state.last_error = str(exc) if exc else None

            logger.warning(
                "Restarting agent %s (attempt %d): %s",
                state.agent.agent_id, state.restart_count, state.last_error,
            )

            if self._activity_log:
                try:
                    await self._activity_log.log_monitoring_cycle(
                        state.agent.agent_id, 0, 0, 0,
                    )
                except Exception:
                    logger.error(
                        "Failed to log restart event for agent %s",
                        state.agent.agent_id,
                    )

            # Brief pause before restart (within SLO_MAX_RESTART_SECONDS)
            await asyncio.sleep(0.5)
            await state.agent.start()
            state.status = "running"
            state.started_at = datetime.now(timezone.utc)
            # Close downtime record and start new uptime record
            self._close_current_record(state)
            self._record_uptime_start(state)
            # Prune old records and check SLO compliance
            self._prune_old_records(state)
            self._check_and_alert_uptime_slo(state)

    def _can_restart(self, state: AgentState) -> bool:
        """Check if restart is allowed within the SLO window."""
        if state.policy == RestartPolicy.NEVER:
            return False
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - SLO_RESTART_WINDOW_SECONDS
        recent = [
            ts for ts in state.restart_timestamps
            if ts.timestamp() > cutoff
        ]
        return len(recent) < SLO_MAX_CONSECUTIVE_FAILURES

    def get_health(self) -> Dict[str, Any]:
        """Return health status for all managed agents.

        Returns a dict keyed by agent_id with status, uptime,
        restart_count, last_error, policy, and uptime_pct_24h for each agent.
        """
        result = {}
        now = datetime.now(timezone.utc)
        for agent_id, state in self._agents.items():
            uptime = state.total_uptime_seconds
            if state.started_at and state.status == "running":
                uptime += (now - state.started_at).total_seconds()
            uptime_pct = self.get_uptime_pct(agent_id)
            result[agent_id] = {
                "status": state.status,
                "uptime_seconds": uptime,
                "restart_count": state.restart_count,
                "last_error": state.last_error,
                "policy": state.policy.value,
                "uptime_pct_24h": uptime_pct,
            }
        return result

    # ------------------------------------------------------------------
    # SLO compliance tracking (Req 8.3, 8.4)
    # ------------------------------------------------------------------

    def _record_uptime_start(self, state: AgentState) -> None:
        """Record the start of an uptime interval for an agent."""
        state.uptime_records.append(
            UptimeRecord(start=datetime.now(timezone.utc), is_up=True)
        )

    def _record_downtime_start(self, state: AgentState) -> None:
        """Close the current uptime interval and start a downtime interval."""
        now = datetime.now(timezone.utc)
        # Close the last uptime record if open
        if state.uptime_records and state.uptime_records[-1].end is None:
            state.uptime_records[-1].end = now
        # Start a downtime record
        state.uptime_records.append(
            UptimeRecord(start=now, is_up=False)
        )

    def _close_current_record(self, state: AgentState) -> None:
        """Close the current (uptime or downtime) record."""
        now = datetime.now(timezone.utc)
        if state.uptime_records and state.uptime_records[-1].end is None:
            state.uptime_records[-1].end = now

    def get_uptime_pct(self, agent_id: str) -> float:
        """Calculate the rolling 24-hour uptime percentage for an agent.

        Returns 100.0 if the agent has no records or has been tracked
        for less than 1 second within the window.
        """
        if agent_id not in self._agents:
            return 100.0

        state = self._agents[agent_id]
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(seconds=SLO_UPTIME_WINDOW_SECONDS)

        total_up = 0.0
        total_tracked = 0.0

        for record in state.uptime_records:
            rec_start = max(record.start, window_start)
            rec_end = record.end if record.end else now

            # Skip records entirely outside the window
            if rec_end <= window_start:
                continue
            if rec_start >= now:
                continue

            rec_end = min(rec_end, now)
            duration = (rec_end - rec_start).total_seconds()
            if duration <= 0:
                continue

            total_tracked += duration
            if record.is_up:
                total_up += duration

        if total_tracked < 1.0:
            return 100.0

        return (total_up / total_tracked) * 100.0

    def check_slo_compliance(self, agent_id: str) -> Tuple[bool, float]:
        """Check if an agent meets the uptime SLO.

        Returns:
            A tuple of (is_compliant, uptime_pct) where is_compliant
            is True if uptime_pct >= SLO_MIN_UPTIME_PCT.
        """
        uptime_pct = self.get_uptime_pct(agent_id)
        return uptime_pct >= SLO_MIN_UPTIME_PCT, uptime_pct

    def _check_and_alert_uptime_slo(self, state: AgentState) -> None:
        """Check uptime SLO and emit a warning alert if violated (Req 8.4)."""
        is_compliant, uptime_pct = self.check_slo_compliance(state.agent.agent_id)
        if not is_compliant and self._telemetry:
            try:
                self._telemetry.emit_alert(
                    "agent_uptime_slo_violation",
                    agent_id=state.agent.agent_id,
                    uptime_pct=uptime_pct,
                    slo_target=SLO_MIN_UPTIME_PCT,
                    window_seconds=SLO_UPTIME_WINDOW_SECONDS,
                )
                logger.warning(
                    "Agent %s uptime SLO violation: %.2f%% (target: %.1f%%)",
                    state.agent.agent_id, uptime_pct, SLO_MIN_UPTIME_PCT,
                )
            except Exception:
                logger.error(
                    "Failed to emit uptime SLO alert for agent %s",
                    state.agent.agent_id,
                )

    def _prune_old_records(self, state: AgentState) -> None:
        """Remove uptime records older than the 24-hour window to limit memory."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=SLO_UPTIME_WINDOW_SECONDS)
        state.uptime_records = [
            r for r in state.uptime_records
            if (r.end is None or r.end > cutoff)
        ]
