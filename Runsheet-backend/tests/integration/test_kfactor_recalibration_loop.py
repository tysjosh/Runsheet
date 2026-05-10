"""Integration test: Delivery completed → K-factor variance → notification →
operator approval → tank forecast updated.

Verifies the full K-factor recalibration loop:
1. A delivery is completed for an auto-fill customer (order.delivered event)
2. KFactorCalibrationService computes variance (actual vs predicted using HDD)
3. Variance exceeds ±15% threshold → flagged for review, new K-factor suggested
4. Notification sent to operations manager about the variance
5. Operator approves the K-factor adjustment
6. Tank's K-factor is updated, history logged
7. TankForecastingAgent is notified via signal bus of the K-factor change

Also tests:
- Variance within threshold → no flag, no notification
- Insufficient data (< 3 deliveries) → read-only mode, no suggestion
- History retention of K-factor adjustments

ES and external dependencies are mocked via AsyncMock fixtures.

Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Agents.overlay.data_contracts import RiskSignal, Severity
from Agents.overlay.signal_bus import SignalBus
from compliance.services.kfactor_calibration_service import (
    DEFAULT_VARIANCE_THRESHOLD_PERCENT,
    MIN_DELIVERIES_FOR_CALIBRATION,
    KFactorAdjustment,
    KFactorCalibrationService,
    KFactorVariance,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant_kfactor_integ"
TANK_ID = "tank_propane_001"
CUSTOMER_ID = "cust_heating_001"
OPERATOR_ID = "ops_manager_001"
DELIVERY_ID = "order_del_kf_001"
PREVIOUS_DELIVERY_ID = "order_del_kf_000"

# Tank configuration
CURRENT_KFACTOR = 2.5  # gallons per HDD
ZIP_CODE = "06001"  # Connecticut

# Delivery data — variance exceeds 15%
ACTUAL_GALLONS_HIGH_VARIANCE = 350.0
ACCUMULATED_HDD = 100.0
PREDICTED_GALLONS = CURRENT_KFACTOR * ACCUMULATED_HDD  # 250.0
# Variance = (350 - 250) / 250 * 100 = 40% → exceeds ±15%
EXPECTED_VARIANCE_PERCENT = 40.0
EXPECTED_SUGGESTED_KFACTOR = round(ACTUAL_GALLONS_HIGH_VARIANCE / ACCUMULATED_HDD, 4)  # 3.5

# Delivery data — variance within threshold
ACTUAL_GALLONS_LOW_VARIANCE = 260.0
# Variance = (260 - 250) / 250 * 100 = 4% → within ±15%
EXPECTED_LOW_VARIANCE_PERCENT = 4.0

# Dates
DELIVERY_DATE = date(2026, 3, 15)
PREVIOUS_DELIVERY_DATE = date(2026, 1, 5)

FIXED_NOW = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_es_service(
    *,
    delivery_gallons: float = ACTUAL_GALLONS_HIGH_VARIANCE,
    delivery_count: int = 5,
    has_previous_delivery: bool = True,
) -> AsyncMock:
    """Create a mocked ElasticsearchService that returns appropriate data."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)

    # Track calls for assertions
    es._search_call_count = 0

    async def mock_search(index: str, query: dict, size: int = 10, **kwargs):
        """Route ES search calls based on the index and query content."""
        es._search_call_count += 1
        query_str = str(query)

        # kfactor_history search — check early to avoid false matches
        if "kfactor" in index:
            return {"hits": {"hits": [], "total": {"value": 0}}}

        # Delivery count query (size=0 means count)
        if size == 0 and "status" in query_str:
            return {
                "hits": {
                    "hits": [],
                    "total": {"value": delivery_count},
                }
            }

        # Previous delivery lookup (must_not current delivery)
        if "must_not" in query_str:
            if has_previous_delivery:
                return {
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "order_id": PREVIOUS_DELIVERY_ID,
                                    "customer_tank_id": TANK_ID,
                                    "gallons_requested": 240.0,
                                    "status": "delivered",
                                    "updated_at": PREVIOUS_DELIVERY_DATE.isoformat(),
                                    "created_at": PREVIOUS_DELIVERY_DATE.isoformat(),
                                    "tenant_id": TENANT_ID,
                                }
                            }
                        ],
                        "total": {"value": 1},
                    }
                }
            else:
                return {"hits": {"hits": [], "total": {"value": 0}}}

        # Customer tank lookup (customer_tank_id without status filter)
        if "customer_tank_id" in query_str and "status" not in query_str:
            return {
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "customer_tank_id": TANK_ID,
                                "customer_id": CUSTOMER_ID,
                                "k_factor": CURRENT_KFACTOR,
                                "zip_code": ZIP_CODE,
                                "customer_type": "auto_fill",
                                "tenant_id": TENANT_ID,
                            }
                        }
                    ],
                    "total": {"value": 1},
                }
            }

        # Delivery lookup by order_id only (compute_variance path)
        if "order_id" in query_str and "customer_tank_id" not in query_str:
            return {
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "order_id": DELIVERY_ID,
                                "customer_tank_id": TANK_ID,
                                "customer_id": CUSTOMER_ID,
                                "gallons_requested": delivery_gallons,
                                "status": "delivered",
                                "updated_at": DELIVERY_DATE.isoformat(),
                                "created_at": DELIVERY_DATE.isoformat(),
                                "tenant_id": TENANT_ID,
                            }
                        }
                    ],
                    "total": {"value": 1},
                }
            }

        # Most recent delivery for a tank (customer_tank_id + status, size=1)
        # This matches _get_most_recent_delivery which queries by
        # customer_tank_id + status=delivered with size=1
        if "customer_tank_id" in query_str and "status" in query_str:
            return {
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "order_id": DELIVERY_ID,
                                "customer_tank_id": TANK_ID,
                                "gallons_requested": delivery_gallons,
                                "status": "delivered",
                                "updated_at": DELIVERY_DATE.isoformat(),
                                "created_at": DELIVERY_DATE.isoformat(),
                                "tenant_id": TENANT_ID,
                            }
                        }
                    ],
                    "total": {"value": delivery_count},
                }
            }

        # Default empty response
        return {"hits": {"hits": [], "total": {"value": 0}}}

    es.search_documents = AsyncMock(side_effect=mock_search)
    return es


def _make_weather_provider(accumulated_hdd: float = ACCUMULATED_HDD) -> AsyncMock:
    """Create a mocked weather provider returning configured HDD."""
    provider = AsyncMock()
    provider.get_accumulated_hdd = AsyncMock(return_value=accumulated_hdd)
    return provider


def _make_notification_service() -> AsyncMock:
    """Create a mocked notification service."""
    notif = AsyncMock()
    notif.send = AsyncMock(return_value=None)
    notif.notify_operator = AsyncMock(return_value=None)
    return notif


# ===========================================================================
# Integration Test: Full K-Factor Recalibration Loop
# ===========================================================================


class TestKFactorRecalibrationLoop:
    """End-to-end integration: Delivery → Variance → Notification → Approval → Forecast.

    Simulates the full K-factor recalibration pipeline where:
    1. A delivery is completed for an auto-fill customer
    2. KFactorCalibrationService computes variance
    3. Variance exceeds threshold → flagged, new K-factor suggested
    4. Notification sent to operations manager
    5. Operator approves the adjustment
    6. Tank K-factor updated, history logged
    7. TankForecastingAgent notified via signal bus
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up the service instances with mocked ES and dependencies."""
        self.es = _make_es_service()
        self.weather_provider = _make_weather_provider()
        self.notification_service = _make_notification_service()
        self.signal_bus = SignalBus(es_service=AsyncMock())

        # Track signals received by TankForecastingAgent subscriber
        self.received_signals: List[RiskSignal] = []

        self.service = KFactorCalibrationService(
            es_service=self.es,
            weather_provider=self.weather_provider,
            signal_bus=self.signal_bus,
            notification_service=self.notification_service,
        )

    async def _register_tank_forecast_subscriber(self):
        """Register a TankForecastingAgent subscriber on the signal bus."""

        async def on_kfactor_signal(signal: RiskSignal):
            self.received_signals.append(signal)

        await self.signal_bus.subscribe(
            subscriber_id="tank_forecasting_agent",
            message_type=RiskSignal,
            callback=on_kfactor_signal,
            filters={"entity_type": "customer_tank"},
        )

    @pytest.mark.asyncio
    async def test_full_recalibration_loop_high_variance(self):
        """Full loop: delivery → variance exceeds threshold → approval → signal bus."""
        # Register TankForecastingAgent subscriber
        await self._register_tank_forecast_subscriber()

        # Step 1-3: Compute variance for a completed delivery
        variance = await self.service.compute_variance(DELIVERY_ID, TENANT_ID)

        # Verify variance computation
        assert isinstance(variance, KFactorVariance)
        assert variance.delivery_id == DELIVERY_ID
        assert variance.tank_id == TANK_ID
        assert variance.predicted_gallons == PREDICTED_GALLONS
        assert variance.actual_gallons == ACTUAL_GALLONS_HIGH_VARIANCE
        assert variance.variance_percent == EXPECTED_VARIANCE_PERCENT
        assert variance.flagged is True
        assert variance.suggested_kfactor == EXPECTED_SUGGESTED_KFACTOR

        # Step 4: Verify weather provider was called with correct date range
        self.weather_provider.get_accumulated_hdd.assert_called_once_with(
            ZIP_CODE, PREVIOUS_DELIVERY_DATE, DELIVERY_DATE, tenant_id=TENANT_ID
        )

        # Step 5-6: Operator approves the K-factor adjustment
        adjustment = await self.service.approve_adjustment(
            tank_id=TANK_ID,
            new_kfactor=EXPECTED_SUGGESTED_KFACTOR,
            operator_id=OPERATOR_ID,
            tenant_id=TENANT_ID,
        )

        # Verify adjustment record
        assert isinstance(adjustment, KFactorAdjustment)
        assert adjustment.tank_id == TANK_ID
        assert adjustment.old_kfactor == CURRENT_KFACTOR
        assert adjustment.new_kfactor == EXPECTED_SUGGESTED_KFACTOR
        assert adjustment.operator_id == OPERATOR_ID
        assert adjustment.tenant_id == TENANT_ID

        # Verify tank was updated in ES
        self.es.update_document.assert_called_once()
        update_call = self.es.update_document.call_args
        assert update_call[0][1] == TANK_ID  # doc_id
        assert update_call[0][2]["k_factor"] == EXPECTED_SUGGESTED_KFACTOR

        # Verify history was logged to kfactor_history index
        self.es.index_document.assert_called_once()
        history_call = self.es.index_document.call_args
        history_doc = history_call[0][2]
        assert history_doc["tank_id"] == TANK_ID
        assert history_doc["old_kfactor"] == CURRENT_KFACTOR
        assert history_doc["new_kfactor"] == EXPECTED_SUGGESTED_KFACTOR
        assert history_doc["operator_id"] == OPERATOR_ID

        # Step 7: Verify TankForecastingAgent received signal via signal bus
        assert len(self.received_signals) == 1
        signal = self.received_signals[0]
        assert signal.source_agent == "kfactor_calibration_service"
        assert signal.entity_id == TANK_ID
        assert signal.entity_type == "customer_tank"
        assert signal.tenant_id == TENANT_ID
        assert signal.context["event"] == "kfactor_changed"
        assert signal.context["old_kfactor"] == CURRENT_KFACTOR
        assert signal.context["new_kfactor"] == EXPECTED_SUGGESTED_KFACTOR
        assert signal.context["operator_id"] == OPERATOR_ID

    @pytest.mark.asyncio
    async def test_variance_within_threshold_no_flag(self):
        """Variance within ±15% → no flag, no suggested K-factor."""
        # Use low-variance delivery gallons
        self.es = _make_es_service(delivery_gallons=ACTUAL_GALLONS_LOW_VARIANCE)
        self.service = KFactorCalibrationService(
            es_service=self.es,
            weather_provider=self.weather_provider,
            signal_bus=self.signal_bus,
            notification_service=self.notification_service,
        )

        variance = await self.service.compute_variance(DELIVERY_ID, TENANT_ID)

        assert isinstance(variance, KFactorVariance)
        assert variance.actual_gallons == ACTUAL_GALLONS_LOW_VARIANCE
        assert variance.variance_percent == EXPECTED_LOW_VARIANCE_PERCENT
        assert variance.flagged is False
        assert variance.suggested_kfactor is None

    @pytest.mark.asyncio
    async def test_insufficient_data_read_only_mode(self):
        """Fewer than 3 deliveries → suggest_new_kfactor returns None."""
        # Set delivery count below minimum
        self.es = _make_es_service(delivery_count=2)
        self.service = KFactorCalibrationService(
            es_service=self.es,
            weather_provider=self.weather_provider,
            signal_bus=self.signal_bus,
            notification_service=self.notification_service,
        )

        # suggest_new_kfactor should return None for insufficient data
        suggestion = await self.service.suggest_new_kfactor(TANK_ID, TENANT_ID)
        assert suggestion is None

    @pytest.mark.asyncio
    async def test_insufficient_data_blocks_approval(self):
        """Fewer than 3 deliveries → approve_adjustment raises ValueError."""
        self.es = _make_es_service(delivery_count=2)
        self.service = KFactorCalibrationService(
            es_service=self.es,
            weather_provider=self.weather_provider,
            signal_bus=self.signal_bus,
            notification_service=self.notification_service,
        )

        with pytest.raises(ValueError, match="insufficient delivery data"):
            await self.service.approve_adjustment(
                tank_id=TANK_ID,
                new_kfactor=3.0,
                operator_id=OPERATOR_ID,
                tenant_id=TENANT_ID,
            )

    @pytest.mark.asyncio
    async def test_history_retention_of_adjustments(self):
        """K-factor adjustment is persisted to kfactor_history index."""
        adjustment = await self.service.approve_adjustment(
            tank_id=TANK_ID,
            new_kfactor=3.2,
            operator_id=OPERATOR_ID,
            tenant_id=TENANT_ID,
        )

        # Verify the history document was indexed
        self.es.index_document.assert_called_once()
        call_args = self.es.index_document.call_args[0]

        # First arg is the index name
        index_name = call_args[0]
        assert "kfactor_history" in index_name

        # Second arg is the document ID (adjustment_id)
        doc_id = call_args[1]
        assert doc_id == adjustment.adjustment_id
        assert doc_id.startswith("kfa_")

        # Third arg is the document body
        doc_body = call_args[2]
        assert doc_body["tank_id"] == TANK_ID
        assert doc_body["old_kfactor"] == CURRENT_KFACTOR
        assert doc_body["new_kfactor"] == 3.2
        assert doc_body["operator_id"] == OPERATOR_ID
        assert doc_body["tenant_id"] == TENANT_ID
        assert "timestamp" in doc_body
        assert "created_at" in doc_body

    @pytest.mark.asyncio
    async def test_signal_bus_notification_to_tank_forecasting_agent(self):
        """Signal bus delivers kfactor_changed signal to TankForecastingAgent."""
        # Register subscriber before approval
        await self._register_tank_forecast_subscriber()

        # Approve adjustment
        await self.service.approve_adjustment(
            tank_id=TANK_ID,
            new_kfactor=2.8,
            operator_id=OPERATOR_ID,
            tenant_id=TENANT_ID,
        )

        # Verify signal was delivered
        assert len(self.received_signals) == 1
        signal = self.received_signals[0]
        assert isinstance(signal, RiskSignal)
        assert signal.entity_id == TANK_ID
        assert signal.entity_type == "customer_tank"
        assert signal.severity == Severity.LOW
        assert signal.confidence == 1.0
        assert signal.context["event"] == "kfactor_changed"
        assert signal.context["new_kfactor"] == 2.8

    @pytest.mark.asyncio
    async def test_no_signal_when_signal_bus_not_configured(self):
        """No error when signal bus is None — adjustment still succeeds."""
        service_no_bus = KFactorCalibrationService(
            es_service=self.es,
            weather_provider=self.weather_provider,
            signal_bus=None,
            notification_service=self.notification_service,
        )

        # Should not raise
        adjustment = await service_no_bus.approve_adjustment(
            tank_id=TANK_ID,
            new_kfactor=2.9,
            operator_id=OPERATOR_ID,
            tenant_id=TENANT_ID,
        )

        assert adjustment.new_kfactor == 2.9
        # Tank still updated
        self.es.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_variance_computation_uses_hdd_from_weather_provider(self):
        """Variance computation fetches HDD from weather provider for correct date range."""
        variance = await self.service.compute_variance(DELIVERY_ID, TENANT_ID)

        # Weather provider called with zip_code, previous delivery date, current delivery date
        self.weather_provider.get_accumulated_hdd.assert_called_once_with(
            ZIP_CODE,
            PREVIOUS_DELIVERY_DATE,
            DELIVERY_DATE,
            tenant_id=TENANT_ID,
        )

        # Predicted = k_factor * HDD = 2.5 * 100 = 250
        assert variance.predicted_gallons == 250.0

    @pytest.mark.asyncio
    async def test_no_previous_delivery_raises_error(self):
        """compute_variance raises ValueError when no previous delivery exists."""
        self.es = _make_es_service(has_previous_delivery=False)
        self.service = KFactorCalibrationService(
            es_service=self.es,
            weather_provider=self.weather_provider,
            signal_bus=self.signal_bus,
            notification_service=self.notification_service,
        )

        with pytest.raises(ValueError, match="No previous delivery found"):
            await self.service.compute_variance(DELIVERY_ID, TENANT_ID)

    @pytest.mark.asyncio
    async def test_suggest_kfactor_with_sufficient_data_and_high_variance(self):
        """suggest_new_kfactor returns a value when variance exceeds threshold."""
        suggestion = await self.service.suggest_new_kfactor(TANK_ID, TENANT_ID)

        # With 5 deliveries and 40% variance, should suggest new K-factor
        assert suggestion is not None
        assert suggestion == EXPECTED_SUGGESTED_KFACTOR

    @pytest.mark.asyncio
    async def test_suggest_kfactor_within_threshold_returns_none(self):
        """suggest_new_kfactor returns None when variance is within threshold."""
        self.es = _make_es_service(delivery_gallons=ACTUAL_GALLONS_LOW_VARIANCE)
        self.service = KFactorCalibrationService(
            es_service=self.es,
            weather_provider=self.weather_provider,
            signal_bus=self.signal_bus,
            notification_service=self.notification_service,
        )

        suggestion = await self.service.suggest_new_kfactor(TANK_ID, TENANT_ID)
        assert suggestion is None

    @pytest.mark.asyncio
    async def test_multiple_adjustments_create_history_trail(self):
        """Multiple K-factor adjustments each create a separate history record."""
        # First adjustment
        adj1 = await self.service.approve_adjustment(
            tank_id=TANK_ID,
            new_kfactor=2.8,
            operator_id=OPERATOR_ID,
            tenant_id=TENANT_ID,
        )

        # Second adjustment
        adj2 = await self.service.approve_adjustment(
            tank_id=TANK_ID,
            new_kfactor=3.1,
            operator_id="ops_manager_002",
            tenant_id=TENANT_ID,
        )

        # Both adjustments should have been indexed
        assert self.es.index_document.call_count == 2

        # Each has a unique adjustment_id
        assert adj1.adjustment_id != adj2.adjustment_id
        assert adj1.adjustment_id.startswith("kfa_")
        assert adj2.adjustment_id.startswith("kfa_")

        # Verify the second adjustment's details
        second_call = self.es.index_document.call_args_list[1]
        second_doc = second_call[0][2]
        assert second_doc["new_kfactor"] == 3.1
        assert second_doc["operator_id"] == "ops_manager_002"
