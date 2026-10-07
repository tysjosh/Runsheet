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
        # Per-cycle {tenant_id: [detections, actions]} noted by subclasses so
        # the monitoring-cycle entry can be written per tenant (F9).
        self._tenant_activity: Dict[Optional[str], List[int]] = {}
        self._warned_no_activity_log = False

    def set_activity_log_service(self, activity_log_service) -> None:
        """Late-bind the activity log.

        The compliance crons are built before the agents bootstrap creates
        the ActivityLogService, so they start with ``None`` and are handed
        the real service once it exists.
        """
        self._activity_log = activity_log_service

    def _note_tenant_activity(
        self, tenant_id: Optional[str], detections: int = 0, actions: int = 0
    ) -> None:
        """Record this cycle's detections/actions for one tenant.

        Subclasses call this inside their per-tenant loop; the cycle is then
        logged once per noted tenant with that tenant's id.
        """
        counts = self._tenant_activity.setdefault(tenant_id, [0, 0])
        counts[0] += detections
        counts[1] += actions

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the polling loop as a background asyncio task.

        Idempotent: if a polling loop is already running, this is a no-op so
        a second ``start()`` cannot orphan the existing ``_run_loop`` task
        (which would keep polling forever alongside the new one).
        """
        if self._running and self._task is not None and not self._task.done():
            self.logger.debug(
                "Agent %s already running — start() ignored", self.agent_id
            )
            return
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
        from persistence.leader_election import is_sweep_leader, wait_for_leadership

        while self._running:
            # Only the sweep leader acts. Two processes running the same agent
            # is not merely duplicated work: ``_cooldown_tracker`` is
            # per-process memory, so each copy has its own idea of what it has
            # already handled and both re-escalate the same entity. Standing
            # down keeps the loop alive so leadership can move here later.
            #
            # A follower waits for leadership, not for a whole poll interval,
            # and cycles as soon as it takes over. Leadership arrives a minute
            # or two after boot, so sleeping ``poll_interval`` (24 h for the
            # compliance crons) meant those crons never ran on an environment
            # redeployed more often than daily.
            if not is_sweep_leader():
                await wait_for_leadership(self.poll_interval)
                continue

            cycle_start = datetime.now(timezone.utc)
            self._tenant_activity = {}
            try:
                detections, actions = await self.monitor_cycle()
                duration_ms = (
                    datetime.now(timezone.utc) - cycle_start
                ).total_seconds() * 1000
                # Only log to ES when something was detected or acted on
                # Reduces write volume by ~90% for idle cycles
                if len(detections) > 0 or len(actions) > 0:
                    await self._log_cycle(detections, actions, duration_ms)
            except Exception:
                self.logger.exception("Monitor cycle error")
            await asyncio.sleep(self.poll_interval)

    async def _log_cycle(
        self, detections: List[Any], actions: List[Any], duration_ms: float
    ) -> None:
        """Write one ``monitoring_cycle`` entry per tenant for this cycle."""
        if self._activity_log is None:
            # Built before the activity log existed and never adopted: skip
            # the write rather than raise AttributeError every cycle.
            if not self._warned_no_activity_log:
                self.logger.warning(
                    "Agent %s has no activity log service; monitoring "
                    "cycles are not being logged",
                    self.agent_id,
                )
                self._warned_no_activity_log = True
            return

        per_tenant = {
            tenant_id: counts
            for tenant_id, counts in self._tenant_activity.items()
            if counts[0] or counts[1]
        } or self._cycle_counts_by_tenant(detections, actions)

        for tenant_id, (detection_count, action_count) in per_tenant.items():
            await self._activity_log.log_monitoring_cycle(
                self.agent_id,
                detection_count,
                action_count,
                duration_ms,
                tenant_id=tenant_id,
            )

    @staticmethod
    def _cycle_counts_by_tenant(
        detections: List[Any], actions: List[Any]
    ) -> Dict[Optional[str], List[int]]:
        """Count cycle results by the ``tenant_id`` each item carries.

        Deliberately not named ``_group_by_tenant``: ``OverlayAgentBase``
        defines an instance method of that name with a different signature,
        which shadowed this one and made ``_log_cycle`` raise ``TypeError``
        for every overlay agent cycle that detected something.

        Dicts and objects with a string ``tenant_id`` group under it; bare
        ids and anything else group under ``None``.
        """
        def _tenant_of(item: Any) -> Optional[str]:
            if isinstance(item, dict):
                value = item.get("tenant_id")
            else:
                value = getattr(item, "tenant_id", None)
            return value if isinstance(value, str) else None

        grouped: Dict[Optional[str], List[int]] = {}
        for item in detections:
            grouped.setdefault(_tenant_of(item), [0, 0])[0] += 1
        for item in actions:
            grouped.setdefault(_tenant_of(item), [0, 0])[1] += 1
        return grouped

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
