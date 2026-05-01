"""
Delivery Prioritization Agent — overlay agent for station ranking by delivery urgency.

Subscribes to TankForecast messages from the SignalBus, computes weighted
priority scores using configurable weights (runout_risk_24h, SLA tier,
travel time, business impact), assigns priority buckets, persists to
mvp_delivery_priorities, and publishes DeliveryPriorityList to SignalBus.

Default configuration:
    - decision_cycle: 60 seconds
    - cooldown: 15 minutes per station
    - scoring weights: runout_risk_24h=0.4, sla_tier=0.25,
      travel_time=0.2, business_impact=0.15

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
)
from Agents.overlay.signal_bus import SignalBus
from Agents.support.fuel_distribution_models import (
    DeliveryPriority,
    DeliveryPriorityList,
    FuelGrade,
    PriorityBucket,
    TankForecast,
)
from Agents.support.mvp_es_mappings import MVP_DELIVERY_PRIORITIES_INDEX

logger = logging.getLogger(__name__)

# Elasticsearch indices consumed by this agent
FUEL_STATIONS_INDEX = "fuel_stations"

# Default scoring weights (Req 2.2)
DEFAULT_SCORING_WEIGHTS: Dict[str, float] = {
    "runout_risk_24h": 0.4,
    "sla_tier": 0.25,
    "travel_time": 0.2,
    "business_impact": 0.15,
}

# SLA tier score mapping (higher = more urgent)
SLA_TIER_SCORES: Dict[str, float] = {
    "platinum": 1.0,
    "gold": 0.8,
    "silver": 0.6,
    "bronze": 0.4,
    "basic": 0.2,
}

# Default SLA tier when none is configured (Req 2.7)
DEFAULT_SLA_TIER = "basic"
DEFAULT_SLA_SCORE = 0.2

# Priority bucket thresholds (Req 2.3)
CRITICAL_THRESHOLD = 0.8
HIGH_THRESHOLD = 0.6
MEDIUM_THRESHOLD = 0.3

# Redis key pattern for per-tenant scoring weights (Req 2.6)
SCORING_WEIGHTS_REDIS_KEY = "mvp:scoring_weights:{tenant_id}"


class DeliveryPrioritizationAgent(OverlayAgentBase):
    """Ranks stations by delivery urgency based on forecasts and business factors.

    Consumes TankForecast messages, computes weighted priority scores,
    assigns priority buckets, and publishes a ranked DeliveryPriorityList.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service for querying indices.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        redis_client: Optional Redis client for per-tenant weight config.
        poll_interval: Decision cycle interval in seconds (default 60).
        cooldown_minutes: Per-station cooldown in minutes (default 15).
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
        redis_client=None,
        poll_interval: int = 60,
        cooldown_minutes: int = 15,
    ):
        super().__init__(
            agent_id="delivery_prioritization",
            signal_bus=signal_bus,
            subscriptions=[
                {
                    "message_type": TankForecast,
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
        self._redis = redis_client
        # Buffer forecasts between cycles
        self._forecast_buffer: List[TankForecast] = []

    # ------------------------------------------------------------------
    # Signal handling override — buffer TankForecast messages
    # ------------------------------------------------------------------

    async def _on_signal(self, signal) -> None:
        """Buffer incoming signals. TankForecasts are stored separately."""
        if isinstance(signal, TankForecast):
            self._forecast_buffer.append(signal)
        else:
            await super()._on_signal(signal)

    # ------------------------------------------------------------------
    # Core evaluation (Req 2.1–2.7)
    # ------------------------------------------------------------------

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Consume forecasts, compute priority scores, publish ranked list.

        Steps:
        1. Collect buffered TankForecast messages (Req 2.1).
        2. Load per-tenant scoring weights from Redis (Req 2.6).
        3. Query station metadata for SLA tiers and business impact.
        4. For each forecast: compute weighted priority score (Req 2.2),
           assign priority bucket (Req 2.3), handle missing SLA (Req 2.7).
        5. Persist priority list to mvp_delivery_priorities (Req 2.4).
        6. Publish DeliveryPriorityList to SignalBus (Req 2.5).

        Returns:
            Empty list — priorities are published directly to SignalBus.
        """
        # Step 1: Collect buffered forecasts
        forecasts = list(self._forecast_buffer)
        self._forecast_buffer.clear()

        if not forecasts:
            return []

        tenant_id = forecasts[0].tenant_id

        # Step 2: Load per-tenant scoring weights (Req 2.6)
        weights = await self._load_scoring_weights(tenant_id)

        # Step 3: Query station metadata for SLA tiers and business impact
        station_metadata = await self._query_station_metadata(tenant_id)

        # Step 4: Compute priorities for each forecast
        priorities: List[DeliveryPriority] = []
        for forecast in forecasts:
            priority = self._compute_priority(
                forecast=forecast,
                station_meta=station_metadata.get(forecast.station_id, {}),
                weights=weights,
            )
            priorities.append(priority)

        # Sort by priority_score descending (most urgent first)
        priorities.sort(key=lambda p: p.priority_score, reverse=True)

        # Build the priority list
        run_id = forecasts[0].run_id if forecasts else ""
        priority_list = DeliveryPriorityList(
            priorities=priorities,
            scoring_weights=weights,
            tenant_id=tenant_id,
            run_id=run_id,
        )

        # Step 5: Persist to ES (Req 2.4)
        await self._persist_priority_list(priority_list)

        # Step 6: Publish to SignalBus (Req 2.5)
        await self._signal_bus.publish(priority_list)

        logger.info(
            "DeliveryPrioritizationAgent: published %d priorities for tenant %s "
            "(run_id=%s, critical=%d, high=%d, medium=%d, low=%d)",
            len(priorities),
            tenant_id,
            run_id,
            sum(1 for p in priorities if p.priority_bucket == PriorityBucket.CRITICAL),
            sum(1 for p in priorities if p.priority_bucket == PriorityBucket.HIGH),
            sum(1 for p in priorities if p.priority_bucket == PriorityBucket.MEDIUM),
            sum(1 for p in priorities if p.priority_bucket == PriorityBucket.LOW),
        )

        return []

    # ------------------------------------------------------------------
    # Scoring (Req 2.2, 2.3, 2.7)
    # ------------------------------------------------------------------

    def _compute_priority(
        self,
        forecast: TankForecast,
        station_meta: Dict[str, Any],
        weights: Dict[str, float],
    ) -> DeliveryPriority:
        """Compute weighted priority score and assign bucket for a forecast.

        Score = w_runout * runout_risk_24h
              + w_sla * sla_tier_score
              + w_travel * (1 - normalized_travel_time)
              + w_impact * business_impact_score

        Bucket thresholds (Req 2.3):
            critical >= 0.8, high >= 0.6, medium >= 0.3, low < 0.3
        """
        reasons: List[str] = []

        # Component 1: Runout risk (directly from forecast)
        runout_score = forecast.runout_risk_24h
        if runout_score >= 0.8:
            reasons.append(f"high_runout_risk ({runout_score:.2f})")

        # Component 2: SLA tier (Req 2.7 — default to lowest if missing)
        sla_tier = station_meta.get("sla_tier", "").lower()
        if not sla_tier or sla_tier not in SLA_TIER_SCORES:
            sla_score = DEFAULT_SLA_SCORE
            reasons.append("no_sla_tier_configured")
        else:
            sla_score = SLA_TIER_SCORES[sla_tier]
            if sla_score >= 0.8:
                reasons.append(f"premium_sla_tier ({sla_tier})")

        # Component 3: Travel time (normalized, inverted — closer = higher score)
        travel_time_minutes = station_meta.get("travel_time_minutes", 60.0)
        # Normalize: 0 minutes → 1.0, 120+ minutes → 0.0
        max_travel = 120.0
        travel_score = max(0.0, 1.0 - (travel_time_minutes / max_travel))

        # Component 4: Business impact
        business_impact = station_meta.get("business_impact_score", 0.5)
        business_impact = max(0.0, min(1.0, business_impact))
        if business_impact >= 0.8:
            reasons.append("high_business_impact")

        # Weighted combination (Req 2.2)
        w_runout = weights.get("runout_risk_24h", 0.4)
        w_sla = weights.get("sla_tier", 0.25)
        w_travel = weights.get("travel_time", 0.2)
        w_impact = weights.get("business_impact", 0.15)

        priority_score = (
            w_runout * runout_score
            + w_sla * sla_score
            + w_travel * travel_score
            + w_impact * business_impact
        )

        # Clamp to [0.0, 1.0]
        priority_score = round(max(0.0, min(1.0, priority_score)), 4)

        # Assign bucket (Req 2.3)
        bucket = self._assign_bucket(priority_score)

        return DeliveryPriority(
            station_id=forecast.station_id,
            fuel_grade=forecast.fuel_grade,
            priority_score=priority_score,
            priority_bucket=bucket,
            reasons=reasons,
        )

    @staticmethod
    def _assign_bucket(score: float) -> PriorityBucket:
        """Assign priority bucket based on score thresholds (Req 2.3).

        critical >= 0.8, high >= 0.6, medium >= 0.3, low < 0.3
        """
        if score >= CRITICAL_THRESHOLD:
            return PriorityBucket.CRITICAL
        elif score >= HIGH_THRESHOLD:
            return PriorityBucket.HIGH
        elif score >= MEDIUM_THRESHOLD:
            return PriorityBucket.MEDIUM
        else:
            return PriorityBucket.LOW

    # ------------------------------------------------------------------
    # Per-tenant scoring weights (Req 2.6)
    # ------------------------------------------------------------------

    async def _load_scoring_weights(self, tenant_id: str) -> Dict[str, float]:
        """Load per-tenant scoring weights from Redis.

        Falls back to DEFAULT_SCORING_WEIGHTS if Redis is unavailable
        or no tenant-specific config exists.
        """
        if not self._redis:
            return dict(DEFAULT_SCORING_WEIGHTS)

        try:
            key = SCORING_WEIGHTS_REDIS_KEY.format(tenant_id=tenant_id)
            raw = await self._redis.get(key)
            if raw:
                weights = json.loads(raw)
                # Validate that weights are present and sum reasonably
                if isinstance(weights, dict) and all(
                    k in weights for k in DEFAULT_SCORING_WEIGHTS
                ):
                    return weights
        except Exception as e:
            logger.warning(
                "DeliveryPrioritizationAgent: failed to load weights from Redis "
                "for tenant %s: %s. Using defaults.",
                tenant_id,
                e,
            )

        return dict(DEFAULT_SCORING_WEIGHTS)

    # ------------------------------------------------------------------
    # Station metadata query
    # ------------------------------------------------------------------

    async def _query_station_metadata(
        self, tenant_id: str
    ) -> Dict[str, Dict[str, Any]]:
        """Query fuel_stations for SLA tier, travel time, and business impact.

        Returns a dict keyed by station_id with metadata fields.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                    ],
                },
            },
            "_source": [
                "station_id",
                "sla_tier",
                "travel_time_minutes",
                "business_impact_score",
            ],
            "size": 200,
        }

        metadata: Dict[str, Dict[str, Any]] = {}
        try:
            resp = await self._es.search_documents(FUEL_STATIONS_INDEX, query, 200)
            for hit in resp.get("hits", {}).get("hits", []):
                source = hit["_source"]
                station_id = source.get("station_id", "")
                if station_id:
                    metadata[station_id] = source
        except Exception as e:
            logger.error(
                "DeliveryPrioritizationAgent: failed to query station metadata: %s",
                e,
            )

        return metadata

    # ------------------------------------------------------------------
    # Persistence (Req 2.4)
    # ------------------------------------------------------------------

    async def _persist_priority_list(
        self, priority_list: DeliveryPriorityList
    ) -> None:
        """Persist a DeliveryPriorityList to the mvp_delivery_priorities ES index."""
        try:
            doc = priority_list.model_dump(mode="json")
            await self._es.index_document(
                MVP_DELIVERY_PRIORITIES_INDEX,
                priority_list.priority_list_id,
                doc,
            )
        except Exception as e:
            logger.error(
                "DeliveryPrioritizationAgent: failed to persist priority list %s: %s",
                priority_list.priority_list_id,
                e,
            )
