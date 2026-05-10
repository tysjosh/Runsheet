"""
Unit tests for the Compartment_Loading_Agent dyed-diesel enforcement.

Task 9.8 / Requirements 6.3, 6.4 of the fuel-compliance-backbone spec
wire :meth:`DyedDieselEnforcer.validate_load_plan` into the
CompartmentLoadingAgent so every proposed compartment assignment
involving dyed diesel is validated against the compartment's
dyed-compatible flag before the plan is committed to ``mvp_load_plans``.

These tests exercise:

* The ``set_dyed_diesel_enforcer`` setter injects the enforcer.
* Dyed-diesel assignments to dyed-compatible compartments pass through.
* Dyed-diesel assignments to clear-only compartments are rejected with
  ``dyed.compartment_incompatible`` and stripped from the plan.
* Non-dyed-diesel assignments are never checked (pass through unchanged).
* When no enforcer is configured, all assignments pass through (graceful
  degradation).
* Rejected volume is charged to ``unserved_demand_liters``.
* If the enforcer raises an exception, the assignment is allowed
  (fail-open).

Validates: Requirements 6.3, 6.4.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Agents.overlay.compartment_loading_agent import CompartmentLoadingAgent
from Agents.support.compartment_models import (
    CompartmentAssignment,
    LoadingPlan,
)
from compliance.services.dyed_diesel_enforcer import (
    DyedDieselEnforcer,
    ValidationResult,
)


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _make_deps():
    """Build the standard mocked dependency dict used by the agent."""

    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    es_service.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
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


def _make_agent(deps: Dict[str, Any]) -> CompartmentLoadingAgent:
    """Construct a CompartmentLoadingAgent with mocked dependencies."""
    return CompartmentLoadingAgent(
        signal_bus=deps["signal_bus"],
        es_service=deps["es_service"],
        activity_log_service=deps["activity_log_service"],
        ws_manager=deps["ws_manager"],
        confirmation_protocol=deps["confirmation_protocol"],
        autonomy_config_service=deps["autonomy_config_service"],
        feature_flag_service=deps["feature_flag_service"],
    )


def _make_assignment(
    compartment_id: str = "comp_1",
    fuel_grade: str = "OFF_ROAD_DIESEL",
    quantity_liters: float = 5000.0,
    station_id: str = "station_1",
    compartment_capacity_liters: float = 10000.0,
) -> CompartmentAssignment:
    """Build a CompartmentAssignment for testing."""
    return CompartmentAssignment(
        compartment_id=compartment_id,
        fuel_grade=fuel_grade,
        quantity_liters=quantity_liters,
        station_id=station_id,
        compartment_capacity_liters=compartment_capacity_liters,
    )


def _make_loading_plan(
    assignments: List[CompartmentAssignment],
    tenant_id: str = "tenant_1",
    truck_id: str = "truck_1",
    plan_id: str = "plan_1",
) -> LoadingPlan:
    """Build a LoadingPlan for testing."""
    total_capacity = sum(a.compartment_capacity_liters for a in assignments)
    total_volume = sum(a.quantity_liters for a in assignments)
    utilization = (total_volume / total_capacity * 100) if total_capacity else 0.0

    return LoadingPlan(
        plan_id=plan_id,
        truck_id=truck_id,
        tenant_id=tenant_id,
        assignments=assignments,
        total_utilization_pct=round(utilization, 2),
        unserved_demand_liters=0.0,
        total_weight_kg=round(total_volume * 0.85, 2),
    )


# ---------------------------------------------------------------------------
# Tests: set_dyed_diesel_enforcer setter
# ---------------------------------------------------------------------------


class TestSetDyedDieselEnforcer:
    """Tests for the set_dyed_diesel_enforcer setter method."""

    def test_setter_stores_enforcer(self):
        """The setter stores the enforcer on the agent instance."""
        deps = _make_deps()
        agent = _make_agent(deps)

        enforcer = MagicMock(spec=DyedDieselEnforcer)
        agent.set_dyed_diesel_enforcer(enforcer)

        assert agent._dyed_diesel_enforcer is enforcer

    def test_setter_accepts_none(self):
        """The setter accepts None to disable the enforcer."""
        deps = _make_deps()
        agent = _make_agent(deps)

        agent.set_dyed_diesel_enforcer(None)

        assert agent._dyed_diesel_enforcer is None


# ---------------------------------------------------------------------------
# Tests: _enforce_dyed_diesel_compliance
# ---------------------------------------------------------------------------


class TestEnforceDyedDieselCompliance:
    """Tests for the _enforce_dyed_diesel_compliance method."""

    @pytest.mark.asyncio
    async def test_no_enforcer_passes_all_assignments(self):
        """When no enforcer is configured, all assignments pass through."""
        deps = _make_deps()
        agent = _make_agent(deps)
        # No enforcer set — _dyed_diesel_enforcer attribute doesn't exist

        dyed_assignment = _make_assignment(fuel_grade="OFF_ROAD_DIESEL")
        plan = _make_loading_plan([dyed_assignment])

        result = await agent._enforce_dyed_diesel_compliance(
            loading_plan=plan,
            tenant_id="tenant_1",
        )

        assert len(result.assignments) == 1
        assert result.assignments[0] is dyed_assignment

    @pytest.mark.asyncio
    async def test_non_dyed_product_not_checked(self):
        """Non-dyed-diesel products are never validated by the enforcer."""
        deps = _make_deps()
        agent = _make_agent(deps)

        enforcer = MagicMock(spec=DyedDieselEnforcer)
        enforcer.is_dyed_diesel = DyedDieselEnforcer.is_dyed_diesel
        enforcer.validate_load_plan = AsyncMock()
        agent.set_dyed_diesel_enforcer(enforcer)

        clear_assignment = _make_assignment(fuel_grade="DIESEL_2")
        plan = _make_loading_plan([clear_assignment])

        result = await agent._enforce_dyed_diesel_compliance(
            loading_plan=plan,
            tenant_id="tenant_1",
        )

        assert len(result.assignments) == 1
        enforcer.validate_load_plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_dyed_diesel_compatible_compartment_passes(self):
        """Dyed diesel assigned to a dyed-compatible compartment passes."""
        deps = _make_deps()
        agent = _make_agent(deps)

        enforcer = MagicMock(spec=DyedDieselEnforcer)
        enforcer.is_dyed_diesel = DyedDieselEnforcer.is_dyed_diesel
        enforcer.validate_load_plan = AsyncMock(
            return_value=ValidationResult(valid=True)
        )
        agent.set_dyed_diesel_enforcer(enforcer)

        dyed_assignment = _make_assignment(
            compartment_id="comp_dyed",
            fuel_grade="OFF_ROAD_DIESEL",
        )
        plan = _make_loading_plan([dyed_assignment])

        result = await agent._enforce_dyed_diesel_compliance(
            loading_plan=plan,
            tenant_id="tenant_1",
        )

        assert len(result.assignments) == 1
        assert result.assignments[0] is dyed_assignment
        enforcer.validate_load_plan.assert_called_once_with(
            tenant_id="tenant_1",
            compartment_id="comp_dyed",
            product_code="OFF_ROAD_DIESEL",
        )

    @pytest.mark.asyncio
    async def test_dyed_diesel_clear_only_compartment_rejected(self):
        """Dyed diesel assigned to a clear-only compartment is rejected.

        Validates: Requirement 6.4.
        """
        deps = _make_deps()
        agent = _make_agent(deps)

        enforcer = MagicMock(spec=DyedDieselEnforcer)
        enforcer.is_dyed_diesel = DyedDieselEnforcer.is_dyed_diesel
        enforcer.validate_load_plan = AsyncMock(
            return_value=ValidationResult(
                valid=False,
                error_code="dyed.compartment_incompatible",
                message="Compartment 'comp_clear' is designated as clear-only",
            )
        )
        agent.set_dyed_diesel_enforcer(enforcer)

        dyed_assignment = _make_assignment(
            compartment_id="comp_clear",
            fuel_grade="OFF_ROAD_DIESEL",
            quantity_liters=5000.0,
        )
        plan = _make_loading_plan([dyed_assignment])

        result = await agent._enforce_dyed_diesel_compliance(
            loading_plan=plan,
            tenant_id="tenant_1",
        )

        # Assignment should be stripped
        assert len(result.assignments) == 0
        # Rejected volume charged to unserved_demand_liters
        assert result.unserved_demand_liters == 5000.0

    @pytest.mark.asyncio
    async def test_mixed_assignments_only_dyed_rejected(self):
        """Only dyed-diesel assignments to clear-only compartments are rejected.

        Non-dyed assignments and dyed assignments to compatible compartments
        are kept.
        """
        deps = _make_deps()
        agent = _make_agent(deps)

        enforcer = MagicMock(spec=DyedDieselEnforcer)
        enforcer.is_dyed_diesel = DyedDieselEnforcer.is_dyed_diesel

        async def _mock_validate(tenant_id, compartment_id, product_code):
            if compartment_id == "comp_clear":
                return ValidationResult(
                    valid=False,
                    error_code="dyed.compartment_incompatible",
                    message="Clear-only compartment",
                )
            return ValidationResult(valid=True)

        enforcer.validate_load_plan = AsyncMock(side_effect=_mock_validate)
        agent.set_dyed_diesel_enforcer(enforcer)

        clear_diesel = _make_assignment(
            compartment_id="comp_1",
            fuel_grade="DIESEL_2",
            quantity_liters=4000.0,
        )
        dyed_compatible = _make_assignment(
            compartment_id="comp_dyed",
            fuel_grade="DYED_DIESEL",
            quantity_liters=3000.0,
        )
        dyed_rejected = _make_assignment(
            compartment_id="comp_clear",
            fuel_grade="OFF_ROAD_DIESEL",
            quantity_liters=2000.0,
        )

        plan = _make_loading_plan([clear_diesel, dyed_compatible, dyed_rejected])

        result = await agent._enforce_dyed_diesel_compliance(
            loading_plan=plan,
            tenant_id="tenant_1",
        )

        # Only the clear-only compartment assignment should be rejected
        assert len(result.assignments) == 2
        assert result.assignments[0] is clear_diesel
        assert result.assignments[1] is dyed_compatible
        assert result.unserved_demand_liters == 2000.0

    @pytest.mark.asyncio
    async def test_enforcer_exception_fails_open(self):
        """If the enforcer raises, the assignment is allowed (fail-open)."""
        deps = _make_deps()
        agent = _make_agent(deps)

        enforcer = MagicMock(spec=DyedDieselEnforcer)
        enforcer.is_dyed_diesel = DyedDieselEnforcer.is_dyed_diesel
        enforcer.validate_load_plan = AsyncMock(
            side_effect=RuntimeError("ES connection failed")
        )
        agent.set_dyed_diesel_enforcer(enforcer)

        dyed_assignment = _make_assignment(fuel_grade="OFF_ROAD_DIESEL")
        plan = _make_loading_plan([dyed_assignment])

        result = await agent._enforce_dyed_diesel_compliance(
            loading_plan=plan,
            tenant_id="tenant_1",
        )

        # Assignment should pass through despite the exception
        assert len(result.assignments) == 1
        assert result.unserved_demand_liters == 0.0

    @pytest.mark.asyncio
    async def test_all_dyed_product_codes_checked(self):
        """All recognized dyed-diesel product codes trigger validation.

        Validates: Requirement 6.3.
        """
        deps = _make_deps()
        agent = _make_agent(deps)

        enforcer = MagicMock(spec=DyedDieselEnforcer)
        enforcer.is_dyed_diesel = DyedDieselEnforcer.is_dyed_diesel
        enforcer.validate_load_plan = AsyncMock(
            return_value=ValidationResult(valid=True)
        )
        agent.set_dyed_diesel_enforcer(enforcer)

        dyed_codes = ["OFF_ROAD_DIESEL", "DYED_DIESEL", "DYED_ULSD", "OFF_ROAD_ULSD"]

        for code in dyed_codes:
            assignment = _make_assignment(
                compartment_id=f"comp_{code}",
                fuel_grade=code,
            )
            plan = _make_loading_plan([assignment])

            await agent._enforce_dyed_diesel_compliance(
                loading_plan=plan,
                tenant_id="tenant_1",
            )

        # Each dyed code should have triggered a validate_load_plan call
        assert enforcer.validate_load_plan.call_count == len(dyed_codes)


# ---------------------------------------------------------------------------
# Tests: Bootstrap wiring
# ---------------------------------------------------------------------------


class TestBootstrapWiring:
    """Tests verifying the bootstrap wires the enforcer into the agent."""

    def test_agent_has_setter_method(self):
        """CompartmentLoadingAgent exposes set_dyed_diesel_enforcer."""
        assert hasattr(CompartmentLoadingAgent, "set_dyed_diesel_enforcer")
        assert callable(CompartmentLoadingAgent.set_dyed_diesel_enforcer)
