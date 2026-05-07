"""
Unit tests for Task 7.6 wiring — CompartmentLoadingAgent invokes the
ContractLiftService on Loading_Plan commit.

Covers:

* ``LoadingPlan.contract_id`` is an optional field (default ``None``).
* ``_record_contract_lift`` is a no-op when ``contract_id`` is ``None`` or
  empty, or when the lift service is the default no-op stand-in.
* ``_record_contract_lift`` sums ``quantity_liters`` across assignments,
  converts to gallons via the exact NIST factor, and calls
  :meth:`ContractLiftService.record_lift` exactly once per commit.
* ``_persist_loading_plan`` calls ``_record_contract_lift`` after the
  mvp_load_plans write, not before — the counter never gets bumped for a
  plan that failed to land in ES.
* Transient lift-service exceptions are logged but never raised, so a
  Redis outage cannot invalidate a plan that already landed in
  ``mvp_load_plans``.

Validates: Requirement 8.3.4.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.compartment_loading_agent import CompartmentLoadingAgent
from Agents.support.compartment_models import (
    CompartmentAssignment,
    LoadingPlan,
)
from fuel.services.contract_lift_service import ContractLiftService


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _RecordingLiftService:
    """Records ``record_lift`` calls so tests can assert on them.

    Doesn't touch Redis — the real :class:`ContractLiftService` is
    covered by ``tests/unit/test_contract_lift_service.py``; here we only
    care that the agent calls ``record_lift`` with the right arguments.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def record_lift(
        self,
        tenant_id: str,
        contract_id: str,
        gallons: float,
        *,
        moment: Optional[datetime] = None,
    ) -> float:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "contract_id": contract_id,
                "gallons": gallons,
                "moment": moment,
            }
        )
        return gallons


def _make_deps():
    """Create mocked dependencies for the CompartmentLoadingAgent."""

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


def _make_plan(
    *,
    contract_id: Optional[str] = None,
    tenant_id: str = "tenant-1",
    quantities_liters: Optional[List[float]] = None,
) -> LoadingPlan:
    if quantities_liters is None:
        quantities_liters = [3785.411784, 7570.823568]  # = 1000 gal + 2000 gal
    assignments = [
        CompartmentAssignment(
            compartment_id=f"comp-{idx}",
            station_id=f"station-{idx}",
            fuel_grade="DIESEL_2",
            quantity_liters=q,
            compartment_capacity_liters=max(q * 1.2, 10000.0),
        )
        for idx, q in enumerate(quantities_liters, start=1)
    ]
    return LoadingPlan(
        plan_id="plan-001",
        truck_id="truck-1",
        assignments=assignments,
        total_utilization_pct=75.0,
        tenant_id=tenant_id,
        run_id="run-1",
        contract_id=contract_id,
    )


# ---------------------------------------------------------------------------
# LoadingPlan model extension
# ---------------------------------------------------------------------------


class TestLoadingPlanContractId:
    def test_contract_id_defaults_to_none(self):
        plan = _make_plan()
        assert plan.contract_id is None
        assert plan.terminal_id is None

    def test_contract_id_optional_value_persists(self):
        plan = _make_plan(contract_id="sc_001")
        assert plan.contract_id == "sc_001"


# ---------------------------------------------------------------------------
# _record_contract_lift
# ---------------------------------------------------------------------------


class TestRecordContractLift:
    @pytest.mark.asyncio
    async def test_noop_when_contract_id_missing(self):
        deps = _make_deps()
        lift = _RecordingLiftService()
        agent = CompartmentLoadingAgent(**deps, contract_lift_service=lift)

        plan = _make_plan(contract_id=None)
        await agent._record_contract_lift(plan)

        assert lift.calls == []

    @pytest.mark.asyncio
    async def test_noop_when_no_assignments(self):
        deps = _make_deps()
        lift = _RecordingLiftService()
        agent = CompartmentLoadingAgent(**deps, contract_lift_service=lift)

        plan = LoadingPlan(
            plan_id="plan-empty",
            truck_id="truck-1",
            assignments=[
                CompartmentAssignment(
                    compartment_id="c1",
                    station_id="s1",
                    fuel_grade="DIESEL_2",
                    quantity_liters=0.001,  # positive required by the schema
                    compartment_capacity_liters=10000.0,
                ),
            ],
            total_utilization_pct=0.0,
            tenant_id="tenant-1",
            contract_id="sc_001",
        )
        # Set quantity to 0 after construction to test the zero-branch.
        plan.assignments[0].quantity_liters = 0.0

        await agent._record_contract_lift(plan)
        assert lift.calls == []

    @pytest.mark.asyncio
    async def test_sums_assignments_and_converts_to_gallons(self):
        deps = _make_deps()
        lift = _RecordingLiftService()
        agent = CompartmentLoadingAgent(**deps, contract_lift_service=lift)

        # 3785.411784 L = 1000 gal; 7570.823568 L = 2000 gal → total 3000 gal
        plan = _make_plan(contract_id="sc_001")
        await agent._record_contract_lift(plan)

        assert len(lift.calls) == 1
        call = lift.calls[0]
        assert call["tenant_id"] == "tenant-1"
        assert call["contract_id"] == "sc_001"
        assert math.isclose(call["gallons"], 3000.0, rel_tol=1e-9, abs_tol=1e-6)

    @pytest.mark.asyncio
    async def test_conversion_factor_matches_nist(self):
        """Exact NIST factor: 1 gallon = 3.785411784 liters."""

        deps = _make_deps()
        lift = _RecordingLiftService()
        agent = CompartmentLoadingAgent(**deps, contract_lift_service=lift)

        plan = _make_plan(
            contract_id="sc_001",
            quantities_liters=[3.785411784],
        )
        await agent._record_contract_lift(plan)

        assert math.isclose(lift.calls[0]["gallons"], 1.0, rel_tol=1e-12)

    @pytest.mark.asyncio
    async def test_tolerates_lift_service_exception(self):
        """Redis hiccup in the lift service must never propagate."""

        deps = _make_deps()
        failing = MagicMock()
        failing.record_lift = AsyncMock(side_effect=ConnectionError("redis down"))
        agent = CompartmentLoadingAgent(**deps, contract_lift_service=failing)

        # Must not raise despite the lift service blowing up.
        plan = _make_plan(contract_id="sc_001")
        await agent._record_contract_lift(plan)

        failing.record_lift.assert_awaited_once()


# ---------------------------------------------------------------------------
# _persist_loading_plan calls _record_contract_lift
# ---------------------------------------------------------------------------


class TestPersistLoadingPlanInvokesLift:
    @pytest.mark.asyncio
    async def test_commit_bumps_counter_for_contract_sourced_plan(self):
        deps = _make_deps()
        lift = _RecordingLiftService()
        agent = CompartmentLoadingAgent(**deps, contract_lift_service=lift)

        plan = _make_plan(contract_id="sc_001")
        await agent._persist_loading_plan(plan)

        # mvp_load_plans write must have succeeded.
        deps["es_service"].index_document.assert_awaited_once()
        # Counter must have been bumped exactly once.
        assert len(lift.calls) == 1
        assert lift.calls[0]["contract_id"] == "sc_001"

    @pytest.mark.asyncio
    async def test_commit_skips_counter_when_plan_has_no_contract(self):
        deps = _make_deps()
        lift = _RecordingLiftService()
        agent = CompartmentLoadingAgent(**deps, contract_lift_service=lift)

        plan = _make_plan(contract_id=None)
        await agent._persist_loading_plan(plan)

        deps["es_service"].index_document.assert_awaited_once()
        assert lift.calls == []

    @pytest.mark.asyncio
    async def test_commit_skips_counter_when_es_write_fails(self):
        """If mvp_load_plans write fails, the counter is not bumped.

        This preserves the invariant that the counter is derived from
        persisted loading plans — a counter bump without a committed
        plan would drift the aggregate.
        """

        deps = _make_deps()
        deps["es_service"].index_document = AsyncMock(
            side_effect=RuntimeError("ES down")
        )
        lift = _RecordingLiftService()
        agent = CompartmentLoadingAgent(**deps, contract_lift_service=lift)

        plan = _make_plan(contract_id="sc_001")
        # _persist_loading_plan swallows the ES error and returns early.
        await agent._persist_loading_plan(plan)

        assert lift.calls == []

    @pytest.mark.asyncio
    async def test_set_contract_lift_service_replaces_default(self):
        """Post-construction wiring via ``set_contract_lift_service`` works."""

        deps = _make_deps()
        agent = CompartmentLoadingAgent(**deps)

        # Default is the no-op ContractLiftService(redis_client=None); the
        # counter call is silent but we can still swap it out.
        replacement = _RecordingLiftService()
        agent.set_contract_lift_service(replacement)

        plan = _make_plan(contract_id="sc_001")
        await agent._persist_loading_plan(plan)

        assert len(replacement.calls) == 1

    @pytest.mark.asyncio
    async def test_set_contract_lift_service_none_falls_back_to_noop(self):
        """Passing ``None`` restores the default no-op wrapper."""

        deps = _make_deps()
        agent = CompartmentLoadingAgent(**deps)
        agent.set_contract_lift_service(None)

        # The default wrapper is a real ContractLiftService with no Redis,
        # so record_lift returns 0.0 without raising. We verify the agent
        # still commits the plan successfully.
        plan = _make_plan(contract_id="sc_001")
        await agent._persist_loading_plan(plan)
        deps["es_service"].index_document.assert_awaited_once()


# ---------------------------------------------------------------------------
# Integration: agent with a real ContractLiftService and fake Redis
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal async Redis for end-to-end counter bumping."""

    def __init__(self) -> None:
        self.store: Dict[str, float] = {}
        self.ttls: Dict[str, int] = {}

    async def incrbyfloat(self, key: str, amount: float) -> float:
        self.store[key] = self.store.get(key, 0.0) + float(amount)
        return self.store[key]

    async def get(self, key: str) -> Optional[str]:
        value = self.store.get(key)
        return str(value) if value is not None else None

    async def expire(self, key: str, ttl_seconds: int) -> None:
        self.ttls[key] = int(ttl_seconds)


class TestAgentEndToEnd:
    @pytest.mark.asyncio
    async def test_commit_increments_redis_counter_exactly_once(self):
        deps = _make_deps()
        redis = _FakeRedis()
        lift = ContractLiftService(redis_client=redis)
        agent = CompartmentLoadingAgent(**deps, contract_lift_service=lift)

        plan = _make_plan(contract_id="sc_001")
        await agent._persist_loading_plan(plan)

        # Exactly one key should have been written with the canonical
        # ``contract_lift:{tenant_id}:{contract_id}:{YYYY-MM}`` shape.
        assert len(redis.store) == 1
        (key,) = redis.store.keys()
        assert key.startswith("contract_lift:tenant-1:sc_001:")
        assert math.isclose(redis.store[key], 3000.0, rel_tol=1e-9, abs_tol=1e-6)
        # The first write stamps the 62-day TTL.
        assert redis.ttls[key] == 62 * 24 * 60 * 60

    @pytest.mark.asyncio
    async def test_two_commits_accumulate_same_month(self):
        deps = _make_deps()
        redis = _FakeRedis()
        lift = ContractLiftService(redis_client=redis)
        agent = CompartmentLoadingAgent(**deps, contract_lift_service=lift)

        plan1 = _make_plan(contract_id="sc_001", quantities_liters=[3785.411784])
        plan2 = _make_plan(contract_id="sc_001", quantities_liters=[7570.823568])
        await agent._persist_loading_plan(plan1)
        await agent._persist_loading_plan(plan2)

        # 1000 gal + 2000 gal = 3000 gal in the single monthly bucket.
        (key,) = redis.store.keys()
        assert math.isclose(redis.store[key], 3000.0, rel_tol=1e-9, abs_tol=1e-6)
