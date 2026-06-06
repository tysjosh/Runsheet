"""K-Factor Calibration Service — HDD-based consumption prediction and recalibration.

Implements the ``KFactor_Calibration_Service`` described in design §9 of the
Fuel Compliance Backbone spec. This service compares actual delivered gallons
against HDD-predicted consumption after each delivery, enabling operators to
retune K-factors for more accurate auto-fill forecasting.

Integration point: Triggered by ``order.delivered`` event (same subscriber
pattern as invoice generation). Updates ``customer_tanks`` K-factor field and
notifies ``TankForecastingAgent`` via signal bus.

All queries are tenant-scoped via ``inject_tenant_filter`` (Constraint C3).

Validates: Requirement 9.1
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from compliance.services.compliance_es_mappings import KFACTOR_HISTORY_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

# Type-only import for SignalBus to avoid circular dependency
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Agents.overlay.signal_bus import SignalBus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default variance threshold (±15%) above which a K-factor review is flagged
DEFAULT_VARIANCE_THRESHOLD_PERCENT = 15.0

# Minimum number of deliveries required before recalibration is allowed (Req 9.7)
MIN_DELIVERIES_FOR_CALIBRATION = 3


# ---------------------------------------------------------------------------
# Response / Data models
# ---------------------------------------------------------------------------


class KFactorVariance(BaseModel):
    """Result of comparing predicted vs actual gallons for a delivery.

    Computed by ``compute_variance()`` when a delivery is completed for
    an auto-fill customer. The predicted_gallons uses the customer's
    current K-factor multiplied by accumulated HDD since last delivery.

    Validates: Requirement 9.1, 9.2
    """

    model_config = ConfigDict(extra="forbid")

    delivery_id: str
    tank_id: str
    predicted_gallons: float = Field(
        ..., description="K-factor × accumulated HDD since last delivery"
    )
    actual_gallons: float = Field(
        ..., description="Actual gallons delivered"
    )
    variance_percent: float = Field(
        ...,
        description=(
            "Variance as (actual - predicted) / predicted × 100"
        ),
    )
    suggested_kfactor: Optional[float] = Field(
        default=None,
        description=(
            "Suggested revised K-factor (actual_gallons / accumulated_HDD) "
            "when variance exceeds threshold"
        ),
    )
    flagged: bool = Field(
        default=False,
        description="True when variance exceeds the configured threshold",
    )


class KFactorEntry(BaseModel):
    """Dashboard entry for a single customer tank's K-factor status.

    Used by ``get_calibration_dashboard()`` to present operators with
    tanks sorted by variance for review.

    When ``read_only`` is True, the UI should display the entry in
    read-only mode and prevent adjustment approval (Req 9.7).

    Validates: Requirement 9.4, 9.7
    """

    model_config = ConfigDict(extra="forbid")

    tank_id: str
    customer_id: str
    current_kfactor: float
    suggested_kfactor: Optional[float] = None
    variance_percent: Optional[float] = None
    last_delivery_date: Optional[date] = None
    delivery_count: int = 0
    read_only: bool = False
    read_only_reason: Optional[str] = None


class KFactorAdjustment(BaseModel):
    """Record of a K-factor adjustment approved by an operator.

    Persisted to the ``kfactor_history`` ES index for audit trail and
    trend analysis.

    Validates: Requirement 9.5, 9.6
    """

    model_config = ConfigDict(extra="forbid")

    adjustment_id: str = Field(
        default_factory=lambda: f"kfa_{uuid4()}",
        description="Server-assigned identifier of shape kfa_<uuid4>",
    )
    tank_id: str
    tenant_id: str
    old_kfactor: float
    new_kfactor: float
    operator_id: str
    timestamp: datetime = Field(default_factory=utcnow)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # ------------------------------------------------------------------
    # Uniform cross-module subject reference (cross-module-entity-linkage
    # task 10, Req 11.1). A k-factor adjustment is about a customer **tank**;
    # the uniform ``subject_ref`` is a view over the existing ``tank_id``.
    # ------------------------------------------------------------------
    @property
    def subject_ref(self) -> "SubjectRef":
        """The tank this k-factor adjustment is about, as a ``SubjectRef``."""
        from compliance.services.compliance_subject_ref import SubjectRef

        return SubjectRef(subject_type="tank", subject_id=self.tank_id)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class KFactorCalibrationService:
    """Service layer for K-factor recalibration against HDD predictions.

    Provides:
    - ``compute_variance(delivery_id)`` — computes predicted vs actual
      gallons using HDD since last delivery (Req 9.1, 9.2).
    - ``suggest_new_kfactor(tank_id)`` — suggests a revised K-factor
      when variance exceeds threshold (Req 9.3).
    - ``approve_adjustment(tank_id, new_kfactor, operator_id)`` — records
      the adjustment and notifies TankForecastingAgent (Req 9.5, 9.6).
    - ``get_calibration_dashboard(tenant_id)`` — returns tanks sorted by
      variance for operator review (Req 9.4).

    Args:
        es_service: Elasticsearch handle for querying delivery, tank, and
            kfactor_history indices.
        weather_provider: Optional provider for HDD (Heating Degree Day)
            data. When None, HDD lookups will raise NotImplementedError
            until wired.
        signal_bus: Optional SignalBus for notifying TankForecastingAgent
            when a K-factor is adjusted (Req 9.5).
        notification_service: Optional notification service for alerting
            operators of flagged variances.

    Validates: Requirement 9.1
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        weather_provider: Optional[Any] = None,
        signal_bus: Optional["SignalBus"] = None,
        notification_service: Optional[Any] = None,
        *,
        variance_threshold_percent: float = DEFAULT_VARIANCE_THRESHOLD_PERCENT,
    ) -> None:
        self._es = es_service
        self._weather_provider = weather_provider
        self._signal_bus = signal_bus
        self._notification_service = notification_service
        self._variance_threshold = variance_threshold_percent

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compute_variance(self, delivery_id: str, tenant_id: str) -> KFactorVariance:
        """Compute predicted vs actual gallons for a completed delivery.

        Retrieves the delivery record, looks up the customer tank's
        current K-factor, fetches accumulated HDD since the last delivery,
        and computes the variance percentage.

        predicted_gallons = current_kfactor × accumulated_hdd
        variance_percent = (actual - predicted) / predicted × 100

        When variance exceeds the configured threshold (default ±15%),
        the result is flagged and a suggested K-factor is computed as
        actual_gallons / accumulated_hdd.

        Args:
            delivery_id: The delivery to evaluate.
            tenant_id: Tenant scope for the query.

        Returns:
            KFactorVariance with predicted, actual, variance, and flag.

        Raises:
            ValueError: If the delivery or tank cannot be found, or if
                there is no previous delivery for this tank.
            RuntimeError: If the weather provider is not configured.

        Validates: Requirement 9.1, 9.2
        """
        # 1. Look up the delivery record from ES
        delivery = await self._get_delivery(delivery_id, tenant_id)
        if delivery is None:
            raise ValueError(
                f"Delivery '{delivery_id}' not found for tenant '{tenant_id}'"
            )

        actual_gallons = delivery.get("gallons_requested", 0.0)
        tank_id = delivery.get("customer_tank_id")
        delivery_date_raw = delivery.get("updated_at") or delivery.get("created_at")

        if not tank_id:
            raise ValueError(
                f"Delivery '{delivery_id}' has no customer_tank_id"
            )

        # Parse delivery date
        delivery_date = self._parse_date(delivery_date_raw)

        # 2. Look up the customer tank record from ES
        tank = await self._get_customer_tank(tank_id, tenant_id)
        if tank is None:
            raise ValueError(
                f"Customer tank '{tank_id}' not found for tenant '{tenant_id}'"
            )

        current_kfactor = tank.get("k_factor", 0.0)
        zip_code = tank.get("zip_code")

        if not current_kfactor or current_kfactor <= 0:
            raise ValueError(
                f"Customer tank '{tank_id}' has no valid K-factor "
                f"(current value: {current_kfactor})"
            )

        if not zip_code:
            raise ValueError(
                f"Customer tank '{tank_id}' has no zip_code for weather lookup"
            )

        # 3. Find the previous delivery date for this tank
        previous_delivery_date = await self._get_previous_delivery_date(
            tank_id, delivery_id, delivery_date, tenant_id
        )
        if previous_delivery_date is None:
            raise ValueError(
                f"No previous delivery found for tank '{tank_id}' — "
                "cannot compute variance without a baseline delivery"
            )

        # 4. Get accumulated HDD from weather provider
        if self._weather_provider is None:
            raise RuntimeError(
                "Weather provider is not configured — cannot compute "
                "accumulated HDD for K-factor variance"
            )

        accumulated_hdd = await self._get_accumulated_hdd(
            zip_code, previous_delivery_date, delivery_date, tenant_id
        )

        if accumulated_hdd <= 0:
            raise ValueError(
                f"Accumulated HDD is zero or negative ({accumulated_hdd}) "
                f"between {previous_delivery_date} and {delivery_date} — "
                "cannot compute meaningful variance"
            )

        # 5. Compute predicted_gallons = current_kfactor × accumulated_hdd
        predicted_gallons = current_kfactor * accumulated_hdd

        # 6. Compute variance_percent = (actual - predicted) / predicted × 100
        if predicted_gallons == 0:
            raise ValueError(
                "Predicted gallons is zero — cannot compute variance percentage"
            )

        variance_percent = round(
            (actual_gallons - predicted_gallons) / predicted_gallons * 100, 2
        )

        # 7. Check threshold and compute suggested K-factor if flagged
        flagged = abs(variance_percent) > self._variance_threshold
        suggested_kfactor: Optional[float] = None

        if flagged:
            suggested_kfactor = round(actual_gallons / accumulated_hdd, 4)

        logger.info(
            "KFactorCalibrationService: computed variance for delivery=%s "
            "tank=%s tenant=%s: predicted=%.2f actual=%.2f variance=%.2f%% "
            "flagged=%s",
            delivery_id,
            tank_id,
            tenant_id,
            predicted_gallons,
            actual_gallons,
            variance_percent,
            flagged,
        )

        # 8. Return KFactorVariance model
        return KFactorVariance(
            delivery_id=delivery_id,
            tank_id=tank_id,
            predicted_gallons=round(predicted_gallons, 2),
            actual_gallons=actual_gallons,
            variance_percent=variance_percent,
            suggested_kfactor=suggested_kfactor,
            flagged=flagged,
        )

    # ------------------------------------------------------------------
    # Private helpers for compute_variance
    # ------------------------------------------------------------------

    async def _get_delivery(
        self, delivery_id: str, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch a delivery (fuel order) record from ES by order_id."""
        from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

        query: Dict[str, Any] = {
            "query": {"term": {"order_id": delivery_id}}
        }
        query = inject_tenant_filter(query, tenant_id)

        try:
            resp = await self._es.search_documents(
                FUEL_ORDERS_CURRENT_INDEX, query, 1
            )
            hits = (resp or {}).get("hits", {}).get("hits", [])
            if hits:
                return hits[0].get("_source", {})
        except Exception as exc:
            logger.warning(
                "KFactorCalibrationService: failed to fetch delivery %s "
                "for tenant=%s: %s",
                delivery_id,
                tenant_id,
                exc,
            )
        return None

    async def _get_customer_tank(
        self, tank_id: str, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch a customer tank record from ES by customer_tank_id."""
        from fuel.services.fuel_ops_es_mappings import CUSTOMER_TANKS_INDEX

        query: Dict[str, Any] = {
            "query": {"term": {"customer_tank_id": tank_id}}
        }
        query = inject_tenant_filter(query, tenant_id)

        try:
            resp = await self._es.search_documents(
                CUSTOMER_TANKS_INDEX, query, 1
            )
            hits = (resp or {}).get("hits", {}).get("hits", [])
            if hits:
                return hits[0].get("_source", {})
        except Exception as exc:
            logger.warning(
                "KFactorCalibrationService: failed to fetch tank %s "
                "for tenant=%s: %s",
                tank_id,
                tenant_id,
                exc,
            )
        return None

    async def _get_previous_delivery_date(
        self,
        tank_id: str,
        current_delivery_id: str,
        current_delivery_date: date,
        tenant_id: str,
    ) -> Optional[date]:
        """Find the most recent delivery before the current one for this tank."""
        from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"customer_tank_id": tank_id}},
                        {"term": {"status": "delivered"}},
                    ],
                    "must_not": [
                        {"term": {"order_id": current_delivery_id}},
                    ],
                    "filter": [
                        {
                            "range": {
                                "updated_at": {
                                    "lt": current_delivery_date.isoformat()
                                }
                            }
                        }
                    ],
                }
            },
            "sort": [{"updated_at": {"order": "desc"}}],
        }
        query = inject_tenant_filter(query, tenant_id)

        try:
            resp = await self._es.search_documents(
                FUEL_ORDERS_CURRENT_INDEX, query, 1
            )
            hits = (resp or {}).get("hits", {}).get("hits", [])
            if hits:
                source = hits[0].get("_source", {})
                prev_date_raw = source.get("updated_at") or source.get("created_at")
                if prev_date_raw:
                    return self._parse_date(prev_date_raw)
        except Exception as exc:
            logger.warning(
                "KFactorCalibrationService: failed to find previous delivery "
                "for tank=%s tenant=%s: %s",
                tank_id,
                tenant_id,
                exc,
            )
        return None

    async def _get_accumulated_hdd(
        self,
        zip_code: str,
        from_date: date,
        to_date: date,
        tenant_id: str,
    ) -> float:
        """Get accumulated HDD between two dates using the weather provider.

        If the weather provider exposes a ``get_accumulated_hdd`` method,
        use it directly. Otherwise, fall back to fetching daily weather
        rows and summing the HDD values.
        """
        # Support both interface styles: direct get_accumulated_hdd or fetch
        if hasattr(self._weather_provider, "get_accumulated_hdd"):
            return await self._weather_provider.get_accumulated_hdd(
                zip_code, from_date, to_date, tenant_id=tenant_id
            )

        # Fall back to fetching daily weather and summing HDD
        rows = await self._weather_provider.fetch(
            zip_code, from_date, to_date, tenant_id=tenant_id
        )
        return sum(row.hdd for row in rows)

    async def _count_deliveries_for_tank(
        self, tank_id: str, tenant_id: str
    ) -> int:
        """Count the total number of delivered orders for a tank."""
        from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"customer_tank_id": tank_id}},
                        {"term": {"status": "delivered"}},
                    ]
                }
            },
        }
        query = inject_tenant_filter(query, tenant_id)

        try:
            resp = await self._es.search_documents(
                FUEL_ORDERS_CURRENT_INDEX, query, 0
            )
            total = (resp or {}).get("hits", {}).get("total", {})
            if isinstance(total, dict):
                return total.get("value", 0)
            return int(total) if total else 0
        except Exception as exc:
            logger.warning(
                "KFactorCalibrationService: failed to count deliveries "
                "for tank=%s tenant=%s: %s",
                tank_id,
                tenant_id,
                exc,
            )
            return 0

    async def _get_most_recent_delivery(
        self, tank_id: str, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get the most recent delivered order for a tank."""
        from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"customer_tank_id": tank_id}},
                        {"term": {"status": "delivered"}},
                    ]
                }
            },
            "sort": [{"updated_at": {"order": "desc"}}],
        }
        query = inject_tenant_filter(query, tenant_id)

        try:
            resp = await self._es.search_documents(
                FUEL_ORDERS_CURRENT_INDEX, query, 1
            )
            hits = (resp or {}).get("hits", {}).get("hits", [])
            if hits:
                return hits[0].get("_source", {})
        except Exception as exc:
            logger.warning(
                "KFactorCalibrationService: failed to get most recent delivery "
                "for tank=%s tenant=%s: %s",
                tank_id,
                tenant_id,
                exc,
            )
        return None

    @staticmethod
    def _parse_date(date_value: Any) -> date:
        """Parse a date value from ES (string or date object) into a date."""
        if isinstance(date_value, date) and not isinstance(date_value, datetime):
            return date_value
        if isinstance(date_value, datetime):
            return date_value.date()
        if isinstance(date_value, str):
            # Handle ISO format with or without time component
            date_str = date_value.split("T")[0]
            return date.fromisoformat(date_str)
        raise ValueError(f"Cannot parse date from: {date_value!r}")

    async def suggest_new_kfactor(self, tank_id: str, tenant_id: str) -> Optional[float]:
        """Suggest a revised K-factor for a tank based on recent deliveries.

        Computes the suggested K-factor as:
            actual_delivered_gallons / accumulated_HDD

        Only returns a suggestion when:
        - At least MIN_DELIVERIES_FOR_CALIBRATION (3) deliveries exist
        - The variance between predicted and actual exceeds the configured
          threshold (default ±15%)

        Args:
            tank_id: The customer tank to evaluate.
            tenant_id: Tenant scope for the query.

        Returns:
            Suggested K-factor float (rounded to 4 decimal places), or
            None if insufficient data, variance is within threshold, or
            weather data is unavailable.

        Validates: Requirement 9.3
        """
        # 1. Look up the customer tank to get current_kfactor and zip_code
        tank = await self._get_customer_tank(tank_id, tenant_id)
        if tank is None:
            logger.warning(
                "suggest_new_kfactor: tank '%s' not found for tenant '%s'",
                tank_id,
                tenant_id,
            )
            return None

        current_kfactor = tank.get("k_factor", 0.0)
        zip_code = tank.get("zip_code")

        if not current_kfactor or current_kfactor <= 0:
            logger.warning(
                "suggest_new_kfactor: tank '%s' has no valid K-factor (%.4f)",
                tank_id,
                current_kfactor or 0.0,
            )
            return None

        if not zip_code:
            logger.warning(
                "suggest_new_kfactor: tank '%s' has no zip_code for weather lookup",
                tank_id,
            )
            return None

        # 2. Count deliveries for this tank — must have at least MIN_DELIVERIES_FOR_CALIBRATION
        delivery_count = await self._count_deliveries_for_tank(tank_id, tenant_id)
        if delivery_count < MIN_DELIVERIES_FOR_CALIBRATION:
            logger.info(
                "suggest_new_kfactor: tank '%s' has only %d deliveries "
                "(need %d) — insufficient data",
                tank_id,
                delivery_count,
                MIN_DELIVERIES_FOR_CALIBRATION,
            )
            return None

        # 3. Get the most recent delivery for this tank
        most_recent_delivery = await self._get_most_recent_delivery(tank_id, tenant_id)
        if most_recent_delivery is None:
            logger.warning(
                "suggest_new_kfactor: no recent delivery found for tank '%s'",
                tank_id,
            )
            return None

        actual_gallons = most_recent_delivery.get("gallons_requested", 0.0)
        delivery_date_raw = most_recent_delivery.get("updated_at") or most_recent_delivery.get("created_at")
        delivery_id = most_recent_delivery.get("order_id", "")

        if not actual_gallons or actual_gallons <= 0:
            logger.warning(
                "suggest_new_kfactor: most recent delivery for tank '%s' "
                "has no valid gallons (%.2f)",
                tank_id,
                actual_gallons or 0.0,
            )
            return None

        delivery_date = self._parse_date(delivery_date_raw)

        # 4. Get the delivery before the most recent one (to compute HDD window)
        previous_delivery_date = await self._get_previous_delivery_date(
            tank_id, delivery_id, delivery_date, tenant_id
        )
        if previous_delivery_date is None:
            logger.warning(
                "suggest_new_kfactor: no previous delivery found for tank '%s' "
                "— cannot compute HDD window",
                tank_id,
            )
            return None

        # 5. Get accumulated HDD from weather provider
        if self._weather_provider is None:
            logger.warning(
                "suggest_new_kfactor: weather provider not configured — "
                "cannot compute accumulated HDD"
            )
            return None

        try:
            accumulated_hdd = await self._get_accumulated_hdd(
                zip_code, previous_delivery_date, delivery_date, tenant_id
            )
        except Exception as exc:
            logger.warning(
                "suggest_new_kfactor: weather provider failed for tank '%s': %s",
                tank_id,
                exc,
            )
            return None

        if accumulated_hdd <= 0:
            logger.info(
                "suggest_new_kfactor: accumulated HDD is zero or negative "
                "(%.2f) for tank '%s' — cannot compute suggestion",
                accumulated_hdd,
                tank_id,
            )
            return None

        # 6. Compute predicted gallons and variance
        predicted_gallons = current_kfactor * accumulated_hdd

        if predicted_gallons == 0:
            return None

        variance_percent = (actual_gallons - predicted_gallons) / predicted_gallons * 100

        # 7. Only suggest if variance exceeds threshold
        if abs(variance_percent) <= self._variance_threshold:
            logger.info(
                "suggest_new_kfactor: tank '%s' variance %.2f%% is within "
                "threshold ±%.1f%% — no suggestion",
                tank_id,
                variance_percent,
                self._variance_threshold,
            )
            return None

        # 8. Compute suggested K-factor = actual_gallons / accumulated_HDD
        suggested_kfactor = round(actual_gallons / accumulated_hdd, 4)

        logger.info(
            "suggest_new_kfactor: tank='%s' current_kfactor=%.4f "
            "suggested_kfactor=%.4f variance=%.2f%% (threshold=±%.1f%%)",
            tank_id,
            current_kfactor,
            suggested_kfactor,
            variance_percent,
            self._variance_threshold,
        )

        return suggested_kfactor

    async def approve_adjustment(
        self,
        tank_id: str,
        new_kfactor: float,
        operator_id: str,
        tenant_id: str,
    ) -> KFactorAdjustment:
        """Approve and apply a K-factor adjustment for a customer tank.

        Updates the customer tank's K-factor field, persists the
        adjustment record to the ``kfactor_history`` index, and notifies
        the TankForecastingAgent via signal bus.

        Args:
            tank_id: The customer tank to update.
            new_kfactor: The new K-factor value to apply.
            operator_id: ID of the operator approving the change.
            tenant_id: Tenant scope for the query.

        Returns:
            KFactorAdjustment record with old and new values.

        Raises:
            ValueError: If tank_id is not found or new_kfactor is invalid.

        Validates: Requirement 9.5, 9.6
        """
        # 1. Validate new_kfactor
        if new_kfactor <= 0:
            raise ValueError(
                f"new_kfactor must be positive, got {new_kfactor}"
            )

        # 2. Look up the customer tank to get the current (old) K-factor
        tank = await self._get_customer_tank(tank_id, tenant_id)
        if tank is None:
            raise ValueError(
                f"Customer tank '{tank_id}' not found for tenant '{tenant_id}'"
            )

        # 2b. Guard: reject adjustment if fewer than MIN_DELIVERIES_FOR_CALIBRATION (Req 9.7)
        delivery_count = await self._count_deliveries_for_tank(tank_id, tenant_id)
        if delivery_count < MIN_DELIVERIES_FOR_CALIBRATION:
            raise ValueError(
                f"Cannot approve K-factor adjustment for tank '{tank_id}' — "
                f"insufficient delivery data (has {delivery_count}, "
                f"requires at least {MIN_DELIVERIES_FOR_CALIBRATION})"
            )

        old_kfactor = tank.get("k_factor", 0.0)

        # 3. Create the KFactorAdjustment record
        adjustment = KFactorAdjustment(
            tank_id=tank_id,
            tenant_id=tenant_id,
            old_kfactor=old_kfactor,
            new_kfactor=new_kfactor,
            operator_id=operator_id,
        )

        # 4. Update the customer tank's k_factor field in ES
        from fuel.services.fuel_ops_es_mappings import CUSTOMER_TANKS_INDEX

        partial_update = {
            "k_factor": new_kfactor,
            "updated_at": adjustment.timestamp.isoformat(),
        }
        await self._es.update_document(
            CUSTOMER_TANKS_INDEX, tank_id, partial_update
        )

        # 5. Persist the adjustment record to kfactor_history index for audit
        adjustment_doc = adjustment.model_dump(mode="json")
        await self._es.index_document(
            KFACTOR_HISTORY_INDEX,
            adjustment.adjustment_id,
            adjustment_doc,
        )

        logger.info(
            "KFactorCalibrationService: approved K-factor adjustment "
            "tank=%s old=%.4f new=%.4f operator=%s tenant=%s",
            tank_id,
            old_kfactor,
            new_kfactor,
            operator_id,
            tenant_id,
        )

        # 6. Notify TankForecastingAgent via signal bus (if configured)
        if self._signal_bus is not None:
            try:
                from Agents.overlay.data_contracts import RiskSignal, Severity

                signal = RiskSignal(
                    source_agent="kfactor_calibration_service",
                    entity_id=tank_id,
                    entity_type="customer_tank",
                    severity=Severity.LOW,
                    confidence=1.0,
                    ttl_seconds=3600,
                    tenant_id=tenant_id,
                    context={
                        "event": "kfactor_changed",
                        "tank_id": tank_id,
                        "old_kfactor": old_kfactor,
                        "new_kfactor": new_kfactor,
                        "operator_id": operator_id,
                        "adjustment_id": adjustment.adjustment_id,
                    },
                )
                await self._signal_bus.publish(signal)
                logger.info(
                    "KFactorCalibrationService: published kfactor_changed "
                    "signal for tank=%s tenant=%s",
                    tank_id,
                    tenant_id,
                )
            except Exception as exc:
                # Non-critical — log and continue; the adjustment itself
                # is the primary action.
                logger.error(
                    "KFactorCalibrationService: failed to publish "
                    "kfactor_changed signal for tank=%s tenant=%s: %s",
                    tank_id,
                    tenant_id,
                    exc,
                )

        return adjustment

    async def get_calibration_dashboard(
        self, tenant_id: str
    ) -> List[KFactorEntry]:
        """Return K-factor calibration dashboard entries for a tenant.

        Retrieves all auto-fill customer tanks for the tenant, computes
        their current variance status, and returns them sorted by
        absolute variance (highest first) for operator review.

        Tanks with fewer than MIN_DELIVERIES_FOR_CALIBRATION deliveries
        are included but with variance_percent=None and
        suggested_kfactor=None (read-only mode per Req 9.7).

        Args:
            tenant_id: Tenant scope for the query.

        Returns:
            List of KFactorEntry sorted by absolute variance (descending).
            Tanks with None variance are placed at the end.

        Validates: Requirement 9.4
        """
        # 1. Query all auto-fill customer tanks for this tenant
        tanks = await self._get_autofill_tanks(tenant_id)

        entries: List[KFactorEntry] = []

        for tank_doc in tanks:
            tank_id = tank_doc.get("customer_tank_id", "")
            customer_id = tank_doc.get("customer_id", "")
            current_kfactor = tank_doc.get("k_factor", 0.0)

            if not tank_id:
                continue

            # 2. Count deliveries for this tank
            delivery_count = await self._count_deliveries_for_tank(
                tank_id, tenant_id
            )

            # 3. Get the most recent delivery date
            last_delivery_date: Optional[date] = None
            most_recent = await self._get_most_recent_delivery(tank_id, tenant_id)
            if most_recent:
                date_raw = most_recent.get("updated_at") or most_recent.get("created_at")
                if date_raw:
                    try:
                        last_delivery_date = self._parse_date(date_raw)
                    except (ValueError, TypeError):
                        pass

            # 4. For tanks with sufficient deliveries, compute variance
            variance_percent: Optional[float] = None
            suggested_kfactor: Optional[float] = None

            if delivery_count >= MIN_DELIVERIES_FOR_CALIBRATION:
                try:
                    suggested = await self.suggest_new_kfactor(tank_id, tenant_id)
                    if suggested is not None:
                        # Compute variance from current vs suggested
                        # variance = (suggested - current) / current * 100
                        if current_kfactor and current_kfactor > 0:
                            variance_percent = round(
                                (suggested - current_kfactor) / current_kfactor * 100,
                                2,
                            )
                        suggested_kfactor = suggested
                    else:
                        # suggest_new_kfactor returned None — variance within threshold
                        # Still compute the actual variance for display purposes
                        variance_percent = await self._compute_display_variance(
                            tank_id, tenant_id
                        )
                except Exception as exc:
                    logger.warning(
                        "get_calibration_dashboard: failed to compute variance "
                        "for tank=%s tenant=%s: %s",
                        tank_id,
                        tenant_id,
                        exc,
                    )

            # 5. Determine read-only status (Req 9.7)
            is_read_only = delivery_count < MIN_DELIVERIES_FOR_CALIBRATION
            read_only_reason: Optional[str] = None
            if is_read_only:
                read_only_reason = (
                    "Insufficient data for recalibration — requires at least "
                    f"{MIN_DELIVERIES_FOR_CALIBRATION} deliveries"
                )

            entry = KFactorEntry(
                tank_id=tank_id,
                customer_id=customer_id,
                current_kfactor=current_kfactor or 0.0,
                suggested_kfactor=suggested_kfactor,
                variance_percent=variance_percent,
                last_delivery_date=last_delivery_date,
                delivery_count=delivery_count,
                read_only=is_read_only,
                read_only_reason=read_only_reason,
            )
            entries.append(entry)

        # 5. Sort by absolute variance (highest first); None values go last
        entries.sort(
            key=lambda e: (
                0 if e.variance_percent is not None else 1,
                -(abs(e.variance_percent) if e.variance_percent is not None else 0),
            )
        )

        logger.info(
            "get_calibration_dashboard: tenant=%s returned %d entries "
            "(%d with variance data)",
            tenant_id,
            len(entries),
            sum(1 for e in entries if e.variance_percent is not None),
        )

        return entries

    # ------------------------------------------------------------------
    # Private helpers for get_calibration_dashboard
    # ------------------------------------------------------------------

    async def _get_autofill_tanks(
        self, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """Retrieve all auto-fill customer tanks for a tenant.

        Queries the customer_tanks index filtering by customer_type
        in ('auto_fill', 'keep_full') — both use HDD-based K-factor
        forecasting and are relevant for calibration review.
        """
        from fuel.services.fuel_ops_es_mappings import CUSTOMER_TANKS_INDEX

        _PAGE_SIZE = 500
        all_tanks: List[Dict[str, Any]] = []
        search_after: Optional[list] = None

        while True:
            base_query: Dict[str, Any] = {
                "query": {
                    "bool": {
                        "must": [
                            {
                                "terms": {
                                    "customer_type": ["auto_fill", "keep_full"]
                                }
                            },
                        ]
                    }
                },
                "size": _PAGE_SIZE,
                "sort": [{"customer_tank_id": {"order": "asc"}}],
            }

            if search_after is not None:
                base_query["search_after"] = search_after

            query = inject_tenant_filter(base_query, tenant_id)

            try:
                response = await self._es.search_documents(
                    CUSTOMER_TANKS_INDEX, query, size=_PAGE_SIZE
                )
            except Exception as exc:
                logger.warning(
                    "_get_autofill_tanks: ES query failed for tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                break

            hits = (response or {}).get("hits", {}).get("hits", [])
            if not hits:
                break

            for hit in hits:
                all_tanks.append(hit.get("_source", {}))

            # Set up search_after for next page
            last_sort = hits[-1].get("sort")
            if last_sort:
                search_after = last_sort
            else:
                break

            # If we got fewer than the page size, we're done
            if len(hits) < _PAGE_SIZE:
                break

        return all_tanks

    async def _compute_display_variance(
        self, tank_id: str, tenant_id: str
    ) -> Optional[float]:
        """Compute the current variance percentage for display purposes.

        Used when suggest_new_kfactor returns None (variance within threshold)
        but we still want to show the actual variance on the dashboard.
        """
        tank = await self._get_customer_tank(tank_id, tenant_id)
        if tank is None:
            return None

        current_kfactor = tank.get("k_factor", 0.0)
        zip_code = tank.get("zip_code")

        if not current_kfactor or current_kfactor <= 0 or not zip_code:
            return None

        # Get most recent delivery
        most_recent = await self._get_most_recent_delivery(tank_id, tenant_id)
        if most_recent is None:
            return None

        actual_gallons = most_recent.get("gallons_requested", 0.0)
        delivery_date_raw = most_recent.get("updated_at") or most_recent.get("created_at")
        delivery_id = most_recent.get("order_id", "")

        if not actual_gallons or actual_gallons <= 0:
            return None

        try:
            delivery_date = self._parse_date(delivery_date_raw)
        except (ValueError, TypeError):
            return None

        # Get previous delivery date
        previous_delivery_date = await self._get_previous_delivery_date(
            tank_id, delivery_id, delivery_date, tenant_id
        )
        if previous_delivery_date is None:
            return None

        # Get accumulated HDD
        if self._weather_provider is None:
            return None

        try:
            accumulated_hdd = await self._get_accumulated_hdd(
                zip_code, previous_delivery_date, delivery_date, tenant_id
            )
        except Exception:
            return None

        if accumulated_hdd <= 0:
            return None

        predicted_gallons = current_kfactor * accumulated_hdd
        if predicted_gallons == 0:
            return None

        return round(
            (actual_gallons - predicted_gallons) / predicted_gallons * 100, 2
        )
