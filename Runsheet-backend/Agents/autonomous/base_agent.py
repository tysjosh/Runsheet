"""
Autonomous Agent Base Class.

Shared base for all background monitoring agents with polling loop,
cooldown tracking, error handling, and activity logging. Concrete
agents implement ``monitor_cycle`` to define their detection and
action logic.

Requirements: 3.1, 3.6, 3.7, 4.1, 4.4, 4.6, 5.1, 5.7
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple, List, Any


class AutonomousAgentBase(ABC):
    """Base class for background monitoring agents.

    Provides a polling loop that repeatedly calls :meth:`monitor_cycle`,
    logs each cycle to the Activity Log Service, and manages per-entity
    cooldown tracking to prevent duplicate actions.

    Subclasses must implement :meth:`monitor_cycle` which returns a
    ``(detections, actions)`` tuple describing what was found and what
    actions were taken during the cycle.

    Args:
        agent_id: Unique identifier for this agent instance.
        poll_interval_seconds: Seconds between polling cycles.
        cooldown_minutes: Minutes to suppress duplicate actions for the
            same entity.
        activity_log_service: Service for logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: Protocol for routing mutations.
        feature_flag_service: Optional service for tenant feature flags.
    """

    def __init__(
        self,
        agent_id: str,
        poll_interval_seconds: int,
        cooldown_minutes: int,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        feature_flag_service=None,
    ):
        self.agent_id = agent_id
        self.poll_interval = poll_interval_seconds
        self.cooldown_minutes = cooldown_minutes
        self._activity_log = activity_log_service
        self._ws = ws_manager
        self._confirmation_protocol = confirmation_protocol
        self._feature_flags = feature_flag_service
        self._cooldown_tracker: Dict[str, datetime] = {}
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self.logger = logging.getLogger(f"agent.{agent_id}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the polling loop as a background asyncio task."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        self.logger.info(f"Agent {self.agent_id} started")

    async def stop(self) -> None:
        """Gracefully stop the agent by cancelling the background task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.logger.info(f"Agent {self.agent_id} stopped")

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Main polling loop.

        Repeatedly calls :meth:`monitor_cycle`, logs the cycle to the
        Activity Log Service, and sleeps for ``poll_interval`` seconds.
        Exceptions inside ``monitor_cycle`` are caught and logged so the
        loop never dies unexpectedly.
        """
        while self._running:
            cycle_start = datetime.now(timezone.utc)
            try:
                detections, actions = await self.monitor_cycle()
                duration_ms = (
                    datetime.now(timezone.utc) - cycle_start
                ).total_seconds() * 1000
                # Only log to ES when something was detected or acted on
                # Reduces write volume by ~90% for idle cycles
                if len(detections) > 0 or len(actions) > 0:
                    await self._activity_log.log_monitoring_cycle(
                        self.agent_id,
                        len(detections),
                        len(actions),
                        duration_ms,
                    )
            except Exception as e:
                self.logger.error(f"Monitor cycle error: {e}", exc_info=True)
            await asyncio.sleep(self.poll_interval)

    # ------------------------------------------------------------------
    # Cooldown management
    # ------------------------------------------------------------------

    def _is_on_cooldown(self, entity_id: str) -> bool:
        """Check whether *entity_id* was acted on within the cooldown window.

        Args:
            entity_id: The identifier of the entity to check.

        Returns:
            ``True`` if the entity is still within the cooldown period.
        """
        last = self._cooldown_tracker.get(entity_id)
        if last is None:
            return False
        return (datetime.now(timezone.utc) - last) < timedelta(
            minutes=self.cooldown_minutes
        )

    def _set_cooldown(self, entity_id: str) -> None:
        """Record the current time as the last-action time for *entity_id*.

        Args:
            entity_id: The identifier of the entity to set cooldown for.
        """
        self._cooldown_tracker[entity_id] = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one monitoring cycle.

        Concrete agents implement this to poll data sources, detect
        conditions, and take corrective actions.

        Returns:
            A ``(detections, actions)`` tuple where *detections* is a
            list of detected conditions and *actions* is a list of
            actions taken during this cycle.
        """
        ...  # pragma: no cover

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def status(self) -> str:
        """Return the current agent status.

        Returns:
            ``"running"`` if the background task is active,
            ``"error"`` if the task finished with an exception,
            ``"stopped"`` otherwise.
        """
        if self._running and self._task and not self._task.done():
            return "running"
        if self._task and self._task.done():
            try:
                exc = self._task.exception()
                if exc is not None:
                    return "error"
            except asyncio.CancelledError:
                return "stopped"
        return "stopped"
