"""
Driver Nudge Agent — Layer 1 overlay agent for unacknowledged assignment monitoring.

Polls Elasticsearch for jobs in 'assigned' status that have not received
a driver acknowledgment within configurable timeouts. Generates reminder
InterventionProposals after nudge_timeout and escalation proposals after
escalation_timeout.

Per-job cooldown prevents duplicate nudges for the same assignment.

Validates: Requirements 15.1, 15.2, 15.3, 15.4
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.overlay.signal_bus import SignalBus

logger = logging.getLogger(__name__)

# Default timeouts in minutes
DEFAULT_NUDGE_TIMEOUT_MINUTES = 10
DEFAULT_ESCALATION_TIMEOUT_MINUTES = 15
DEFAULT_POLL_INTERVAL_SECONDS = 60

# Cooldown key prefixes to distinguish nudge vs escalation cooldowns
_NUDGE_PREFIX = "nudge:"
_ESCALATION_PREFIX = "escalation:"


class DriverNudgeAgent(OverlayAgentBase):
    """Monitors unacknowledged job assignments and generates nudge/escalation proposals.

    Polls ES for jobs in ``assigned`` status without a driver acknowledgment
    event. When the time since assignment exceeds ``nudge_timeout_minutes``,
    a reminder InterventionProposal is generated. When it exceeds
    ``escalation_timeout_minutes``, an escalation proposal is generated
    targeting the dispatcher.

    A per-job cooldown dict prevents duplicate nudges for the same
    assignment within the cooldown window.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service for querying jobs.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        nudge_timeout_minutes: Minutes after assignment before sending
            a reminder (default 10).
        escalation_timeout_minutes: Minutes after assignment before
            escalating to dispatcher (default 15).
        poll_interval: Decision cycle interval in seconds (default 60).
    """

    def __init__(
        self,
        signal_bus: SignalBus,
        es_service,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        autonomy_config_service,
        feature_flag_service,
        nudge_timeout_minutes: int = DEFAULT_NUDGE_TIMEOUT_MINUTES,
        escalation_timeout_minutes: int = DEFAULT_ESCALATION_TIMEOUT_MINUTES,
        poll_interval: int = DEFAULT_POLL_INTERVAL_SECONDS,
    ):
        super().__init__(
            agent_id="driver_nudge_agent",
            signal_bus=signal_bus,
            subscriptions=[],  # This agent polls ES directly, no signal subscriptions
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            autonomy_config_service=autonomy_config_service,
            feature_flag_service=feature_flag_service,
            es_service=es_service,
            poll_interval=poll_interval,
            cooldown_minutes=nudge_timeout_minutes,
        )
        self._nudge_timeout_minutes = nudge_timeout_minutes
        self._escalation_timeout_minutes = escalation_timeout_minutes

        # Per-job cooldown dicts keyed by job_id — stores the last time
        # a nudge or escalation was generated for each job.
        self._nudge_cooldowns: Dict[str, datetime] = {}
        self._escalation_cooldowns: Dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def nudge_timeout_minutes(self) -> int:
        """Return the configured nudge timeout in minutes."""
        return self._nudge_timeout_minutes

    @property
    def escalation_timeout_minutes(self) -> int:
        """Return the configured escalation timeout in minutes."""
        return self._escalation_timeout_minutes

    # ------------------------------------------------------------------
    # Decision cycle override
    # ------------------------------------------------------------------

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one decision cycle: query ES for unacked jobs, generate proposals.

        Unlike signal-driven overlay agents, the DriverNudgeAgent polls
        Elasticsearch directly for jobs in ``assigned`` status without
        an acknowledgment event past the configured timeouts.

        Returns:
            A ``(detections, proposals)`` tuple for activity logging.
        """
        cycle_start = time.monotonic()

        try:
            unacked_jobs = await self._query_unacked_jobs()
        except Exception as e:
            self.logger.error(
                "Failed to query unacknowledged jobs: %s", e, exc_info=True
            )
            return [], []

        if not unacked_jobs:
            return [], []

        proposals = self._generate_proposals(unacked_jobs)

        cycle_duration_ms = (time.monotonic() - cycle_start) * 1000
        self._cycle_metrics.update({
            "signals_consumed": len(unacked_jobs),
            "proposals_generated": len(proposals),
            "cycle_duration_ms": cycle_duration_ms,
        })

        return unacked_jobs, proposals

    # ------------------------------------------------------------------
    # evaluate() — required by OverlayAgentBase but not used directly
    # ------------------------------------------------------------------

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Not used — DriverNudgeAgent overrides monitor_cycle directly.

        This method satisfies the abstract interface but the agent's
        logic lives in ``monitor_cycle`` and ``_generate_proposals``.
        """
        return []

    # ------------------------------------------------------------------
    # ES query
    # ------------------------------------------------------------------

    async def _query_unacked_jobs(self) -> List[Dict[str, Any]]:
        """Query ES for jobs in 'assigned' status without an ack event.

        Returns jobs where:
        - status is 'assigned'
        - assigned_at is older than nudge_timeout_minutes ago
        - No 'ack' event exists in the job's event timeline

        Returns:
            List of job documents from ES.
        """
        nudge_cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=self._nudge_timeout_minutes
        )

        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"status": "assigned"}},
                        {
                            "range": {
                                "assigned_at": {
                                    "lte": nudge_cutoff.isoformat()
                                }
                            }
                        },
                    ],
                    "must_not": [
                        {"term": {"driver_acked": True}},
                    ],
                }
            },
            "size": 100,
        }

        try:
            from scheduling.services.scheduling_es_mappings import JOBS_CURRENT_INDEX
            result = await self._es.search_documents(JOBS_CURRENT_INDEX, query, size=100)
            hits = result.get("hits", {}).get("hits", [])
            return [hit["_source"] for hit in hits]
        except Exception as e:
            self.logger.error("ES query for unacked jobs failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # Proposal generation
    # ------------------------------------------------------------------

    def _generate_proposals(
        self, unacked_jobs: List[Dict[str, Any]]
    ) -> List[InterventionProposal]:
        """Generate nudge and escalation proposals for unacknowledged jobs.

        For each unacked job:
        - If past nudge_timeout and not on nudge cooldown → reminder proposal
        - If past escalation_timeout and not on escalation cooldown → escalation proposal

        Args:
            unacked_jobs: List of job documents from ES.

        Returns:
            List of InterventionProposals.
        """
        now = datetime.now(timezone.utc)
        proposals: List[InterventionProposal] = []

        for job in unacked_jobs:
            job_id = job.get("job_id", "")
            tenant_id = job.get("tenant_id", "default")
            driver_id = job.get("asset_assigned", job.get("driver_id", ""))
            assigned_at_str = job.get("assigned_at", "")

            if not assigned_at_str or not job_id:
                continue

            try:
                assigned_at = datetime.fromisoformat(
                    assigned_at_str.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                continue

            minutes_since_assignment = (now - assigned_at).total_seconds() / 60

            # Check escalation first (higher priority)
            if minutes_since_assignment >= self._escalation_timeout_minutes:
                if not self._is_on_escalation_cooldown(job_id):
                    proposal = self._create_escalation_proposal(
                        job_id=job_id,
                        tenant_id=tenant_id,
                        driver_id=driver_id,
                        minutes_waiting=minutes_since_assignment,
                    )
                    proposals.append(proposal)
                    self._set_escalation_cooldown(job_id)

            # Check nudge timeout
            if minutes_since_assignment >= self._nudge_timeout_minutes:
                if not self._is_on_nudge_cooldown(job_id):
                    proposal = self._create_nudge_proposal(
                        job_id=job_id,
                        tenant_id=tenant_id,
                        driver_id=driver_id,
                        minutes_waiting=minutes_since_assignment,
                    )
                    proposals.append(proposal)
                    self._set_nudge_cooldown(job_id)

        return proposals

    # ------------------------------------------------------------------
    # Proposal factories
    # ------------------------------------------------------------------

    def _create_nudge_proposal(
        self,
        job_id: str,
        tenant_id: str,
        driver_id: str,
        minutes_waiting: float,
    ) -> InterventionProposal:
        """Create a reminder InterventionProposal for an unacknowledged assignment.

        Validates: Requirement 15.1
        """
        return InterventionProposal(
            source_agent=self.agent_id,
            actions=[
                {
                    "tool_name": "send_driver_nudge",
                    "parameters": {
                        "job_id": job_id,
                        "driver_id": driver_id,
                        "notification_type": "assignment_reminder",
                        "message_template": "driver_nudge_reminder",
                        "minutes_waiting": round(minutes_waiting, 1),
                    },
                    "description": (
                        f"Send reminder to driver {driver_id} for "
                        f"unacknowledged job {job_id} "
                        f"({round(minutes_waiting, 1)} min waiting)"
                    ),
                }
            ],
            expected_kpi_delta={
                "driver_ack_rate": 0.1,
                "assignment_response_time": -5.0,
            },
            risk_class=RiskClass.LOW,
            confidence=0.9,
            priority=2,
            tenant_id=tenant_id,
        )

    def _create_escalation_proposal(
        self,
        job_id: str,
        tenant_id: str,
        driver_id: str,
        minutes_waiting: float,
    ) -> InterventionProposal:
        """Create an escalation InterventionProposal for a persistently unacknowledged assignment.

        Validates: Requirement 15.2
        """
        return InterventionProposal(
            source_agent=self.agent_id,
            actions=[
                {
                    "tool_name": "escalate_unresponsive_driver",
                    "parameters": {
                        "job_id": job_id,
                        "driver_id": driver_id,
                        "notification_type": "dispatcher_escalation",
                        "message_template": "driver_escalation_alert",
                        "minutes_waiting": round(minutes_waiting, 1),
                    },
                    "description": (
                        f"Escalate unresponsive driver {driver_id} for "
                        f"job {job_id} to dispatcher "
                        f"({round(minutes_waiting, 1)} min without ack)"
                    ),
                }
            ],
            expected_kpi_delta={
                "unresponsive_driver_resolution_time": -10.0,
                "dispatcher_escalation_count": 1,
            },
            risk_class=RiskClass.MEDIUM,
            confidence=0.95,
            priority=5,
            tenant_id=tenant_id,
        )

    # ------------------------------------------------------------------
    # Per-job cooldown management
    # ------------------------------------------------------------------

    def _is_on_nudge_cooldown(self, job_id: str) -> bool:
        """Check if a nudge was recently sent for this job.

        Validates: Requirement 15.4
        """
        last = self._nudge_cooldowns.get(job_id)
        if last is None:
            return False
        return (datetime.now(timezone.utc) - last) < timedelta(
            minutes=self._nudge_timeout_minutes
        )

    def _set_nudge_cooldown(self, job_id: str) -> None:
        """Record that a nudge was sent for this job."""
        self._nudge_cooldowns[job_id] = datetime.now(timezone.utc)

    def _is_on_escalation_cooldown(self, job_id: str) -> bool:
        """Check if an escalation was recently sent for this job.

        Validates: Requirement 15.4
        """
        last = self._escalation_cooldowns.get(job_id)
        if last is None:
            return False
        return (datetime.now(timezone.utc) - last) < timedelta(
            minutes=self._escalation_timeout_minutes
        )

    def _set_escalation_cooldown(self, job_id: str) -> None:
        """Record that an escalation was sent for this job."""
        self._escalation_cooldowns[job_id] = datetime.now(timezone.utc)
