"""
Unit tests for the TankForecastingAgent overlay agent.

Tests cover:
- Constructor and agent_id configuration
- Signal subscription setup (fuel_management_agent RiskSignals)
- evaluate() with empty signals
- evaluate() queries fuel_stations and fuel_events
- _process_anomaly_signals() extracts anomaly flags from RiskSignals (Req 1.3)
- _compute_forecast() with historical data (Req 1.1, 1.6)
- _compute_forecast() with zero historical data — default risk (Req 1.7)
- _estimate_consumption_rate() from history
- _compute_runout_risk() risk calculation
- _compute_confidence() data quality scoring
- Persistence to mvp_tank_forecasts (Req 1.4)
- Publishing TankForecast to SignalBus (Req 1.5)

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from Agents.overlay.data_contracts import RiskSignal, Severity
from Agents.overlay.tank_forecasting_agent import (
    DEFAULT_CONSUMPTION_RATE,
    FUEL_EVENTS_INDEX,
    FUEL_STATIONS_INDEX,
    P90_VARIANCE_MULTIPLIER,
    RISK_HORIZON_HOURS,
    TankForecastingAgent,
)
from Agents.support.fuel_distribution_models import FuelGrade, TankForecast


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal(
    entity_id="station-1",
    severity=Severity.HIGH,
    confidence=0.9,
    tenant_id="tenant-1",
    context=None,
):
    return RiskSignal(
        source_agent="fuel_management_agent",
        entity_id=entity_id,
        entity_type="fuel_station",
        severity=severity,
        confidence=confidence,
        ttl_seconds=300,
        tenant_id=tenant_id,
        context=context or {},
    )


def _make_station(
    station_id="station-1",
    fuel_grade="AGO",
    current_stock=5000.0,
    capacity=20000.0,
    tenant_id="tenant-1",
):
    return {
        "station_id": station_id,
        "fuel_grade": fuel_grade,
        "current_stock_liters": current_stock,
        "capacity_liters": capacity,
        "tenant_id": tenant_id,
    }


def _make_consumption_events(station_id="station-1", fuel_grade="AGO", count=10):
    """Generate consumption events over the last 7 days."""
    now = datetime.now(timezone.utc)
    events = []
    for i in range(count):
        ts = now - timedelta(hours=i * 12)
        events.append({
            "station_id": station_id,
            "fuel_grade": fuel_grade,
            "quantity_liters": 200.0,
            "timestamp": ts.isoformat(),
            "tenant_id": "tenant-1",
        })
    return events


def _make_deps():
    """Create mocked dependencies for the TankForecastingAgent."""
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    es_service.search_documents = AsyncMock(
        return_value={"hits": {"hits": []}}
    )
    es_service.index_document = AsyncMock()

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")
    activity_log.log = AsyncMock()

    ws_manager = MagicMock()
    ws_manager.broadcast_activity = AsyncMock()

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock()

    autonomy_config = MagicMock()
    feature_flags = MagicMock()
    feature_flags.is_enabled = AsyncMock(return_value=True)

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "autonomy_config_service": autonomy_config,
        "feature_flag_service": feature_flags,
    }


def _make_agent(**overrides):
    deps = _make_deps()
    deps.update(overrides)
    return TankForecastingAgent(**deps), deps


# ---------------------------------------------------------------------------
# Tests: Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_agent_id(self):
        agent, _ = _make_agent()
        assert agent.agent_id == "tank_forecasting"

    def test_subscription_to_risk_signals(self):
        agent, _ = _make_agent()
        assert len(agent._subscription_specs) == 1
        spec = agent._subscription_specs[0]
        assert spec["message_type"] is RiskSignal
        assert spec["filters"]["source_agent"] == "fuel_management_agent"

    def test_default_poll_interval(self):
        agent, _ = _make_agent()
        assert agent.poll_interval == 300

    def test_custom_poll_interval(self):
        agent, _ = _make_agent(poll_interval=120)
        assert agent.poll_interval == 120

    def test_anomaly_cache_initially_empty(self):
        agent, _ = _make_agent()
        assert agent._anomaly_cache == {}


# ---------------------------------------------------------------------------
# Tests: evaluate()
# ---------------------------------------------------------------------------


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_empty_signals_returns_empty(self):
        agent, _ = _make_agent()
        result = await agent.evaluate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_no_stations_returns_empty(self):
        """When no stations exist, evaluate returns empty."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        signal = _make_signal()
        result = await agent.evaluate([signal])
        assert result == []

    @pytest.mark.asyncio
    async def test_produces_forecasts_for_stations(self):
        """Req 1.1: Produces TankForecast for each station."""
        agent, deps = _make_agent()

        station = _make_station()
        events = _make_consumption_events()

        # First call: fuel_stations, second call: fuel_events
        deps["es_service"].search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [{"_source": station}]}},
                {"hits": {"hits": [{"_source": e} for e in events]}},
            ]
        )

        signal = _make_signal()
        result = await agent.evaluate([signal])

        # evaluate returns empty (forecasts published directly)
        assert result == []
        # But forecasts should be published to SignalBus
        assert deps["signal_bus"].publish.call_count == 1
        published = deps["signal_bus"].publish.call_args[0][0]
        assert isinstance(published, TankForecast)
        assert published.station_id == "station-1"
        assert published.fuel_grade == FuelGrade.AGO

    @pytest.mark.asyncio
    async def test_persists_forecasts_to_es(self):
        """Req 1.4: Forecasts persisted to mvp_tank_forecasts."""
        agent, deps = _make_agent()

        station = _make_station()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [{"_source": station}]}},
                {"hits": {"hits": []}},
            ]
        )

        signal = _make_signal()
        await agent.evaluate([signal])

        # index_document should be called for the forecast
        assert deps["es_service"].index_document.call_count == 1
        call_args = deps["es_service"].index_document.call_args
        assert call_args[0][0] == "mvp_tank_forecasts"


# ---------------------------------------------------------------------------
# Tests: _process_anomaly_signals() (Req 1.3)
# ---------------------------------------------------------------------------


class TestProcessAnomalySignals:
    def test_extracts_sensor_drift(self):
        agent, _ = _make_agent()
        signal = _make_signal(
            entity_id="station-1",
            context={"sensor_drift": True},
        )
        agent._process_anomaly_signals([signal])
        assert "sensor_drift" in agent._anomaly_cache.get("station-1", [])

    def test_extracts_station_outage(self):
        agent, _ = _make_agent()
        signal = _make_signal(
            entity_id="station-2",
            context={"station_outage": True},
        )
        agent._process_anomaly_signals([signal])
        assert "station_outage" in agent._anomaly_cache.get("station-2", [])

    def test_extracts_demand_spike(self):
        agent, _ = _make_agent()
        signal = _make_signal(
            entity_id="station-3",
            context={"demand_spike": True},
        )
        agent._process_anomaly_signals([signal])
        assert "demand_spike" in agent._anomaly_cache.get("station-3", [])

    def test_critical_severity_adds_critical_risk(self):
        agent, _ = _make_agent()
        signal = _make_signal(
            entity_id="station-4",
            severity=Severity.CRITICAL,
        )
        agent._process_anomaly_signals([signal])
        assert "critical_risk" in agent._anomaly_cache.get("station-4", [])

    def test_merges_without_duplicates(self):
        agent, _ = _make_agent()
        signal1 = _make_signal(
            entity_id="station-1",
            context={"sensor_drift": True},
        )
        signal2 = _make_signal(
            entity_id="station-1",
            context={"sensor_drift": True, "demand_spike": True},
        )
        agent._process_anomaly_signals([signal1])
        agent._process_anomaly_signals([signal2])
        flags = agent._anomaly_cache["station-1"]
        assert flags.count("sensor_drift") == 1
        assert "demand_spike" in flags


# ---------------------------------------------------------------------------
# Tests: _compute_forecast() (Req 1.1, 1.6, 1.7)
# ---------------------------------------------------------------------------


class TestComputeForecast:
    def test_zero_history_returns_default_risk(self):
        """Req 1.7: Zero historical data → risk 0.5, confidence 0.1, insufficient_data flag."""
        agent, _ = _make_agent()
        forecast = agent._compute_forecast(
            station_id="station-1",
            fuel_grade=FuelGrade.AGO,
            current_stock=5000.0,
            capacity=20000.0,
            consumption_history=[],
            tenant_id="tenant-1",
            run_id="test-run",
        )
        assert forecast.runout_risk_24h == 0.5
        assert forecast.confidence == 0.1
        assert "insufficient_data" in forecast.anomaly_flags

    def test_with_history_produces_valid_forecast(self):
        """Req 1.1: Forecast contains all required fields."""
        agent, _ = _make_agent()
        events = _make_consumption_events(count=20)
        forecast = agent._compute_forecast(
            station_id="station-1",
            fuel_grade=FuelGrade.PMS,
            current_stock=10000.0,
            capacity=50000.0,
            consumption_history=events,
            tenant_id="tenant-1",
            run_id="test-run",
        )
        assert forecast.station_id == "station-1"
        assert forecast.fuel_grade == FuelGrade.PMS
        assert forecast.hours_to_runout_p50 >= 0
        assert forecast.hours_to_runout_p90 >= 0
        assert 0.0 <= forecast.runout_risk_24h <= 1.0
        assert 0.0 <= forecast.confidence <= 1.0
        assert forecast.feature_version == "v1.0"
        assert forecast.tenant_id == "tenant-1"
        assert forecast.run_id == "test-run"

    def test_anomaly_flags_included(self):
        """Req 1.3: Anomaly flags from cache are included in forecast."""
        agent, _ = _make_agent()
        agent._anomaly_cache["station-1"] = ["sensor_drift", "demand_spike"]
        events = _make_consumption_events(count=5)
        forecast = agent._compute_forecast(
            station_id="station-1",
            fuel_grade=FuelGrade.AGO,
            current_stock=5000.0,
            capacity=20000.0,
            consumption_history=events,
            tenant_id="tenant-1",
            run_id="test-run",
        )
        assert "sensor_drift" in forecast.anomaly_flags
        assert "demand_spike" in forecast.anomaly_flags

    def test_demand_spike_boosts_risk(self):
        """Demand spike anomaly should increase runout_risk_24h."""
        agent, _ = _make_agent()
        events = _make_consumption_events(count=10)

        # Without demand spike
        forecast_normal = agent._compute_forecast(
            station_id="station-1",
            fuel_grade=FuelGrade.AGO,
            current_stock=5000.0,
            capacity=20000.0,
            consumption_history=events,
            tenant_id="tenant-1",
            run_id="test-run",
        )

        # With demand spike
        agent._anomaly_cache["station-2"] = ["demand_spike"]
        forecast_spike = agent._compute_forecast(
            station_id="station-2",
            fuel_grade=FuelGrade.AGO,
            current_stock=5000.0,
            capacity=20000.0,
            consumption_history=events,
            tenant_id="tenant-1",
            run_id="test-run",
        )

        assert forecast_spike.runout_risk_24h >= forecast_normal.runout_risk_24h


# ---------------------------------------------------------------------------
# Tests: _estimate_consumption_rate() (Req 1.6)
# ---------------------------------------------------------------------------


class TestEstimateConsumptionRate:
    def test_empty_history_returns_default(self):
        agent, _ = _make_agent()
        rate = agent._estimate_consumption_rate([])
        assert rate == DEFAULT_CONSUMPTION_RATE

    def test_computes_rate_from_events(self):
        agent, _ = _make_agent()
        now = datetime.now(timezone.utc)
        events = [
            {"quantity_liters": 100.0, "timestamp": (now - timedelta(hours=10)).isoformat()},
            {"quantity_liters": 100.0, "timestamp": now.isoformat()},
        ]
        rate = agent._estimate_consumption_rate(events)
        # 200 liters over 10 hours = 20 liters/hour
        assert abs(rate - 20.0) < 1.0

    def test_zero_quantity_returns_default(self):
        agent, _ = _make_agent()
        now = datetime.now(timezone.utc)
        events = [
            {"quantity_liters": 0.0, "timestamp": now.isoformat()},
        ]
        rate = agent._estimate_consumption_rate(events)
        assert rate == DEFAULT_CONSUMPTION_RATE


# ---------------------------------------------------------------------------
# Tests: _compute_runout_risk()
# ---------------------------------------------------------------------------


class TestComputeRunoutRisk:
    def test_zero_hours_returns_max_risk(self):
        agent, _ = _make_agent()
        risk = agent._compute_runout_risk(0.0, 0.0)
        assert risk == 1.0

    def test_high_hours_returns_low_risk(self):
        agent, _ = _make_agent()
        risk = agent._compute_runout_risk(200.0, 200.0)
        assert risk < 0.2

    def test_within_24h_returns_high_risk(self):
        agent, _ = _make_agent()
        risk = agent._compute_runout_risk(12.0, 12.0)
        assert risk >= 0.5

    def test_risk_bounded_0_to_1(self):
        agent, _ = _make_agent()
        for hours in [0, 1, 5, 12, 24, 48, 72, 100, 500]:
            risk = agent._compute_runout_risk(float(hours), float(hours))
            assert 0.0 <= risk <= 1.0


# ---------------------------------------------------------------------------
# Tests: _compute_confidence()
# ---------------------------------------------------------------------------


class TestComputeConfidence:
    def test_more_data_higher_confidence(self):
        agent, _ = _make_agent()
        few_events = [{"quantity_liters": 100}] * 5
        many_events = [{"quantity_liters": 100}] * 50
        conf_few = agent._compute_confidence(few_events, [])
        conf_many = agent._compute_confidence(many_events, [])
        assert conf_many > conf_few

    def test_anomalies_reduce_confidence(self):
        agent, _ = _make_agent()
        events = [{"quantity_liters": 100}] * 30
        conf_clean = agent._compute_confidence(events, [])
        conf_anomaly = agent._compute_confidence(events, ["sensor_drift"])
        assert conf_anomaly < conf_clean

    def test_minimum_confidence(self):
        agent, _ = _make_agent()
        conf = agent._compute_confidence(
            [{"quantity_liters": 100}],
            ["sensor_drift", "station_outage", "demand_spike"],
        )
        assert conf >= 0.1
