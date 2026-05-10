"""Unit tests filling coverage gaps for K-Factor Calibration Service (Task 13.10).

Tests cover gaps identified in existing test suite:
- Variance computation with very small HDD values (near-zero but positive)
- Variance computation with very large variances (500%+)
- Threshold triggering at exact boundary (±15.0% exactly — NOT flagged)
- History retention: multiple adjustments accumulate in kfactor_history
- Insufficient data cases: 0, 1, 2 deliveries for suggest_new_kfactor
- Integration flow: compute_variance → suggest_new_kfactor → approve_adjustment

Validates: Requirements 9.1, 9.2, 9.3, 9.5, 9.6, 9.7
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, call, patch

import pytest

from compliance.services.kfactor_calibration_service import (
    DEFAULT_VARIANCE_THRESHOLD_PERCENT,
    KFactorAdjustment,
    KFactorCalibrationService,
    KFactorEntry,
    KFactorVariance,
    MIN_DELIVERIES_FOR_CALIBRATION,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_fuel_co"


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    return es


def _make_signal_bus() -> AsyncMock:
    """Create a mocked SignalBus."""
    bus = AsyncMock()
    bus.publish = AsyncMock(return_value=None)
    return bus


def _make_weather_provider() -> AsyncMock:
    """Create a mocked weather provider for HDD data."""
    provider = AsyncMock()
    provider.get_accumulated_hdd = AsyncMock(return_value=150.0)
    return provider


def _delivery_hit(
    order_id: str = "del_001",
    tank_id: str = "tank_abc",
    gallons: float = 230.0,
    updated_at: str = "2026-03-15T10:00:00",
    status: str = "delivered",
):
    """Build a mock ES hit for a fuel order (delivery)."""
    return {
        "_source": {
            "order_id": order_id,
            "customer_tank_id": tank_id,
            "gallons_requested": gallons,
            "status": status,
            "updated_at": updated_at,
            "created_at": "2026-03-10T08:00:00",
        }
    }


def _tank_hit(
    tank_id: str = "tank_abc",
    k_factor: float = 1.5,
    zip_code: str = "06001",
    customer_id: str = "cust_123",
):
    """Build a mock ES hit for a customer tank."""
    return {
        "_source": {
            "customer_tank_id": tank_id,
            "k_factor": k_factor,
            "zip_code": zip_code,
            "customer_id": customer_id,
            "tenant_id": _TENANT_ID,
        }
    }


def _prev_delivery_hit(updated_at: str = "2026-02-01T10:00:00"):
    """Build a mock ES hit for a previous delivery."""
    return {
        "_source": {
            "order_id": "del_000",
            "customer_tank_id": "tank_abc",
            "gallons_requested": 200.0,
            "status": "delivered",
            "updated_at": updated_at,
            "created_at": "2026-01-28T08:00:00",
        }
    }


# ---------------------------------------------------------------------------
# 1. Variance Computation Edge Cases
# ---------------------------------------------------------------------------


class TestVarianceEdgeCases:
    """Tests for compute_variance edge cases not covered by existing tests.

    Validates: Requirement 9.1, 9.2
    """

    @pytest.mark.asyncio
    async def test_very_small_hdd_produces_large_variance(self):
        """Very small HDD (0.5) with normal delivery produces large variance and is flagged.

        k_factor=1.5, hdd=0.5 -> predicted=0.75, actual=230
        variance = (230-0.75)/0.75*100 = 30566.67% -> flagged
        suggested = 230/0.5 = 460.0
        Validates: 9.1, 9.2
        """
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit(gallons=230.0)]}},
                {"hits": {"hits": [_tank_hit(k_factor=1.5)]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=0.5)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.compute_variance("del_001", _TENANT_ID)

        assert result.predicted_gallons == 0.75  # 1.5 * 0.5
        assert result.actual_gallons == 230.0
        assert result.variance_percent > 30000.0
        assert result.flagged is True
        assert result.suggested_kfactor == 460.0  # 230 / 0.5

    @pytest.mark.asyncio
    async def test_very_large_variance_over_500_percent(self):
        """Variance exceeding 500% is correctly computed and flagged.

        k_factor=1.0, hdd=10 -> predicted=10, actual=70
        variance = (70-10)/10*100 = 600% -> flagged
        suggested = 70/10 = 7.0
        Validates: 9.1, 9.2
        """
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit(gallons=70.0)]}},
                {"hits": {"hits": [_tank_hit(k_factor=1.0)]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=10.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.compute_variance("del_001", _TENANT_ID)

        assert result.predicted_gallons == 10.0
        assert result.actual_gallons == 70.0
        assert result.variance_percent == 600.0
        assert result.flagged is True
        assert result.suggested_kfactor == 7.0

    @pytest.mark.asyncio
    async def test_negative_hdd_raises_valueerror(self):
        """Negative accumulated HDD raises ValueError. Validates: 9.1"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit()]}},
                {"hits": {"hits": [_tank_hit()]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=-5.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        with pytest.raises(ValueError, match="zero or negative"):
            await service.compute_variance("del_001", _TENANT_ID)

    @pytest.mark.asyncio
    async def test_tank_with_zero_kfactor_raises_valueerror(self):
        """Tank with k_factor=0 raises ValueError. Validates: 9.1"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit()]}},
                {"hits": {"hits": [_tank_hit(k_factor=0.0)]}},
            ]
        )

        weather = _make_weather_provider()
        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        with pytest.raises(ValueError, match="no valid K-factor"):
            await service.compute_variance("del_001", _TENANT_ID)

    @pytest.mark.asyncio
    async def test_delivery_with_no_tank_id_raises_valueerror(self):
        """Delivery missing customer_tank_id raises ValueError. Validates: 9.1"""
        es = _make_es_service()
        # Delivery without customer_tank_id
        delivery_no_tank = {
            "_source": {
                "order_id": "del_001",
                "gallons_requested": 230.0,
                "status": "delivered",
                "updated_at": "2026-03-15T10:00:00",
                "created_at": "2026-03-10T08:00:00",
            }
        }
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [delivery_no_tank]}}
        )

        weather = _make_weather_provider()
        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        with pytest.raises(ValueError, match="no customer_tank_id"):
            await service.compute_variance("del_001", _TENANT_ID)


# ---------------------------------------------------------------------------
# 2. Threshold Triggering at Exact Boundary (±15.0%)
# ---------------------------------------------------------------------------


class TestExactThresholdBoundary:
    """Tests for exact ±15.0% threshold boundary behavior.

    The implementation uses strict greater-than: abs(variance) > threshold.
    Therefore exactly ±15.0% should NOT be flagged.

    Validates: Requirement 9.2, 9.3
    """

    @pytest.mark.asyncio
    async def test_exactly_positive_15_percent_not_flagged(self):
        """Variance of exactly +15.0% is NOT flagged (strict > comparison).

        k_factor=1.0, hdd=100 -> predicted=100, actual=115
        variance = (115-100)/100*100 = 15.0% -> NOT flagged (not > 15)
        Validates: 9.2
        """
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit(gallons=115.0)]}},
                {"hits": {"hits": [_tank_hit(k_factor=1.0)]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.compute_variance("del_001", _TENANT_ID)

        assert result.variance_percent == 15.0
        assert result.flagged is False
        assert result.suggested_kfactor is None

    @pytest.mark.asyncio
    async def test_exactly_negative_15_percent_not_flagged(self):
        """Variance of exactly -15.0% is NOT flagged (strict > comparison).

        k_factor=1.0, hdd=100 -> predicted=100, actual=85
        variance = (85-100)/100*100 = -15.0% -> NOT flagged (abs not > 15)
        Validates: 9.2
        """
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit(gallons=85.0)]}},
                {"hits": {"hits": [_tank_hit(k_factor=1.0)]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.compute_variance("del_001", _TENANT_ID)

        assert result.variance_percent == -15.0
        assert result.flagged is False
        assert result.suggested_kfactor is None

    @pytest.mark.asyncio
    async def test_just_above_positive_15_percent_is_flagged(self):
        """Variance of +15.01% IS flagged.

        k_factor=1.0, hdd=100 -> predicted=100, actual=115.01
        variance = (115.01-100)/100*100 = 15.01% -> flagged
        Validates: 9.2
        """
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit(gallons=115.01)]}},
                {"hits": {"hits": [_tank_hit(k_factor=1.0)]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.compute_variance("del_001", _TENANT_ID)

        assert result.variance_percent == pytest.approx(15.01, abs=0.01)
        assert result.flagged is True
        assert result.suggested_kfactor is not None

    @pytest.mark.asyncio
    async def test_just_below_negative_15_percent_is_flagged(self):
        """Variance of -15.01% IS flagged.

        k_factor=1.0, hdd=100 -> predicted=100, actual=84.99
        variance = (84.99-100)/100*100 = -15.01% -> flagged
        Validates: 9.2
        """
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit(gallons=84.99)]}},
                {"hits": {"hits": [_tank_hit(k_factor=1.0)]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.compute_variance("del_001", _TENANT_ID)

        assert result.variance_percent == pytest.approx(-15.01, abs=0.01)
        assert result.flagged is True
        assert result.suggested_kfactor is not None

    @pytest.mark.asyncio
    async def test_suggest_new_kfactor_exactly_at_threshold_returns_none(self):
        """suggest_new_kfactor returns None when variance is exactly ±15%.

        k_factor=1.0, hdd=100 -> predicted=100, actual=115
        variance = 15.0% -> within threshold (not > 15) -> None
        Validates: 9.3
        """
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.0, zip_code="06001")]}},
                # _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 5}}},
                # _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=115.0, updated_at="2026-03-15T10:00:00")]}},
                # _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)

        assert result is None


# ---------------------------------------------------------------------------
# 3. History Retention — kfactor_history index population
# ---------------------------------------------------------------------------


class TestHistoryRetention:
    """Tests verifying kfactor_history index is populated correctly.

    Validates: Requirement 9.6
    """

    @pytest.mark.asyncio
    async def test_multiple_adjustments_each_persisted_to_history(self):
        """Multiple approve_adjustment calls each write to kfactor_history.

        Validates: 9.6
        """
        es = _make_es_service()

        # Each approve_adjustment call needs: _get_customer_tank + _count_deliveries
        es.search_documents = AsyncMock(
            side_effect=[
                # First adjustment
                {"hits": {"hits": [_tank_hit(k_factor=1.35)]}},
                {"hits": {"hits": [], "total": {"value": 5}}},
                # Second adjustment
                {"hits": {"hits": [_tank_hit(k_factor=1.50)]}},
                {"hits": {"hits": [], "total": {"value": 6}}},
                # Third adjustment
                {"hits": {"hits": [_tank_hit(k_factor=1.42)]}},
                {"hits": {"hits": [], "total": {"value": 7}}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        # Perform three adjustments
        r1 = await service.approve_adjustment("tank_abc", 1.50, "op_smith", _TENANT_ID)
        r2 = await service.approve_adjustment("tank_abc", 1.42, "op_jones", _TENANT_ID)
        r3 = await service.approve_adjustment("tank_abc", 1.55, "op_smith", _TENANT_ID)

        # Verify index_document was called 3 times for kfactor_history
        assert es.index_document.call_count == 3

        # Verify each call wrote to kfactor_history with correct data
        calls = es.index_document.call_args_list

        # First adjustment: 1.35 -> 1.50
        assert calls[0][0][0] == "kfactor_history"
        assert calls[0][0][2]["old_kfactor"] == 1.35
        assert calls[0][0][2]["new_kfactor"] == 1.50
        assert calls[0][0][2]["operator_id"] == "op_smith"

        # Second adjustment: 1.50 -> 1.42
        assert calls[1][0][0] == "kfactor_history"
        assert calls[1][0][2]["old_kfactor"] == 1.50
        assert calls[1][0][2]["new_kfactor"] == 1.42
        assert calls[1][0][2]["operator_id"] == "op_jones"

        # Third adjustment: 1.42 -> 1.55
        assert calls[2][0][0] == "kfactor_history"
        assert calls[2][0][2]["old_kfactor"] == 1.42
        assert calls[2][0][2]["new_kfactor"] == 1.55
        assert calls[2][0][2]["operator_id"] == "op_smith"

    @pytest.mark.asyncio
    async def test_history_record_contains_all_required_fields(self):
        """History record contains tank_id, tenant_id, old/new kfactor, operator, timestamp.

        Validates: 9.6
        """
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_tank_hit(k_factor=1.35)]}},
                {"hits": {"hits": [], "total": {"value": 5}}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        result = await service.approve_adjustment(
            "tank_abc", 1.50, "op_smith", _TENANT_ID
        )

        # Verify the document written to kfactor_history
        call_args = es.index_document.call_args
        doc = call_args[0][2]

        assert doc["tank_id"] == "tank_abc"
        assert doc["tenant_id"] == _TENANT_ID
        assert doc["old_kfactor"] == 1.35
        assert doc["new_kfactor"] == 1.50
        assert doc["operator_id"] == "op_smith"
        assert "timestamp" in doc or "created_at" in doc
        assert doc.get("adjustment_id", "").startswith("kfa_")

    @pytest.mark.asyncio
    async def test_each_history_record_has_unique_adjustment_id(self):
        """Each history record gets a unique adjustment_id.

        Validates: 9.6
        """
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_tank_hit(k_factor=1.35)]}},
                {"hits": {"hits": [], "total": {"value": 5}}},
                {"hits": {"hits": [_tank_hit(k_factor=1.50)]}},
                {"hits": {"hits": [], "total": {"value": 6}}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        r1 = await service.approve_adjustment("tank_abc", 1.50, "op_smith", _TENANT_ID)
        r2 = await service.approve_adjustment("tank_abc", 1.60, "op_smith", _TENANT_ID)

        # Each adjustment should have a unique ID
        assert r1.adjustment_id != r2.adjustment_id
        assert r1.adjustment_id.startswith("kfa_")
        assert r2.adjustment_id.startswith("kfa_")

        # Verify the doc_id used in index_document is the adjustment_id
        calls = es.index_document.call_args_list
        assert calls[0][0][1] == r1.adjustment_id
        assert calls[1][0][1] == r2.adjustment_id


# ---------------------------------------------------------------------------
# 4. Insufficient Data Cases (0, 1, 2 deliveries)
# ---------------------------------------------------------------------------


class TestInsufficientDataCases:
    """Tests for insufficient delivery data across all methods.

    Validates: Requirement 9.7
    """

    @pytest.mark.asyncio
    async def test_suggest_kfactor_with_zero_deliveries(self):
        """suggest_new_kfactor returns None with 0 deliveries. Validates: 9.7"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.5, zip_code="06001")]}},
                # _count_deliveries_for_tank — 0 deliveries
                {"hits": {"hits": [], "total": {"value": 0}}},
            ]
        )

        weather = _make_weather_provider()
        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)

        assert result is None

    @pytest.mark.asyncio
    async def test_suggest_kfactor_with_one_delivery(self):
        """suggest_new_kfactor returns None with 1 delivery. Validates: 9.7"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.5, zip_code="06001")]}},
                # _count_deliveries_for_tank — 1 delivery
                {"hits": {"hits": [], "total": {"value": 1}}},
            ]
        )

        weather = _make_weather_provider()
        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)

        assert result is None

    @pytest.mark.asyncio
    async def test_suggest_kfactor_with_two_deliveries(self):
        """suggest_new_kfactor returns None with 2 deliveries. Validates: 9.7"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.5, zip_code="06001")]}},
                # _count_deliveries_for_tank — 2 deliveries
                {"hits": {"hits": [], "total": {"value": 2}}},
            ]
        )

        weather = _make_weather_provider()
        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)

        assert result is None

    @pytest.mark.asyncio
    async def test_approve_adjustment_with_one_delivery_rejected(self):
        """approve_adjustment raises ValueError with 1 delivery. Validates: 9.7"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_tank_hit(k_factor=1.35)]}},
                {"hits": {"hits": [], "total": {"value": 1}}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        with pytest.raises(ValueError, match="insufficient delivery data"):
            await service.approve_adjustment(
                "tank_abc", 1.50, "op_smith", _TENANT_ID
            )

    @pytest.mark.asyncio
    async def test_dashboard_one_delivery_is_read_only(self):
        """Dashboard entry with 1 delivery is read-only. Validates: 9.7"""
        es = _make_es_service()

        def _autofill_tank_hit(tank_id="tank_001", k_factor=1.5):
            return {
                "_source": {
                    "customer_tank_id": tank_id,
                    "customer_id": "cust_abc",
                    "k_factor": k_factor,
                    "zip_code": "06001",
                    "customer_type": "auto_fill",
                    "tenant_id": _TENANT_ID,
                },
                "sort": [tank_id],
            }

        es.search_documents = AsyncMock(
            side_effect=[
                # _get_autofill_tanks
                {"hits": {"hits": [_autofill_tank_hit(tank_id="tank_one")]}},
                # _count_deliveries_for_tank — 1 delivery
                {"hits": {"hits": [], "total": {"value": 1}}},
                # _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=100.0, updated_at="2026-03-10T10:00:00")]}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert len(result) == 1
        entry = result[0]
        assert entry.delivery_count == 1
        assert entry.read_only is True
        assert "3 deliveries" in entry.read_only_reason


# ---------------------------------------------------------------------------
# 5. Integration: compute_variance → suggest_new_kfactor → approve_adjustment
# ---------------------------------------------------------------------------


class TestIntegrationFlow:
    """Integration-style tests verifying components work together.

    Tests the full flow: a delivery triggers variance computation,
    which leads to a suggestion, which is then approved.

    Validates: Requirements 9.1, 9.2, 9.3, 9.5, 9.6
    """

    @pytest.mark.asyncio
    async def test_full_flow_variance_to_suggestion_to_approval(self):
        """Full flow: compute_variance flags -> suggest_new_kfactor returns value -> approve_adjustment persists.

        Scenario: k_factor=1.5, hdd=100, actual=200
        - compute_variance: predicted=150, actual=200, variance=33.33%, flagged=True, suggested=2.0
        - suggest_new_kfactor: returns 2.0
        - approve_adjustment: updates tank to 2.0, logs history

        Validates: 9.1, 9.2, 9.3, 9.5, 9.6
        """
        es = _make_es_service()
        weather = _make_weather_provider()
        bus = _make_signal_bus()

        # --- Step 1: compute_variance ---
        es.search_documents = AsyncMock(
            side_effect=[
                # compute_variance: get delivery
                {"hits": {"hits": [_delivery_hit(gallons=200.0)]}},
                # compute_variance: get tank
                {"hits": {"hits": [_tank_hit(k_factor=1.5)]}},
                # compute_variance: get previous delivery
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather, signal_bus=bus
        )

        variance = await service.compute_variance("del_001", _TENANT_ID)

        assert variance.flagged is True
        assert variance.variance_percent == pytest.approx(33.33, abs=0.01)
        assert variance.suggested_kfactor == 2.0
        assert variance.tank_id == "tank_abc"

        # --- Step 2: suggest_new_kfactor ---
        es.search_documents = AsyncMock(
            side_effect=[
                # _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.5, zip_code="06001")]}},
                # _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 5}}},
                # _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        suggestion = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)

        assert suggestion == 2.0

        # --- Step 3: approve_adjustment ---
        es.search_documents = AsyncMock(
            side_effect=[
                # _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.5)]}},
                # _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 5}}},
            ]
        )
        es.update_document = AsyncMock(return_value=None)
        es.index_document = AsyncMock(return_value=None)

        adjustment = await service.approve_adjustment(
            "tank_abc", suggestion, "op_smith", _TENANT_ID
        )

        # Verify adjustment record
        assert adjustment.tank_id == "tank_abc"
        assert adjustment.old_kfactor == 1.5
        assert adjustment.new_kfactor == 2.0
        assert adjustment.operator_id == "op_smith"

        # Verify tank was updated in ES
        es.update_document.assert_called_once()
        update_args = es.update_document.call_args[0]
        assert update_args[0] == "customer_tanks"
        assert update_args[1] == "tank_abc"
        assert update_args[2]["k_factor"] == 2.0

        # Verify history was persisted
        es.index_document.assert_called_once()
        history_args = es.index_document.call_args[0]
        assert history_args[0] == "kfactor_history"
        assert history_args[2]["old_kfactor"] == 1.5
        assert history_args[2]["new_kfactor"] == 2.0

        # Verify signal bus was notified
        bus.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_flow_variance_within_threshold_no_suggestion(self):
        """When variance is within threshold, suggest_new_kfactor returns None.

        Scenario: k_factor=1.5, hdd=150, actual=160
        - compute_variance: predicted=225, actual=160, variance=-28.89% -> flagged
        Wait — let's use a within-threshold scenario:
        - k_factor=1.5, hdd=150, actual=230
        - predicted=225, variance=(230-225)/225*100=2.22% -> NOT flagged

        Validates: 9.2, 9.3
        """
        es = _make_es_service()
        weather = _make_weather_provider()

        # compute_variance
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit(gallons=230.0)]}},
                {"hits": {"hits": [_tank_hit(k_factor=1.5)]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )
        weather.get_accumulated_hdd = AsyncMock(return_value=150.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        variance = await service.compute_variance("del_001", _TENANT_ID)

        assert variance.flagged is False
        assert variance.suggested_kfactor is None

        # suggest_new_kfactor should also return None
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_tank_hit(k_factor=1.5, zip_code="06001")]}},
                {"hits": {"hits": [], "total": {"value": 5}}},
                {"hits": {"hits": [_delivery_hit(gallons=230.0, updated_at="2026-03-15T10:00:00")]}},
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        suggestion = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)

        assert suggestion is None

    @pytest.mark.asyncio
    async def test_flow_insufficient_data_blocks_approval(self):
        """When tank has < 3 deliveries, suggest returns None and approve raises.

        Validates: 9.3, 9.7
        """
        es = _make_es_service()
        weather = _make_weather_provider()

        # suggest_new_kfactor with 2 deliveries
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_tank_hit(k_factor=1.5, zip_code="06001")]}},
                {"hits": {"hits": [], "total": {"value": 2}}},
            ]
        )

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        suggestion = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)
        assert suggestion is None

        # Even if operator tries to force an adjustment, it should be rejected
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_tank_hit(k_factor=1.5)]}},
                {"hits": {"hits": [], "total": {"value": 2}}},
            ]
        )

        with pytest.raises(ValueError, match="insufficient delivery data"):
            await service.approve_adjustment(
                "tank_abc", 2.0, "op_smith", _TENANT_ID
            )
