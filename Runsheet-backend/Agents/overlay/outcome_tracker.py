"""
Outcome Tracker — links proposals to execution results.

Captures before-KPIs at proposal time, after-KPIs after observation
window (default 1 hour), computes realized_delta, flags adverse
outcomes (>10% worse), and handles inconclusive cases.

Validates: Requirements 11.1–11.8
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from Agents.overlay.data_contracts import OutcomeRecord
from Agents.overlay.signal_bus import SignalBus

logger = logging.getLogger(__name__)

OUTCOMES_INDEX = "agent_outcomes"

# Default observation window in seconds (1 hour)
DEFAULT_OBSERVATION_WINDOW_SECONDS = 3600
# Adverse outcome threshold (10% worse)
DEFAULT_ADVERSE_THRESHOLD_PCT = 10.0


class PendingOutcome:
    """Tracks a pending outcome awaiting after-KPI measurement."""

    def __init__(
        self,
        intervention_id: str,
        before_kpis: Dict[str, float],
        tenant_id: str,
        entity_ids: List[str],
        created_at: datetime,
        observation_window_seconds: int = DEFAULT_OBSERVATION_WINDOW_SECONDS,
    ):
        self.intervention_id = intervention_id
        self.before_kpis = before_kpis
        self.tenant_id = tenant_id
        self.entity_ids = entity_ids
        self.created_at = created_at
        self.measure_at = created_at + timedelta(
            seconds=observation_window_seconds
        )
        self.notification_ids: Optional[List[str]] = None
        self.confidence_score: Optional[float] = None
        self.confidence_rationale: Optional[List[str]] = None


class OutcomeTracker:
    """Links InterventionProposals to execution results.

    Lifecycle:
    1. ``record_proposal_execution`` — called when a proposal is approved
       and executed. Captures before-KPIs and schedules measurement.
    2. ``check_pending_outcomes`` — called periodically to measure
       after-KPIs for proposals past their observation window.
    3. Publishes OutcomeRecords to the SignalBus for LearningPolicyAgent.

    Args:
        signal_bus: SignalBus for publishing OutcomeRecords.
        es_service: Elasticsearch service for KPI queries and persistence.
        adverse_threshold_pct: Threshold for flagging adverse outcomes (default 10.0).
        observation_window_seconds: Seconds to wait before measuring after-KPIs (default 3600).
    """

    def __init__(
        self,
        signal_bus: SignalBus,
        es_service: Any,
        adverse_threshold_pct: float = DEFAULT_ADVERSE_THRESHOLD_PCT,
        observation_window_seconds: int = DEFAULT_OBSERVATION_WINDOW_SECONDS,
    ):
        self._signal_bus = signal_bus
        self._es = es_service
        self._adverse_threshold = adverse_threshold_pct
        self._observation_window = observation_window_seconds
        self._pending: Dict[str, PendingOutcome] = {}

    # ------------------------------------------------------------------
    # Record proposal execution
    # ------------------------------------------------------------------

    async def record_proposal_execution(
        self,
        intervention_id: str,
        before_kpis: Dict[str, float],
        tenant_id: str,
        entity_ids: List[str],
        notification_ids: Optional[List[str]] = None,
        confidence_score: Optional[float] = None,
        confidence_rationale: Optional[List[str]] = None,
    ) -> None:
        """Record that a proposal has been executed (Req 11.1, 11.2, 4.2, 4.4, 17.4).

        Captures before-KPIs and schedules after-KPI measurement.
        Optionally stores notification_ids generated during execution.
        Optionally stores confidence_score and confidence_rationale for
        retrospective accuracy analysis (Req 17.4).
        """
        pending = PendingOutcome(
            intervention_id=intervention_id,
            before_kpis=before_kpis,
            tenant_id=tenant_id,
            entity_ids=entity_ids,
            created_at=datetime.now(timezone.utc),
            observation_window_seconds=self._observation_window,
        )
        self._pending[intervention_id] = pending
        # Store notification_ids for inclusion in the OutcomeRecord (Req 4.2, 4.4)
        if notification_ids is not None:
            pending.notification_ids = notification_ids
        # Store confidence data for inclusion in the OutcomeRecord (Req 17.4)
        if confidence_score is not None:
            pending.confidence_score = confidence_score
        if confidence_rationale is not None:
            pending.confidence_rationale = confidence_rationale

    # ------------------------------------------------------------------
    # Check pending outcomes
    # ------------------------------------------------------------------

    async def check_pending_outcomes(self) -> List[OutcomeRecord]:
        """Check and measure outcomes for proposals past observation window.

        Returns:
            List of newly created OutcomeRecords.
        """
        now = datetime.now(timezone.utc)
        completed: List[OutcomeRecord] = []

        for iid, pending in list(self._pending.items()):
            if now < pending.measure_at:
                continue

            # Measure after-KPIs
            after_kpis = await self._measure_kpis(
                pending.entity_ids, pending.tenant_id
            )

            if after_kpis is None:
                # Entity deleted or tenant disabled (Req 11.8)
                outcome = OutcomeRecord(
                    intervention_id=iid,
                    before_kpis=pending.before_kpis,
                    after_kpis={},
                    realized_delta={},
                    execution_duration_ms=(
                        now - pending.created_at
                    ).total_seconds() * 1000,
                    tenant_id=pending.tenant_id,
                    status="inconclusive",
                    notification_ids=pending.notification_ids,
                    confidence_score=pending.confidence_score,
                    confidence_rationale=pending.confidence_rationale,
                )
            else:
                # Compute realized delta (Req 11.3)
                realized_delta = {
                    k: after_kpis.get(k, 0) - pending.before_kpis.get(k, 0)
                    for k in set(pending.before_kpis) | set(after_kpis)
                }

                # Determine status (Req 11.7)
                status = "measured"
                for kpi, before_val in pending.before_kpis.items():
                    delta = realized_delta.get(kpi, 0)
                    if before_val != 0:
                        pct_change = (delta / abs(before_val)) * 100
                        if pct_change < -self._adverse_threshold:
                            status = "adverse"
                            break

                outcome = OutcomeRecord(
                    intervention_id=iid,
                    before_kpis=pending.before_kpis,
                    after_kpis=after_kpis,
                    realized_delta=realized_delta,
                    execution_duration_ms=(
                        now - pending.created_at
                    ).total_seconds() * 1000,
                    tenant_id=pending.tenant_id,
                    status=status,
                    notification_ids=pending.notification_ids,
                    confidence_score=pending.confidence_score,
                    confidence_rationale=pending.confidence_rationale,
                )

            # Persist to ES (Req 11.4)
            await self._persist_outcome(outcome)

            # Publish to SignalBus (Req 11.5)
            await self._signal_bus.publish(outcome)

            completed.append(outcome)
            del self._pending[iid]

        return completed

    # ------------------------------------------------------------------
    # KPI measurement
    # ------------------------------------------------------------------

    async def _measure_kpis(
        self, entity_ids: List[str], tenant_id: str
    ) -> Optional[Dict[str, float]]:
        """Measure current KPIs for the given entities.

        Returns None if entities cannot be found (deleted/disabled).
        """
        try:
            query = {
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"tenant_id": tenant_id}},
                            {"terms": {"job_id": entity_ids}},
                        ]
                    }
                },
                "size": len(entity_ids),
            }
            resp = await self._es.search_documents(
                "jobs_current", query, len(entity_ids)
            )
            hits = [h["_source"] for h in resp["hits"]["hits"]]

            if not hits:
                return None

            # Aggregate KPIs across entities
            total_delivery_time = 0.0
            total_fuel_cost = 0.0
            total_sla_compliance = 0.0
            count = len(hits)

            for job in hits:
                total_delivery_time += job.get("actual_delivery_minutes", 0)
                total_fuel_cost += job.get("fuel_cost", 0)
                total_sla_compliance += 1 if job.get("sla_met", False) else 0

            return {
                "avg_delivery_time_minutes": (
                    total_delivery_time / count if count else 0
                ),
                "total_fuel_cost": total_fuel_cost,
                "sla_compliance_rate": (
                    total_sla_compliance / count if count else 0
                ),
            }
        except Exception as e:
            logger.error("Failed to measure KPIs: %s", e)
            return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist_outcome(self, outcome: OutcomeRecord) -> None:
        """Persist an OutcomeRecord to the agent_outcomes index (Req 11.4)."""
        try:
            doc = outcome.model_dump(mode="json")
            await self._es.index_document(
                OUTCOMES_INDEX, outcome.outcome_id, doc
            )
        except Exception as e:
            logger.error("Failed to persist outcome record: %s", e)
