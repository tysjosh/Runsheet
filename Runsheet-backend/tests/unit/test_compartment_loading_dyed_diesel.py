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
* When no enforcer is configured, a plan carrying dyed diesel is blocked
  with ``DyedDieselCheckUnavailable`` (fail closed, OI-02); a plan with no
  dyed diesel passes through.
* Rejected volume is charged to ``unserved_demand_liters``.
* If the enforcer raises an exception, the plan is blocked with
  ``DyedDieselCheckUnavailable`` and an ERROR is logged (fail closed, OI-02).

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
    DyedDieselCheckUnavailable,
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
    async def test_no_enforcer_blocks_dyed_plan(self, caplog):
        """No enforcer wired and dyed diesel on the plan: blocked (OI-02)."""
        deps = _make_deps()
        agent = _make_agent(deps)
        # No enforcer set — _dyed_diesel_enforcer attribute doesn't exist

        dyed_assignment = _make_assignment(fuel_grade="OFF_ROAD_DIESEL")
        plan = _make_loading_plan([dyed_assignment])

        with caplog.at_level("ERROR"):
            with pytest.raises(DyedDieselCheckUnavailable) as excinfo:
                await agent._enforce_dyed_diesel_compliance(
                    loading_plan=plan,
                    tenant_id="tenant_1",
                )

        assert excinfo.value.reason == "enforcer_not_wired"
        assert excinfo.value.details["plan_id"] == "plan_1"
        assert excinfo.value.details["truck_id"] == "truck_1"
        assert any(
            r.levelname == "ERROR" and "blocking plan plan_1" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_no_enforcer_passes_non_dyed_plan(self):
        """No enforcer wired but no dyed diesel: the check does not apply."""
        deps = _make_deps()
        agent = _make_agent(deps)

        clear = _make_assignment(fuel_grade="DIESEL_2")
        plan = _make_loading_plan([clear])

        result, stripped = await agent._enforce_dyed_diesel_compliance(
            loading_plan=plan,
            tenant_id="tenant_1",
        )

        assert result is plan
        assert stripped == []

    @pytest.mark.asyncio
    async def test_enforcer_not_a_dyed_diesel_enforcer_blocks_dyed_plan(self):
        """A wired object that is not a DyedDieselEnforcer cannot verify: blocked."""
        deps = _make_deps()
        agent = _make_agent(deps)
        agent.set_dyed_diesel_enforcer(object())

        plan = _make_loading_plan([_make_assignment(fuel_grade="DYED_DIESEL")])

        with pytest.raises(DyedDieselCheckUnavailable) as excinfo:
            await agent._enforce_dyed_diesel_compliance(
                loading_plan=plan,
                tenant_id="tenant_1",
            )
        assert excinfo.value.reason == "enforcer_not_wired"

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

        result, stripped = await agent._enforce_dyed_diesel_compliance(
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

        result, stripped = await agent._enforce_dyed_diesel_compliance(
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

        result, stripped = await agent._enforce_dyed_diesel_compliance(
            loading_plan=plan,
            tenant_id="tenant_1",
        )

        # Assignment should be stripped
        assert len(result.assignments) == 0
        assert stripped == [dyed_assignment]
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

        result, stripped = await agent._enforce_dyed_diesel_compliance(
            loading_plan=plan,
            tenant_id="tenant_1",
        )

        # Only the clear-only compartment assignment should be rejected
        assert len(result.assignments) == 2
        assert result.assignments[0] is clear_diesel
        assert result.assignments[1] is dyed_compatible
        assert result.unserved_demand_liters == 2000.0

    @pytest.mark.asyncio
    async def test_enforcer_exception_blocks_plan(self, caplog):
        """If the enforcer raises, the plan is blocked (fail closed, OI-02)."""
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

        with caplog.at_level("ERROR"):
            with pytest.raises(DyedDieselCheckUnavailable) as excinfo:
                await agent._enforce_dyed_diesel_compliance(
                    loading_plan=plan,
                    tenant_id="tenant_1",
                )

        exc = excinfo.value
        assert exc.reason == "enforcer_error"
        assert exc.details["compartment_id"] == "comp_1"
        assert exc.details["cause"] == "RuntimeError"
        assert isinstance(exc.__cause__, RuntimeError)
        assert exc.status_code == 503
        assert any(
            r.levelname == "ERROR" and "blocking the plan" in r.getMessage()
            for r in caplog.records
        )

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


# ---------------------------------------------------------------------------
# Tests: evaluate() withholds a blocked plan (OI-02)
# ---------------------------------------------------------------------------


def _hit(truck_id: str, capacity: float) -> Dict[str, Any]:
    return {
        "_source": {
            "compartment_id": "c0",
            "truck_id": truck_id,
            "capacity_liters": capacity,
            "allowed_grades": ["AGO"],
            "position_index": 0,
            "tenant_id": "tenant-1",
        }
    }


def _order_doc(order_id: str, product_code: str) -> Dict[str, Any]:
    return {
        "order_id": order_id,
        "customer_id": f"cust-{order_id}",
        "customer_tank_id": None,
        "product_code": product_code,
        "gallons_requested": 300.0,
        "fill_to_full": False,
        "status": "placed",
        "tenant_id": "tenant-1",
    }


class TestEvaluateFailClosed:
    @pytest.mark.asyncio
    async def test_evaluate_blocks_dyed_plan_when_enforcer_raises(self):
        """The dyed truck's plan is withheld; the clear truck's plan proceeds."""
        from Agents.overlay.data_contracts import RiskSignal
        from Agents.support.fuel_distribution_models import (
            DeliveryPriority,
            DeliveryPriorityList,
            FuelGrade,
            PriorityBucket,
        )

        deps = _make_deps()
        orders = [
            _order_doc("ord_D", "OFF_ROAD_DIESEL"),
            _order_doc("ord_C", "DIESEL_2"),
        ]
        # One 1200 L compartment per truck: the two products cannot share a
        # compartment, so each order lands on its own truck.
        hits = [_hit("truck-A", 1200.0), _hit("truck-B", 1200.0)]

        async def _search(index, query=None, size=None):
            if index == "fuel_orders_current":
                return {"hits": {"hits": [{"_source": o} for o in orders]}}
            if index == "truck_compartments":
                return {"hits": {"hits": hits}}
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=_search)
        agent = _make_agent(deps)
        agent._compartment_state_repo = MagicMock()
        agent._compartment_state_repo.mark_loaded = AsyncMock()

        enforcer = MagicMock(spec=DyedDieselEnforcer)
        enforcer.validate_load_plan = AsyncMock(
            side_effect=RuntimeError("ES connection failed")
        )
        agent.set_dyed_diesel_enforcer(enforcer)

        agent._priority_buffer.append(DeliveryPriorityList(
            priorities=[
                DeliveryPriority(
                    station_id="ord_D", fuel_grade=FuelGrade.AGO,
                    priority_score=0.9, priority_bucket=PriorityBucket.CRITICAL,
                ),
                DeliveryPriority(
                    station_id="ord_C", fuel_grade=FuelGrade.AGO,
                    priority_score=0.8, priority_bucket=PriorityBucket.CRITICAL,
                ),
            ],
            tenant_id="tenant-1",
            run_id="run-1",
        ))

        proposals = await agent.evaluate([])

        persisted = [
            c.args[2] for c in deps["es_service"].index_document.await_args_list
            if c.args and c.args[0] == "mvp_load_plans"
        ]
        assert [p["truck_id"] for p in persisted] == ["truck-B"]
        assert {a["order_id"] for a in persisted[0]["assignments"]} == {"ord_C"}
        assert len(proposals) == 1
        assert proposals[0].actions[0]["parameters"]["truck_id"] == "truck-B"

        reasons = agent.cycle_metrics.get("degradation_reasons") or []
        [blocked] = [
            r for r in reasons if r["reason_code"] == "dyed_diesel_check_unavailable"
        ]
        assert blocked["detail"]
        assert "truck-A" in blocked["detail"]
        assert blocked["blocked_trucks"] == ["truck-A"]
        assert blocked["blocked_orders"] == ["ord_D"]

        assert [
            (e["order_id"], e["reason"]) for e in agent.last_unplaced_orders
        ] == [("ord_D", "dyed_diesel_check_unavailable")]
        fail_signals = [
            c.args[0] for c in deps["signal_bus"].publish.call_args_list
            if isinstance(c.args[0], RiskSignal)
            and c.args[0].entity_type == "fuel_order"
        ]
        assert [(s.entity_id, s.context["reason"]) for s in fail_signals] == [
            ("ord_D", "dyed_diesel_check_unavailable")
        ]
