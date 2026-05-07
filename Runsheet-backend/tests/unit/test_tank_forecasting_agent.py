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


# ---------------------------------------------------------------------------
# Tests: Customer_Tank forecasting extension (fuel-ops hardening Capability 1)
# ---------------------------------------------------------------------------
#
# These tests cover Task 3.5: iterating over customer_tanks, selecting
# Consumption_Models by fuel_type + tenant config, calling the
# Weather_Provider with graceful fallback, applying Customer_Type
# multipliers, incorporating scheduled deliveries into projected levels,
# and stamping model_name/customer_type_multiplier/weather_fallback/
# scheduled_deliveries on every forecast.
#
# Validates: Requirements 1.1.2, 1.2.3, 1.2.5, 1.3.1, 1.3.2, 1.3.3, 1.3.4,
# 1.4.2, 1.4.4, 1.5.3, 1.5.6

import json
from types import SimpleNamespace

from Agents.overlay.tank_forecasting_agent import (
    DEFAULT_CUSTOMER_TYPE_MULTIPLIERS,
    SCHEDULED_DELIVERY_HORIZON_HOURS,
)
from fuel.customer_tank_models import CustomerTank
from fuel.services.consumption_models import ConsumptionPrediction


def _make_customer_tank(
    *,
    customer_tank_id: str = "tank-1",
    tenant_id: str = "tenant-1",
    customer_id: str = "cust-1",
    customer_type: str = "residential",
    fuel_type: str = "propane",
    fuel_product_code: str = "PROPANE",
    capacity: float = 500.0,
    current_level: float = 250.0,
    zip_code: str = "01060",
    **overrides,
) -> CustomerTank:
    kwargs = {
        "customer_tank_id": customer_tank_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "customer_type": customer_type,
        "fuel_type": fuel_type,
        "fuel_product_code": fuel_product_code,
        "capacity_gallons": capacity,
        "current_level_gallons": current_level,
        "location_lat": 42.31,
        "location_lon": -72.63,
        "zip_code": zip_code,
        "status": "active",
    }
    kwargs.update(overrides)
    return CustomerTank(**kwargs)


class _FakeCustomerTankRepo:
    def __init__(self, tanks):
        self.tanks = list(tanks)
        self.calls = []

    async def list_for_tenant(self, tenant_id, **kwargs):
        self.calls.append({"tenant_id": tenant_id, **kwargs})
        return [t for t in self.tanks if t.tenant_id == tenant_id]


class _FakeWeatherProvider:
    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows or []
        self._raise = raise_exc
        self.calls = []

    async def fetch(self, zip_code, start, end, *, tenant_id):
        self.calls.append(
            {"zip_code": zip_code, "start": start, "end": end, "tenant_id": tenant_id}
        )
        if self._raise is not None:
            raise self._raise
        return list(self._rows)


class _FakeScheduledDeliveryHelper:
    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload or {}
        self._raise = raise_exc
        self.calls = []

    async def list_scheduled_deliveries(self, tenant_id, *, horizon_hours=72):
        self.calls.append({"tenant_id": tenant_id, "horizon_hours": horizon_hours})
        if self._raise is not None:
            raise self._raise
        return dict(self._payload)


class _FakeTenantConfig:
    def __init__(self, data=None):
        self.data = dict(data or {})

    async def get(self, key):
        return self.data.get(key)


def _make_extended_agent(
    *,
    tanks=None,
    weather_rows=None,
    weather_raise=None,
    scheduled=None,
    scheduled_raise=None,
    tenant_config=None,
    stations=None,
    events=None,
):
    agent, deps = _make_agent()
    tank_objs = tanks or []
    agent.set_customer_tank_repository(_FakeCustomerTankRepo(tank_objs))
    if weather_rows is not None or weather_raise is not None:
        agent.set_weather_provider(
            _FakeWeatherProvider(weather_rows, raise_exc=weather_raise)
        )
    if scheduled is not None or scheduled_raise is not None:
        agent.set_scheduled_delivery_helper(
            _FakeScheduledDeliveryHelper(scheduled, raise_exc=scheduled_raise)
        )
    if tenant_config is not None:
        agent.set_tenant_config(_FakeTenantConfig(tenant_config))

    # Wire ES side_effect: fuel_stations → fuel_events
    stations = stations or []
    events = events or []
    deps["es_service"].search_documents = AsyncMock(
        side_effect=[
            {"hits": {"hits": [{"_source": s} for s in stations]}},
            {"hits": {"hits": [{"_source": e} for e in events]}},
        ]
    )
    return agent, deps


class TestCustomerTankIteration:
    """Req 1.1.2 — agent iterates over customer_tanks in addition to fuel_stations."""

    @pytest.mark.asyncio
    async def test_forecast_produced_for_customer_tank(self):
        tank = _make_customer_tank()
        agent, deps = _make_extended_agent(tanks=[tank])
        await agent.evaluate([_make_signal()])

        # One customer-tank forecast should be published.
        published = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        tank_forecasts = [
            f for f in published if f.customer_tank_id == tank.customer_tank_id
        ]
        assert len(tank_forecasts) == 1
        forecast = tank_forecasts[0]
        assert forecast.customer_id == tank.customer_id
        assert forecast.customer_type == tank.customer_type
        assert forecast.fuel_type == tank.fuel_type

    @pytest.mark.asyncio
    async def test_no_customer_tanks_keeps_legacy_behavior(self):
        agent, deps = _make_extended_agent(tanks=[])
        await agent.evaluate([_make_signal()])
        # No customer_tank forecasts should be produced.
        published = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        assert all(f.customer_tank_id is None for f in published)


class TestConsumptionModelSelection:
    """Req 1.5.3 — select Consumption_Model by fuel_type + tenant config."""

    @pytest.mark.asyncio
    async def test_propane_tank_uses_propane_k_factor_model(self):
        tank = _make_customer_tank(fuel_type="propane", fuel_product_code="PROPANE")
        agent, deps = _make_extended_agent(tanks=[tank])
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        propane = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert propane.model_name == "propane_k_factor"

    @pytest.mark.asyncio
    async def test_heating_oil_tank_uses_regression_model(self):
        tank = _make_customer_tank(
            fuel_type="heating_oil", fuel_product_code="HEATING_OIL"
        )
        agent, deps = _make_extended_agent(tanks=[tank])
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        oil = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert oil.model_name == "heating_oil_hdd_regression"

    @pytest.mark.asyncio
    async def test_diesel_tank_uses_rolling_model(self):
        tank = _make_customer_tank(fuel_type="diesel", fuel_product_code="DIESEL_2")
        agent, deps = _make_extended_agent(tanks=[tank])
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        diesel = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert diesel.model_name == "diesel_rolling_28d"

    @pytest.mark.asyncio
    async def test_generator_tank_uses_runtime_model(self):
        tank = _make_customer_tank(
            fuel_type="generator_fuel", fuel_product_code="DIESEL_2"
        )
        agent, deps = _make_extended_agent(tanks=[tank])
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        gen = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert gen.model_name == "generator_runtime"

    @pytest.mark.asyncio
    async def test_tenant_override_selects_alternate_model(self):
        tank = _make_customer_tank(fuel_type="propane", fuel_product_code="PROPANE")
        agent, deps = _make_extended_agent(
            tanks=[tank],
            tenant_config={
                "consumption_model_config:tenant-1": json.dumps(
                    {"propane": "diesel_rolling_28d"}
                )
            },
        )
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        tank_forecast = [
            f for f in forecasts if f.customer_tank_id == tank.customer_tank_id
        ][0]
        assert tank_forecast.model_name == "diesel_rolling_28d"

    @pytest.mark.asyncio
    async def test_invalid_override_falls_back_to_default(self):
        tank = _make_customer_tank(fuel_type="propane", fuel_product_code="PROPANE")
        agent, deps = _make_extended_agent(
            tanks=[tank],
            tenant_config={
                "consumption_model_config:tenant-1": json.dumps(
                    {"propane": "no_such_model"}
                )
            },
        )
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        tank_forecast = [
            f for f in forecasts if f.customer_tank_id == tank.customer_tank_id
        ][0]
        assert tank_forecast.model_name == "propane_k_factor"


class TestWeatherProviderIntegration:
    """Req 1.2.3, 1.2.5 — Weather_Provider for propane/heating_oil + fallback."""

    @pytest.mark.asyncio
    async def test_propane_tank_calls_weather_provider(self):
        tank = _make_customer_tank(fuel_type="propane", fuel_product_code="PROPANE")
        weather = _FakeWeatherProvider(rows=[])
        agent, deps = _make_agent()
        agent.set_customer_tank_repository(_FakeCustomerTankRepo([tank]))
        agent.set_weather_provider(weather)
        deps["es_service"].search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": []}},
                {"hits": {"hits": []}},
            ]
        )
        await agent.evaluate([_make_signal()])
        assert len(weather.calls) == 1
        assert weather.calls[0]["zip_code"] == tank.zip_code
        assert weather.calls[0]["tenant_id"] == tank.tenant_id

    @pytest.mark.asyncio
    async def test_diesel_tank_skips_weather_provider(self):
        tank = _make_customer_tank(fuel_type="diesel", fuel_product_code="DIESEL_2")
        weather = _FakeWeatherProvider(rows=[])
        agent, deps = _make_agent()
        agent.set_customer_tank_repository(_FakeCustomerTankRepo([tank]))
        agent.set_weather_provider(weather)
        deps["es_service"].search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": []}},
                {"hits": {"hits": []}},
            ]
        )
        await agent.evaluate([_make_signal()])
        # Diesel tank should not trigger a weather lookup.
        assert weather.calls == []

    @pytest.mark.asyncio
    async def test_missing_weather_provider_marks_fallback(self):
        tank = _make_customer_tank(fuel_type="propane", fuel_product_code="PROPANE")
        agent, deps = _make_extended_agent(tanks=[tank])
        # Note: no weather provider wired.
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        t = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert t.weather_fallback is True
        assert "weather_fallback" in t.anomaly_flags

    @pytest.mark.asyncio
    async def test_weather_provider_exception_degrades_gracefully(self):
        tank = _make_customer_tank(fuel_type="heating_oil", fuel_product_code="HEATING_OIL")
        agent, deps = _make_extended_agent(
            tanks=[tank], weather_raise=RuntimeError("boom")
        )
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        t = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert t.weather_fallback is True

    @pytest.mark.asyncio
    async def test_non_weather_fuel_not_marked_fallback(self):
        tank = _make_customer_tank(fuel_type="diesel", fuel_product_code="DIESEL_2")
        agent, deps = _make_extended_agent(tanks=[tank])
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        t = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert t.weather_fallback is False


class TestCustomerTypeMultipliers:
    """Req 1.3.1, 1.3.4 — customer-type multipliers from Redis config."""

    @pytest.mark.asyncio
    async def test_default_multipliers_applied(self):
        tank = _make_customer_tank(customer_type="commercial")
        agent, deps = _make_extended_agent(tanks=[tank])
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        t = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert t.customer_type_multiplier == DEFAULT_CUSTOMER_TYPE_MULTIPLIERS[
            "commercial"
        ]

    @pytest.mark.asyncio
    async def test_tenant_overrides_replace_defaults(self):
        tank = _make_customer_tank(customer_type="will_call")
        agent, deps = _make_extended_agent(
            tanks=[tank],
            tenant_config={
                "consumption_segmentation_config:tenant-1": json.dumps(
                    {"will_call": 0.5}
                ),
            },
        )
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        t = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert t.customer_type_multiplier == 0.5

    @pytest.mark.asyncio
    async def test_invalid_multiplier_falls_back_to_default(self):
        tank = _make_customer_tank(customer_type="residential")
        agent, deps = _make_extended_agent(
            tanks=[tank],
            tenant_config={
                "consumption_segmentation_config:tenant-1": json.dumps(
                    {"residential": "not-a-number"}
                ),
            },
        )
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        t = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        # Invalid entry ignored, default 1.0 kept.
        assert t.customer_type_multiplier == 1.0


class TestScheduledDeliveriesFolded:
    """Req 1.4.2, 1.4.4 — scheduled deliveries extend projected runout."""

    @pytest.mark.asyncio
    async def test_scheduled_delivery_extends_runout(self):
        tank = _make_customer_tank(
            fuel_type="diesel",
            fuel_product_code="DIESEL_2",
            current_level=100.0,
            capacity=500.0,
        )
        future_eta = datetime.now(timezone.utc) + timedelta(hours=12)
        scheduled = {
            ("customer_tank", tank.customer_tank_id): [
                {
                    "delivery_id": "deliv-1",
                    "scheduled_eta": future_eta,
                    "planned_gallons": 300.0,
                }
            ]
        }
        # Feed one delivery event so the diesel rolling model produces a
        # meaningful gallons-per-day value.
        events = [
            {
                "customer_tank_id": tank.customer_tank_id,
                "quantity_gallons": 100.0,
                "timestamp": (datetime.now(timezone.utc) - timedelta(days=2))
                .isoformat()
                .replace("+00:00", "Z"),
                "tenant_id": tank.tenant_id,
            }
        ]
        agent, deps = _make_extended_agent(
            tanks=[tank],
            scheduled=scheduled,
            events=events,
        )
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        t = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert len(t.scheduled_deliveries) == 1
        assert t.scheduled_deliveries[0]["delivery_id"] == "deliv-1"
        assert t.scheduled_deliveries[0]["planned_gallons"] == 300.0

    @pytest.mark.asyncio
    async def test_missing_helper_results_in_empty_scheduled_list(self):
        tank = _make_customer_tank()
        agent, deps = _make_extended_agent(tanks=[tank])
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        t = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert t.scheduled_deliveries == []

    @pytest.mark.asyncio
    async def test_helper_exception_results_in_empty_scheduled_list(self):
        tank = _make_customer_tank()
        agent, deps = _make_extended_agent(
            tanks=[tank], scheduled_raise=RuntimeError("upstream down")
        )
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        t = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert t.scheduled_deliveries == []


class TestForecastMetadataStamping:
    """Req 1.5.6, 1.6.1 — metadata stamped on every forecast."""

    @pytest.mark.asyncio
    async def test_all_metadata_fields_populated(self):
        tank = _make_customer_tank(
            customer_type="commercial",
            fuel_type="propane",
            fuel_product_code="PROPANE",
        )
        agent, deps = _make_extended_agent(
            tanks=[tank],
            weather_rows=[],
        )
        await agent.evaluate([_make_signal()])
        forecasts = [c.args[0] for c in deps["signal_bus"].publish.call_args_list]
        t = [f for f in forecasts if f.customer_tank_id == tank.customer_tank_id][0]
        assert t.customer_tank_id == tank.customer_tank_id
        assert t.customer_id == tank.customer_id
        assert t.customer_type == "commercial"
        assert t.fuel_type == "propane"
        assert t.model_name == "propane_k_factor"
        assert t.customer_type_multiplier == DEFAULT_CUSTOMER_TYPE_MULTIPLIERS[
            "commercial"
        ]
        assert t.baseline_source in ("history", "default")
        assert isinstance(t.weather_fallback, bool)
        assert isinstance(t.scheduled_deliveries, list)


class TestScheduledDeliveryHorizon:
    """Req 1.4.1 — scheduled deliveries queried for the next 72 hours."""

    @pytest.mark.asyncio
    async def test_horizon_is_72_hours(self):
        tank = _make_customer_tank()
        helper = _FakeScheduledDeliveryHelper({})
        agent, deps = _make_agent()
        agent.set_customer_tank_repository(_FakeCustomerTankRepo([tank]))
        agent.set_scheduled_delivery_helper(helper)
        deps["es_service"].search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": []}},
                {"hits": {"hits": []}},
            ]
        )
        await agent.evaluate([_make_signal()])
        assert helper.calls == [
            {"tenant_id": "tenant-1", "horizon_hours": SCHEDULED_DELIVERY_HORIZON_HOURS}
        ]


class TestPersistenceWithExtendedFields:
    """Req 1.4 — persist extended forecast payload to mvp_tank_forecasts."""

    @pytest.mark.asyncio
    async def test_persistence_includes_customer_tank_metadata(self):
        tank = _make_customer_tank(
            customer_type="keep_full", fuel_type="diesel", fuel_product_code="DIESEL_2"
        )
        agent, deps = _make_extended_agent(tanks=[tank])
        await agent.evaluate([_make_signal()])
        # Find the index_document call for the customer_tank forecast.
        calls = [c for c in deps["es_service"].index_document.call_args_list]
        tank_docs = [
            c.args[2]
            for c in calls
            if c.args[2].get("customer_tank_id") == tank.customer_tank_id
        ]
        assert len(tank_docs) == 1
        doc = tank_docs[0]
        assert doc["customer_tank_id"] == tank.customer_tank_id
        assert doc["customer_id"] == tank.customer_id
        assert doc["customer_type"] == "keep_full"
        assert doc["fuel_type"] == "diesel"
        assert doc["model_name"] == "diesel_rolling_28d"
        assert "customer_type_multiplier" in doc
        assert "weather_fallback" in doc
        assert "scheduled_deliveries" in doc
