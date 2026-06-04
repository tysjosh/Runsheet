"""
Job SLA Monitor — autonomous background agent for proactive SLA breach detection.

Monitors the ``jobs_current`` Elasticsearch index for in-progress jobs whose
``estimated_arrival`` is approaching or has already passed the current UTC
time. For each at-risk job the agent publishes a RiskSignal to the SignalBus
and broadcasts a ``job_sla_warning`` WebSocket event.

Default configuration:
    - poll_interval: 90 seconds
    - cooldown: 15 minutes (per job)
    - sla_warning_threshold_minutes: 30 minutes

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from Agents.autonomous.base_agent import AutonomousAgentBase
from Agents.overlay.data_contracts import RiskSignal, Severity

logger = logging.getLogger(__name__)

# Elasticsearch index name
JOBS_CURRENT_INDEX = "jobs_current"


class JobSLAMonitor(AutonomousAgentBase):
    """Monitors jobs approaching their SLA deadline and emits warnings.

    Polls ``jobs_current`` for jobs where ``status == "in_progress"`` and
    ``estimated_arrival`` is within a configurable warning threshold of
    the current UTC time. For each at-risk job the agent:

    1. Checks tenant feature flags — skips disabled tenants.
    2. Checks per-job cooldown — skips recently warned jobs.
    3. Skips jobs without an ``estimated_arrival`` value.
    4. Computes ``time_remaining_minutes`` until the SLA deadline.
    5. Derives severity: ``critical`` if breach already occurred,
       ``high`` otherwise.
    6. Publishes a ``RiskSignal`` to the SignalBus.
    7. Broadcasts a ``job_sla_warning`` WebSocket event.
    8. Sets cooldown for the job.

    Args:
        es_service: Elasticsearch service for querying indices.
        activity_log_service: Service for logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: Protocol for routing mutation requests.
        feature_flag_service: Optional service for tenant feature flags.
        signal_bus: Optional SignalBus instance for publishing RiskSignals
            to Layer 1 overlay agents.
        poll_interval: Seconds between polling cycles (default 90).
        cooldown_minutes: Minutes to suppress duplicate warnings per job
            (default 15).
        sla_warning_threshold_minutes: Minutes before estimated_arrival to
            start warning (default 30).
    """

    def __init__(
        self,
        es_service,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        feature_flag_service=None,
        signal_bus=None,
        poll_interval: int = 90,
        cooldown_minutes: int = 15,
        sla_warning_threshold_minutes: int = 30,
    ):
        super().__init__(
            agent_id="job_sla_monitor",
            poll_interval_seconds=poll_interval,
            cooldown_minutes=cooldown_minutes,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            feature_flag_service=feature_flag_service,
        )
        self._es = es_service
        self._signal_bus = signal_bus
        self._sla_warning_threshold_minutes = sla_warning_threshold_minutes

    # ------------------------------------------------------------------
    # Core monitoring cycle
    # ------------------------------------------------------------------

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one monitoring cycle.

        Queries Elasticsearch for in-progress jobs whose estimated_arrival
        is within the warning threshold, then processes each at-risk job.

        Returns:
            A ``(detections, actions)`` tuple where *detections* is a
            list of at-risk job IDs and *actions* is a list of dicts
            describing the action taken for each job.
        """
        detections: List[str] = []
        actions: List[Dict[str, Any]] = []

        now = datetime.now(timezone.utc)
        threshold_time = now + timedelta(minutes=self._sla_warning_threshold_minutes)

        # Query for in-progress jobs with estimated_arrival within threshold (Req 4.2)
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"status": "in_progress"}},
                        {"range": {"estimated_arrival": {"lte": threshold_time.isoformat()}}},
                    ]
                }
            },
            "size": 200,
            "sort": [{"estimated_arrival": {"order": "asc"}}],
        }

        # Read-cutover: serve the cross-tenant sweep from Postgres when enabled
        # (the agent dispatches per-tenant below). Same status+range+sort as ES.
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_search_all_tenants,
        )

        try:
            pg = await read_hybrid_search_all_tenants(
                "job",
                term_filters={"status": "in_progress"},
                range_field="estimated_arrival",
                range_lte=threshold_time.isoformat(),
                sort_field="estimated_arrival", sort_order="asc", size=200,
            )
        except Exception:
            self.logger.exception("Failed to query jobs from Postgres")
            return detections, actions

        if pg is not _NOT_CUT_OVER:
            at_risk_jobs = pg
        else:
            try:
                resp = await self._es.search_documents(JOBS_CURRENT_INDEX, query, 200)
            except Exception:
                # Log error and continue without crashing (follows Req 1.8 pattern)
                self.logger.exception("Failed to query jobs_current index")
                return detections, actions

            at_risk_jobs = [h["_source"] for h in resp.get("hits", {}).get("hits", [])]

        for job in at_risk_jobs:
            job_id = job.get("job_id")
            tenant_id = job.get("tenant_id")
            if not job_id or not tenant_id:
                self.logger.warning(
                    "JobSLAMonitor: skipping at-risk job missing job_id "
                    "or tenant_id: job_id=%s tenant_id=%s",
                    job_id,
                    tenant_id,
                )
                continue

            # Skip if estimated_arrival is None (Req 4.8)
            estimated_arrival_str = job.get("estimated_arrival")
            if estimated_arrival_str is None:
                continue

            # Respect tenant feature flags (Req 4.7)
            if self._feature_flags:
                enabled = await self._feature_flags.is_enabled(tenant_id)
                if not enabled:
                    continue

            detections.append(job_id)

            # Respect per-job cooldown (Req 4.6)
            if self._is_on_cooldown(job_id):
                continue

            # Parse estimated_arrival and compute time remaining
            try:
                estimated_arrival = datetime.fromisoformat(
                    estimated_arrival_str.replace("Z", "+00:00")
                )
            except (ValueError, TypeError) as e:
                self.logger.warning(
                    f"Failed to parse estimated_arrival for job {job_id}: {e}"
                )
                continue

            current_time = datetime.now(timezone.utc)
            time_remaining_minutes = (
                estimated_arrival - current_time
            ).total_seconds() / 60.0

            # Derive severity (Req 4.9)
            severity = self._derive_severity(time_remaining_minutes)

            # Publish RiskSignal to SignalBus (Req 4.3, 4.5)
            if self._signal_bus:
                try:
                    signal = RiskSignal(
                        source_agent=self.agent_id,
                        entity_id=job_id,
                        entity_type="job",
                        severity=severity,
                        confidence=0.85,
                        ttl_seconds=1800,
                        tenant_id=tenant_id,
                        context={
                            "job_type": job.get("job_type"),
                            "priority": job.get("priority"),
                            "asset_assigned": job.get("asset_assigned"),
                            "estimated_arrival": estimated_arrival_str,
                            "time_remaining_minutes": round(time_remaining_minutes, 2),
                        },
                    )
                    await self._signal_bus.publish(signal)
                except Exception:
                    self.logger.exception("Failed to publish RiskSignal")

            # Broadcast WebSocket event (Req 4.4)
            await self._ws.broadcast_event("job_sla_warning", {
                "job_id": job_id,
                "estimated_arrival": estimated_arrival_str,
                "time_remaining_minutes": round(time_remaining_minutes, 2),
                "asset_assigned": job.get("asset_assigned"),
                "tenant_id": tenant_id,
            })

            actions.append({
                "job_id": job_id,
                "action": "sla_warning",
                "severity": severity.value,
                "time_remaining_minutes": round(time_remaining_minutes, 2),
            })

            # Set cooldown (Req 4.6)
            self._set_cooldown(job_id)

        return detections, actions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_severity(time_remaining_minutes: float) -> Severity:
        """Derive severity from the time remaining until SLA breach.

        Args:
            time_remaining_minutes: Minutes remaining until the
                estimated arrival time. Negative values indicate
                a breach has already occurred.

        Returns:
            A ``Severity`` enum value: ``CRITICAL`` when breach has
            occurred (time_remaining ≤ 0), ``HIGH`` when approaching
            breach (within warning threshold).
        """
        if time_remaining_minutes <= 0:
            return Severity.CRITICAL
        return Severity.HIGH
