"""
Unit tests for :class:`FuelPlanningWSManager`.

Ensures the WebSocket manager created by Task 3.6:

* Formats the ``customer_tank_forecast_ready`` payload per Req 1.6.4
  (``run_id``, ``tenant_id``, ``customer_tank_id``, ``fuel_type``,
  ``runout_risk_24h``, ``model_name``).
* Passes auxiliary fields through the ``extra`` parameter without
  letting them overwrite any of the mandatory fields.
* Broadcasts over the generic ``type/data/timestamp`` envelope used by
  other WS managers in the platform.

Validates: Requirement 1.6.4.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from fuel.services.fuel_planning_ws_manager import (
    FuelPlanningWSManager,
    get_fuel_planning_ws_manager,
)


@pytest.fixture
def manager() -> FuelPlanningWSManager:
    mgr = FuelPlanningWSManager()
    # Replace the base ``broadcast`` with a spy so we don't need real
    # WebSocket clients; we only care about what the manager emits.
    mgr.broadcast = AsyncMock(return_value=0)  # type: ignore[assignment]
    return mgr


class TestCustomerTankForecastReady:
    @pytest.mark.asyncio
    async def test_payload_carries_mandatory_fields(self, manager):
        await manager.broadcast_customer_tank_forecast_ready(
            run_id="run-1",
            tenant_id="tenant-a",
            customer_tank_id="ct-42",
            fuel_type="propane",
            runout_risk_24h=0.73,
            model_name="propane_k_factor",
        )

        manager.broadcast.assert_awaited_once()
        envelope = manager.broadcast.await_args.args[0]
        assert envelope["type"] == "customer_tank_forecast_ready"
        assert "timestamp" in envelope
        # Timestamp must be ISO-8601 parseable so the FE can ingest it.
        datetime.fromisoformat(envelope["timestamp"])

        data = envelope["data"]
        assert data == {
            "run_id": "run-1",
            "tenant_id": "tenant-a",
            "customer_tank_id": "ct-42",
            "fuel_type": "propane",
            "runout_risk_24h": 0.73,
            "model_name": "propane_k_factor",
        }

    @pytest.mark.asyncio
    async def test_extra_fields_are_merged_without_overwriting_required(
        self, manager
    ):
        await manager.broadcast_customer_tank_forecast_ready(
            run_id="run-2",
            tenant_id="tenant-a",
            customer_tank_id="ct-99",
            fuel_type="heating_oil",
            runout_risk_24h=0.15,
            model_name="heating_oil_hdd",
            extra={
                "customer_type": "residential",
                "weather_fallback": True,
                # Attempt to spoof a required field — must be ignored.
                "run_id": "EVIL",
                "customer_tank_id": "EVIL",
            },
        )

        envelope = manager.broadcast.await_args.args[0]
        data = envelope["data"]
        # Required fields remain authoritative.
        assert data["run_id"] == "run-2"
        assert data["customer_tank_id"] == "ct-99"
        # Auxiliary fields present.
        assert data["customer_type"] == "residential"
        assert data["weather_fallback"] is True

    @pytest.mark.asyncio
    async def test_runout_risk_is_coerced_to_float(self, manager):
        await manager.broadcast_customer_tank_forecast_ready(
            run_id="run-3",
            tenant_id="tenant-a",
            customer_tank_id="ct-1",
            fuel_type="diesel",
            runout_risk_24h=1,  # int on purpose
            model_name="diesel_rolling",
        )
        data = manager.broadcast.await_args.args[0]["data"]
        assert data["runout_risk_24h"] == 1.0
        assert isinstance(data["runout_risk_24h"], float)


class TestSingletonAccessor:
    def test_returns_same_instance_each_call(self):
        a = get_fuel_planning_ws_manager()
        b = get_fuel_planning_ws_manager()
        assert a is b
        assert isinstance(a, FuelPlanningWSManager)
