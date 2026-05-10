"""
Unit tests for the low_tank_autofill_alert notification wiring in
TankForecastingAgent.

Validates: Requirement 12.5 — WHEN a customer tank level drops below the
configured auto-fill trigger threshold, THE Notification_Template_Service
SHALL send the low_tank_autofill_alert via the customer's preferred channel
(email or SMS).

Tests cover:
- Notification fires when tank level < reorder_point for auto_fill customers
- Notification fires for keep_full customers below reorder_point
- Notification does NOT fire for will_call customers
- Notification does NOT fire when level >= reorder_point
- Notification does NOT fire when notification_service is not wired
- Deduplication: only one alert per tank per forecast cycle
- Custom reorder_point_percent from tenant config is respected
- Notification failure does not break the forecast cycle
- Event data payload contains all required template placeholders
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.overlay.data_contracts import RiskSignal, Severity
from Agents.overlay.tank_forecasting_agent import (
    DEFAULT_REORDER_POINT_PERCENT,
    TankForecastingAgent,
)
from Agents.support.fuel_distribution_models import FuelGrade, TankForecast
from fuel.customer_tank_models import CustomerTank


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(notification_service=None, tenant_config=None):
    """Create a TankForecastingAgent with mocked dependencies."""
    signal_bus = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)
    es_service = AsyncMock()
    es_service.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    es_service.index_document = AsyncMock()
    activity_log_service = AsyncMock()
    ws_manager = AsyncMock()
    confirmation_protocol = AsyncMock()
    autonomy_config_service = AsyncMock()
    feature_flag_service = AsyncMock()

    agent = TankForecastingAgent(
        signal_bus=signal_bus,
        es_service=es_service,
        activity_log_service=activity_log_service,
        ws_manager=ws_manager,
        confirmation_protocol=confirmation_protocol,
        autonomy_config_service=autonomy_config_service,
        feature_flag_service=feature_flag_service,
        tenant_config=tenant_config,
    )

    if notification_service is not None:
        agent.set_notification_service(notification_service)

    return agent


def _make_customer_tank(
    customer_tank_id="tank-1",
    tenant_id="tenant-1",
    customer_id="customer-1",
    customer_type="auto_fill",
    fuel_type="propane",
    capacity_gallons=1000.0,
    current_level_gallons=200.0,  # 20% — below default 25% reorder point
    zip_code="10001",
):
    """Create a CustomerTank instance for testing."""
    return CustomerTank(
        customer_tank_id=customer_tank_id,
        tenant_id=tenant_id,
        customer_id=customer_id,
        customer_type=customer_type,
        fuel_type=fuel_type,
        fuel_product_code="PROPANE",
        capacity_gallons=capacity_gallons,
        current_level_gallons=current_level_gallons,
        location_lat=40.7128,
        location_lon=-74.0060,
        zip_code=zip_code,
        status="active",
    )


def _make_forecast(
    customer_tank_id="tank-1",
    tenant_id="tenant-1",
    hours_to_runout_p50=48.0,
    scheduled_deliveries=None,
):
    """Create a TankForecast for testing."""
    return TankForecast(
        station_id=customer_tank_id,
        fuel_grade=FuelGrade.LPG,
        hours_to_runout_p50=hours_to_runout_p50,
        hours_to_runout_p90=36.0,
        runout_risk_24h=0.6,
        confidence=0.8,
        tenant_id=tenant_id,
        run_id="test_run",
        customer_tank_id=customer_tank_id,
        customer_id="customer-1",
        customer_type="auto_fill",
        fuel_type="propane",
        scheduled_deliveries=scheduled_deliveries or [],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLowTankAutofillAlert:
    """Tests for the low_tank_autofill_alert notification wiring."""

    @pytest.mark.asyncio
    async def test_fires_when_level_below_reorder_point_auto_fill(self):
        """Notification fires for auto_fill tank below reorder point."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(notification_service=notification_service)

        tank = _make_customer_tank(
            current_level_gallons=200.0,  # 20% of 1000 — below 25%
            customer_type="auto_fill",
        )
        forecast = _make_forecast()

        await agent._check_low_tank_autofill_alert(tank, forecast)

        notification_service.notify_event.assert_called_once()
        call_kwargs = notification_service.notify_event.call_args
        assert call_kwargs[1]["event_type"] == "low_tank_autofill_alert" or \
               call_kwargs[0][0] == "low_tank_autofill_alert"

    @pytest.mark.asyncio
    async def test_fires_for_keep_full_customers(self):
        """Notification fires for keep_full tank below reorder point."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(notification_service=notification_service)

        tank = _make_customer_tank(
            current_level_gallons=150.0,  # 15% of 1000 — below 25%
            customer_type="keep_full",
        )
        forecast = _make_forecast()

        await agent._check_low_tank_autofill_alert(tank, forecast)

        notification_service.notify_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_fire_for_will_call_customers(self):
        """Notification does NOT fire for will_call customers."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(notification_service=notification_service)

        tank = _make_customer_tank(
            current_level_gallons=100.0,  # 10% — well below threshold
            customer_type="will_call",
        )
        forecast = _make_forecast()

        await agent._check_low_tank_autofill_alert(tank, forecast)

        notification_service.notify_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_for_residential_customers(self):
        """Notification does NOT fire for residential customers."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(notification_service=notification_service)

        tank = _make_customer_tank(
            current_level_gallons=100.0,
            customer_type="residential",
        )
        forecast = _make_forecast()

        await agent._check_low_tank_autofill_alert(tank, forecast)

        notification_service.notify_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_when_level_above_reorder_point(self):
        """Notification does NOT fire when level >= reorder_point."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(notification_service=notification_service)

        tank = _make_customer_tank(
            current_level_gallons=300.0,  # 30% — above 25% threshold
            customer_type="auto_fill",
        )
        forecast = _make_forecast()

        await agent._check_low_tank_autofill_alert(tank, forecast)

        notification_service.notify_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_when_level_equals_reorder_point(self):
        """Notification does NOT fire when level == reorder_point exactly."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(notification_service=notification_service)

        tank = _make_customer_tank(
            current_level_gallons=250.0,  # Exactly 25%
            customer_type="auto_fill",
        )
        forecast = _make_forecast()

        await agent._check_low_tank_autofill_alert(tank, forecast)

        notification_service.notify_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_when_notification_service_not_wired(self):
        """No error when notification_service is None."""
        agent = _make_agent(notification_service=None)

        tank = _make_customer_tank(current_level_gallons=100.0)
        forecast = _make_forecast()

        # Should not raise
        await agent._check_low_tank_autofill_alert(tank, forecast)

    @pytest.mark.asyncio
    async def test_deduplication_within_forecast_cycle(self):
        """Only one alert per tank per forecast cycle."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(notification_service=notification_service)

        tank = _make_customer_tank(current_level_gallons=100.0)
        forecast = _make_forecast()

        # First call fires
        await agent._check_low_tank_autofill_alert(tank, forecast)
        assert notification_service.notify_event.call_count == 1

        # Second call for same tank is deduplicated
        await agent._check_low_tank_autofill_alert(tank, forecast)
        assert notification_service.notify_event.call_count == 1

    @pytest.mark.asyncio
    async def test_deduplication_resets_on_new_cycle(self):
        """Deduplication set is cleared between forecast cycles."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(notification_service=notification_service)

        tank = _make_customer_tank(current_level_gallons=100.0)
        forecast = _make_forecast()

        # First cycle
        await agent._check_low_tank_autofill_alert(tank, forecast)
        assert notification_service.notify_event.call_count == 1

        # Simulate new cycle by clearing the set (as evaluate() does)
        agent._alerted_tanks.clear()

        # Second cycle fires again
        await agent._check_low_tank_autofill_alert(tank, forecast)
        assert notification_service.notify_event.call_count == 2

    @pytest.mark.asyncio
    async def test_custom_reorder_point_from_tenant_config(self):
        """Custom reorder_point_percent from tenant config is respected."""
        import json

        # Set up tenant config to return 40% reorder point
        tenant_config = AsyncMock()
        tenant_config.get = AsyncMock(
            return_value=json.dumps({"reorder_point_percent": 40.0})
        )

        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(
            notification_service=notification_service,
            tenant_config=tenant_config,
        )

        # Tank at 35% — below custom 40% threshold
        tank = _make_customer_tank(current_level_gallons=350.0)
        forecast = _make_forecast()

        await agent._check_low_tank_autofill_alert(tank, forecast)

        notification_service.notify_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_custom_reorder_point_above_level_no_fire(self):
        """Tank above custom reorder point does not fire."""
        import json

        # Set up tenant config to return 10% reorder point
        tenant_config = AsyncMock()
        tenant_config.get = AsyncMock(
            return_value=json.dumps({"reorder_point_percent": 10.0})
        )

        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(
            notification_service=notification_service,
            tenant_config=tenant_config,
        )

        # Tank at 20% — above custom 10% threshold
        tank = _make_customer_tank(current_level_gallons=200.0)
        forecast = _make_forecast()

        await agent._check_low_tank_autofill_alert(tank, forecast)

        notification_service.notify_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_break_forecast(self):
        """Notification service failure is swallowed gracefully."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(
            side_effect=RuntimeError("Notification service down")
        )
        agent = _make_agent(notification_service=notification_service)

        tank = _make_customer_tank(current_level_gallons=100.0)
        forecast = _make_forecast()

        # Should not raise
        await agent._check_low_tank_autofill_alert(tank, forecast)

    @pytest.mark.asyncio
    async def test_event_data_contains_required_placeholders(self):
        """Event data payload contains all template placeholders."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(notification_service=notification_service)

        tank = _make_customer_tank(
            current_level_gallons=150.0,  # 15%
            customer_id="cust-abc",
            zip_code="90210",
        )
        forecast = _make_forecast(
            hours_to_runout_p50=72.0,
            scheduled_deliveries=[
                {
                    "delivery_id": "del-1",
                    "scheduled_eta": "2025-01-15T10:00:00Z",
                    "planned_gallons": 500.0,
                }
            ],
        )

        await agent._check_low_tank_autofill_alert(tank, forecast)

        call_args = notification_service.notify_event.call_args
        # Extract event_data from positional or keyword args
        if call_args[1]:
            event_data = call_args[1].get("event_data", call_args[0][1] if len(call_args[0]) > 1 else None)
        else:
            event_data = call_args[0][1]

        # Verify all required placeholders are present
        assert "customer_id" in event_data
        assert "customer_name" in event_data
        assert "tank_location" in event_data
        assert "current_level_percent" in event_data
        assert "estimated_days_to_empty" in event_data
        assert "scheduled_delivery_date" in event_data

        # Verify values
        assert event_data["customer_id"] == "cust-abc"
        assert event_data["current_level_percent"] == 15.0
        assert event_data["estimated_days_to_empty"] == 3.0  # 72h / 24
        assert event_data["scheduled_delivery_date"] == "2025-01-15"
        assert event_data["tank_location"] == "90210"

    @pytest.mark.asyncio
    async def test_zero_capacity_tank_does_not_fire(self):
        """Tank with zero capacity does not trigger notification."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(notification_service=notification_service)

        # Can't create a tank with 0 capacity via the model (gt=0 constraint),
        # so we test the guard by patching capacity_gallons
        tank = _make_customer_tank(current_level_gallons=0.0, capacity_gallons=1.0)
        # Manually override for the test
        object.__setattr__(tank, "capacity_gallons", 0.0)
        forecast = _make_forecast()

        await agent._check_low_tank_autofill_alert(tank, forecast)

        notification_service.notify_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_tenant_id_passed_to_notify_event(self):
        """The correct tenant_id is passed to notify_event."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(notification_service=notification_service)

        tank = _make_customer_tank(
            tenant_id="my-tenant",
            current_level_gallons=100.0,
        )
        forecast = _make_forecast(tenant_id="my-tenant")

        await agent._check_low_tank_autofill_alert(tank, forecast)

        call_args = notification_service.notify_event.call_args
        # Check tenant_id is passed correctly
        if call_args[1]:
            assert call_args[1].get("tenant_id") == "my-tenant" or \
                   call_args[0][2] == "my-tenant"
        else:
            assert call_args[0][2] == "my-tenant"

    @pytest.mark.asyncio
    async def test_no_scheduled_delivery_shows_tbd(self):
        """When no scheduled delivery exists, date shows TBD."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        agent = _make_agent(notification_service=notification_service)

        tank = _make_customer_tank(current_level_gallons=100.0)
        forecast = _make_forecast(scheduled_deliveries=[])

        await agent._check_low_tank_autofill_alert(tank, forecast)

        call_args = notification_service.notify_event.call_args
        if call_args[1]:
            event_data = call_args[1].get("event_data", call_args[0][1] if len(call_args[0]) > 1 else None)
        else:
            event_data = call_args[0][1]

        assert event_data["scheduled_delivery_date"] == "TBD"
