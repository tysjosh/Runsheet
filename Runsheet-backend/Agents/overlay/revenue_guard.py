"""
Revenue Guard — Layer 1 overlay agent for margin protection.

Subscribes to fuel RiskSignals and OutcomeRecords, computes per-job
and per-route margin metrics, detects margin leakage patterns, and
produces PolicyChangeProposals (always HIGH risk).

Weekly summary reports are persisted to agent_revenue_reports index.

Validates: Requirements 6.1–6.8
"""
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    OutcomeRecord,
    PolicyChangeProposal,
    RiskClass,
    RiskSignal,
)
from Agents.overlay.signal_bus import SignalBus

logger = logging.getLogger(__name__)

REVENUE_REPORTS_INDEX = "agent_revenue_reports"
JOBS_CURRENT_INDEX = "jobs_current"

# Default margin target (percentage)
DEFAULT_MARGIN_TARGET_PCT = 15.0
# Consecutive below-target jobs to trigger leakage detection
DEFAULT_LEAKAGE_THRESHOLD = 3


class RevenueGuard(OverlayAgentBase):
    """Margin protection and leakage detection agent.

    Monitors fuel costs and intervention outcomes to compute per-job
    and per-route margin metrics. Detects patterns of margin leakage
    and produces PolicyChangeProposals for corrective action.

    All proposals are HIGH risk and require human approval (Req 6.5, 6.7).

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        margin_target_pct: Target margin percentage (default 15.0).
        leakage_threshold: Consecutive below-target jobs to trigger (default 3).
        poll_interval: Decision cycle interval in seconds (default 120).
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
        margin_target_pct: float = DEFAULT_MARGIN_TARGET_PCT,
        leakage_threshold: int = DEFAULT_LEAKAGE_THRESHOLD,
        poll_interval: int = 120,
    ):
        super().__init__(
            agent_id="revenue_guard",
            signal_bus=signal_bus,
            subscriptions=[
                {
                    "message_type": RiskSignal,
                    "filters": {"source_agent": "fuel_management_agent"},
                },
                {"message_type": OutcomeRecord},
            ],
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            autonomy_config_service=autonomy_config_service,
            feature_flag_service=feature_flag_service,
            es_service=es_service,
            poll_interval=poll_interval,
            cooldown_minutes=60,
        )
        self._margin_target = margin_target_pct
        self._leakage_threshold = leakage_threshold
        # Track per-route margin history: route_id -> list of margin values
        self._route_margins: Dict[str, List[float]] = defaultdict(list)
        self._last_weekly_report: Optional[datetime] = None

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Evaluate margin metrics and detect leakage patterns.

        Steps:
        1. Extract fuel cost signals and outcome records.
        2. Query job/route data to compute margin metrics (Req 6.2).
        3. Detect leakage patterns (3+ consecutive below-target) (Req 6.3).
        4. Generate PolicyChangeProposals for detected patterns (Req 6.3, 6.4).
        5. Check if weekly report is due (Req 6.6).

        Returns:
            List of PolicyChangeProposals (cast as InterventionProposal
            for base class compatibility).
        """
        if not signals:
            return []

        tenant_id = signals[0].tenant_id
        proposals = []

        # Compute margin metrics from recent jobs
        route_margins = await self._compute_route_margins(tenant_id)

        # Detect leakage patterns (Req 6.3)
        for route_id, margins in route_margins.items():
            self._route_margins[route_id].extend(margins)
            # Keep only last 20 entries per route
            self._route_margins[route_id] = self._route_margins[route_id][-20:]

            leakage = self._detect_leakage(self._route_margins[route_id])
            if leakage:
                # Cooldown check per route
                if self._is_on_cooldown(f"route:{route_id}"):
                    continue

                avg_margin = sum(margins[-self._leakage_threshold:]) / self._leakage_threshold
                weekly_impact = (self._margin_target - avg_margin) * len(margins)

                proposal = PolicyChangeProposal(
                    source_agent=self.agent_id,
                    parameter=f"route.{route_id}.pricing_adjustment",
                    old_value={"margin_target_pct": self._margin_target},
                    new_value={
                        "margin_target_pct": self._margin_target,
                        "fuel_surcharge_pct": max(2.0, self._margin_target - avg_margin),
                        "route_optimization": True,
                    },
                    evidence=[s.signal_id for s in signals[:5] if hasattr(s, "signal_id")],
                    rollback_plan={
                        "action": "revert_pricing",
                        "route_id": route_id,
                        "revert_to": {"margin_target_pct": self._margin_target},
                    },
                    confidence=0.7,
                    tenant_id=tenant_id,
                )
                proposals.append(proposal)
                self._set_cooldown(f"route:{route_id}")

        # Weekly report (Req 6.6)
        await self._maybe_generate_weekly_report(tenant_id)

        # Return as list — base class handles routing
        return proposals

    # ------------------------------------------------------------------
    # Margin computation
    # ------------------------------------------------------------------

    async def _compute_route_margins(
        self, tenant_id: str
    ) -> Dict[str, List[float]]:
        """Query recent jobs and compute per-route margin percentages."""
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"status": ["completed", "delivered"]}},
                        {"range": {"completed_at": {"gte": "now-7d"}}},
                    ]
                }
            },
            "size": 200,
            "sort": [{"completed_at": {"order": "desc"}}],
        }
        resp = await self._es.search_documents(JOBS_CURRENT_INDEX, query, 200)
        jobs = [h["_source"] for h in resp["hits"]["hits"]]

        route_margins: Dict[str, List[float]] = defaultdict(list)
        for job in jobs:
            route_id = job.get("route_id", "unknown")
            revenue = job.get("revenue", 0)
            fuel_cost = job.get("fuel_cost", 0)
            sla_penalty = job.get("sla_penalty", 0)
            if revenue > 0:
                margin = ((revenue - fuel_cost - sla_penalty) / revenue) * 100
                route_margins[route_id].append(margin)

        return dict(route_margins)

    def _detect_leakage(self, margins: List[float]) -> bool:
        """Detect if the last N margins are all below target (Req 6.3)."""
        if len(margins) < self._leakage_threshold:
            return False
        recent = margins[-self._leakage_threshold:]
        return all(m < self._margin_target for m in recent)

    # ------------------------------------------------------------------
    # Weekly report
    # ------------------------------------------------------------------

    async def _maybe_generate_weekly_report(self, tenant_id: str) -> None:
        """Generate weekly summary report if due (Req 6.6)."""
        now = datetime.now(timezone.utc)
        if self._last_weekly_report and (now - self._last_weekly_report) < timedelta(days=7):
            return

        report = {
            "report_type": "weekly_revenue_summary",
            "tenant_id": tenant_id,
            "period_start": (now - timedelta(days=7)).isoformat(),
            "period_end": now.isoformat(),
            "total_routes_analyzed": len(self._route_margins),
            "leakage_patterns_detected": sum(
                1 for margins in self._route_margins.values()
                if self._detect_leakage(margins)
            ),
            "generated_at": now.isoformat(),
        }

        try:
            report_id = f"rev-report-{tenant_id}-{now.strftime('%Y%W')}"
            await self._es.index_document(REVENUE_REPORTS_INDEX, report_id, report)
            self._last_weekly_report = now
        except Exception as e:
            logger.error("Failed to persist weekly revenue report: %s", e)
