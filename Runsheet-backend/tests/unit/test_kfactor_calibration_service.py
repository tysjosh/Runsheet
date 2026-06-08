"""Unit tests for KFactorCalibrationService — initialization, models, and compute_variance.

Tests cover:
- Service initialization with required and optional dependencies
- KFactorVariance model creation and validation
- KFactorEntry model creation and defaults
- KFactorAdjustment model creation with auto-generated fields
- compute_variance: normal variance computation
- compute_variance: flagged variance (exceeds threshold)
- compute_variance: below-threshold variance (not flagged)
- compute_variance: no previous delivery (raises ValueError)
- compute_variance: weather provider failure (raises RuntimeError)
- Skeleton methods raise NotImplementedError (pending Tasks 13.3–13.5)

Validates: Requirement 9.1, 9.2
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

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
# Fixtures / Helpers
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


def _make_notification_service() -> AsyncMock:
    """Create a mocked notification service."""
    return AsyncMock()


# ---------------------------------------------------------------------------
# Service Initialization Tests
# ---------------------------------------------------------------------------


class TestKFactorCalibrationServiceInit:
    """Tests for KFactorCalibrationService.__init__."""

    def test_init_with_required_args_only(self):
        """Service initializes with only es_service (all optionals default to None)."""
        es = _make_es_service()
        service = KFactorCalibrationService(es_service=es)

        assert service._es is es
        assert service._weather_provider is None
        assert service._signal_bus is None
        assert service._notification_service is None
        assert service._variance_threshold == DEFAULT_VARIANCE_THRESHOLD_PERCENT

    def test_init_with_all_optional_args(self):
        """Service initializes with all optional dependencies provided."""
        es = _make_es_service()
        weather = _make_weather_provider()
        bus = _make_signal_bus()
        notif = _make_notification_service()

        service = KFactorCalibrationService(
            es_service=es,
            weather_provider=weather,
            signal_bus=bus,
            notification_service=notif,
        )

        assert service._es is es
        assert service._weather_provider is weather
        assert service._signal_bus is bus
        assert service._notification_service is notif

    def test_init_custom_variance_threshold(self):
        """Service accepts a custom variance threshold."""
        es = _make_es_service()
        service = KFactorCalibrationService(
            es_service=es,
            variance_threshold_percent=10.0,
        )

        assert service._variance_threshold == 10.0

    def test_default_variance_threshold_is_15_percent(self):
        """Default variance threshold is 15% per Req 9.3."""
        assert DEFAULT_VARIANCE_THRESHOLD_PERCENT == 15.0

    def test_min_deliveries_for_calibration_is_3(self):
        """Minimum deliveries for calibration is 3 per Req 9.7."""
        assert MIN_DELIVERIES_FOR_CALIBRATION == 3


# ---------------------------------------------------------------------------
# KFactorVariance Model Tests
# ---------------------------------------------------------------------------


class TestKFactorVarianceModel:
    """Tests for the KFactorVariance Pydantic model."""

    def test_create_basic_variance(self):
        """KFactorVariance can be created with required fields."""
        variance = KFactorVariance(
            delivery_id="del_001",
            tank_id="tank_abc",
            predicted_gallons=200.0,
            actual_gallons=230.0,
            variance_percent=15.0,
        )

        assert variance.delivery_id == "del_001"
        assert variance.tank_id == "tank_abc"
        assert variance.predicted_gallons == 200.0
        assert variance.actual_gallons == 230.0
        assert variance.variance_percent == 15.0
        assert variance.suggested_kfactor is None
        assert variance.flagged is False

    def test_create_flagged_variance_with_suggestion(self):
        """KFactorVariance with flagged=True and suggested_kfactor."""
        variance = KFactorVariance(
            delivery_id="del_002",
            tank_id="tank_xyz",
            predicted_gallons=180.0,
            actual_gallons=220.0,
            variance_percent=22.2,
            suggested_kfactor=1.47,
            flagged=True,
        )

        assert variance.flagged is True
        assert variance.suggested_kfactor == 1.47
        assert variance.variance_percent == 22.2

    def test_variance_rejects_extra_fields(self):
        """KFactorVariance rejects unknown fields (extra='forbid')."""
        with pytest.raises(Exception):
            KFactorVariance(
                delivery_id="del_001",
                tank_id="tank_abc",
                predicted_gallons=200.0,
                actual_gallons=230.0,
                variance_percent=15.0,
                unknown_field="bad",
            )


# ---------------------------------------------------------------------------
# KFactorEntry Model Tests
# ---------------------------------------------------------------------------


class TestKFactorEntryModel:
    """Tests for the KFactorEntry Pydantic model."""

    def test_create_basic_entry(self):
        """KFactorEntry can be created with required fields and defaults."""
        entry = KFactorEntry(
            tank_id="tank_001",
            customer_id="cust_abc",
            current_kfactor=1.35,
        )

        assert entry.tank_id == "tank_001"
        assert entry.customer_id == "cust_abc"
        assert entry.current_kfactor == 1.35
        assert entry.suggested_kfactor is None
        assert entry.variance_percent is None
        assert entry.last_delivery_date is None
        assert entry.delivery_count == 0

    def test_create_full_entry(self):
        """KFactorEntry with all fields populated."""
        entry = KFactorEntry(
            tank_id="tank_002",
            customer_id="cust_xyz",
            current_kfactor=1.50,
            suggested_kfactor=1.42,
            variance_percent=-5.3,
            last_delivery_date=date(2026, 5, 15),
            delivery_count=7,
        )

        assert entry.suggested_kfactor == 1.42
        assert entry.variance_percent == -5.3
        assert entry.last_delivery_date == date(2026, 5, 15)
        assert entry.delivery_count == 7

    def test_entry_rejects_extra_fields(self):
        """KFactorEntry rejects unknown fields (extra='forbid')."""
        with pytest.raises(Exception):
            KFactorEntry(
                tank_id="tank_001",
                customer_id="cust_abc",
                current_kfactor=1.35,
                extra_field="bad",
            )


# ---------------------------------------------------------------------------
# KFactorAdjustment Model Tests
# ---------------------------------------------------------------------------


class TestKFactorAdjustmentModel:
    """Tests for the KFactorAdjustment Pydantic model."""

    def test_create_adjustment(self):
        """KFactorAdjustment can be created with required fields."""
        adj = KFactorAdjustment(
            tank_id="tank_001",
            tenant_id=_TENANT_ID,
            old_kfactor=1.35,
            new_kfactor=1.42,
            operator_id="op_smith",
        )

        assert adj.tank_id == "tank_001"
        assert adj.tenant_id == _TENANT_ID
        assert adj.old_kfactor == 1.35
        assert adj.new_kfactor == 1.42
        assert adj.operator_id == "op_smith"
        # Auto-generated fields
        assert adj.adjustment_id.startswith("kfa_")
        assert isinstance(adj.timestamp, datetime)
        assert adj.timestamp.tzinfo is not None

    def test_adjustment_id_auto_generated(self):
        """Each KFactorAdjustment gets a unique auto-generated ID."""
        adj1 = KFactorAdjustment(
            tank_id="tank_001",
            tenant_id=_TENANT_ID,
            old_kfactor=1.0,
            new_kfactor=1.1,
            operator_id="op_1",
        )
        adj2 = KFactorAdjustment(
            tank_id="tank_001",
            tenant_id=_TENANT_ID,
            old_kfactor=1.1,
            new_kfactor=1.2,
            operator_id="op_1",
        )

        assert adj1.adjustment_id != adj2.adjustment_id

    def test_adjustment_rejects_extra_fields(self):
        """KFactorAdjustment rejects unknown fields (extra='forbid')."""
        with pytest.raises(Exception):
            KFactorAdjustment(
                tank_id="tank_001",
                tenant_id=_TENANT_ID,
                old_kfactor=1.0,
                new_kfactor=1.1,
                operator_id="op_1",
                unknown="bad",
            )


# ---------------------------------------------------------------------------
# Skeleton Method Tests (NotImplementedError)
# ---------------------------------------------------------------------------


class TestSkeletonMethods:
    """Verify skeleton methods raise NotImplementedError until implemented."""

    @pytest.fixture
    def service(self):
        """Create a service instance with mocked dependencies."""
        return KFactorCalibrationService(es_service=_make_es_service())


# ---------------------------------------------------------------------------
# get_calibration_dashboard Tests — Validates: Requirement 9.4
# ---------------------------------------------------------------------------


def _autofill_tank_hit(
    tank_id: str = "tank_001",
    customer_id: str = "cust_abc",
    k_factor: float = 1.5,
    zip_code: str = "06001",
    customer_type: str = "auto_fill",
):
    """Build a mock ES hit for an auto-fill customer tank.

    Carries the full set of ``CustomerTank`` fields (not just the handful the
    dashboard reads) so it round-trips through the model-validation guard in
    ``get_calibration_dashboard`` exactly as a real ``customer_tanks`` _source
    document would.
    """
    return {
        "_source": {
            "customer_tank_id": tank_id,
            "customer_id": customer_id,
            "k_factor": k_factor,
            "zip_code": zip_code,
            "customer_type": customer_type,
            "tenant_id": _TENANT_ID,
            "fuel_type": "propane",
            "fuel_product_code": "PROPANE",
            "capacity_gallons": 1000.0,
            "current_level_gallons": 400.0,
            "location_lat": 41.88,
            "location_lon": -87.62,
            "status": "active",
        },
        "sort": [tank_id],
    }


class TestGetCalibrationDashboard:
    """Tests for KFactorCalibrationService.get_calibration_dashboard.

    Validates: Requirement 9.4
    """

    @pytest.mark.asyncio
    async def test_empty_fleet_returns_empty_list(self):
        """Returns empty list when no auto-fill tanks exist. Validates: 9.4"""
        es = _make_es_service()
        # _get_autofill_tanks returns no hits
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        service = KFactorCalibrationService(es_service=es)

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert result == []

    @pytest.mark.asyncio
    async def test_skips_tank_failing_model_validation(self):
        """A tank whose stored doc fails CustomerTank validation is dropped.

        The detail endpoint (GET /fuel/mvp/customer-tanks/{id}) validates
        against the same strict model and 404s on failure, so listing such a
        tank would render an un-navigable drill-in row. The dashboard must
        skip it so the two views stay consistent.
        """
        es = _make_es_service()

        # One valid tank and one with an out-of-enum use_case (which the
        # strict CustomerTank model rejects). Only the valid one should
        # survive into the dashboard.
        valid = _autofill_tank_hit(tank_id="tank_ok", k_factor=1.5)
        invalid = _autofill_tank_hit(tank_id="tank_bad", k_factor=1.5)
        invalid["_source"]["use_case"] = "auto_fill"  # not a valid UseCase

        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_autofill_tanks — returns both tanks
                {"hits": {"hits": [valid, invalid]}},
                # 2. _count_deliveries_for_tank for the valid tank
                {"hits": {"hits": [], "total": {"value": 1}}},
                # 3. _get_most_recent_delivery for the valid tank
                {"hits": {"hits": []}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert [e.tank_id for e in result] == ["tank_ok"]
        """Tank with ≥3 deliveries includes variance and suggested kfactor. Validates: 9.4"""
        es = _make_es_service()

        # Call sequence:
        # 1. _get_autofill_tanks (paginated query)
        # 2. _count_deliveries_for_tank
        # 3. _get_most_recent_delivery (for last_delivery_date)
        # 4. suggest_new_kfactor calls: _get_customer_tank, _count_deliveries,
        #    _get_most_recent_delivery, _get_previous_delivery_date
        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_autofill_tanks
                {"hits": {"hits": [_autofill_tank_hit(tank_id="tank_001", k_factor=1.5)]}},
                # 2. _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 3. _get_most_recent_delivery (for last_delivery_date)
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # 4. suggest_new_kfactor → _get_customer_tank
                {"hits": {"hits": [_tank_hit(tank_id="tank_001", k_factor=1.5, zip_code="06001")]}},
                # 5. suggest_new_kfactor → _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 6. suggest_new_kfactor → _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # 7. suggest_new_kfactor → _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        weather = _make_weather_provider()
        # k_factor=1.5, hdd=100 → predicted=150, actual=200
        # variance = (200-150)/150*100 = 33.33% → exceeds 15% → suggested = 200/100 = 2.0
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert len(result) == 1
        entry = result[0]
        assert entry.tank_id == "tank_001"
        assert entry.customer_id == "cust_abc"
        assert entry.current_kfactor == 1.5
        assert entry.suggested_kfactor == 2.0
        assert entry.variance_percent is not None
        assert entry.delivery_count == 5
        assert entry.last_delivery_date == date(2026, 3, 15)

    @pytest.mark.asyncio
    async def test_tank_with_insufficient_data_has_none_variance(self):
        """Tank with <3 deliveries has variance_percent=None and suggested_kfactor=None. Validates: 9.4, 9.7"""
        es = _make_es_service()

        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_autofill_tanks
                {"hits": {"hits": [_autofill_tank_hit(tank_id="tank_002", k_factor=1.2)]}},
                # 2. _count_deliveries_for_tank — only 2 deliveries
                {"hits": {"hits": [], "total": {"value": 2}}},
                # 3. _get_most_recent_delivery (for last_delivery_date)
                {"hits": {"hits": [_delivery_hit(gallons=100.0, updated_at="2026-03-10T10:00:00")]}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert len(result) == 1
        entry = result[0]
        assert entry.tank_id == "tank_002"
        assert entry.current_kfactor == 1.2
        assert entry.suggested_kfactor is None
        assert entry.variance_percent is None
        assert entry.delivery_count == 2
        assert entry.last_delivery_date == date(2026, 3, 10)

    @pytest.mark.asyncio
    async def test_sorting_by_absolute_variance_descending(self):
        """Tanks are sorted by absolute variance (highest first). Validates: 9.4"""
        es = _make_es_service()

        # Two tanks: one with high variance (exceeds threshold), one with low variance (within threshold)
        # For tank_low: suggest_new_kfactor returns None (within threshold),
        #   then _compute_display_variance is called
        # For tank_high: suggest_new_kfactor returns a suggestion (exceeds threshold)
        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_autofill_tanks — two tanks
                {"hits": {"hits": [
                    _autofill_tank_hit(tank_id="tank_low", k_factor=1.5, customer_id="cust_1"),
                    _autofill_tank_hit(tank_id="tank_high", k_factor=2.0, customer_id="cust_2"),
                ]}},
                # --- tank_low processing ---
                # 2. _count_deliveries_for_tank (tank_low)
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 3. _get_most_recent_delivery (tank_low) for last_delivery_date
                {"hits": {"hits": [_delivery_hit(gallons=160.0, updated_at="2026-03-15T10:00:00")]}},
                # 4. suggest_new_kfactor(tank_low) → _get_customer_tank
                {"hits": {"hits": [_tank_hit(tank_id="tank_low", k_factor=1.5, zip_code="06001")]}},
                # 5. suggest_new_kfactor(tank_low) → _count_deliveries
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 6. suggest_new_kfactor(tank_low) → _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=160.0, updated_at="2026-03-15T10:00:00")]}},
                # 7. suggest_new_kfactor(tank_low) → _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
                # suggest_new_kfactor returns None (6.67% within 15%)
                # 8. _compute_display_variance(tank_low) → _get_customer_tank
                {"hits": {"hits": [_tank_hit(tank_id="tank_low", k_factor=1.5, zip_code="06001")]}},
                # 9. _compute_display_variance(tank_low) → _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=160.0, updated_at="2026-03-15T10:00:00")]}},
                # 10. _compute_display_variance(tank_low) → _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
                # --- tank_high processing ---
                # 11. _count_deliveries_for_tank (tank_high)
                {"hits": {"hits": [], "total": {"value": 4}}},
                # 12. _get_most_recent_delivery (tank_high) for last_delivery_date
                {"hits": {"hits": [_delivery_hit(gallons=300.0, updated_at="2026-03-15T10:00:00")]}},
                # 13. suggest_new_kfactor(tank_high) → _get_customer_tank
                {"hits": {"hits": [_tank_hit(tank_id="tank_high", k_factor=2.0, zip_code="06001")]}},
                # 14. suggest_new_kfactor(tank_high) → _count_deliveries
                {"hits": {"hits": [], "total": {"value": 4}}},
                # 15. suggest_new_kfactor(tank_high) → _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=300.0, updated_at="2026-03-15T10:00:00")]}},
                # 16. suggest_new_kfactor(tank_high) → _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        weather = _make_weather_provider()
        # For tank_low: k=1.5, hdd=100 → predicted=150, actual=160
        #   variance = (160-150)/150*100 = 6.67% → within threshold → no suggestion
        #   _compute_display_variance also uses hdd=100 → variance=6.67%
        # For tank_high: k=2.0, hdd=100 → predicted=200, actual=300
        #   variance = (300-200)/200*100 = 50% → exceeds threshold → suggested=3.0
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert len(result) == 2
        # tank_high should be first (50% absolute variance > 6.67%)
        assert result[0].tank_id == "tank_high"
        assert result[0].suggested_kfactor == 3.0
        # tank_low should be second (lower variance, within threshold)
        assert result[1].tank_id == "tank_low"
        assert result[1].variance_percent is not None

    @pytest.mark.asyncio
    async def test_mixed_tanks_sufficient_and_insufficient(self):
        """Mixed fleet: tanks with sufficient data sorted first, insufficient last. Validates: 9.4, 9.7"""
        es = _make_es_service()

        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_autofill_tanks — two tanks
                {"hits": {"hits": [
                    _autofill_tank_hit(tank_id="tank_new", k_factor=1.0, customer_id="cust_new"),
                    _autofill_tank_hit(tank_id="tank_old", k_factor=1.5, customer_id="cust_old"),
                ]}},
                # --- tank_new processing ---
                # 2. _count_deliveries_for_tank (tank_new) — only 1 delivery
                {"hits": {"hits": [], "total": {"value": 1}}},
                # 3. _get_most_recent_delivery (tank_new)
                {"hits": {"hits": [_delivery_hit(gallons=50.0, updated_at="2026-03-01T10:00:00")]}},
                # --- tank_old processing ---
                # 4. _count_deliveries_for_tank (tank_old) — 5 deliveries
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 5. _get_most_recent_delivery (tank_old)
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # 6. suggest_new_kfactor(tank_old) → _get_customer_tank
                {"hits": {"hits": [_tank_hit(tank_id="tank_old", k_factor=1.5, zip_code="06001")]}},
                # 7. suggest_new_kfactor(tank_old) → _count_deliveries
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 8. suggest_new_kfactor(tank_old) → _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # 9. suggest_new_kfactor(tank_old) → _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        weather = _make_weather_provider()
        # For tank_old: k=1.5, hdd=100 → predicted=150, actual=200
        # variance = (200-150)/150*100 = 33.33% → exceeds threshold → suggested=2.0
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert len(result) == 2
        # tank_old has variance data → sorted first
        assert result[0].tank_id == "tank_old"
        assert result[0].variance_percent is not None
        assert result[0].suggested_kfactor == 2.0
        assert result[0].delivery_count == 5
        # tank_new has no variance data → sorted last
        assert result[1].tank_id == "tank_new"
        assert result[1].variance_percent is None
        assert result[1].suggested_kfactor is None
        assert result[1].delivery_count == 1

    @pytest.mark.asyncio
    async def test_tank_with_no_deliveries(self):
        """Tank with zero deliveries is included with delivery_count=0. Validates: 9.4"""
        es = _make_es_service()

        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_autofill_tanks
                {"hits": {"hits": [_autofill_tank_hit(tank_id="tank_empty", k_factor=1.0)]}},
                # 2. _count_deliveries_for_tank — zero deliveries
                {"hits": {"hits": [], "total": {"value": 0}}},
                # 3. _get_most_recent_delivery — no delivery
                {"hits": {"hits": []}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert len(result) == 1
        entry = result[0]
        assert entry.tank_id == "tank_empty"
        assert entry.delivery_count == 0
        assert entry.variance_percent is None
        assert entry.suggested_kfactor is None
        assert entry.last_delivery_date is None

    @pytest.mark.asyncio
    async def test_weather_provider_failure_graceful(self):
        """Weather provider failure doesn't crash dashboard; tank gets None variance. Validates: 9.4"""
        es = _make_es_service()

        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_autofill_tanks
                {"hits": {"hits": [_autofill_tank_hit(tank_id="tank_001", k_factor=1.5)]}},
                # 2. _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 3. _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # 4. suggest_new_kfactor → _get_customer_tank
                {"hits": {"hits": [_tank_hit(tank_id="tank_001", k_factor=1.5, zip_code="06001")]}},
                # 5. suggest_new_kfactor → _count_deliveries
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 6. suggest_new_kfactor → _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # 7. suggest_new_kfactor → _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(
            side_effect=RuntimeError("Weather API down")
        )

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.get_calibration_dashboard(_TENANT_ID)

        # Should not crash — tank is included with None variance
        assert len(result) == 1
        entry = result[0]
        assert entry.tank_id == "tank_001"
        assert entry.suggested_kfactor is None

    @pytest.mark.asyncio
    async def test_includes_keep_full_tanks(self):
        """Dashboard includes keep_full tanks (also use K-factor forecasting). Validates: 9.4"""
        es = _make_es_service()

        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_autofill_tanks — returns a keep_full tank
                {"hits": {"hits": [
                    _autofill_tank_hit(tank_id="tank_kf", k_factor=1.8, customer_type="keep_full"),
                ]}},
                # 2. _count_deliveries_for_tank — only 2 deliveries
                {"hits": {"hits": [], "total": {"value": 2}}},
                # 3. _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=180.0, updated_at="2026-03-12T10:00:00")]}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert len(result) == 1
        assert result[0].tank_id == "tank_kf"
        assert result[0].current_kfactor == 1.8


# ---------------------------------------------------------------------------
# approve_adjustment Tests — Validates: Requirement 9.5, 9.6
# ---------------------------------------------------------------------------


class TestApproveAdjustment:
    """Tests for KFactorCalibrationService.approve_adjustment.

    Validates: Requirement 9.5, 9.6
    """

    @pytest.mark.asyncio
    async def test_successful_adjustment(self):
        """Successful adjustment updates tank, logs history, returns record. Validates: 9.5, 9.6"""
        es = _make_es_service()
        # _get_customer_tank returns a tank with current k_factor=1.35
        # _count_deliveries_for_tank returns 5 (sufficient)
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

        # Verify the returned adjustment record
        assert result.tank_id == "tank_abc"
        assert result.tenant_id == _TENANT_ID
        assert result.old_kfactor == 1.35
        assert result.new_kfactor == 1.50
        assert result.operator_id == "op_smith"
        assert result.adjustment_id.startswith("kfa_")
        assert isinstance(result.timestamp, datetime)

    @pytest.mark.asyncio
    async def test_tank_kfactor_updated_in_es(self):
        """Tank's k_factor field is updated in ES. Validates: 9.5"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_tank_hit(k_factor=1.35)]}},
                {"hits": {"hits": [], "total": {"value": 5}}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        await service.approve_adjustment(
            "tank_abc", 1.50, "op_smith", _TENANT_ID
        )

        # Verify update_document was called with the correct args
        es.update_document.assert_called_once()
        call_args = es.update_document.call_args
        assert call_args[0][0] == "customer_tanks"  # index
        assert call_args[0][1] == "tank_abc"  # doc_id
        partial = call_args[0][2]
        assert partial["k_factor"] == 1.50
        assert "updated_at" in partial

    @pytest.mark.asyncio
    async def test_history_persisted_to_kfactor_history_index(self):
        """Adjustment record is persisted to kfactor_history index. Validates: 9.6"""
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

        # Verify index_document was called for kfactor_history
        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args[0][0] == "kfactor_history"  # index
        assert call_args[0][1] == result.adjustment_id  # doc_id
        doc = call_args[0][2]
        assert doc["tank_id"] == "tank_abc"
        assert doc["old_kfactor"] == 1.35
        assert doc["new_kfactor"] == 1.50
        assert doc["operator_id"] == "op_smith"
        assert doc["tenant_id"] == _TENANT_ID

    @pytest.mark.asyncio
    async def test_signal_bus_notified(self):
        """Signal bus is called with kfactor_changed event. Validates: 9.5"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_tank_hit(k_factor=1.35)]}},
                {"hits": {"hits": [], "total": {"value": 5}}},
            ]
        )
        bus = _make_signal_bus()

        service = KFactorCalibrationService(es_service=es, signal_bus=bus)

        await service.approve_adjustment(
            "tank_abc", 1.50, "op_smith", _TENANT_ID
        )

        # Verify signal bus was called
        bus.publish.assert_called_once()
        signal = bus.publish.call_args[0][0]
        assert signal.source_agent == "kfactor_calibration_service"
        assert signal.entity_id == "tank_abc"
        assert signal.entity_type == "customer_tank"
        assert signal.context["event"] == "kfactor_changed"
        assert signal.context["old_kfactor"] == 1.35
        assert signal.context["new_kfactor"] == 1.50
        assert signal.context["operator_id"] == "op_smith"

    @pytest.mark.asyncio
    async def test_tank_not_found_raises_valueerror(self):
        """Raises ValueError when tank_id is not found. Validates: 9.5"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        service = KFactorCalibrationService(es_service=es)

        with pytest.raises(ValueError, match="not found"):
            await service.approve_adjustment(
                "nonexistent_tank", 1.50, "op_smith", _TENANT_ID
            )

    @pytest.mark.asyncio
    async def test_invalid_kfactor_zero_raises_valueerror(self):
        """Raises ValueError when new_kfactor is zero. Validates: 9.5"""
        es = _make_es_service()

        service = KFactorCalibrationService(es_service=es)

        with pytest.raises(ValueError, match="must be positive"):
            await service.approve_adjustment(
                "tank_abc", 0.0, "op_smith", _TENANT_ID
            )

    @pytest.mark.asyncio
    async def test_invalid_kfactor_negative_raises_valueerror(self):
        """Raises ValueError when new_kfactor is negative. Validates: 9.5"""
        es = _make_es_service()

        service = KFactorCalibrationService(es_service=es)

        with pytest.raises(ValueError, match="must be positive"):
            await service.approve_adjustment(
                "tank_abc", -1.5, "op_smith", _TENANT_ID
            )

    @pytest.mark.asyncio
    async def test_signal_bus_not_configured_graceful_skip(self):
        """When signal_bus is None, adjustment succeeds without notification. Validates: 9.5"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_tank_hit(k_factor=1.35)]}},
                {"hits": {"hits": [], "total": {"value": 5}}},
            ]
        )

        # No signal bus configured
        service = KFactorCalibrationService(es_service=es, signal_bus=None)

        result = await service.approve_adjustment(
            "tank_abc", 1.50, "op_smith", _TENANT_ID
        )

        # Should succeed without error
        assert result.tank_id == "tank_abc"
        assert result.new_kfactor == 1.50
        # ES operations should still have been called
        es.update_document.assert_called_once()
        es.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_signal_bus_failure_does_not_block_adjustment(self):
        """Signal bus failure is non-critical; adjustment still succeeds. Validates: 9.5"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_tank_hit(k_factor=1.35)]}},
                {"hits": {"hits": [], "total": {"value": 5}}},
            ]
        )
        bus = _make_signal_bus()
        bus.publish = AsyncMock(side_effect=RuntimeError("Bus unavailable"))

        service = KFactorCalibrationService(es_service=es, signal_bus=bus)

        # Should not raise despite signal bus failure
        result = await service.approve_adjustment(
            "tank_abc", 1.50, "op_smith", _TENANT_ID
        )

        assert result.tank_id == "tank_abc"
        assert result.new_kfactor == 1.50
        # ES operations should still have been called
        es.update_document.assert_called_once()
        es.index_document.assert_called_once()


# ---------------------------------------------------------------------------
# suggest_new_kfactor Tests — Validates: Requirement 9.3
# ---------------------------------------------------------------------------


class TestSuggestNewKfactor:
    """Tests for KFactorCalibrationService.suggest_new_kfactor.

    Validates: Requirement 9.3
    """

    @pytest.mark.asyncio
    async def test_returns_suggestion_when_variance_exceeds_threshold(self):
        """Returns suggested K-factor when variance > ±15%. Validates: 9.3"""
        # Setup: k_factor=1.5, accumulated_hdd=100 → predicted=150
        # actual=200 → variance = (200-150)/150*100 = 33.33% (exceeds 15%)
        # suggested = 200/100 = 2.0
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.5, zip_code="06001")]}},
                # 2. _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 3. _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # 4. _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)

        assert result is not None
        assert result == 2.0  # 200 / 100

    @pytest.mark.asyncio
    async def test_returns_none_when_variance_within_threshold(self):
        """Returns None when variance is within ±15%. Validates: 9.3"""
        # Setup: k_factor=1.5, accumulated_hdd=150 → predicted=225
        # actual=230 → variance = (230-225)/225*100 = 2.22% (within 15%)
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.5, zip_code="06001")]}},
                # 2. _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 3. _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=230.0, updated_at="2026-03-15T10:00:00")]}},
                # 4. _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=150.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_fewer_than_3_deliveries(self):
        """Returns None when tank has fewer than MIN_DELIVERIES_FOR_CALIBRATION (3). Validates: 9.3, 9.7"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.5, zip_code="06001")]}},
                # 2. _count_deliveries_for_tank — only 2 deliveries
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
    async def test_returns_none_when_weather_provider_fails(self):
        """Returns None (gracefully) when weather provider raises an exception. Validates: 9.3"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.5, zip_code="06001")]}},
                # 2. _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 3. _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # 4. _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(
            side_effect=RuntimeError("Weather API unavailable")
        )

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_weather_provider_not_configured(self):
        """Returns None when weather provider is None. Validates: 9.3"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.5, zip_code="06001")]}},
                # 2. _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 3. _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # 4. _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        # No weather provider
        service = KFactorCalibrationService(es_service=es, weather_provider=None)

        result = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_tank_not_found(self):
        """Returns None when tank_id doesn't exist. Validates: 9.3"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        weather = _make_weather_provider()
        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.suggest_new_kfactor("nonexistent_tank", _TENANT_ID)

        assert result is None

    @pytest.mark.asyncio
    async def test_negative_variance_exceeding_threshold_returns_suggestion(self):
        """Returns suggestion for negative variance exceeding -15%. Validates: 9.3"""
        # Setup: k_factor=2.0, accumulated_hdd=100 → predicted=200
        # actual=150 → variance = (150-200)/200*100 = -25% (exceeds -15%)
        # suggested = 150/100 = 1.5
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=2.0, zip_code="06001")]}},
                # 2. _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 4}}},
                # 3. _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=150.0, updated_at="2026-03-15T10:00:00")]}},
                # 4. _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)

        assert result is not None
        assert result == 1.5  # 150 / 100

    @pytest.mark.asyncio
    async def test_returns_none_when_no_previous_delivery(self):
        """Returns None when there's no previous delivery to compute HDD window. Validates: 9.3"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.5, zip_code="06001")]}},
                # 2. _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 3}}},
                # 3. _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # 4. _get_previous_delivery_date — no previous delivery
                {"hits": {"hits": []}},
            ]
        )

        weather = _make_weather_provider()

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.suggest_new_kfactor("tank_abc", _TENANT_ID)

        assert result is None


# ---------------------------------------------------------------------------
# compute_variance Tests — Validates: Requirement 9.1, 9.2
# ---------------------------------------------------------------------------


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


class TestComputeVariance:
    """Tests for KFactorCalibrationService.compute_variance.

    Validates: Requirement 9.1, 9.2
    """

    @pytest.mark.asyncio
    async def test_normal_variance_below_threshold(self):
        """Variance within ±15% is not flagged. Validates: 9.1, 9.2"""
        # Setup: k_factor=1.5, accumulated_hdd=150 → predicted=225
        # actual=230 → variance = (230-225)/225*100 = 2.22%
        es = _make_es_service()

        # First call: get delivery
        # Second call: get tank
        # Third call: get previous delivery
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit(gallons=230.0)]}},
                {"hits": {"hits": [_tank_hit(k_factor=1.5)]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=150.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.compute_variance("del_001", _TENANT_ID)

        assert result.delivery_id == "del_001"
        assert result.tank_id == "tank_abc"
        assert result.predicted_gallons == 225.0  # 1.5 * 150
        assert result.actual_gallons == 230.0
        # (230 - 225) / 225 * 100 = 2.22%
        assert result.variance_percent == pytest.approx(2.22, abs=0.01)
        assert result.flagged is False
        assert result.suggested_kfactor is None

    @pytest.mark.asyncio
    async def test_flagged_variance_exceeds_threshold(self):
        """Variance exceeding ±15% is flagged with suggested K-factor. Validates: 9.1, 9.2"""
        # Setup: k_factor=1.5, accumulated_hdd=100 → predicted=150
        # actual=200 → variance = (200-150)/150*100 = 33.33%
        # suggested_kfactor = 200/100 = 2.0
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit(gallons=200.0)]}},
                {"hits": {"hits": [_tank_hit(k_factor=1.5)]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.compute_variance("del_001", _TENANT_ID)

        assert result.flagged is True
        assert result.variance_percent == pytest.approx(33.33, abs=0.01)
        assert result.predicted_gallons == 150.0  # 1.5 * 100
        assert result.suggested_kfactor == 2.0  # 200 / 100

    @pytest.mark.asyncio
    async def test_negative_variance_flagged(self):
        """Negative variance exceeding -15% is also flagged. Validates: 9.1, 9.2"""
        # Setup: k_factor=2.0, accumulated_hdd=100 → predicted=200
        # actual=150 → variance = (150-200)/200*100 = -25%
        # suggested_kfactor = 150/100 = 1.5
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit(gallons=150.0)]}},
                {"hits": {"hits": [_tank_hit(k_factor=2.0)]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.compute_variance("del_001", _TENANT_ID)

        assert result.flagged is True
        assert result.variance_percent == pytest.approx(-25.0, abs=0.01)
        assert result.suggested_kfactor == 1.5

    @pytest.mark.asyncio
    async def test_no_previous_delivery_raises(self):
        """Raises ValueError when no previous delivery exists for the tank. Validates: 9.1"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit()]}},
                {"hits": {"hits": [_tank_hit()]}},
                {"hits": {"hits": []}},  # No previous delivery
            ]
        )

        weather = _make_weather_provider()
        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        with pytest.raises(ValueError, match="No previous delivery"):
            await service.compute_variance("del_001", _TENANT_ID)

    @pytest.mark.asyncio
    async def test_weather_provider_not_configured_raises(self):
        """Raises RuntimeError when weather provider is None. Validates: 9.1"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit()]}},
                {"hits": {"hits": [_tank_hit()]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        # No weather provider
        service = KFactorCalibrationService(es_service=es, weather_provider=None)

        with pytest.raises(RuntimeError, match="Weather provider is not configured"):
            await service.compute_variance("del_001", _TENANT_ID)

    @pytest.mark.asyncio
    async def test_delivery_not_found_raises(self):
        """Raises ValueError when delivery_id is not found in ES. Validates: 9.1"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        weather = _make_weather_provider()
        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        with pytest.raises(ValueError, match="not found"):
            await service.compute_variance("nonexistent_del", _TENANT_ID)

    @pytest.mark.asyncio
    async def test_zero_hdd_raises(self):
        """Raises ValueError when accumulated HDD is zero. Validates: 9.1"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit()]}},
                {"hits": {"hits": [_tank_hit()]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=0.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        with pytest.raises(ValueError, match="zero or negative"):
            await service.compute_variance("del_001", _TENANT_ID)

    @pytest.mark.asyncio
    async def test_custom_variance_threshold(self):
        """Custom threshold (10%) flags variance that default (15%) would not. Validates: 9.2"""
        # Setup: k_factor=1.5, accumulated_hdd=150 → predicted=225
        # actual=250 → variance = (250-225)/225*100 = 11.11%
        # With 10% threshold → flagged; with 15% threshold → not flagged
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit(gallons=250.0)]}},
                {"hits": {"hits": [_tank_hit(k_factor=1.5)]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=150.0)

        service = KFactorCalibrationService(
            es_service=es,
            weather_provider=weather,
            variance_threshold_percent=10.0,
        )

        result = await service.compute_variance("del_001", _TENANT_ID)

        assert result.flagged is True
        assert result.variance_percent == pytest.approx(11.11, abs=0.01)
        assert result.suggested_kfactor is not None

    @pytest.mark.asyncio
    async def test_uses_fetch_fallback_when_no_get_accumulated_hdd(self):
        """Falls back to fetch() + sum(hdd) when get_accumulated_hdd is absent. Validates: 9.1"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [_delivery_hit(gallons=230.0)]}},
                {"hits": {"hits": [_tank_hit(k_factor=1.5)]}},
                {"hits": {"hits": [_prev_delivery_hit()]}},
            ]
        )

        # Weather provider without get_accumulated_hdd but with fetch
        class FakeRow:
            def __init__(self, hdd):
                self.hdd = hdd

        weather = AsyncMock()
        # Remove get_accumulated_hdd so hasattr returns False
        del weather.get_accumulated_hdd
        weather.fetch = AsyncMock(
            return_value=[FakeRow(50.0), FakeRow(60.0), FakeRow(40.0)]
        )

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.compute_variance("del_001", _TENANT_ID)

        # accumulated_hdd = 50 + 60 + 40 = 150
        # predicted = 1.5 * 150 = 225
        assert result.predicted_gallons == 225.0
        assert result.actual_gallons == 230.0
        weather.fetch.assert_called_once()


# ---------------------------------------------------------------------------
# Read-Only Mode Tests — Validates: Requirement 9.7
# ---------------------------------------------------------------------------


class TestReadOnlyMode:
    """Tests for read-only mode when fewer than 3 deliveries exist.

    Validates: Requirement 9.7
    """

    def test_kfactor_entry_read_only_field_defaults_false(self):
        """KFactorEntry.read_only defaults to False. Validates: 9.7"""
        entry = KFactorEntry(
            tank_id="tank_001",
            customer_id="cust_abc",
            current_kfactor=1.35,
        )
        assert entry.read_only is False
        assert entry.read_only_reason is None

    def test_kfactor_entry_read_only_can_be_set_true(self):
        """KFactorEntry.read_only can be set to True with a reason. Validates: 9.7"""
        entry = KFactorEntry(
            tank_id="tank_001",
            customer_id="cust_abc",
            current_kfactor=1.35,
            read_only=True,
            read_only_reason="Insufficient data for recalibration — requires at least 3 deliveries",
        )
        assert entry.read_only is True
        assert "Insufficient data" in entry.read_only_reason
        assert "3 deliveries" in entry.read_only_reason

    @pytest.mark.asyncio
    async def test_dashboard_sets_read_only_for_insufficient_deliveries(self):
        """Dashboard entry has read_only=True when delivery_count < 3. Validates: 9.7"""
        es = _make_es_service()

        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_autofill_tanks
                {"hits": {"hits": [_autofill_tank_hit(tank_id="tank_new", k_factor=1.2)]}},
                # 2. _count_deliveries_for_tank — only 2 deliveries
                {"hits": {"hits": [], "total": {"value": 2}}},
                # 3. _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=100.0, updated_at="2026-03-10T10:00:00")]}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert len(result) == 1
        entry = result[0]
        assert entry.tank_id == "tank_new"
        assert entry.delivery_count == 2
        assert entry.read_only is True
        assert entry.read_only_reason is not None
        assert "Insufficient data" in entry.read_only_reason
        assert "3 deliveries" in entry.read_only_reason

    @pytest.mark.asyncio
    async def test_dashboard_sets_read_only_false_for_sufficient_deliveries(self):
        """Dashboard entry has read_only=False when delivery_count >= 3. Validates: 9.7"""
        es = _make_es_service()

        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_autofill_tanks
                {"hits": {"hits": [_autofill_tank_hit(tank_id="tank_001", k_factor=1.5)]}},
                # 2. _count_deliveries_for_tank — 5 deliveries
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 3. _get_most_recent_delivery (for last_delivery_date)
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # 4. suggest_new_kfactor → _get_customer_tank
                {"hits": {"hits": [_tank_hit(tank_id="tank_001", k_factor=1.5, zip_code="06001")]}},
                # 5. suggest_new_kfactor → _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 5}}},
                # 6. suggest_new_kfactor → _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=200.0, updated_at="2026-03-15T10:00:00")]}},
                # 7. suggest_new_kfactor → _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert len(result) == 1
        entry = result[0]
        assert entry.tank_id == "tank_001"
        assert entry.delivery_count == 5
        assert entry.read_only is False
        assert entry.read_only_reason is None

    @pytest.mark.asyncio
    async def test_dashboard_zero_deliveries_is_read_only(self):
        """Tank with zero deliveries is read-only. Validates: 9.7"""
        es = _make_es_service()

        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_autofill_tanks
                {"hits": {"hits": [_autofill_tank_hit(tank_id="tank_empty", k_factor=1.0)]}},
                # 2. _count_deliveries_for_tank — zero deliveries
                {"hits": {"hits": [], "total": {"value": 0}}},
                # 3. _get_most_recent_delivery — no delivery
                {"hits": {"hits": []}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert len(result) == 1
        entry = result[0]
        assert entry.delivery_count == 0
        assert entry.read_only is True
        assert entry.read_only_reason is not None

    @pytest.mark.asyncio
    async def test_dashboard_exactly_3_deliveries_is_not_read_only(self):
        """Tank with exactly 3 deliveries is NOT read-only (boundary). Validates: 9.7"""
        es = _make_es_service()

        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_autofill_tanks
                {"hits": {"hits": [_autofill_tank_hit(tank_id="tank_boundary", k_factor=1.5)]}},
                # 2. _count_deliveries_for_tank — exactly 3 deliveries
                {"hits": {"hits": [], "total": {"value": 3}}},
                # 3. _get_most_recent_delivery (for last_delivery_date)
                {"hits": {"hits": [_delivery_hit(gallons=180.0, updated_at="2026-03-15T10:00:00")]}},
                # 4. suggest_new_kfactor → _get_customer_tank
                {"hits": {"hits": [_tank_hit(tank_id="tank_boundary", k_factor=1.5, zip_code="06001")]}},
                # 5. suggest_new_kfactor → _count_deliveries_for_tank
                {"hits": {"hits": [], "total": {"value": 3}}},
                # 6. suggest_new_kfactor → _get_most_recent_delivery
                {"hits": {"hits": [_delivery_hit(gallons=180.0, updated_at="2026-03-15T10:00:00")]}},
                # 7. suggest_new_kfactor → _get_previous_delivery_date
                {"hits": {"hits": [_prev_delivery_hit(updated_at="2026-02-01T10:00:00")]}},
            ]
        )

        weather = _make_weather_provider()
        weather.get_accumulated_hdd = AsyncMock(return_value=100.0)

        service = KFactorCalibrationService(
            es_service=es, weather_provider=weather
        )

        result = await service.get_calibration_dashboard(_TENANT_ID)

        assert len(result) == 1
        entry = result[0]
        assert entry.delivery_count == 3
        assert entry.read_only is False
        assert entry.read_only_reason is None

    @pytest.mark.asyncio
    async def test_approve_adjustment_rejects_insufficient_deliveries(self):
        """approve_adjustment raises ValueError when tank has < 3 deliveries. Validates: 9.7"""
        es = _make_es_service()
        # _get_customer_tank returns a valid tank
        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.35)]}},
                # 2. _count_deliveries_for_tank — only 2 deliveries
                {"hits": {"hits": [], "total": {"value": 2}}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        with pytest.raises(ValueError, match="insufficient delivery data"):
            await service.approve_adjustment(
                "tank_abc", 1.50, "op_smith", _TENANT_ID
            )

    @pytest.mark.asyncio
    async def test_approve_adjustment_rejects_zero_deliveries(self):
        """approve_adjustment raises ValueError when tank has 0 deliveries. Validates: 9.7"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.0)]}},
                # 2. _count_deliveries_for_tank — zero deliveries
                {"hits": {"hits": [], "total": {"value": 0}}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        with pytest.raises(ValueError, match="insufficient delivery data"):
            await service.approve_adjustment(
                "tank_abc", 1.50, "op_smith", _TENANT_ID
            )

    @pytest.mark.asyncio
    async def test_approve_adjustment_succeeds_with_3_deliveries(self):
        """approve_adjustment succeeds when tank has exactly 3 deliveries (boundary). Validates: 9.7"""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                # 1. _get_customer_tank
                {"hits": {"hits": [_tank_hit(k_factor=1.35)]}},
                # 2. _count_deliveries_for_tank — exactly 3 deliveries
                {"hits": {"hits": [], "total": {"value": 3}}},
            ]
        )

        service = KFactorCalibrationService(es_service=es)

        result = await service.approve_adjustment(
            "tank_abc", 1.50, "op_smith", _TENANT_ID
        )

        assert result.tank_id == "tank_abc"
        assert result.old_kfactor == 1.35
        assert result.new_kfactor == 1.50
