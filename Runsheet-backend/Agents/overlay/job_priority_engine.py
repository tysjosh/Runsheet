"""
Job Priority Engine — overlay agent for general job prioritization.

Subscribes to RiskSignal messages from job_sla_monitor and
delay_response_agent via the SignalBus, computes weighted priority
scores using configurable weights (sla_urgency, cargo_priority,
customer_tier), assigns priority buckets, persists to job_priorities
ES index, and publishes JobPriorityList to SignalBus.

Default configuration:
    - decision_cycle: 60 seconds
    - cooldown: 15 minutes per entity
    - scoring weights: sla_urgency=0.40, cargo_priority=0.30,
      customer_tier=0.30

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.10
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskSignal,
)
from Agents.overlay.signal_bus import SignalBus
from Agents.support.fuel_distribution_models import PriorityBucket

logger = logging.getLogger(__name__)

# Elasticsearch index for active jobs
JOBS_CURRENT_INDEX = "jobs_current"

# Elasticsearch index for persisted priority lists (Req 3.7)
JOB_PRIORITIES_INDEX = "job_priorities"

# Default scoring weights (Req 3.3)
DEFAULT_SCORING_WEIGHTS: Dict[str, float] = {
    "sla_urgency": 0.40,
    "cargo_priority": 0.30,
    "customer_tier": 0.30,
}

# Priority field numeric mapping (Req 3.4)
PRIORITY_SCORE_MAP: Dict[str, float] = {
    "urgent": 1.0,
    "high": 0.75,
    "normal": 0.5,
    "low": 0.25,
}


# ------------------------------------------------------------------
# Data models
# ------------------------------------------------------------------


class JobPriority(BaseModel):
    """Priority record for a single job.

    Validates: Requirements 3.3, 3.6
    """

    job_id: str
    job_type: str
    priority_score: float = Field(ge=0.0, le=1.0)
    priority_bucket: PriorityBucket
    sla_urgency: float = Field(ge=0.0, le=1.0)
    cargo_priority_score: float = Field(ge=0.0, le=1.0)
    customer_tier_score: float = Field(ge=0.0, le=1.0)
    reasons: List[str] = Field(default_factory=list)


class JobPriorityList(BaseModel):
    """Ranked list of job priorities for a tenant.

    Validates: Requirements 3.7, 3.8
    """

    priority_list_id: str = Field(
        default_factory=lambda: str(uuid.uuid4())
    )
    priorities: List[JobPriority]
    scoring_weights: Dict[str, float] = Field(default_factory=dict)
    tenant_id: str
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ------------------------------------------------------------------
# Agent
# ------------------------------------------------------------------


class JobPriorityEngine(OverlayAgentBase):
    """Ranks general jobs by SLA urgency, cargo priority, and customer tier.

    Consumes RiskSignal messages from job_sla_monitor and
    delay_response_agent, queries active jobs from jobs_current,
    computes weighted priority scores, assigns buckets, persists
    to job_priorities ES index, and publishes to SignalBus.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service for querying indices.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        poll_interval: Decision cycle interval in seconds (default 60).
        cooldown_minutes: Per-entity cooldown in minutes (default 15).
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
        poll_interval: int = 60,
        cooldown_minutes: int = 15,
    ):
        super().__init__(
            agent_id="job_priority_engine",
            signal_bus=signal_bus,
            subscriptions=[
                {
                    "message_type": RiskSignal,
                    "filters": {
                        "source_agent": [
                            "job_sla_monitor",
                            "delay_response_agent",
                        ],
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
            cooldown_minutes=cooldown_minutes,
        )

    # ------------------------------------------------------------------
    # Core evaluation (Req 3.1–3.8, 3.10)
    # ------------------------------------------------------------------

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Compute priority scores for active jobs and publish ranked list.

        Steps:
        1. Query jobs_current for active jobs (Req 3.2).
        2. For each job compute weighted priority score (Req 3.3).
        3. Clamp score to [0.0, 1.0], assign bucket (Req 3.6, 3.10).
        4. Persist JobPriorityList to job_priorities ES index (Req 3.7).
        5. Publish JobPriorityList to SignalBus (Req 3.8).

        Returns:
            Empty list — priorities are published directly to SignalBus.
        """
        tenant_id = signals[0].tenant_id if signals else "default"

        # Step 1: Query active jobs (Req 3.2)
        active_jobs = await self._query_active_jobs(tenant_id)
        if not active_jobs:
            return []

        # Build a lookup of signal context by entity_id for enrichment
        signal_context: Dict[str, Dict[str, Any]] = {}
        for sig in signals:
            signal_context[sig.entity_id] = sig.context

        # Step 2–3: Compute priorities
        weights = dict(DEFAULT_SCORING_WEIGHTS)
        priorities: List[JobPriority] = []

        for job in active_jobs:
            job_id = job.get("job_id", job.get("_id", ""))
            job_type = job.get("job_type", "unknown")
            priority_field = job.get("priority", "normal").lower()

            # SLA urgency (Req 3.5)
            scheduled_time = job.get("scheduled_time") or job.get("created_at")
            estimated_arrival = job.get("estimated_arrival")
            sla_urgency = self._compute_sla_urgency(
                scheduled_time, estimated_arrival
            )

            # Cargo priority (Req 3.4)
            cargo_priority_score = PRIORITY_SCORE_MAP.get(
                priority_field, 0.5
            )

            # Customer tier (from signal context or default 0.5)
            ctx = signal_context.get(job_id, {})
            customer_tier_score = float(ctx.get("customer_tier", 0.5))
            customer_tier_score = max(0.0, min(1.0, customer_tier_score))

            # Weighted score (Req 3.3)
            w_sla = weights.get("sla_urgency", 0.40)
            w_cargo = weights.get("cargo_priority", 0.30)
            w_tier = weights.get("customer_tier", 0.30)

            raw_score = (
                w_sla * sla_urgency
                + w_cargo * cargo_priority_score
                + w_tier * customer_tier_score
            )

            # Clamp to [0.0, 1.0] (Req 3.10)
            priority_score = round(max(0.0, min(1.0, raw_score)), 4)

            # Assign bucket (Req 3.6)
            bucket = self._assign_bucket(priority_score)

            # Build reasons
            reasons: List[str] = []
            if sla_urgency >= 0.8:
                reasons.append(f"high_sla_urgency ({sla_urgency:.2f})")
            if cargo_priority_score >= 0.75:
                reasons.append(
                    f"high_cargo_priority ({priority_field})"
                )
            if customer_tier_score >= 0.8:
                reasons.append(
                    f"premium_customer_tier ({customer_tier_score:.2f})"
                )

            priorities.append(
                JobPriority(
                    job_id=job_id,
                    job_type=job_type,
                    priority_score=priority_score,
                    priority_bucket=bucket,
                    sla_urgency=sla_urgency,
                    cargo_priority_score=cargo_priority_score,
                    customer_tier_score=customer_tier_score,
                    reasons=reasons,
                )
            )

        # Sort by priority_score descending (most urgent first)
        priorities.sort(key=lambda p: p.priority_score, reverse=True)

        # Build the priority list
        priority_list = JobPriorityList(
            priorities=priorities,
            scoring_weights=weights,
            tenant_id=tenant_id,
        )

        # Step 4: Persist to ES (Req 3.7)
        await self._persist_priority_list(priority_list)

        # Step 5: Publish to SignalBus (Req 3.8)
        await self._signal_bus.publish(priority_list)

        logger.info(
            "JobPriorityEngine: published %d priorities for tenant %s "
            "(critical=%d, high=%d, medium=%d, low=%d)",
            len(priorities),
            tenant_id,
            sum(
                1
                for p in priorities
                if p.priority_bucket == PriorityBucket.CRITICAL
            ),
            sum(
                1
                for p in priorities
                if p.priority_bucket == PriorityBucket.HIGH
            ),
            sum(
                1
                for p in priorities
                if p.priority_bucket == PriorityBucket.MEDIUM
            ),
            sum(
                1
                for p in priorities
                if p.priority_bucket == PriorityBucket.LOW
            ),
        )

        return []

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _assign_bucket(score: float) -> PriorityBucket:
        """Assign priority bucket based on score thresholds (Req 3.6).

        critical >= 0.8, high >= 0.6, medium >= 0.3, low < 0.3
        """
        if score >= 0.8:
            return PriorityBucket.CRITICAL
        elif score >= 0.6:
            return PriorityBucket.HIGH
        elif score >= 0.3:
            return PriorityBucket.MEDIUM
        return PriorityBucket.LOW

    @staticmethod
    def _compute_sla_urgency(
        scheduled_time, estimated_arrival
    ) -> float:
        """Compute SLA urgency as a value in [0.0, 1.0] (Req 3.5).

        Less remaining time produces a higher urgency score.
        Urgency = 1.0 - (remaining_time / total_duration), clamped to [0, 1].

        If scheduled_time or estimated_arrival is missing or unparseable,
        returns a default urgency of 0.5.
        """
        now = datetime.now(timezone.utc)

        try:
            if isinstance(scheduled_time, str):
                scheduled_time = datetime.fromisoformat(
                    scheduled_time.replace("Z", "+00:00")
                )
            if isinstance(estimated_arrival, str):
                estimated_arrival = datetime.fromisoformat(
                    estimated_arrival.replace("Z", "+00:00")
                )
        except (ValueError, TypeError):
            return 0.5

        if scheduled_time is None or estimated_arrival is None:
            return 0.5

        # Ensure timezone-aware
        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
        if estimated_arrival.tzinfo is None:
            estimated_arrival = estimated_arrival.replace(tzinfo=timezone.utc)

        total_duration = (
            estimated_arrival - scheduled_time
        ).total_seconds()
        if total_duration <= 0:
            # Already past or zero duration — maximum urgency
            return 1.0

        remaining = (estimated_arrival - now).total_seconds()
        if remaining <= 0:
            # Breach has occurred
            return 1.0

        urgency = 1.0 - (remaining / total_duration)
        return max(0.0, min(1.0, urgency))

    # ------------------------------------------------------------------
    # ES queries
    # ------------------------------------------------------------------

    async def _query_active_jobs(
        self, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """Query jobs_current for active jobs (Req 3.2).

        Active statuses: scheduled, assigned, in_progress.
        """
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {
                            "terms": {
                                "status": [
                                    "scheduled",
                                    "assigned",
                                    "in_progress",
                                ],
                            },
                        },
                    ],
                },
            },
            "size": 200,
            "sort": [{"estimated_arrival": {"order": "asc"}}],
        }

        try:
            resp = await self._es.search_documents(
                JOBS_CURRENT_INDEX, query, 200
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception as e:
            logger.error(
                "JobPriorityEngine: failed to query active jobs: %s", e
            )
            return []

    # ------------------------------------------------------------------
    # Persistence (Req 3.7)
    # ------------------------------------------------------------------

    async def _persist_priority_list(
        self, priority_list: JobPriorityList
    ) -> None:
        """Persist a JobPriorityList to the job_priorities ES index."""
        try:
            doc = priority_list.model_dump(mode="json")
            await self._es.index_document(
                JOB_PRIORITIES_INDEX,
                priority_list.priority_list_id,
                doc,
            )
        except Exception as e:
            logger.error(
                "JobPriorityEngine: failed to persist priority list %s: %s",
                priority_list.priority_list_id,
                e,
            )
