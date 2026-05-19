"""
Delivery Prioritization Agent — overlay agent for fuel order priority scoring.

Reads pending work from ``fuel_orders_current`` WHERE
``status IN {"placed", "confirmed", "scheduled"}`` AND ``tenant_id = {tenant}``.

Priority weights key off ``call_type``:
- ``keep_full`` and ``auto_fill`` score via the linked ``customer_tank_id``
  forecast from ``mvp_tank_forecasts``.
- ``will_call`` and ``one_off`` score via ``delivery_window_end`` urgency.

Missing scoring inputs emit ``scoring_input_missing`` on the entry and
score LOW.

Validates: Requirements 5.1.1, 5.1.2, 5.1.3, 5.1.5
"""

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.overlay.signal_bus import SignalBus
from Agents.support.fuel_distribution_models import (
    DeliveryPriority,
    DeliveryPriorityList,
    FuelGrade,
    PriorityBucket,
    TankForecast,
)
from Agents.support.mvp_es_mappings import MVP_TANK_FORECASTS_INDEX
from fuel.services.fuel_ops_es_mappings import CUSTOMER_TANKS_INDEX
from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

logger = logging.getLogger(__name__)

# Priority score thresholds
CRITICAL_THRESHOLD = 0.85
HIGH_THRESHOLD = 0.65
MEDIUM_THRESHOLD = 0.40

# Score assigned when scoring inputs are missing
LOW_SCORE = 0.15

# Default urgency window in hours
DEFAULT_URGENCY_WINDOW_HOURS = 4.0

# Maximum lookahead for window-based scoring (hours)
MAX_WINDOW_LOOKAHEAD_HOURS = 72.0

# Legacy constants preserved for backward compatibility
DEFAULT_SCORING_WEIGHTS: Dict[str, float] = {
    "runout_risk_24h": 0.4,
    "sla_tier": 0.25,
    "travel_time": 0.2,
    "business_impact": 0.15,
}

SLA_TIER_SCORES: Dict[str, float] = {
    "platinum": 1.0,
    "gold": 0.8,
    "silver": 0.6,
    "bronze": 0.4,
    "basic": 0.2,
}

DEFAULT_SLA_TIER = "basic"
DEFAULT_SLA_SCORE = 0.2
SCORING_WEIGHTS_REDIS_KEY = "mvp:scoring_weights:{tenant_id}"
DEFAULT_GENERATOR_PRIORITY_BOOST = 0.2


def _bucket_from_score(score: float) -> PriorityBucket:
    """Map a numeric priority score to a PriorityBucket."""
    if score >= CRITICAL_THRESHOLD:
        return PriorityBucket.CRITICAL
    elif score >= HIGH_THRESHOLD:
        return PriorityBucket.HIGH
    elif score >= MEDIUM_THRESHOLD:
        return PriorityBucket.MEDIUM
    else:
        return PriorityBucket.LOW


class DeliveryPrioritizationAgent(OverlayAgentBase):
    """Scores and ranks fuel orders for delivery prioritization.

    Reads from ``fuel_orders_current`` and produces a
    :class:`DeliveryPriorityList` on the SignalBus for downstream
    consumers (Route_Planning_Agent, Compartment_Loading_Agent).

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service for querying indices.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        poll_interval: Decision cycle interval in seconds (default 60).
        cooldown_minutes: Per-tenant cooldown in minutes (default 5).
        storm_mode_evaluator: Optional storm mode evaluator for boosting.
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
        cooldown_minutes: int = 5,
        *,
        storm_mode_evaluator: Optional[Any] = None,
        redis_client=None,
        combinable_group_repository=None,
        customer_profile_loader=None,
        customer_tank_loader=None,
        generator_priority_boost: float = DEFAULT_GENERATOR_PRIORITY_BOOST,
    ):
        super().__init__(
            agent_id="delivery_prioritization",
            signal_bus=signal_bus,
            subscriptions=[],
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            autonomy_config_service=autonomy_config_service,
            feature_flag_service=feature_flag_service,
            es_service=es_service,
            poll_interval=poll_interval,
            cooldown_minutes=cooldown_minutes,
        )
        self._storm_mode_evaluator = storm_mode_evaluator

    @staticmethod
    def _assign_bucket(score: float) -> PriorityBucket:
        """Map a numeric priority score to a PriorityBucket."""
        return _bucket_from_score(score)

    def set_storm_mode_evaluator(self, evaluator: Optional[Any]) -> None:
        """Inject the StormModeEvaluator post-construction."""
        self._storm_mode_evaluator = evaluator

    async def prioritize_fuel_orders(
        self, tenant_id: str
    ) -> Optional[DeliveryPriorityList]:
        """Public entry point for per-tenant prioritization.

        Reads pending orders from fuel_orders_current, scores them,
        and returns the ranked DeliveryPriorityList.
        """
        return await self._prioritize_tenant(tenant_id)

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Score and rank pending fuel orders for each tenant.

        Steps:
        1. Discover tenants with pending orders.
        2. For each tenant, fetch orders with status in
           {placed, confirmed, scheduled}.
        3. Score each order based on call_type.
        4. Publish a DeliveryPriorityList on the SignalBus.
        5. Return InterventionProposals.
        """
        tenant_ids = await self._discover_tenants_with_pending_orders()
        proposals: List[InterventionProposal] = []

        for tenant_id in tenant_ids:
            priority_list = await self._prioritize_tenant(tenant_id)
            if priority_list and priority_list.priorities:
                await self._signal_bus.publish(priority_list)
                proposal = self._build_proposal(priority_list, tenant_id)
                proposals.append(proposal)

        return proposals

    async def _discover_tenants_with_pending_orders(self) -> List[str]:
        """Aggregate distinct tenant_ids from fuel_orders_current with
        pending statuses."""
        query = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"terms": {"status": ["placed", "confirmed", "scheduled"]}}
                    ]
                }
            },
            "aggs": {
                "tenants": {"terms": {"field": "tenant_id", "size": 500}}
            },
        }
        try:
            resp = await self._es.search_documents(
                FUEL_ORDERS_CURRENT_INDEX, query, 0
            )
            buckets = (
                (resp or {}).get("aggregations", {})
                .get("tenants", {})
                .get("buckets", [])
            )
            return [
                b["key"]
                for b in buckets
                if isinstance(b, dict) and b.get("key")
            ]
        except Exception as exc:
            logger.warning(
                "DeliveryPrioritizationAgent: tenant discovery failed: %s",
                exc,
            )
            return []

    async def _prioritize_tenant(
        self, tenant_id: str
    ) -> Optional[DeliveryPriorityList]:
        """Fetch and score all pending orders for a tenant."""
        orders = await self._fetch_pending_orders(tenant_id)
        if not orders:
            return None

        # Batch-fetch forecasts for orders that need them
        tank_ids = [
            o["customer_tank_id"]
            for o in orders
            if o.get("call_type") in ("keep_full", "auto_fill")
            and o.get("customer_tank_id")
        ]
        forecasts = (
            await self._fetch_forecasts(tenant_id, tank_ids)
            if tank_ids
            else {}
        )

        # Batch-fetch customer tanks for criticality_tier (storm mode)
        all_tank_ids = [
            o["customer_tank_id"]
            for o in orders
            if o.get("customer_tank_id")
        ]
        customer_tanks = (
            await self._fetch_customer_tanks(tenant_id, all_tank_ids)
            if all_tank_ids
            else {}
        )

        # Check storm mode
        storm_active = await self._is_storm_active(tenant_id)

        # Score each order
        now = datetime.now(timezone.utc)
        run_id = str(uuid4())
        priorities: List[DeliveryPriority] = []

        for order in orders:
            priority = self._score_order(
                order, forecasts, customer_tanks, storm_active, now
            )
            priorities.append(priority)

        # Sort by score descending
        priorities.sort(key=lambda p: p.priority_score, reverse=True)

        return DeliveryPriorityList(
            priorities=priorities,
            tenant_id=tenant_id,
            run_id=run_id,
        )

    async def _fetch_pending_orders(
        self, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """Fetch orders from fuel_orders_current with pending statuses."""
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"status": ["placed", "confirmed", "scheduled"]}},
                    ]
                }
            },
            "size": 1000,
        }
        try:
            resp = await self._es.search_documents(
                FUEL_ORDERS_CURRENT_INDEX, query, 1000
            )
            hits = (resp or {}).get("hits", {}).get("hits", [])
            return [hit["_source"] for hit in hits if hit.get("_source")]
        except Exception as exc:
            logger.error(
                "DeliveryPrioritizationAgent: failed to fetch orders for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )
            return []

    async def _fetch_forecasts(
        self, tenant_id: str, tank_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch the latest forecasts for the given tank_ids."""
        if not tank_ids:
            return {}

        unique_ids = list(set(tank_ids))
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"station_id": unique_ids}},
                    ]
                }
            },
            "sort": [{"timestamp": {"order": "desc"}}],
            "size": len(unique_ids),
        }
        try:
            resp = await self._es.search_documents(
                MVP_TANK_FORECASTS_INDEX, query, len(unique_ids)
            )
            hits = (resp or {}).get("hits", {}).get("hits", [])
            result: Dict[str, Dict[str, Any]] = {}
            for hit in hits:
                source = hit.get("_source", {})
                sid = source.get("station_id")
                if sid and sid not in result:
                    result[sid] = source
            return result
        except Exception as exc:
            logger.warning(
                "DeliveryPrioritizationAgent: forecast fetch failed for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )
            return {}

    async def _fetch_customer_tanks(
        self, tenant_id: str, tank_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch customer tank docs for criticality_tier lookups."""
        if not tank_ids:
            return {}

        unique_ids = list(set(tank_ids))
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"tank_id": unique_ids}},
                    ]
                }
            },
            "size": len(unique_ids),
        }
        try:
            resp = await self._es.search_documents(
                CUSTOMER_TANKS_INDEX, query, len(unique_ids)
            )
            hits = (resp or {}).get("hits", {}).get("hits", [])
            result: Dict[str, Dict[str, Any]] = {}
            for hit in hits:
                source = hit.get("_source", {})
                tid = source.get("tank_id")
                if tid:
                    result[tid] = source
            return result
        except Exception as exc:
            logger.warning(
                "DeliveryPrioritizationAgent: customer_tanks fetch failed "
                "for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return {}

    async def _is_storm_active(self, tenant_id: str) -> bool:
        """Check if storm mode is active for the tenant."""
        if self._storm_mode_evaluator is None:
            return False
        try:
            state = await self._storm_mode_evaluator.get_state(tenant_id)
            return state.state == "active"
        except Exception:
            return False

    def _score_order(
        self,
        order: Dict[str, Any],
        forecasts: Dict[str, Dict[str, Any]],
        customer_tanks: Dict[str, Dict[str, Any]],
        storm_active: bool,
        now: datetime,
    ) -> DeliveryPriority:
        """Score a single fuel order based on its call_type.

        - keep_full / auto_fill: score via linked forecast
          (hours_to_runout -> urgency).
        - will_call / one_off: score via delivery_window_end proximity.

        Missing scoring inputs -> scoring_input_missing flag + LOW score.
        Storm mode boosts orders whose linked tank has a criticality_tier
        in the storm-priority set.
        """
        call_type = order.get("call_type", "one_off")
        order_id = order.get("order_id", "unknown")
        customer_tank_id = order.get("customer_tank_id")
        reasons: List[str] = []
        score = LOW_SCORE
        scoring_input_missing = False

        if call_type in ("keep_full", "auto_fill"):
            score, scoring_input_missing, reasons = (
                self._score_forecast_based(order, forecasts, customer_tank_id)
            )
        elif call_type in ("will_call", "one_off"):
            score, scoring_input_missing, reasons = (
                self._score_window_based(order, now)
            )
        else:
            scoring_input_missing = True
            reasons.append("unknown_call_type")

        # Storm mode boost
        if storm_active and customer_tank_id:
            tank = customer_tanks.get(customer_tank_id, {})
            criticality_tier = tank.get("criticality_tier")
            if criticality_tier in ("critical", "medical", "essential"):
                original_score = score
                score = min(1.0, score + 0.2)
                reasons.append(f"storm_boost:{criticality_tier}")
                logger.debug(
                    "DeliveryPrioritizationAgent: storm boost for "
                    "order=%s tank=%s tier=%s (%.2f -> %.2f)",
                    order_id,
                    customer_tank_id,
                    criticality_tier,
                    original_score,
                    score,
                )

        if scoring_input_missing:
            reasons.append("scoring_input_missing")

        fuel_grade = self._resolve_fuel_grade(order.get("product_code"))
        bucket = _bucket_from_score(score)

        return DeliveryPriority(
            station_id=customer_tank_id or order_id,
            fuel_grade=fuel_grade,
            priority_score=round(score, 4),
            priority_bucket=bucket,
            reasons=reasons,
        )

    def _score_forecast_based(
        self,
        order: Dict[str, Any],
        forecasts: Dict[str, Dict[str, Any]],
        customer_tank_id: Optional[str],
    ) -> Tuple[float, bool, List[str]]:
        """Score keep_full/auto_fill orders via tank forecast."""
        reasons: List[str] = []

        if not customer_tank_id:
            reasons.append("no_customer_tank_id")
            return LOW_SCORE, True, reasons

        forecast = forecasts.get(customer_tank_id)
        if not forecast:
            reasons.append("no_forecast_available")
            return LOW_SCORE, True, reasons

        hours_to_runout = forecast.get(
            "hours_to_runout_p90"
        ) or forecast.get("hours_to_runout")
        if hours_to_runout is None:
            reasons.append("no_hours_to_runout")
            return LOW_SCORE, True, reasons

        try:
            hours = float(hours_to_runout)
        except (TypeError, ValueError):
            reasons.append("invalid_hours_to_runout")
            return LOW_SCORE, True, reasons

        if hours <= 0:
            score = 1.0
            reasons.append("runout_imminent")
        elif hours <= 12:
            score = 0.85 + (1.0 - 0.85) * (1.0 - hours / 12.0)
            reasons.append("runout_critical")
        elif hours <= 24:
            score = 0.65 + (0.85 - 0.65) * (1.0 - (hours - 12) / 12.0)
            reasons.append("runout_high")
        elif hours <= 48:
            score = 0.40 + (0.65 - 0.40) * (1.0 - (hours - 24) / 24.0)
            reasons.append("runout_medium")
        else:
            score = max(
                0.15, 0.40 * (1.0 - min((hours - 48) / 72.0, 1.0))
            )
            reasons.append("runout_low")

        return score, False, reasons

    def _score_window_based(
        self, order: Dict[str, Any], now: datetime
    ) -> Tuple[float, bool, List[str]]:
        """Score will_call/one_off orders via delivery_window_end proximity."""
        reasons: List[str] = []
        window_end_raw = order.get("delivery_window_end")

        if not window_end_raw:
            reasons.append("no_delivery_window_end")
            return LOW_SCORE, True, reasons

        try:
            if isinstance(window_end_raw, datetime):
                window_end = window_end_raw
            else:
                window_end = datetime.fromisoformat(
                    str(window_end_raw).replace("Z", "+00:00")
                )
            if window_end.tzinfo is None:
                window_end = window_end.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            reasons.append("invalid_delivery_window_end")
            return LOW_SCORE, True, reasons

        hours_until_end = (window_end - now).total_seconds() / 3600.0

        if hours_until_end <= 0:
            score = 1.0
            reasons.append("window_overdue")
        elif hours_until_end <= DEFAULT_URGENCY_WINDOW_HOURS:
            score = 0.85 + (1.0 - 0.85) * (
                1.0 - hours_until_end / DEFAULT_URGENCY_WINDOW_HOURS
            )
            reasons.append("window_urgent")
        elif hours_until_end <= 12:
            score = 0.65 + (0.85 - 0.65) * (
                1.0
                - (hours_until_end - DEFAULT_URGENCY_WINDOW_HOURS)
                / (12 - DEFAULT_URGENCY_WINDOW_HOURS)
            )
            reasons.append("window_high")
        elif hours_until_end <= 24:
            score = 0.40 + (0.65 - 0.40) * (
                1.0 - (hours_until_end - 12) / 12.0
            )
            reasons.append("window_medium")
        else:
            score = max(
                0.15,
                0.40
                * (
                    1.0
                    - min(
                        (hours_until_end - 24)
                        / MAX_WINDOW_LOOKAHEAD_HOURS,
                        1.0,
                    )
                ),
            )
            reasons.append("window_low")

        return score, False, reasons

    def _resolve_fuel_grade(self, product_code: Optional[str]) -> FuelGrade:
        """Map a product_code to a FuelGrade enum value using the mapping service."""
        if not product_code:
            return FuelGrade.AGO

        # Try direct FuelGrade enum parsing first
        try:
            return FuelGrade(product_code)
        except ValueError:
            pass

        # Use the mapping service for US product codes
        from fuel.services.fuel_product_mapping import fuel_product_mapper
        mapped_grade = fuel_product_mapper.us_to_fuel_grade(product_code)
        return mapped_grade if mapped_grade else FuelGrade.AGO

    def _build_proposal(
        self, priority_list: DeliveryPriorityList, tenant_id: str
    ) -> InterventionProposal:
        """Build an InterventionProposal from a priority list."""
        critical_count = sum(
            1
            for p in priority_list.priorities
            if p.priority_bucket == PriorityBucket.CRITICAL
        )
        high_count = sum(
            1
            for p in priority_list.priorities
            if p.priority_bucket == PriorityBucket.HIGH
        )
        return InterventionProposal(
            source_agent=self.agent_id,
            tenant_id=tenant_id,
            risk_class=RiskClass.MEDIUM,
            expected_kpi_delta={
                "orders_prioritized": float(len(priority_list.priorities)),
                "critical_orders": float(critical_count),
                "high_orders": float(high_count),
            },
            confidence=0.9,
            priority=1,
            actions=[
                {
                    "tool": "publish_priority_list",
                    "params": {
                        "priority_list_id": priority_list.priority_list_id,
                        "order_count": len(priority_list.priorities),
                        "run_id": priority_list.run_id,
                    },
                }
            ],
        )

    async def _persist_priority_list(
        self, priority_list: DeliveryPriorityList
    ) -> None:
        """Persist priority list to ES (legacy compat stub).

        The new fuel-order-based agent publishes to the SignalBus
        rather than persisting directly. This method is retained for
        backward compatibility with tests that call it directly.
        """
        from fuel.services.fuel_product_catalog import canonicalize_or_warn

        doc = {
            "priority_list_id": priority_list.priority_list_id,
            "tenant_id": priority_list.tenant_id,
            "run_id": priority_list.run_id,
            "priorities": [
                {
                    **p.model_dump(),
                    "fuel_grade": canonicalize_or_warn(p.fuel_grade.value),
                }
                for p in priority_list.priorities
            ],
        }
        await self._es.index_document(
            "mvp_delivery_priorities",
            priority_list.priority_list_id,
            doc,
        )
