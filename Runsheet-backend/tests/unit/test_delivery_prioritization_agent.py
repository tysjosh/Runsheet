"""
Unit tests for the DeliveryPrioritizationAgent overlay agent.

Tests cover:
- Constructor and agent_id configuration
- Signal subscription setup (TankForecast messages)
- evaluate() with empty forecasts
- evaluate() computes weighted priority scores (Req 2.2)
- evaluate() assigns priority buckets (Req 2.3)
- evaluate() handles missing SLA tier (Req 2.7)
- evaluate() persists to mvp_delivery_priorities (Req 2.4)
- evaluate() publishes DeliveryPriorityList to SignalBus (Req 2.5)
- _load_scoring_weights() from Redis (Req 2.6)
- _assign_bucket() threshold logic (Req 2.3)
- _compute_priority() weighted scoring (Req 2.2)

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7
"""
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from Agents.overlay.data_contracts import RiskSignal, Severity
from Agents.overlay.delivery_prioritization_agent import (
    CRITICAL_THRESHOLD,
    DEFAULT_SCORING_WEIGHTS,
    DEFAULT_SLA_SCORE,
    DEFAULT_SLA_TIER,
    HIGH_THRESHOLD,
    MEDIUM_THRESHOLD,
    SCORING_WEIGHTS_REDIS_KEY,
    SLA_TIER_SCORES,
    DeliveryPrioritizationAgent,
)
from Agents.support.fuel_distribution_models import (
    DeliveryPriority,
    DeliveryPriorityList,
    FuelGrade,
    PriorityBucket,
    TankForecast,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_forecast(
    station_id="station-1",
    fuel_grade=FuelGrade.AGO,
    runout_risk_24h=0.8,
    confidence=0.7,
    tenant_id="tenant-1",
    run_id="run-1",
):
    return TankForecast(
        station_id=station_id,
        fuel_grade=fuel_grade,
        hours_to_runout_p50=12.0,
        hours_to_runout_p90=8.0,
        runout_risk_24h=runout_risk_24h,
        confidence=confidence,
        tenant_id=tenant_id,
        run_id=run_id,
    )


def _make_deps():
    """Create mocked dependencies for the DeliveryPrioritizationAgent."""
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

    redis_client = MagicMock()
    redis_client.get = AsyncMock(return_value=None)

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "autonomy_config_service": autonomy_config,
        "feature_flag_service": feature_flags,
        "redis_client": redis_client,
    }


def _make_agent(**overrides):
    deps = _make_deps()
    deps.update(overrides)
    return DeliveryPrioritizationAgent(**deps), deps


# ---------------------------------------------------------------------------
# Tests: Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_default_scoring_weights(self):
        assert DEFAULT_SCORING_WEIGHTS == {
            "runout_risk_24h": 0.4,
            "sla_tier": 0.25,
            "travel_time": 0.2,
            "business_impact": 0.15,
        }

    def test_weights_sum_to_one(self):
        total = sum(DEFAULT_SCORING_WEIGHTS.values())
        assert abs(total - 1.0) < 0.001

    def test_bucket_thresholds(self):
        assert CRITICAL_THRESHOLD == 0.8
        assert HIGH_THRESHOLD == 0.6
        assert MEDIUM_THRESHOLD == 0.3


# ---------------------------------------------------------------------------
# Tests: Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_agent_id(self):
        agent, _ = _make_agent()
        assert agent.agent_id == "delivery_prioritization"

    def test_subscription_to_tank_forecast(self):
        agent, _ = _make_agent()
        assert len(agent._subscription_specs) == 1
        spec = agent._subscription_specs[0]
        assert spec["message_type"] is TankForecast

    def test_default_poll_interval(self):
        agent, _ = _make_agent()
        assert agent.poll_interval == 60

    def test_custom_poll_interval(self):
        agent, _ = _make_agent(poll_interval=120)
        assert agent.poll_interval == 120

    def test_forecast_buffer_initially_empty(self):
        agent, _ = _make_agent()
        assert agent._forecast_buffer == []


# ---------------------------------------------------------------------------
# Tests: _on_signal() — TankForecast buffering
# ---------------------------------------------------------------------------


class TestOnSignal:
    @pytest.mark.asyncio
    async def test_buffers_tank_forecast(self):
        agent, _ = _make_agent()
        forecast = _make_forecast()
        await agent._on_signal(forecast)
        assert len(agent._forecast_buffer) == 1
        assert agent._forecast_buffer[0] is forecast

    @pytest.mark.asyncio
    async def test_non_forecast_goes_to_parent(self):
        """Non-TankForecast signals go to the parent signal buffer."""
        agent, _ = _make_agent()
        signal = RiskSignal(
            source_agent="test",
            entity_id="e1",
            entity_type="test",
            severity=Severity.LOW,
            confidence=0.5,
            ttl_seconds=300,
            tenant_id="tenant-1",
        )
        await agent._on_signal(signal)
        assert len(agent._forecast_buffer) == 0


# ---------------------------------------------------------------------------
# Tests: evaluate()
# ---------------------------------------------------------------------------


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_empty_forecasts_returns_empty(self):
        agent, _ = _make_agent()
        # No forecasts buffered, signals list is irrelevant
        result = await agent.evaluate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_produces_priority_list(self):
        """Req 2.1: Produces ranked priority list from forecasts."""
        agent, deps = _make_agent()

        # Buffer a forecast
        forecast = _make_forecast(station_id="station-1", runout_risk_24h=0.9)
        agent._forecast_buffer.append(forecast)

        # Station metadata query returns SLA info
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "station_id": "station-1",
                                "sla_tier": "gold",
                                "travel_time_minutes": 30.0,
                                "business_impact_score": 0.7,
                            }
                        }
                    ]
                }
            }
        )

        result = await agent.evaluate([])
        assert result == []  # Priorities published directly

        # Verify SignalBus publish was called
        assert deps["signal_bus"].publish.call_count == 1
        published = deps["signal_bus"].publish.call_args[0][0]
        assert isinstance(published, DeliveryPriorityList)
        assert len(published.priorities) == 1
        assert published.priorities[0].station_id == "station-1"

    @pytest.mark.asyncio
    async def test_persists_to_es(self):
        """Req 2.4: Priority list persisted to mvp_delivery_priorities."""
        agent, deps = _make_agent()
        agent._forecast_buffer.append(_make_forecast())

        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        await agent.evaluate([])

        assert deps["es_service"].index_document.call_count == 1
        call_args = deps["es_service"].index_document.call_args
        assert call_args[0][0] == "mvp_delivery_priorities"

    @pytest.mark.asyncio
    async def test_priorities_sorted_descending(self):
        """Priorities should be sorted by score descending (most urgent first)."""
        agent, deps = _make_agent()

        agent._forecast_buffer.extend([
            _make_forecast(station_id="low-risk", runout_risk_24h=0.1),
            _make_forecast(station_id="high-risk", runout_risk_24h=0.95),
            _make_forecast(station_id="mid-risk", runout_risk_24h=0.5),
        ])

        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        scores = [p.priority_score for p in published.priorities]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_scoring_weights_included_in_output(self):
        """Req 2.6: Scoring weights are included in the priority list."""
        agent, deps = _make_agent()
        agent._forecast_buffer.append(_make_forecast())

        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        assert published.scoring_weights == DEFAULT_SCORING_WEIGHTS


# ---------------------------------------------------------------------------
# Tests: _assign_bucket() (Req 2.3)
# ---------------------------------------------------------------------------


class TestAssignBucket:
    def test_critical_at_threshold(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.8) == PriorityBucket.CRITICAL

    def test_critical_above_threshold(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.95) == PriorityBucket.CRITICAL

    def test_high_at_threshold(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.6) == PriorityBucket.HIGH

    def test_high_below_critical(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.79) == PriorityBucket.HIGH

    def test_medium_at_threshold(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.3) == PriorityBucket.MEDIUM

    def test_medium_below_high(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.59) == PriorityBucket.MEDIUM

    def test_low_below_medium(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.29) == PriorityBucket.LOW

    def test_low_at_zero(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.0) == PriorityBucket.LOW


# ---------------------------------------------------------------------------
# Tests: _compute_priority() (Req 2.2, 2.7)
# ---------------------------------------------------------------------------


class TestComputePriority:
    def test_high_runout_risk_produces_high_score(self):
        agent, _ = _make_agent()
        forecast = _make_forecast(runout_risk_24h=0.95)
        station_meta = {
            "sla_tier": "platinum",
            "travel_time_minutes": 10.0,
            "business_impact_score": 0.9,
        }
        priority = agent._compute_priority(
            forecast, station_meta, DEFAULT_SCORING_WEIGHTS
        )
        assert priority.priority_score >= 0.8
        assert priority.priority_bucket == PriorityBucket.CRITICAL

    def test_low_runout_risk_produces_low_score(self):
        agent, _ = _make_agent()
        forecast = _make_forecast(runout_risk_24h=0.05)
        station_meta = {
            "sla_tier": "basic",
            "travel_time_minutes": 100.0,
            "business_impact_score": 0.1,
        }
        priority = agent._compute_priority(
            forecast, station_meta, DEFAULT_SCORING_WEIGHTS
        )
        assert priority.priority_score < 0.3
        assert priority.priority_bucket == PriorityBucket.LOW

    def test_missing_sla_tier_defaults_to_lowest(self):
        """Req 2.7: Missing SLA tier defaults to lowest with reason."""
        agent, _ = _make_agent()
        forecast = _make_forecast(runout_risk_24h=0.5)
        station_meta = {}  # No SLA tier
        priority = agent._compute_priority(
            forecast, station_meta, DEFAULT_SCORING_WEIGHTS
        )
        assert "no_sla_tier_configured" in priority.reasons

    def test_unknown_sla_tier_defaults_to_lowest(self):
        """Req 2.7: Unknown SLA tier also defaults to lowest."""
        agent, _ = _make_agent()
        forecast = _make_forecast(runout_risk_24h=0.5)
        station_meta = {"sla_tier": "unknown_tier"}
        priority = agent._compute_priority(
            forecast, station_meta, DEFAULT_SCORING_WEIGHTS
        )
        assert "no_sla_tier_configured" in priority.reasons

    def test_score_bounded_0_to_1(self):
        agent, _ = _make_agent()
        for risk in [0.0, 0.25, 0.5, 0.75, 1.0]:
            forecast = _make_forecast(runout_risk_24h=risk)
            priority = agent._compute_priority(
                forecast, {}, DEFAULT_SCORING_WEIGHTS
            )
            assert 0.0 <= priority.priority_score <= 1.0

    def test_premium_sla_adds_reason(self):
        agent, _ = _make_agent()
        forecast = _make_forecast(runout_risk_24h=0.5)
        station_meta = {"sla_tier": "platinum"}
        priority = agent._compute_priority(
            forecast, station_meta, DEFAULT_SCORING_WEIGHTS
        )
        assert any("premium_sla_tier" in r for r in priority.reasons)

    def test_high_business_impact_adds_reason(self):
        agent, _ = _make_agent()
        forecast = _make_forecast(runout_risk_24h=0.5)
        station_meta = {"business_impact_score": 0.9}
        priority = agent._compute_priority(
            forecast, station_meta, DEFAULT_SCORING_WEIGHTS
        )
        assert any("high_business_impact" in r for r in priority.reasons)


# ---------------------------------------------------------------------------
# Tests: _load_scoring_weights() (Req 2.6)
# ---------------------------------------------------------------------------


class TestLoadScoringWeights:
    @pytest.mark.asyncio
    async def test_no_redis_returns_defaults(self):
        agent, _ = _make_agent(redis_client=None)
        weights = await agent._load_scoring_weights("tenant-1")
        assert weights == DEFAULT_SCORING_WEIGHTS

    @pytest.mark.asyncio
    async def test_redis_returns_custom_weights(self):
        agent, deps = _make_agent()
        custom_weights = {
            "runout_risk_24h": 0.5,
            "sla_tier": 0.2,
            "travel_time": 0.15,
            "business_impact": 0.15,
        }
        deps["redis_client"].get = AsyncMock(
            return_value=json.dumps(custom_weights)
        )
        weights = await agent._load_scoring_weights("tenant-1")
        assert weights == custom_weights

    @pytest.mark.asyncio
    async def test_redis_error_returns_defaults(self):
        agent, deps = _make_agent()
        deps["redis_client"].get = AsyncMock(side_effect=Exception("Redis down"))
        weights = await agent._load_scoring_weights("tenant-1")
        assert weights == DEFAULT_SCORING_WEIGHTS

    @pytest.mark.asyncio
    async def test_redis_invalid_json_returns_defaults(self):
        agent, deps = _make_agent()
        deps["redis_client"].get = AsyncMock(return_value="not-json")
        weights = await agent._load_scoring_weights("tenant-1")
        assert weights == DEFAULT_SCORING_WEIGHTS

    @pytest.mark.asyncio
    async def test_redis_incomplete_weights_returns_defaults(self):
        """If Redis weights are missing required keys, fall back to defaults."""
        agent, deps = _make_agent()
        incomplete = {"runout_risk_24h": 0.5}  # Missing other keys
        deps["redis_client"].get = AsyncMock(
            return_value=json.dumps(incomplete)
        )
        weights = await agent._load_scoring_weights("tenant-1")
        assert weights == DEFAULT_SCORING_WEIGHTS
