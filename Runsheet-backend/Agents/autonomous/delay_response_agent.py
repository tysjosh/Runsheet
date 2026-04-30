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

        # Query for in-progress jobs past estimated_arrival
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"status": "in_progress"}},
                        {"range": {"estimated_arrival": {"lt": now}}},
                    ]
                }
            },
            "size": 50,
        }

        resp = await self._es.search_documents(JOBS_CURRENT_INDEX, query, 50)
        delayed_jobs = [h["_source"] for h in resp["hits"]["hits"]]

        for job in delayed_jobs:
            job_id = job.get("job_id")
            tenant_id = job.get("tenant_id", "default")

            # Respect tenant feature flags (Req 3.8)
            if self._feature_flags:
                enabled = await self._feature_flags.is_enabled(tenant_id)
                if not enabled:
                    continue

            detections.append(job_id)

            # Respect cooldown (Req 3.6)
            if self._is_on_cooldown(job_id):
                continue

            # Find a compatible available asset (Req 3.3)
            job_type = job.get("job_type")
            asset_type = self._job_type_to_asset_type(job_type)
            available = await self._find_available_asset(asset_type, tenant_id)

            if available:
                # Propose reassignment via Confirmation Protocol (Req 3.4)
                request = MutationRequest(
                    tool_name="assign_asset_to_job",
                    parameters={
                        "job_id": job_id,
                        "asset_id": available["asset_id"],
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
        resp = await self._es.search_documents(ASSETS_INDEX, query, 1)
        hits = [h["_source"] for h in resp["hits"]["hits"]]
        return hits[0] if hits else None
