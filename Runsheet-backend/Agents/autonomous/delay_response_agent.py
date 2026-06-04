"""
Delay Response Agent — autonomous background agent for delayed job detection.

Monitors the ``jobs_current`` Elasticsearch index for in-progress jobs that
have exceeded their ``estimated_arrival`` time. For each delayed job the agent
either proposes a reassignment to a compatible available asset (via the
Confirmation Protocol) or escalates via WebSocket when no alternative asset
is available.

Default configuration:
    - poll_interval: 60 seconds
    - cooldown: 15 minutes (per job)

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from Agents.autonomous.base_agent import AutonomousAgentBase
from Agents.confirmation_protocol import MutationRequest
from Agents.overlay.data_contracts import RiskSignal, Severity

logger = logging.getLogger(__name__)

# Elasticsearch index names
JOBS_CURRENT_INDEX = "jobs_current"
ASSETS_INDEX = "trucks"

# Mapping from job type to the asset type required to service it
JOB_TYPE_TO_ASSET_TYPE: Dict[str, str] = {
    "cargo_transport": "vehicle",
    "passenger_transport": "vehicle",
    "vessel_movement": "vessel",
    "airport_transfer": "vehicle",
    "crane_booking": "equipment",
}


class DelayResponseAgent(AutonomousAgentBase):
    """Monitors for delayed jobs and proposes corrective actions.

    Polls ``jobs_current`` for jobs where ``status == "in_progress"`` and
    the current UTC time exceeds ``estimated_arrival``. For each detected
    delay the agent:

    1. Checks tenant feature flags — skips disabled tenants.
    2. Checks per-job cooldown — skips recently processed jobs.
    3. Searches for a compatible available asset.
    4. If found → proposes reassignment via the Confirmation Protocol.
    5. If not found → broadcasts a ``delay_alert`` via WebSocket.

    Args:
        es_service: Elasticsearch service for querying indices.
        activity_log_service: Service for logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: Protocol for routing mutation requests.
        feature_flag_service: Optional service for tenant feature flags.
        signal_bus: Optional SignalBus instance for publishing RiskSignals
            to Layer 1 overlay agents.
        poll_interval: Seconds between polling cycles (default 60).
        cooldown_minutes: Minutes to suppress duplicate actions per job
            (default 15).
    """

    def __init__(
        self,
        es_service,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        feature_flag_service=None,
        signal_bus=None,
        poll_interval: int = 60,
        cooldown_minutes: int = 15,
    ):
        super().__init__(
            agent_id="delay_response_agent",
            poll_interval_seconds=poll_interval,
            cooldown_minutes=cooldown_minutes,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            feature_flag_service=feature_flag_service,
        )
        self._es = es_service
        self._signal_bus = signal_bus

    # ------------------------------------------------------------------
    # Core monitoring cycle
    # ------------------------------------------------------------------

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one monitoring cycle.

        Queries Elasticsearch for in-progress jobs past their estimated
        arrival, then attempts corrective action for each.

        Returns:
            A ``(detections, actions)`` tuple where *detections* is a
            list of delayed job IDs and *actions* is a list of dicts
            describing the action taken for each job.
        """
        detections: List[str] = []
        actions: List[Dict[str, Any]] = []

        now = datetime.now(timezone.utc).isoformat()

        # Query for in-progress jobs past estimated_arrival, scoped per
        # tenant. Only process documents that carry a tenant_id — legacy
        # docs without one are skipped rather than falling back to a
        # hard-coded "default" tenant which would bypass isolation.
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"status": "in_progress"}},
                        {"range": {"estimated_arrival": {"lt": now}}},
                        {"exists": {"field": "tenant_id"}},
                    ]
                }
            },
            "size": 50,
        }

        # Read-cutover: serve the cross-tenant sweep from Postgres when enabled
        # (each job is dispatched per its own tenant_id below). The PG ``job``
        # rows always carry a tenant_id (NOT NULL), so the ES ``exists:
        # tenant_id`` filter is implicitly satisfied.
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_search_all_tenants,
        )

        pg = await read_hybrid_search_all_tenants(
            "job",
            term_filters={"status": "in_progress"},
            range_field="estimated_arrival", range_lt=now,
            exists_fields=["tenant_id"],
            sort_field="estimated_arrival", sort_order="asc", size=50,
        )
        if pg is not _NOT_CUT_OVER:
            delayed_jobs = pg
        else:
            resp = await self._es.search_documents(JOBS_CURRENT_INDEX, query, 50)
            delayed_jobs = [h["_source"] for h in resp["hits"]["hits"]]

        for job in delayed_jobs:
            job_id = job.get("job_id")
            tenant_id = job.get("tenant_id")

            # Skip documents that somehow lack a tenant_id (defensive)
            if not tenant_id:
                continue

            # Respect tenant feature flags (Req 3.8)
            if self._feature_flags:
                enabled = await self._feature_flags.is_enabled(tenant_id)
                if not enabled:
                    continue

            detections.append(job_id)

            # Respect cooldown (Req 3.6)
            if self._is_on_cooldown(job_id):
                continue

            # Publish RiskSignal BEFORE MutationRequest (Req 5.6)
            if self._signal_bus:
                try:
                    estimated_arrival_str = job.get("estimated_arrival")
                    if estimated_arrival_str:
                        est_arrival = datetime.fromisoformat(
                            estimated_arrival_str.replace("Z", "+00:00")
                        )
                        delay_minutes = (
                            datetime.now(timezone.utc) - est_arrival
                        ).total_seconds() / 60.0
                    else:
                        delay_minutes = 0.0

                    signal = RiskSignal(
                        source_agent=self.agent_id,
                        entity_id=job_id,
                        entity_type="job",
                        severity=self._derive_delay_severity(delay_minutes),
                        confidence=0.9,
                        ttl_seconds=1800,
                        tenant_id=tenant_id,
                        context={
                            "job_type": job.get("job_type"),
                            "priority": job.get("priority"),
                            "asset_assigned": job.get("asset_assigned"),
                            "estimated_arrival": job.get("estimated_arrival"),
                            "detected_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                        },
                    )
                    await self._signal_bus.publish(signal)
                except Exception:
                    self.logger.exception("Failed to publish RiskSignal")

            # Find a compatible available asset (Req 3.3)
            job_type = job.get("job_type")
            asset_type = self._job_type_to_asset_type(job_type)
            available = await self._find_available_asset(asset_type, tenant_id)

            if available:
                # Propose reassignment via Confirmation Protocol (Req 3.4).
                #
                # Older fleet documents seeded before the ``asset_id``
                # alias was added only carry ``truck_id``. Both fields
                # identify the same row (see ``data_endpoints.py`` where
                # POST /api/fleet/assets writes both), so treat either
                # as the canonical asset identifier and skip the row
                # when neither is present rather than raising KeyError
                # out of the monitor cycle.
                asset_id = available.get("asset_id") or available.get("truck_id")
                if not asset_id:
                    self.logger.warning(
                        "Skipping reassignment for job %s: available "
                        "asset has neither asset_id nor truck_id "
                        "(fields=%s)",
                        job_id, list(available.keys()),
                    )
                    await self._ws.broadcast_event("delay_alert", {
                        "job_id": job_id,
                        "reason": "no_alternative_available",
                        "job_details": job,
                    })
                    actions.append({
                        "job_id": job_id,
                        "action": "escalation",
                    })
                    self._set_cooldown(job_id)
                    continue

                request = MutationRequest(
                    tool_name="assign_asset_to_job",
                    parameters={
                        "job_id": job_id,
                        "asset_id": asset_id,
                    },
                    tenant_id=tenant_id,
                    agent_id=self.agent_id,
                )
                result = await self._confirmation_protocol.process_mutation(request)
                actions.append({
                    "job_id": job_id,
                    "action": "reassignment",
                    "result": result,
                })
            else:
                # Escalate — no alternative available (Req 3.5)
                self.logger.warning(
                    f"No compatible asset for delayed job {job_id}"
                )
                await self._ws.broadcast_event("delay_alert", {
                    "job_id": job_id,
                    "reason": "no_alternative_available",
                    "job_details": job,
                })
                actions.append({
                    "job_id": job_id,
                    "action": "escalation",
                })

            # Set cooldown regardless of action taken (Req 3.6)
            self._set_cooldown(job_id)

        return detections, actions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_delay_severity(delay_minutes: float) -> Severity:
        """Derive severity from the delay magnitude in minutes.

        Args:
            delay_minutes: The delay duration in minutes.

        Returns:
            A ``Severity`` enum value based on the delay magnitude:
            critical > 60, high > 30, medium > 15, low ≤ 15.
        """
        if delay_minutes > 60:
            return Severity.CRITICAL
        elif delay_minutes > 30:
            return Severity.HIGH
        elif delay_minutes > 15:
            return Severity.MEDIUM
        return Severity.LOW

    @staticmethod
    def _job_type_to_asset_type(job_type: Optional[str]) -> str:
        """Map a job type to the compatible asset type.

        Args:
            job_type: The job type string (e.g. ``"cargo_transport"``).

        Returns:
            The corresponding asset type, defaulting to ``"vehicle"``.
        """
        return JOB_TYPE_TO_ASSET_TYPE.get(job_type, "vehicle")

    async def _find_available_asset(
        self, asset_type: str, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """Find an available asset of the given type for a tenant.

        Queries the assets index for assets matching the type and tenant
        with status ``"on_time"`` (indicating availability).

        Args:
            asset_type: The required asset type.
            tenant_id: The tenant scope.

        Returns:
            The first matching asset document, or ``None`` if none found.
        """
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"asset_type": asset_type}},
                        {"term": {"status": "on_time"}},
                    ]
                }
            },
            "size": 1,
        }
        # Read-cutover: serve from Postgres when enabled. ``truck`` is
        # tenant-optional so search() does not tenant-filter — pass the
        # tenant_id as a document term filter (matching the ES term) so the
        # result is tenant-scoped exactly like ES.
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_search,
        )

        pg = await read_hybrid_search(
            "truck", tenant_id,
            term_filters={
                "tenant_id": tenant_id,
                "asset_type": asset_type,
                "status": "on_time",
            },
            page=1, size=1,
        )
        if pg is not _NOT_CUT_OVER:
            items = pg.get("items", [])
            return items[0] if items else None

        resp = await self._es.search_documents(ASSETS_INDEX, query, 1)
        hits = [h["_source"] for h in resp["hits"]["hits"]]
        return hits[0] if hits else None
