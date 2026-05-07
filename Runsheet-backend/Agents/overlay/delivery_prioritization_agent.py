"""
Delivery Prioritization Agent — overlay agent for station ranking by delivery urgency.

Subscribes to TankForecast messages from the SignalBus, computes weighted
priority scores using configurable weights (runout_risk_24h, SLA tier,
travel time, business impact), assigns priority buckets, persists to
mvp_delivery_priorities, and publishes DeliveryPriorityList to SignalBus.

Phase 5 (fuel-ops hardening Capability 3) layers these extensions on top of
the legacy flow:

* Each priority entry carries ``safe_to_delay_days`` / ``safe_to_delay_bucket``
  computed via :func:`fuel.services.prioritization_helpers.compute_safe_to_delay`
  (Req 3.1.3).
* The placeholder ``business_impact`` component of the weighted score is
  replaced with the real
  :func:`fuel.services.prioritization_helpers.compute_business_impact`
  output (Req 3.3.3), with the existing 0.15 weight retained. Both the
  raw score and its reasons list are persisted to
  ``mvp_delivery_priorities`` (Req 3.3.4).
* After the priority list is built, the agent computes Combinable_Groups
  via :func:`fuel.combinable_group_models.compute_combinable_groups` and
  persists them to ``mvp_combinable_groups`` through
  :class:`fuel.combinable_group_models.CombinableGroupRepository`
  (Req 3.2.1, 3.2.2, 3.2.3).
* ``cluster_id`` / ``cluster_size`` are populated by the Task-5.4
  DBSCAN helper (:func:`fuel.services.prioritization_helpers.compute_priority_clusters`)
  and stamped onto each priority before persistence (Req 3.4.1, 3.4.2).
  Entries without usable coordinates keep the default ``None`` values.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 3.1.3, 3.2.3, 3.3.3, 3.3.4, 3.4.1, 3.4.2
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

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
from fuel.combinable_group_models import (
    CombinableGroup,
    CombinableGroupEntry,
    CombinableGroupRepository,
    compute_combinable_groups,
)
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
    canonicalize_or_warn,
)
from fuel.services.prioritization_helpers import (
    compute_business_impact,
    compute_priority_clusters,
    compute_safe_to_delay,
)

logger = logging.getLogger(__name__)

# Elasticsearch indices consumed by this agent
FUEL_STATIONS_INDEX = "fuel_stations"

# Optional Customer_Profile index (Phase-5 Req 3.3.1 stores profiles on the
# existing ``customers`` ES index). Looked up lazily via
# ``_load_customer_profiles``; missing indices are tolerated so tests and
# tenants without the feature enabled keep working.
CUSTOMERS_INDEX = "customers"

# The monetary fields that participate in the business-impact score.
# Kept here (not imported) to limit the module's coupling to
# ``prioritization_helpers`` internals.
_BUSINESS_IMPACT_MONETARY_FIELDS: Tuple[str, ...] = (
    "annual_revenue_usd",
    "contract_penalty_usd_per_day",
    "missed_delivery_cost_usd",
)

# Radius (miles) for :func:`fuel.combinable_group_models.compute_combinable_groups`
# (Req 3.2.1). 2.0 matches the spec default.
DEFAULT_COMBINABLE_RADIUS_MILES: float = 2.0

# DBSCAN defaults for route-friendly clustering (Req 3.4.1, 3.4.4).
# Match the tenant-configurable defaults documented in the spec so runs
# produce deterministic output when no override is supplied.
DEFAULT_CLUSTER_EPS_MILES: float = 3.0
DEFAULT_CLUSTER_MIN_SAMPLES: int = 2

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
        combinable_group_repository: Optional repository for persisting
            :class:`CombinableGroup` records. Lazily constructed from the
            shared ES service when ``None``.
        customer_profile_loader: Optional callable
            ``(tenant_id, customer_ids) -> mapping`` (or awaitable of one)
            used by :meth:`_load_customer_profiles`. When ``None`` the
            agent falls back to a direct ``customers`` ES query.
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
        combinable_group_repository: Optional[CombinableGroupRepository] = None,
        customer_profile_loader: Optional[
            Callable[[str, List[str]], Any]
        ] = None,
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

        # Combinable-group emission (Req 3.2.3). Lazily constructed.
        if combinable_group_repository is not None:
            self._combinable_group_repo = combinable_group_repository
        elif es_service is not None:
            try:
                self._combinable_group_repo = CombinableGroupRepository(es_service)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "DeliveryPrioritizationAgent: failed to construct "
                    "CombinableGroupRepository (%s); group emission disabled",
                    exc,
                )
                self._combinable_group_repo = None
        else:
            self._combinable_group_repo = None

        self._customer_profile_loader = customer_profile_loader

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
    # Core evaluation (Req 2.1–2.7, 3.1.3, 3.2.3, 3.3.3, 3.3.4)
    # ------------------------------------------------------------------

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Consume forecasts, compute priority scores, publish ranked list.

        Steps:
        1. Collect buffered TankForecast messages (Req 2.1).
        2. Load per-tenant scoring weights from Redis (Req 2.6).
        3. Query station metadata for SLA tiers and business impact.
        4. Load Customer_Profiles and tenant maxima for business-impact
           scoring (Req 3.3.2, 3.3.5).
        5. For each forecast: compute weighted priority score (Req 2.2),
           assign priority bucket (Req 2.3), attach safe-to-delay
           (Req 3.1.3) and business-impact (Req 3.3.3, 3.3.4) fields.
        5b. Stamp DBSCAN cluster_id / cluster_size on each entry via
            :meth:`_stamp_priority_clusters` (Req 3.4.1, 3.4.2).
        6. Persist priority list to mvp_delivery_priorities (Req 2.4).
        7. Publish DeliveryPriorityList to SignalBus (Req 2.5).
        8. Emit Combinable_Groups for the run (Req 3.2.3).

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

        # Step 4: Load Customer_Profiles for business-impact scoring.
        customer_ids = sorted(
            {f.customer_id for f in forecasts if f.customer_id}
        )
        profiles_by_id = await self._load_customer_profiles(
            tenant_id, customer_ids
        )
        tenant_max = self._compute_tenant_max(profiles_by_id)

        # Step 5: Compute priorities for each forecast
        priorities: List[DeliveryPriority] = []
        for forecast in forecasts:
            profile = (
                profiles_by_id.get(forecast.customer_id)
                if forecast.customer_id
                else None
            )
            priority = self._compute_priority(
                forecast=forecast,
                station_meta=station_metadata.get(forecast.station_id, {}),
                weights=weights,
                profile=profile,
                tenant_max=tenant_max,
            )
            priorities.append(priority)

        # Step 5b: Stamp DBSCAN cluster metadata on each priority (Req 3.4.2).
        # Runs compute_priority_clusters with the spec-default 3.0-mile eps
        # and min_samples=2 so cluster_id/cluster_size flow through to
        # ``mvp_delivery_priorities`` alongside safe-to-delay and
        # business-impact. Failures are logged and swallowed so an
        # sklearn hiccup never blocks the priority signal.
        self._stamp_priority_clusters(priorities, forecasts, station_metadata)

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

        # Step 6: Persist to ES (Req 2.4)
        await self._persist_priority_list(priority_list)

        # Step 7: Publish to SignalBus (Req 2.5)
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

        # Step 8: Emit Combinable_Groups (Req 3.2.3). Failures are logged
        # but never propagated — the priority signal has already shipped.
        await self._emit_combinable_groups(
            tenant_id=tenant_id,
            run_id=run_id,
            forecasts=forecasts,
            station_metadata=station_metadata,
        )

        return []

    # ------------------------------------------------------------------
    # Scoring (Req 2.2, 2.3, 2.7, 3.1.3, 3.3.3)
    # ------------------------------------------------------------------

    def _compute_priority(
        self,
        forecast: TankForecast,
        station_meta: Dict[str, Any],
        weights: Dict[str, float],
        profile: Optional[Any] = None,
        tenant_max: Optional[Dict[str, float]] = None,
    ) -> DeliveryPriority:
        """Compute weighted priority score and assign bucket for a forecast.

        Score = w_runout * runout_risk_24h
              + w_sla * sla_tier_score
              + w_travel * (1 - normalized_travel_time)
              + w_impact * business_impact_score

        The ``business_impact`` component is populated by
        :func:`fuel.services.prioritization_helpers.compute_business_impact`
        when a :class:`~fuel.storm_mode_models.CustomerProfile` is
        available for the forecast (Req 3.3.3). When no profile is
        available — either because the forecast is for a retail station
        or the ``customers`` index has no matching document — we fall
        back to the station's legacy ``business_impact_score`` metadata
        so station-level tagging continues to work unchanged.

        Bucket thresholds (Req 2.3):
            critical >= 0.8, high >= 0.6, medium >= 0.3, low < 0.3

        The result carries the new Phase-5 fields:

        * ``safe_to_delay_days`` / ``safe_to_delay_bucket`` (Req 3.1.3)
        * ``business_impact_score`` / ``business_impact_reasons``
          (Req 3.3.3, 3.3.4)
        * ``cluster_id`` / ``cluster_size`` (Req 3.4.2) — seeded as
          ``None`` here; :meth:`_stamp_priority_clusters` populates them
          in-place after every priority entry has been scored.
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

        # Component 4: Business impact (Req 3.3.3)
        business_impact_score, business_impact_reasons = self._resolve_business_impact(
            forecast=forecast,
            station_meta=station_meta,
            profile=profile,
            tenant_max=tenant_max or {},
        )
        # Clamp for safety; compute_business_impact already guarantees
        # the bound but station-metadata fallbacks do not.
        business_impact_component = max(
            0.0, min(1.0, float(business_impact_score))
        )
        if business_impact_component >= 0.8:
            reasons.append("high_business_impact")

        # Weighted combination (Req 2.2). The ``business_impact`` weight
        # of 0.15 is retained (Req 3.3.3) — only the source of the
        # component value moved from the placeholder to the real score.
        w_runout = weights.get("runout_risk_24h", 0.4)
        w_sla = weights.get("sla_tier", 0.25)
        w_travel = weights.get("travel_time", 0.2)
        w_impact = weights.get("business_impact", 0.15)

        priority_score = (
            w_runout * runout_score
            + w_sla * sla_score
            + w_travel * travel_score
            + w_impact * business_impact_component
        )

        # Clamp to [0.0, 1.0]
        priority_score = round(max(0.0, min(1.0, priority_score)), 4)

        # Assign bucket (Req 2.3)
        bucket = self._assign_bucket(priority_score)

        # Safe-to-delay (Req 3.1.3).
        safe_to_delay_days: Optional[int]
        safe_to_delay_bucket: Optional[str]
        try:
            safe = compute_safe_to_delay(forecast)
            raw_days = safe["safe_to_delay_days"]
            # ``math.inf`` is returned for forecasts with no projected
            # runout; surface as ``None`` so the ES integer mapping holds.
            if isinstance(raw_days, bool):
                safe_to_delay_days = None
            elif isinstance(raw_days, int):
                safe_to_delay_days = raw_days
            else:
                safe_to_delay_days = None
            safe_to_delay_bucket = safe["safe_to_delay_bucket"]
        except (TypeError, ValueError) as exc:
            logger.warning(
                "DeliveryPrioritizationAgent: compute_safe_to_delay "
                "failed for station=%s: %s",
                forecast.station_id,
                exc,
            )
            safe_to_delay_days = None
            safe_to_delay_bucket = None

        return DeliveryPriority(
            station_id=forecast.station_id,
            fuel_grade=forecast.fuel_grade,
            priority_score=priority_score,
            priority_bucket=bucket,
            reasons=reasons,
            safe_to_delay_days=safe_to_delay_days,
            safe_to_delay_bucket=safe_to_delay_bucket,
            business_impact_score=round(business_impact_component, 4),
            business_impact_reasons=list(business_impact_reasons),
            # cluster_id / cluster_size start as ``None``; they are
            # stamped onto each priority by
            # :meth:`_stamp_priority_clusters` after scoring completes
            # (Req 3.4.1, 3.4.2).
            cluster_id=None,
            cluster_size=None,
        )

    # ------------------------------------------------------------------
    # Business-impact resolution helper (Req 3.3.2, 3.3.3, 3.3.4, 3.3.5)
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_business_impact(
        *,
        forecast: TankForecast,
        station_meta: Dict[str, Any],
        profile: Optional[Any],
        tenant_max: Dict[str, float],
    ) -> Tuple[float, List[str]]:
        """Return ``(business_impact_score, business_impact_reasons)``.

        Preference order:

        1. When a :class:`CustomerProfile` is supplied, compute the real
           normalized score via :func:`compute_business_impact`
           (Req 3.3.2).
        2. Otherwise, fall back to the station-level
           ``business_impact_score`` metadata — this preserves the legacy
           retail-station surface where no customer profile exists. The
           reasons list surfaces a ``legacy_station_metadata`` marker so
           consumers know the score did not come from a Customer_Profile.
        3. If neither is available, return ``(0.0, ["missing_profile"])``
           so the entry still receives a deterministic score.
        """

        if profile is not None:
            try:
                score, reasons = compute_business_impact(profile, tenant_max)
            except ValueError as exc:
                logger.warning(
                    "DeliveryPrioritizationAgent: compute_business_impact "
                    "raised for customer_id=%s: %s",
                    getattr(forecast, "customer_id", None),
                    exc,
                )
                return 0.0, ["invalid_profile"]
            return score, list(reasons)

        # Station-level fallback (retail stations / legacy tenants).
        legacy = station_meta.get("business_impact_score")
        if legacy is not None:
            try:
                legacy_score = float(legacy)
            except (TypeError, ValueError):
                return 0.0, ["invalid_station_metadata"]
            clamped = max(0.0, min(1.0, legacy_score))
            return clamped, ["legacy_station_metadata"]

        return 0.0, ["missing_profile"]

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
        """Query fuel_stations for SLA tier, travel time, business impact,
        and location.

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
                "latitude",
                "longitude",
                "location",
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
        """Persist a DeliveryPriorityList to the mvp_delivery_priorities ES index.

        Canonicalizes each entry's ``fuel_grade`` before write so the
        persisted document only contains US-canonical product codes even
        when upstream stations still carry legacy NG aliases
        (Req 6.1.4). Unknown values are preserved with a logged warning
        rather than dropped — historical forecasts must not be silently
        mutated, only normalized.
        """
        try:
            doc = priority_list.model_dump(mode="json")
            priorities = doc.get("priorities") or []
            for entry in priorities:
                grade = entry.get("fuel_grade")
                if grade is not None:
                    entry["fuel_grade"] = canonicalize_or_warn(
                        grade,
                        context="mvp_delivery_priorities.fuel_grade",
                        logger_=logger,
                    )
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

    # ------------------------------------------------------------------
    # Customer profile loading (Req 3.3.1, 3.3.2, 3.3.5)
    # ------------------------------------------------------------------

    async def _load_customer_profiles(
        self,
        tenant_id: str,
        customer_ids: List[str],
    ) -> Dict[str, Any]:
        """Return a ``{customer_id: profile}`` map for the given ids.

        Three resolution paths:

        1. If a ``customer_profile_loader`` callable was injected, it is
           invoked with ``(tenant_id, customer_ids)`` and its return
           value (either a mapping or an awaitable of one) is normalized
           into a dict.
        2. Otherwise, the agent queries the ``customers`` ES index for
           the listed ids scoped to ``tenant_id`` and attempts to parse
           each source as a
           :class:`fuel.storm_mode_models.CustomerProfile`.
        3. When either path fails (missing index, deserialization
           error, tenant mismatch), the id is dropped from the result
           and the business-impact helper falls back to the station-level
           metadata or to a zero score with a ``missing_profile`` reason.

        Returns an empty dict when ``customer_ids`` is empty so the
        downstream loop can always ``profiles_by_id.get(customer_id)``
        safely.
        """

        if not customer_ids:
            return {}

        if self._customer_profile_loader is not None:
            try:
                loaded = self._customer_profile_loader(tenant_id, customer_ids)
                if hasattr(loaded, "__await__"):
                    loaded = await loaded  # type: ignore[assignment]
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "DeliveryPrioritizationAgent: customer_profile_loader "
                    "raised for tenant %s: %s",
                    tenant_id,
                    exc,
                )
                return {}
            if isinstance(loaded, dict):
                return {
                    cid: profile
                    for cid, profile in loaded.items()
                    if cid in customer_ids
                }
            logger.warning(
                "DeliveryPrioritizationAgent: customer_profile_loader "
                "returned %s; expected mapping",
                type(loaded).__name__,
            )
            return {}

        # Default path: query the ``customers`` ES index.
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"customer_id": list(customer_ids)}},
                    ],
                },
            },
            "size": max(len(customer_ids), 1),
        }
        try:
            resp = await self._es.search_documents(
                CUSTOMERS_INDEX, query, query["size"]
            )
        except Exception as exc:
            logger.debug(
                "DeliveryPrioritizationAgent: Customer_Profile lookup "
                "skipped for tenant %s: %s",
                tenant_id,
                exc,
            )
            return {}

        profiles_by_id: Dict[str, Any] = {}
        for hit in resp.get("hits", {}).get("hits", []):
            source = hit.get("_source") or {}
            if source.get("tenant_id") != tenant_id:
                continue
            cid = source.get("customer_id")
            if not cid:
                continue
            profile = self._parse_customer_profile(source)
            if profile is not None:
                profiles_by_id[cid] = profile
        return profiles_by_id

    @staticmethod
    def _parse_customer_profile(source: Dict[str, Any]) -> Optional[Any]:
        """Parse an ES ``customers`` document into a CustomerProfile.

        Imported lazily to avoid a hard dependency on the Phase-10 storm
        models — tests that do not exercise the customer-profile path
        should not need those imports to succeed.
        """

        try:
            from fuel.storm_mode_models import CustomerProfile
        except Exception:  # pragma: no cover — defensive
            return source  # return raw dict; helper supports mappings
        try:
            return CustomerProfile(**source)
        except Exception as exc:
            logger.debug(
                "DeliveryPrioritizationAgent: failed to parse "
                "CustomerProfile for customer_id=%s: %s",
                source.get("customer_id"),
                exc,
            )
            # Fall back to the raw dict — ``compute_business_impact``
            # accepts any attribute-accessible object or mapping.
            return source

    @staticmethod
    def _compute_tenant_max(
        profiles_by_id: Dict[str, Any],
    ) -> Dict[str, float]:
        """Derive the tenant's observed maxima across the monetary fields.

        Required by :func:`compute_business_impact` (Req 3.3.2). A
        missing or zero value is replaced with ``1.0`` inside the
        helper so the division never blows up — we still compute and
        forward any strictly-positive observed maximum here so the
        score is normalized against the actual fleet distribution.
        """

        maxima: Dict[str, float] = {}
        for field in _BUSINESS_IMPACT_MONETARY_FIELDS:
            highest = 0.0
            for profile in profiles_by_id.values():
                if profile is None:
                    continue
                if isinstance(profile, dict):
                    raw = profile.get(field)
                else:
                    raw = getattr(profile, field, None)
                if raw is None:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if value > highest:
                    highest = value
            if highest > 0.0:
                maxima[field] = highest
        return maxima

    # ------------------------------------------------------------------
    # DBSCAN cluster stamping (Req 3.4.1, 3.4.2, 3.4.4)
    # ------------------------------------------------------------------

    def _stamp_priority_clusters(
        self,
        priorities: List[DeliveryPriority],
        forecasts: List[TankForecast],
        station_metadata: Dict[str, Dict[str, Any]],
    ) -> None:
        """Populate ``cluster_id`` / ``cluster_size`` on each priority.

        Runs :func:`compute_priority_clusters` over the coordinates of
        the priority entries that can be geo-resolved and stamps the
        returned assignments in place. Entries without usable coordinates
        keep their default ``None`` values so the ES mapping stays clean.

        This is Task 5.5's wiring of the helper introduced in Task 5.4
        (:func:`fuel.services.prioritization_helpers.compute_priority_clusters`).
        Failures are logged and swallowed — cluster metadata is a
        nice-to-have enrichment, not a precondition for publishing the
        priority signal.
        """

        if not priorities:
            return

        # Map station_id -> forecast (first match). Priorities index by
        # station_id so this lets us look up the coordinates without
        # depending on the input ordering of ``forecasts``.
        forecast_by_station: Dict[str, TankForecast] = {}
        for forecast in forecasts:
            if forecast.station_id and forecast.station_id not in forecast_by_station:
                forecast_by_station[forecast.station_id] = forecast

        cluster_inputs: List[Dict[str, Any]] = []
        index_map: List[int] = []
        for idx, priority in enumerate(priorities):
            forecast = forecast_by_station.get(priority.station_id)
            if forecast is None:
                continue
            coords = self._resolve_coordinates(forecast, station_metadata)
            if coords is None:
                continue
            lat, lon = coords
            cluster_inputs.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "priority_bucket": (
                        priority.priority_bucket.value
                        if hasattr(priority.priority_bucket, "value")
                        else priority.priority_bucket
                    ),
                    "fuel_grade": (
                        priority.fuel_grade.value
                        if hasattr(priority.fuel_grade, "value")
                        else priority.fuel_grade
                    ),
                }
            )
            index_map.append(idx)

        if not cluster_inputs:
            return

        try:
            assignments, _clusters = compute_priority_clusters(
                cluster_inputs,
                eps_miles=DEFAULT_CLUSTER_EPS_MILES,
                min_samples=DEFAULT_CLUSTER_MIN_SAMPLES,
            )
        except Exception as exc:
            logger.warning(
                "DeliveryPrioritizationAgent: compute_priority_clusters "
                "raised; leaving cluster fields None: %s",
                exc,
            )
            return

        if len(assignments) != len(index_map):
            logger.warning(
                "DeliveryPrioritizationAgent: cluster assignment count "
                "(%d) does not match input count (%d); skipping stamp",
                len(assignments),
                len(index_map),
            )
            return

        for priority_idx, assignment in zip(index_map, assignments):
            priorities[priority_idx].cluster_id = assignment.cluster_id
            priorities[priority_idx].cluster_size = assignment.cluster_size

    # ------------------------------------------------------------------
    # Combinable-group emission (Req 3.2.1, 3.2.2, 3.2.3)
    # ------------------------------------------------------------------

    async def _emit_combinable_groups(
        self,
        *,
        tenant_id: str,
        run_id: str,
        forecasts: List[TankForecast],
        station_metadata: Dict[str, Dict[str, Any]],
    ) -> List[CombinableGroup]:
        """Compute and persist Combinable_Groups for the run.

        Builds :class:`CombinableGroupEntry` records from each forecast,
        runs :func:`compute_combinable_groups` with the spec-default
        2.0-mile radius, and persists the output through
        :class:`CombinableGroupRepository`.

        Returns the list of persisted groups (empty when the repository
        is unavailable, no entries are eligible, or the computation
        produced no ≥2-member components). Never raises — any failure
        is logged and swallowed so the prioritization pipeline stays
        responsive.
        """

        if self._combinable_group_repo is None or not forecasts:
            return []

        entries = self._build_combinable_group_entries(forecasts, station_metadata)
        if len(entries) < 2:
            return []

        try:
            groups = compute_combinable_groups(
                entries,
                radius_miles=DEFAULT_COMBINABLE_RADIUS_MILES,
                tenant_id=tenant_id,
                run_id=run_id,
            )
        except Exception as exc:
            logger.warning(
                "DeliveryPrioritizationAgent: compute_combinable_groups "
                "raised for tenant %s: %s",
                tenant_id,
                exc,
            )
            return []

        if not groups:
            return []

        try:
            persisted = await self._combinable_group_repo.persist_groups(
                tenant_id, groups
            )
        except Exception as exc:
            logger.warning(
                "DeliveryPrioritizationAgent: failed to persist %d "
                "combinable groups for tenant %s: %s",
                len(groups),
                tenant_id,
                exc,
            )
            return []

        logger.info(
            "DeliveryPrioritizationAgent: emitted %d combinable_groups "
            "for tenant %s (run_id=%s)",
            len(persisted),
            tenant_id,
            run_id,
        )
        return persisted

    @staticmethod
    def _build_combinable_group_entries(
        forecasts: List[TankForecast],
        station_metadata: Dict[str, Dict[str, Any]],
    ) -> List[CombinableGroupEntry]:
        """Translate forecasts into :class:`CombinableGroupEntry` records.

        A forecast is only eligible when we can resolve:

        * A non-empty destination id (station_id or customer_tank_id);
        * Valid latitude/longitude (Customer_Tank forecasts expose
          coordinates directly on the forecast; retail-station forecasts
          borrow them from ``station_metadata``);
        * A fuel grade that canonicalizes under the US product catalog.

        Forecasts that fail any check are skipped with a debug log so
        the pipeline never blocks on a single malformed record.
        """

        entries: List[CombinableGroupEntry] = []
        for forecast in forecasts:
            fuel_grade_raw = getattr(forecast.fuel_grade, "value", forecast.fuel_grade)
            try:
                fuel_grade = canonicalize(fuel_grade_raw)
            except (UnknownFuelProductError, AttributeError, TypeError):
                logger.debug(
                    "DeliveryPrioritizationAgent: dropping combinable "
                    "entry for forecast=%s (unknown fuel_grade=%s)",
                    forecast.forecast_id,
                    forecast.fuel_grade,
                )
                continue

            if forecast.customer_tank_id:
                destination_type = "customer_tank"
                destination_id = forecast.customer_tank_id
            else:
                destination_type = "station"
                destination_id = forecast.station_id

            if not destination_id:
                continue

            coords = DeliveryPrioritizationAgent._resolve_coordinates(
                forecast, station_metadata
            )
            if coords is None:
                logger.debug(
                    "DeliveryPrioritizationAgent: dropping combinable "
                    "entry for forecast=%s (no coordinates)",
                    forecast.forecast_id,
                )
                continue
            lat, lon = coords

            try:
                entries.append(
                    CombinableGroupEntry(
                        destination_type=destination_type,  # type: ignore[arg-type]
                        destination_id=destination_id,
                        fuel_grade=fuel_grade,
                        estimated_gallons=0.0,
                        location_lat=lat,
                        location_lon=lon,
                    )
                )
            except Exception as exc:
                logger.debug(
                    "DeliveryPrioritizationAgent: rejected combinable "
                    "entry for forecast=%s: %s",
                    forecast.forecast_id,
                    exc,
                )
                continue
        return entries

    @staticmethod
    def _resolve_coordinates(
        forecast: TankForecast,
        station_metadata: Dict[str, Dict[str, Any]],
    ) -> Optional[Tuple[float, float]]:
        """Return ``(lat, lon)`` for a forecast or ``None`` if unknown.

        Preference order:

        1. Customer_Tank forecasts expose ``location_lat`` / ``location_lon``
           directly on the TankForecast when Phase-1 customer-tank fields
           are populated (Req 1.1.2). Attribute access uses ``getattr``
           so the helper works both with current and future forecast
           schemas.
        2. Retail-station metadata (``latitude``/``longitude`` on
           ``fuel_stations``).
        3. Nested ``location`` geo_point on the station metadata
           (``{"lat": …, "lon": …}``) used by some Elasticsearch writers.
        """

        # 1) Forecast-level coordinates (Customer_Tanks).
        fc_lat = getattr(forecast, "location_lat", None)
        fc_lon = getattr(forecast, "location_lon", None)
        if fc_lat is not None and fc_lon is not None:
            try:
                return float(fc_lat), float(fc_lon)
            except (TypeError, ValueError):
                pass

        # 2–3) Station metadata fallbacks.
        meta = station_metadata.get(forecast.station_id) or {}
        lat = meta.get("latitude")
        lon = meta.get("longitude")
        if lat is None or lon is None:
            location = meta.get("location")
            if isinstance(location, dict):
                lat = location.get("lat")
                lon = location.get("lon")
        if lat is None or lon is None:
            return None
        try:
            return float(lat), float(lon)
        except (TypeError, ValueError):
            return None
