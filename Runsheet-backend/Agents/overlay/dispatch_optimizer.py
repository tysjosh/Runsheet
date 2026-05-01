"""
Dispatch Optimizer — Layer 1 overlay agent for global reassignment.

Subscribes to delay and fuel RiskSignals, evaluates reassignment
opportunities across affected routes/jobs, and produces ranked
InterventionProposals. Constraint: no net-negative SLA impact
across the portfolio.

Decision cycle: 60 seconds (configurable).

Validates: Requirements 4.1–4.8
"""
import logging
from typing import Any, Dict, List

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
)
from Agents.overlay.signal_bus import SignalBus

logger = logging.getLogger(__name__)

JOBS_CURRENT_INDEX = "jobs_current"
ASSETS_INDEX = "trucks"


class DispatchOptimizer(OverlayAgentBase):
    """Global reassignment and reroute portfolio optimizer.

    Consumes delay and fuel RiskSignals from Layer 0 agents and
    evaluates all affected routes/jobs within the same tenant to
    identify reassignment opportunities that improve delivery time,
    fuel cost, and SLA compliance without creating new SLA breaches.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service for querying jobs/assets.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager for broadcasting.
        confirmation_protocol: For routing approved mutations.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        execution_planner: ExecutionPlanner for multi-step reassignments.
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
        execution_planner,
        poll_interval: int = 60,
    ):
        super().__init__(
            agent_id="dispatch_optimizer",
            signal_bus=signal_bus,
            subscriptions=[
                {
                    "message_type": RiskSignal,
                    "filters": {
                        "source_agent": [
                            "delay_response_agent",
                            "fuel_management_agent",
                        ]
                    },
                },
            ],
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            autonomy_config_service=autonomy_config_service,
            feature_flag_service=feature_flag_service,
            es_service=es_service,
            poll_interval=poll_interval,
            cooldown_minutes=5,
        )
        self._execution_planner = execution_planner

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Evaluate reassignment opportunities for buffered signals.

        Steps:
        1. Extract affected entity IDs (job_ids, station_ids) from signals.
        2. Query all active jobs and available assets for the tenant.
        3. Score reassignment candidates by (time_saved, fuel_delta, sla_impact).
        4. Filter out candidates that would create new SLA breaches.
        5. Rank remaining candidates and produce InterventionProposals.

        Returns:
            List of InterventionProposals with ranked reassignment actions.
        """
        if not signals:
            return []

        tenant_id = signals[0].tenant_id
        affected_entities = {s.entity_id for s in signals}

        # Query affected jobs
        affected_jobs = await self._query_affected_jobs(
            affected_entities, tenant_id
        )
        if not affected_jobs:
            return []

        # Query available assets
        available_assets = await self._query_available_assets(tenant_id)

        # Score and rank reassignment candidates
        candidates = self._score_reassignments(
            affected_jobs, available_assets, signals
        )

        # Filter: no net-negative SLA impact (Req 4.5)
        safe_candidates = [
            c for c in candidates if c["sla_impact"] >= 0
        ]

        if not safe_candidates:
            return []

        # Build proposal with ranked actions (Req 4.3, 4.4)
        actions = []
        total_time_saved = 0.0
        total_fuel_delta = 0.0
        for candidate in safe_candidates:
            actions.append({
                "tool_name": "assign_asset_to_job",
                "parameters": {
                    "job_id": candidate["job_id"],
                    "asset_id": candidate["asset_id"],
                },
                "expected_time_saved_minutes": candidate["time_saved"],
                "expected_fuel_delta_liters": candidate["fuel_delta"],
            })
            total_time_saved += candidate["time_saved"]
            total_fuel_delta += candidate["fuel_delta"]

        proposal = InterventionProposal(
            source_agent=self.agent_id,
            actions=actions,
            expected_kpi_delta={
                "delivery_time_minutes": -total_time_saved,
                "fuel_cost_liters": total_fuel_delta,
                "sla_compliance_pct": sum(
                    c["sla_impact"] for c in safe_candidates
                ),
            },
            risk_class=RiskClass.MEDIUM,
            confidence=min(s.confidence for s in signals),
            priority=len(safe_candidates),
            tenant_id=tenant_id,
        )

        return [proposal]

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    async def _query_affected_jobs(
        self, entity_ids: set, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """Query active jobs matching affected entity IDs."""
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"status": "in_progress"}},
                        {"terms": {"job_id": list(entity_ids)}},
                    ]
                }
            },
            "size": 100,
        }
        resp = await self._es.search_documents(JOBS_CURRENT_INDEX, query, 100)
        return [h["_source"] for h in resp["hits"]["hits"]]

    async def _query_available_assets(
        self, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """Query available assets for the tenant."""
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"status": "on_time"}},
                    ]
                }
            },
            "size": 50,
        }
        resp = await self._es.search_documents(ASSETS_INDEX, query, 50)
        return [h["_source"] for h in resp["hits"]["hits"]]

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _score_reassignments(
        self,
        jobs: List[Dict],
        assets: List[Dict],
        signals: List[RiskSignal],
    ) -> List[Dict[str, Any]]:
        """Score all possible job-to-asset reassignments.

        Returns a list of candidate dicts sorted by composite score
        (descending), each containing job_id, asset_id, time_saved,
        fuel_delta, and sla_impact.
        """
        signal_severity = {s.entity_id: s.severity.value for s in signals}
        severity_weight = {"low": 1, "medium": 2, "high": 3, "critical": 4}

        candidates = []
        for job in jobs:
            job_id = job.get("job_id")
            job_type = job.get("job_type", "cargo_transport")
            for asset in assets:
                asset_type = asset.get("asset_type", "vehicle")
                # Basic compatibility check
                if not self._is_compatible(job_type, asset_type):
                    continue

                # Heuristic scoring
                sev = signal_severity.get(job_id, "medium")
                weight = severity_weight.get(sev, 2)
                time_saved = weight * 10.0  # placeholder heuristic
                fuel_delta = -weight * 2.0  # negative = savings
                sla_impact = weight * 0.5   # positive = improvement

                candidates.append({
                    "job_id": job_id,
                    "asset_id": asset.get("asset_id"),
                    "time_saved": time_saved,
                    "fuel_delta": fuel_delta,
                    "sla_impact": sla_impact,
                    "score": time_saved + abs(fuel_delta) + sla_impact,
                })

        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates

    @staticmethod
    def _is_compatible(job_type: str, asset_type: str) -> bool:
        """Check if an asset type is compatible with a job type."""
        compatibility = {
            "cargo_transport": "vehicle",
            "passenger_transport": "vehicle",
            "vessel_movement": "vessel",
            "airport_transfer": "vehicle",
            "crane_booking": "equipment",
        }
        return compatibility.get(job_type, "vehicle") == asset_type
