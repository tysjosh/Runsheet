"""
Unit tests for the DeliveryPrioritizationAgent overlay agent.

Tests cover:
- Constructor and agent_id configuration
- evaluate() discovers tenants with pending orders from fuel_orders_current
- evaluate() scores keep_full/auto_fill orders via forecast
- evaluate() scores will_call/one_off orders via delivery_window_end
- evaluate() emits scoring_input_missing when inputs are absent
- evaluate() applies storm mode boost for critical tanks
- _assign_bucket() threshold logic
- Tenant isolation via tenant_id filter in ES queries

Requirements: 5.1.1, 5.1.2, 5.1.3, 5.1.5
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.overlay.data_contracts import RiskSignal, Severity
from Agents.overlay.delivery_prioritization_agent import (
    CRITICAL_THRESHOLD,
    DEFAULT_SCORING_WEIGHTS,
    DEFAULT_SLA_SCORE,
    DEFAULT_SLA_TIER,
    HIGH_THRESHOLD,
    LOW_SCORE,
    MEDIUM_THRESHOLD,
    SCORING_WEIGHTS_REDIS_KEY,
    SLA_TIER_SCORES,
    DeliveryPrioritizationAgent,
    _bucket_from_score,
)
from Agents.support.fuel_distribution_models import (
    DeliveryPriority,
    DeliveryPriorityList,
    FuelGrade,
    PriorityBucket,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order(
    order_id="ord_001",
    tenant_id="tenant-1",
    call_type="will_call",
    customer_tank_id=None,
    delivery_window_end=None,
    product_code="DIESEL_2",
    status="placed",
):
    """Create a minimal fuel order dict."""
    order = {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "call_type": call_type,
        "customer_tank_id": customer_tank_id,
        "product_code": product_code,
        "status": status,
    }
    if delivery_window_end is not None:
        order["delivery_window_end"] = delivery_window_end
    return order


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
    return DeliveryPrioritizationAgent(**deps), deps


def _es_response_with_orders(orders):
    """Build an ES search response containing the given order dicts."""
    return {
        "hits": {
            "hits": [{"_source": o} for o in orders],
        }
    }


def _es_response_with_tenants(tenant_ids):
    """Build an ES aggregation response with tenant buckets."""
    return {
        "aggregations": {
            "tenants": {
                "buckets": [{"key": tid} for tid in tenant_ids]
            }
        }
    }


def _es_response_with_forecasts(forecasts_by_tank):
    """Build an ES response with forecast hits keyed by station_id."""
    hits = []
    for tank_id, forecast in forecasts_by_tank.items():
        hits.append({"_source": {"station_id": tank_id, **forecast}})
    return {"hits": {"hits": hits}}


def _es_response_with_tanks(tanks_by_id):
    """Build an ES response with customer tank hits."""
    hits = []
    for tank_id, tank in tanks_by_id.items():
        hits.append({"_source": {"tank_id": tank_id, **tank}})
    return {"hits": {"hits": hits}}


# ---------------------------------------------------------------------------
# Tests: Module constants (backward compat)
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_default_scoring_weights_exported(self):
        """Legacy constant is still exported for backward compat."""
        assert "runout_risk_24h" in DEFAULT_SCORING_WEIGHTS

    def test_sla_tier_scores_exported(self):
        assert SLA_TIER_SCORES["platinum"] == 1.0

    def test_bucket_thresholds(self):
        assert CRITICAL_THRESHOLD == 0.85
        assert HIGH_THRESHOLD == 0.65
        assert MEDIUM_THRESHOLD == 0.40


# ---------------------------------------------------------------------------
# Tests: Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_agent_id(self):
        agent, _ = _make_agent()
        assert agent.agent_id == "delivery_prioritization"

    def test_default_poll_interval(self):
        agent, _ = _make_agent()
        assert agent.poll_interval == 60

    def test_custom_poll_interval(self):
        agent, _ = _make_agent(poll_interval=120)
        assert agent.poll_interval == 120

    def test_accepts_legacy_kwargs(self):
        """Constructor accepts legacy kwargs without error."""
        agent, _ = _make_agent(
            redis_client=MagicMock(),
            combinable_group_repository=MagicMock(),
            customer_profile_loader=None,
            customer_tank_loader=None,
            generator_priority_boost=0.3,
        )
        assert agent.agent_id == "delivery_prioritization"


# ---------------------------------------------------------------------------
# Tests: _assign_bucket() threshold logic
# ---------------------------------------------------------------------------


class TestAssignBucket:
    def test_critical_at_threshold(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.85) == PriorityBucket.CRITICAL

    def test_critical_above_threshold(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.95) == PriorityBucket.CRITICAL

    def test_high_at_threshold(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.65) == PriorityBucket.HIGH

    def test_high_below_critical(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.84) == PriorityBucket.HIGH

    def test_medium_at_threshold(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.40) == PriorityBucket.MEDIUM

    def test_medium_below_high(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.64) == PriorityBucket.MEDIUM

    def test_low_below_medium(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.39) == PriorityBucket.LOW

    def test_low_at_zero(self):
        assert DeliveryPrioritizationAgent._assign_bucket(0.0) == PriorityBucket.LOW


# ---------------------------------------------------------------------------
# Tests: evaluate() — reads from fuel_orders_current
# ---------------------------------------------------------------------------


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_no_pending_orders_returns_empty(self):
        """No tenants with pending orders → empty result."""
        agent, deps = _make_agent()
        # Tenant discovery returns no buckets
        deps["es_service"].search_documents = AsyncMock(
            return_value={"aggregations": {"tenants": {"buckets": []}}}
        )
        result = await agent.evaluate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_discovers_tenants_from_fuel_orders_current(self):
        """Tenant discovery queries fuel_orders_current with pending statuses."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            return_value={"aggregations": {"tenants": {"buckets": []}}}
        )
        await agent.evaluate([])

        call_args = deps["es_service"].search_documents.call_args
        assert call_args[0][0] == "fuel_orders_current"
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]
        assert {"terms": {"status": ["placed", "confirmed", "scheduled"]}} in filters

    @pytest.mark.asyncio
    async def test_fetches_orders_with_tenant_filter(self):
        """Order fetch includes tenant_id filter for isolation."""
        agent, deps = _make_agent()

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Tenant discovery
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                # Order fetch — verify tenant filter
                filters = query["query"]["bool"]["filter"]
                assert {"term": {"tenant_id": "tenant-1"}} in filters
                assert {"terms": {"status": ["placed", "confirmed", "scheduled"]}} in filters
                return _es_response_with_orders([])
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

    @pytest.mark.asyncio
    async def test_publishes_priority_list_to_signal_bus(self):
        """Scored orders are published as a DeliveryPriorityList."""
        agent, deps = _make_agent()

        now = datetime.now(timezone.utc)
        window_end = (now + timedelta(hours=2)).isoformat()
        orders = [
            _make_order(
                order_id="ord_001",
                call_type="will_call",
                delivery_window_end=window_end,
            )
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

        assert deps["signal_bus"].publish.call_count == 1
        published = deps["signal_bus"].publish.call_args[0][0]
        assert isinstance(published, DeliveryPriorityList)
        assert len(published.priorities) == 1
        assert published.tenant_id == "tenant-1"

    @pytest.mark.asyncio
    async def test_returns_intervention_proposal(self):
        """evaluate() returns InterventionProposals for downstream agents."""
        agent, deps = _make_agent()

        now = datetime.now(timezone.utc)
        window_end = (now + timedelta(hours=2)).isoformat()
        orders = [
            _make_order(call_type="one_off", delivery_window_end=window_end)
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        result = await agent.evaluate([])

        assert len(result) == 1
        assert result[0].source_agent == "delivery_prioritization"
        assert result[0].tenant_id == "tenant-1"


# ---------------------------------------------------------------------------
# Tests: Scoring — keep_full / auto_fill via forecast
# ---------------------------------------------------------------------------


class TestForecastBasedScoring:
    @pytest.mark.asyncio
    async def test_keep_full_scores_via_forecast(self):
        """keep_full orders score via linked customer_tank_id forecast."""
        agent, deps = _make_agent()

        orders = [
            _make_order(
                order_id="ord_kf",
                call_type="keep_full",
                customer_tank_id="tank-1",
            )
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            elif index == "mvp_tank_forecasts":
                return _es_response_with_forecasts(
                    {"tank-1": {"hours_to_runout_p90": 6.0}}
                )
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        priority = published.priorities[0]
        # 6 hours → critical range (0.85-1.0)
        assert priority.priority_score >= 0.85
        assert priority.priority_bucket == PriorityBucket.CRITICAL
        assert "scoring_input_missing" not in priority.reasons

    @pytest.mark.asyncio
    async def test_auto_fill_scores_via_forecast(self):
        """auto_fill orders also score via forecast."""
        agent, deps = _make_agent()

        orders = [
            _make_order(
                order_id="ord_af",
                call_type="auto_fill",
                customer_tank_id="tank-2",
            )
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            elif index == "mvp_tank_forecasts":
                return _es_response_with_forecasts(
                    {"tank-2": {"hours_to_runout_p90": 36.0}}
                )
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        priority = published.priorities[0]
        # 36 hours → medium range (0.40-0.65)
        assert 0.40 <= priority.priority_score < 0.65
        assert priority.priority_bucket == PriorityBucket.MEDIUM

    @pytest.mark.asyncio
    async def test_missing_tank_id_scores_low_with_flag(self):
        """keep_full without customer_tank_id → scoring_input_missing + LOW."""
        agent, deps = _make_agent()

        orders = [
            _make_order(
                order_id="ord_no_tank",
                call_type="keep_full",
                customer_tank_id=None,
            )
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        priority = published.priorities[0]
        assert priority.priority_score == LOW_SCORE
        assert "scoring_input_missing" in priority.reasons
        assert "no_customer_tank_id" in priority.reasons

    @pytest.mark.asyncio
    async def test_missing_forecast_scores_low_with_flag(self):
        """keep_full with tank_id but no forecast → scoring_input_missing."""
        agent, deps = _make_agent()

        orders = [
            _make_order(
                order_id="ord_no_fc",
                call_type="keep_full",
                customer_tank_id="tank-missing",
            )
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            elif index == "mvp_tank_forecasts":
                # No forecast for tank-missing
                return {"hits": {"hits": []}}
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        priority = published.priorities[0]
        assert priority.priority_score == LOW_SCORE
        assert "scoring_input_missing" in priority.reasons
        assert "no_forecast_available" in priority.reasons


# ---------------------------------------------------------------------------
# Tests: Scoring — will_call / one_off via delivery_window_end
# ---------------------------------------------------------------------------


class TestWindowBasedScoring:
    @pytest.mark.asyncio
    async def test_will_call_scores_via_window_end(self):
        """will_call orders score via delivery_window_end proximity."""
        agent, deps = _make_agent()

        now = datetime.now(timezone.utc)
        # Window ending in 2 hours → urgent
        window_end = (now + timedelta(hours=2)).isoformat()
        orders = [
            _make_order(
                order_id="ord_wc",
                call_type="will_call",
                delivery_window_end=window_end,
            )
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        priority = published.priorities[0]
        # 2 hours until end → urgent range (0.85-1.0)
        assert priority.priority_score >= 0.85
        assert "scoring_input_missing" not in priority.reasons

    @pytest.mark.asyncio
    async def test_one_off_overdue_scores_max(self):
        """one_off with past-due window → score 1.0."""
        agent, deps = _make_agent()

        now = datetime.now(timezone.utc)
        window_end = (now - timedelta(hours=1)).isoformat()
        orders = [
            _make_order(
                order_id="ord_overdue",
                call_type="one_off",
                delivery_window_end=window_end,
            )
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        priority = published.priorities[0]
        assert priority.priority_score == 1.0
        assert "window_overdue" in priority.reasons

    @pytest.mark.asyncio
    async def test_missing_window_end_scores_low_with_flag(self):
        """will_call without delivery_window_end → scoring_input_missing."""
        agent, deps = _make_agent()

        orders = [
            _make_order(
                order_id="ord_no_window",
                call_type="will_call",
                # No delivery_window_end
            )
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        priority = published.priorities[0]
        assert priority.priority_score == LOW_SCORE
        assert "scoring_input_missing" in priority.reasons
        assert "no_delivery_window_end" in priority.reasons

    @pytest.mark.asyncio
    async def test_far_future_window_scores_low(self):
        """Window far in the future → low score."""
        agent, deps = _make_agent()

        now = datetime.now(timezone.utc)
        window_end = (now + timedelta(hours=96)).isoformat()
        orders = [
            _make_order(
                order_id="ord_far",
                call_type="one_off",
                delivery_window_end=window_end,
            )
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        priority = published.priorities[0]
        assert priority.priority_score < MEDIUM_THRESHOLD


# ---------------------------------------------------------------------------
# Tests: Storm mode boost
# ---------------------------------------------------------------------------


class TestStormModeBoosts:
    @pytest.mark.asyncio
    async def test_storm_boost_for_critical_tank(self):
        """Storm mode boosts orders with critical criticality_tier."""
        storm_eval = MagicMock()
        storm_state = MagicMock()
        storm_state.state = "active"
        storm_eval.get_state = AsyncMock(return_value=storm_state)

        agent, deps = _make_agent(storm_mode_evaluator=storm_eval)

        now = datetime.now(timezone.utc)
        window_end = (now + timedelta(hours=18)).isoformat()
        orders = [
            _make_order(
                order_id="ord_storm",
                call_type="will_call",
                customer_tank_id="tank-crit",
                delivery_window_end=window_end,
            )
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            elif index == "customer_tanks":
                return _es_response_with_tanks(
                    {"tank-crit": {"criticality_tier": "critical"}}
                )
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        priority = published.priorities[0]
        assert any("storm_boost" in r for r in priority.reasons)

    @pytest.mark.asyncio
    async def test_no_boost_when_storm_inactive(self):
        """No boost when storm mode is inactive."""
        storm_eval = MagicMock()
        storm_state = MagicMock()
        storm_state.state = "inactive"
        storm_eval.get_state = AsyncMock(return_value=storm_state)

        agent, deps = _make_agent(storm_mode_evaluator=storm_eval)

        now = datetime.now(timezone.utc)
        window_end = (now + timedelta(hours=18)).isoformat()
        orders = [
            _make_order(
                order_id="ord_no_storm",
                call_type="will_call",
                customer_tank_id="tank-crit",
                delivery_window_end=window_end,
            )
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            elif index == "customer_tanks":
                return _es_response_with_tanks(
                    {"tank-crit": {"criticality_tier": "critical"}}
                )
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        priority = published.priorities[0]
        assert not any("storm_boost" in r for r in priority.reasons)


# ---------------------------------------------------------------------------
# Tests: Priority ordering
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    @pytest.mark.asyncio
    async def test_priorities_sorted_descending(self):
        """Priorities are sorted by score descending (most urgent first)."""
        agent, deps = _make_agent()

        now = datetime.now(timezone.utc)
        orders = [
            _make_order(
                order_id="ord_low",
                call_type="one_off",
                delivery_window_end=(now + timedelta(hours=48)).isoformat(),
            ),
            _make_order(
                order_id="ord_high",
                call_type="one_off",
                delivery_window_end=(now + timedelta(hours=1)).isoformat(),
            ),
            _make_order(
                order_id="ord_mid",
                call_type="one_off",
                delivery_window_end=(now + timedelta(hours=10)).isoformat(),
            ),
        ]

        call_count = [0]

        async def mock_search(index, query, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _es_response_with_tenants(["tenant-1"])
            elif call_count[0] == 2:
                return _es_response_with_orders(orders)
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)
        await agent.evaluate([])

        published = deps["signal_bus"].publish.call_args[0][0]
        scores = [p.priority_score for p in published.priorities]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Tests: Product code to FuelGrade mapping
# ---------------------------------------------------------------------------


class TestFuelGradeMapping:
    def test_diesel_2_maps_to_ago(self):
        agent, _ = _make_agent()
        assert agent._resolve_fuel_grade("DIESEL_2") == FuelGrade.AGO

    def test_gasoline_reg_maps_to_pms(self):
        agent, _ = _make_agent()
        assert agent._resolve_fuel_grade("GASOLINE_REG") == FuelGrade.PMS

    def test_kerosene_maps_to_atk(self):
        agent, _ = _make_agent()
        assert agent._resolve_fuel_grade("KEROSENE") == FuelGrade.ATK

    def test_propane_maps_to_lpg(self):
        agent, _ = _make_agent()
        assert agent._resolve_fuel_grade("PROPANE") == FuelGrade.LPG

    def test_none_defaults_to_ago(self):
        agent, _ = _make_agent()
        assert agent._resolve_fuel_grade(None) == FuelGrade.AGO

    def test_unknown_defaults_to_ago(self):
        agent, _ = _make_agent()
        assert agent._resolve_fuel_grade("UNKNOWN_FUEL") == FuelGrade.AGO
