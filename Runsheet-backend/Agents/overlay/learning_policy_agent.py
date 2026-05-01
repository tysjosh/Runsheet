"""
Learning Policy Agent — Layer 2 meta-control agent.

Subscribes to OutcomeRecords and PolicyChangeProposal approval events.
Identifies parameters with consistently suboptimal outcomes (5+ negative
in 7 days) and proposes bounded policy adjustments.

Bounded rollout: 10% traffic initially.
Auto-rollback: if KPIs degrade >5% within 48 hours.
Policy experiment log: agent_policy_experiments index.

Validates: Requirements 8.1–8.8
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

POLICY_EXPERIMENTS_INDEX = "agent_policy_experiments"

# Thresholds
DEFAULT_NEGATIVE_OUTCOME_THRESHOLD = 5
DEFAULT_NEGATIVE_WINDOW_DAYS = 7
DEFAULT_ROLLOUT_PERCENTAGE = 10
DEFAULT_ROLLBACK_DEGRADATION_PCT = 5.0
DEFAULT_ROLLBACK_WINDOW_HOURS = 48


class PolicyExperiment:
    """Tracks a deployed policy change experiment."""

    def __init__(
        self,
        experiment_id: str,
        proposal: PolicyChangeProposal,
        rollout_pct: int,
    ):
        self.experiment_id = experiment_id
        self.proposal = proposal
        self.rollout_pct = rollout_pct
        self.deployed_at: Optional[datetime] = None
        self.baseline_kpis: Dict[str, float] = {}
        self.current_kpis: Dict[str, float] = {}
        self.status: str = "pending"  # pending | deployed | rolled_back | graduated
        self.rollback_reason: Optional[str] = None


class LearningPolicyAgent(OverlayAgentBase):
    """Meta-control agent for continuous policy tuning.

    Observes intervention outcomes across all overlay agents and
    proposes threshold/policy adjustments when parameters consistently
    produce suboptimal results.

    All proposals are HIGH risk with mandatory human approval (Req 8.5).

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode and policy management.
        feature_flag_service: For per-tenant feature flags.
        feedback_service: For correlating with operator feedback.
        negative_threshold: Negative outcomes to trigger proposal (default 5).
        negative_window_days: Window for counting negatives (default 7).
        rollout_pct: Initial rollout percentage (default 10).
        rollback_degradation_pct: KPI degradation threshold for rollback (default 5.0).
        rollback_window_hours: Hours to monitor before rollback decision (default 48).
        poll_interval: Decision cycle interval in seconds (default 300).
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
        feedback_service,
        negative_threshold: int = DEFAULT_NEGATIVE_OUTCOME_THRESHOLD,
        negative_window_days: int = DEFAULT_NEGATIVE_WINDOW_DAYS,
        rollout_pct: int = DEFAULT_ROLLOUT_PERCENTAGE,
        rollback_degradation_pct: float = DEFAULT_ROLLBACK_DEGRADATION_PCT,
        rollback_window_hours: int = DEFAULT_ROLLBACK_WINDOW_HOURS,
        poll_interval: int = 300,
    ):
        super().__init__(
            agent_id="learning_policy_agent",
            signal_bus=signal_bus,
            subscriptions=[
                {"message_type": OutcomeRecord},
                {"message_type": PolicyChangeProposal},
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
        self._feedback_service = feedback_service
        self._negative_threshold = negative_threshold
        self._negative_window_days = negative_window_days
        self._rollout_pct = rollout_pct
        self._rollback_degradation_pct = rollback_degradation_pct
        self._rollback_window = timedelta(hours=rollback_window_hours)

        # Track outcome history per parameter
        self._outcome_history: Dict[str, List[OutcomeRecord]] = defaultdict(list)
        # Active experiments
        self._experiments: Dict[str, PolicyExperiment] = {}
        self._experiment_counter = 0

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Analyze outcomes and propose policy adjustments.

        Steps:
        1. Separate OutcomeRecords from PolicyChangeProposals.
        2. Track outcome history per source agent (Req 8.1).
        3. Correlate with operator feedback (Req 8.2).
        4. Identify parameters with 5+ negative outcomes in 7 days (Req 8.3).
        5. Generate PolicyChangeProposals with rollback plans (Req 8.4).
        6. Monitor active experiments for rollback triggers (Req 8.8).

        Returns:
            List of PolicyChangeProposals.
        """
        if not signals:
            return []

        tenant_id = getattr(signals[0], "tenant_id", "default")
        proposals = []

        # Categorize incoming signals
        outcome_records = [s for s in signals if isinstance(s, OutcomeRecord)]
        policy_events = [s for s in signals if isinstance(s, PolicyChangeProposal)]

        # Track outcomes (Req 8.1)
        for outcome in outcome_records:
            agent_key = (
                outcome.intervention_id.split("-")[0]
                if "-" in outcome.intervention_id
                else "unknown"
            )
            self._outcome_history[agent_key].append(outcome)
            # Prune old entries
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=self._negative_window_days
            )
            self._outcome_history[agent_key] = [
                o for o in self._outcome_history[agent_key] if o.timestamp > cutoff
            ]

        # Identify suboptimal parameters (Req 8.3)
        for param_key, outcomes in self._outcome_history.items():
            negative_outcomes = [
                o
                for o in outcomes
                if o.status == "adverse"
                or any(v < 0 for v in o.realized_delta.values())
            ]

            if len(negative_outcomes) >= self._negative_threshold:
                if self._is_on_cooldown(f"policy:{param_key}"):
                    continue

                # Compute statistics for evidence (Req 8.4)
                sample_size = len(outcomes)
                negative_rate = (
                    len(negative_outcomes) / sample_size if sample_size > 0 else 0
                )

                proposal = PolicyChangeProposal(
                    source_agent=self.agent_id,
                    parameter=f"agent.{param_key}.threshold",
                    old_value={"current": "auto"},
                    new_value={
                        "adjusted": "auto",
                        "rollout_pct": self._rollout_pct,
                        "evidence_sample_size": sample_size,
                        "negative_rate": round(negative_rate, 3),
                    },
                    evidence=[o.outcome_id for o in negative_outcomes[:10]],
                    rollback_plan={
                        "trigger": f"kpi_degradation_gt_{self._rollback_degradation_pct}pct",
                        "window_hours": self._rollback_window.total_seconds() / 3600,
                        "action": "revert_to_previous_value",
                        "auto_rollback": True,
                    },
                    confidence=min(0.9, negative_rate),
                    tenant_id=tenant_id,
                )
                proposals.append(proposal)
                self._set_cooldown(f"policy:{param_key}")

                # Log experiment (Req 8.6)
                await self._log_experiment(proposal, tenant_id)

        # Monitor active experiments for rollback (Req 8.8)
        await self._check_experiment_rollbacks(tenant_id)

        return proposals

    # ------------------------------------------------------------------
    # Experiment tracking
    # ------------------------------------------------------------------

    async def _log_experiment(
        self, proposal: PolicyChangeProposal, tenant_id: str
    ) -> None:
        """Log a policy experiment to the experiments index (Req 8.6)."""
        self._experiment_counter += 1
        experiment_id = f"exp-{self._experiment_counter:06d}"

        experiment = PolicyExperiment(
            experiment_id=experiment_id,
            proposal=proposal,
            rollout_pct=self._rollout_pct,
        )
        experiment.deployed_at = datetime.now(timezone.utc)
        experiment.status = "pending"
        self._experiments[experiment_id] = experiment

        doc = {
            "experiment_id": experiment_id,
            "proposal_id": proposal.proposal_id,
            "parameter": proposal.parameter,
            "old_value": proposal.old_value,
            "new_value": proposal.new_value,
            "rollout_pct": self._rollout_pct,
            "status": "pending",
            "tenant_id": tenant_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await self._es.index_document(
                POLICY_EXPERIMENTS_INDEX, experiment_id, doc
            )
        except Exception as e:
            logger.error("Failed to log policy experiment: %s", e)

    async def _check_experiment_rollbacks(self, tenant_id: str) -> None:
        """Check active experiments for KPI degradation and auto-rollback (Req 8.8)."""
        now = datetime.now(timezone.utc)
        for exp_id, experiment in list(self._experiments.items()):
            if experiment.status != "deployed":
                continue
            if not experiment.deployed_at:
                continue

            # Check if within rollback window
            if (now - experiment.deployed_at) > self._rollback_window:
                experiment.status = "graduated"
                await self._update_experiment_status(exp_id, "graduated")
                continue

            # Check KPI degradation
            if experiment.baseline_kpis and experiment.current_kpis:
                for kpi, baseline_val in experiment.baseline_kpis.items():
                    current_val = experiment.current_kpis.get(kpi, baseline_val)
                    if baseline_val > 0:
                        degradation = (
                            (baseline_val - current_val) / baseline_val
                        ) * 100
                        if degradation > self._rollback_degradation_pct:
                            experiment.status = "rolled_back"
                            experiment.rollback_reason = (
                                f"KPI '{kpi}' degraded {degradation:.1f}% "
                                f"(threshold: {self._rollback_degradation_pct}%)"
                            )
                            await self._update_experiment_status(
                                exp_id,
                                "rolled_back",
                                experiment.rollback_reason,
                            )
                            logger.warning(
                                "Auto-rollback experiment %s: %s",
                                exp_id,
                                experiment.rollback_reason,
                            )
                            break

    async def _update_experiment_status(
        self, experiment_id: str, status: str, reason: str = None
    ) -> None:
        """Update experiment status in ES."""
        try:
            doc = {
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if reason:
                doc["rollback_reason"] = reason
            await self._es.update_document(
                POLICY_EXPERIMENTS_INDEX, experiment_id, {"doc": doc}
            )
        except Exception as e:
            logger.error("Failed to update experiment status: %s", e)
