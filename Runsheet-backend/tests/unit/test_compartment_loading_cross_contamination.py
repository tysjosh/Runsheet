"""
Unit tests for the Compartment_Loading_Agent cross-contamination guard.

Task 6.5 / Requirements 7.2.2, 7.2.3, 7.2.6 of the fuel-ops hardening
spec wire :func:`fuel.services.compatibility_matrix.check_compatibility`
into the Compartment_Loading_Agent so every proposed compartment
assignment is evaluated before the plan lands in ``mvp_load_plans``.

These tests exercise the three spec contracts:

* An assignment with an **incompatible previous product** is rejected
  with reason ``cross_contamination_blocked`` (Req 7.2.2).
* An assignment gated by a ``requires_cleaning`` rule without a fresh
  :class:`CleaningEvent` is rejected with reason ``cleaning_required``
  (Req 7.2.3).
* On every rejection the agent **persists a
  :class:`CrossContaminationViolation`** to the
  ``cross_contamination_events`` ES index and **publishes a
  ``cross_contamination_violation`` RiskSignal** on the SignalBus
  (Req 7.2.6).

The tests also lock in a couple of downstream invariants that fall out
of the same code path:

* Rejected assignments never appear in ``mvp_load_plans`` — the
  index_document payload only contains the retained assignments.
* Rejected volume is charged to ``unserved_demand_liters`` so the
  prioritization agent and dispatch KPIs see the blocked delivery as
  unmet demand rather than silently disappearing.
* Assignments whose previous product is the same as the attempted
  product are allowed (matrix short-circuit) and never emit a
  violation.
* Compartments with no prior load (``last_loaded_product=None``) are
  allowed unconditionally.

Validates: Requirements 7.2.2, 7.2.3, 7.2.6.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.compartment_loading_agent import CompartmentLoadingAgent
from Agents.overlay.data_contracts import RiskSignal, Severity
from Agents.support.fuel_distribution_models import (
    DeliveryPriority,
    DeliveryPriorityList,
    FuelGrade,
    PriorityBucket,
)
from Agents.support.mvp_es_mappings import MVP_LOAD_PLANS_INDEX
from fuel.compartment_state_models import (
    CROSS_CONTAMINATION_VIOLATION_ENTITY_TYPE,
)
from fuel.services.compatibility_matrix import (
    REASON_CLEANING_REQUIRED,
    REASON_CROSS_CONTAMINATION_BLOCKED,
)
from fuel.services.fuel_ops_es_mappings import CROSS_CONTAMINATION_EVENTS_INDEX


NOW = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)


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


def _make_agent(**overrides):
    deps = _make_deps()
    deps.update(overrides)
    agent = CompartmentLoadingAgent(**deps)
    # Stub out the compartment-state OCC path so the post-persist
    # ``_record_compartment_loads`` call does not try to talk to a real
    # ES client. The cross-contamination guard runs before that call so
    # the stub has no effect on the behaviour under test.
    agent._compartment_state_repo = MagicMock()
    agent._compartment_state_repo.mark_loaded = AsyncMock()
    return agent, deps


def _compartment_hit(
    *,
    compartment_id: str = "c1",
    truck_id: str = "truck-1",
    tenant_id: str = "tenant-1",
    capacity_liters: float = 12000.0,
    allowed_grades: List[str] = None,
    position_index: int = 0,
    last_loaded_product: str = None,
    last_loaded_at: datetime = None,
    last_cleaned_at: datetime = None,
    state: str = "clean",
) -> Dict[str, Any]:
    """Build one truck_compartments hit matching the search API shape."""

    if allowed_grades is None:
        allowed_grades = ["AGO", "PMS", "ATK", "LPG"]
    source: Dict[str, Any] = {
        "compartment_id": compartment_id,
        "truck_id": truck_id,
        "capacity_liters": capacity_liters,
        "allowed_grades": allowed_grades,
        "position_index": position_index,
        "tenant_id": tenant_id,
        "state": state,
    }
    if last_loaded_product is not None:
        source["last_loaded_product"] = last_loaded_product
    if last_loaded_at is not None:
        source["last_loaded_at"] = last_loaded_at.isoformat()
    if last_cleaned_at is not None:
        source["last_cleaned_at"] = last_cleaned_at.isoformat()
    return {"_source": source}


def _priority_list(
    *,
    fuel_grade: FuelGrade,
    priority_score: float = 0.9,
    priority_bucket: PriorityBucket = PriorityBucket.CRITICAL,
    tenant_id: str = "tenant-1",
    run_id: str = "run-1",
    station_id: str = "s1",
) -> DeliveryPriorityList:
    return DeliveryPriorityList(
        priorities=[
            DeliveryPriority(
                station_id=station_id,
                fuel_grade=fuel_grade,
                priority_score=priority_score,
                priority_bucket=priority_bucket,
                reasons=["test"],
            ),
        ],
        scoring_weights={"runout_risk_24h": 0.4},
        tenant_id=tenant_id,
        run_id=run_id,
    )


def _install_compartments(deps: Dict[str, Any], *hits: Dict[str, Any]) -> None:
    """Wire the ES mock so the agent sees exactly these compartment hits.

    The agent issues searches in this order:
    1. ``fuel_orders_current`` — for the new order-based delivery request path.
       We return empty so the agent falls back to the legacy priority-list path.
    2. ``truck_compartments`` — for available trucks.
    3. ``inventory`` — for fuel_equipment availability check.
       We return empty so the fail-open equipment branch keeps the truck.
    """

    call_count = [0]

    async def _search(index: str, query: Dict[str, Any], size: int = None):
        call_count[0] += 1
        if index == "fuel_orders_current":
            # Return empty so the agent falls back to legacy priority-list path
            return {"hits": {"hits": []}}
        if call_count[0] <= 2:
            # First non-fuel-orders call is truck_compartments
            return {"hits": {"hits": list(hits)}}
        return {"hits": {"hits": []}}

    deps["es_service"].search_documents = AsyncMock(side_effect=_search)


def _violation_index_calls(es_service) -> List[Any]:
    """Return the ``index_document`` calls targetting the cross-contamination index."""

    out = []
    for call in es_service.index_document.call_args_list:
        args, kwargs = call
        index_name = args[0] if args else kwargs.get("index")
        if index_name == CROSS_CONTAMINATION_EVENTS_INDEX:
            out.append(call)
    return out


def _load_plan_index_calls(es_service) -> List[Any]:
    out = []
    for call in es_service.index_document.call_args_list:
        args, kwargs = call
        index_name = args[0] if args else kwargs.get("index")
        if index_name == MVP_LOAD_PLANS_INDEX:
            out.append(call)
    return out


def _contamination_signals(signal_bus) -> List[RiskSignal]:
    out: List[RiskSignal] = []
    for call in signal_bus.publish.call_args_list:
        signal = call.args[0] if call.args else call.kwargs.get("signal")
        if (
            isinstance(signal, RiskSignal)
            and signal.entity_type == CROSS_CONTAMINATION_VIOLATION_ENTITY_TYPE
        ):
            out.append(signal)
    return out


# ---------------------------------------------------------------------------
# Tests — blocked transitions (Req 7.2.2)
# ---------------------------------------------------------------------------


class TestBlockedTransitions:
    @pytest.mark.asyncio
    async def test_heating_oil_to_gasoline_is_blocked(self):
        """HEATING_OIL → GASOLINE_REG is blocked regardless of cleaning (Req 7.2.1)."""

        agent, deps = _make_agent()
        # Prior heating oil load plus a fresh cleaning record — the rule
        # is still ``blocked``, so cleaning age does not matter.
        _install_compartments(
            deps,
            _compartment_hit(
                last_loaded_product="HEATING_OIL",
                last_loaded_at=NOW - timedelta(days=3),
                last_cleaned_at=NOW - timedelta(hours=1),
                state="loaded",
            ),
        )
        agent._priority_buffer.append(_priority_list(fuel_grade=FuelGrade.PMS))

        proposals = await agent.evaluate([])

        # Plan was dropped entirely because its only assignment was
        # blocked — no mvp_load_plans write, no intervention proposal.
        assert proposals == []
        assert _load_plan_index_calls(deps["es_service"]) == []

        # A violation was persisted to cross_contamination_events with
        # reason=cross_contamination_blocked.
        violation_calls = _violation_index_calls(deps["es_service"])
        assert len(violation_calls) == 1
        violation_doc = violation_calls[0].args[2]
        assert violation_doc["decision"] == "blocked"
        assert violation_doc["reason"] == REASON_CROSS_CONTAMINATION_BLOCKED
        assert violation_doc["governing_rule"] == "blocked"
        assert violation_doc["previous_product"] == "HEATING_OIL"
        assert violation_doc["attempted_product"] == "GASOLINE_REG"
        assert violation_doc["compartment_id"] == "truck-1_c1"
        assert violation_doc["truck_id"] == "truck-1"
        assert violation_doc["tenant_id"] == "tenant-1"
        assert violation_doc["actor_id"] == "compartment_loading"

        # A matching RiskSignal fired on the bus with severity=HIGH.
        signals = _contamination_signals(deps["signal_bus"])
        assert len(signals) == 1
        signal = signals[0]
        assert signal.severity == Severity.HIGH
        assert signal.tenant_id == "tenant-1"
        assert signal.entity_id == "truck-1_c1"
        assert signal.context["decision"] == "blocked"
        assert signal.context["reason"] == REASON_CROSS_CONTAMINATION_BLOCKED
        assert signal.context["previous_product"] == "HEATING_OIL"
        assert signal.context["attempted_product"] == "GASOLINE_REG"


# ---------------------------------------------------------------------------
# Tests — requires_cleaning transitions (Req 7.2.3)
# ---------------------------------------------------------------------------


class TestRequiresCleaning:
    @pytest.mark.asyncio
    async def test_gasoline_to_diesel_without_cleaning_requires_cleaning(self):
        """GASOLINE_REG → DIESEL_2 with stale cleaning is gated (Req 7.2.3)."""

        agent, deps = _make_agent()
        _install_compartments(
            deps,
            _compartment_hit(
                last_loaded_product="GASOLINE_REG",
                last_loaded_at=NOW - timedelta(hours=2),
                last_cleaned_at=NOW - timedelta(days=2),
                state="loaded",
            ),
        )
        agent._priority_buffer.append(_priority_list(fuel_grade=FuelGrade.AGO))

        proposals = await agent.evaluate([])

        assert proposals == []
        assert _load_plan_index_calls(deps["es_service"]) == []

        violation_calls = _violation_index_calls(deps["es_service"])
        assert len(violation_calls) == 1
        doc = violation_calls[0].args[2]
        assert doc["decision"] == "requires_cleaning"
        assert doc["reason"] == REASON_CLEANING_REQUIRED
        assert doc["governing_rule"] == "requires_cleaning"
        assert doc["previous_product"] == "GASOLINE_REG"
        assert doc["attempted_product"] == "DIESEL_2"

        signals = _contamination_signals(deps["signal_bus"])
        assert len(signals) == 1
        # requires_cleaning is MEDIUM severity (not HIGH) — the
        # compartment can still be recovered by a cleaning event.
        assert signals[0].severity == Severity.MEDIUM
        assert signals[0].context["reason"] == REASON_CLEANING_REQUIRED

    @pytest.mark.asyncio
    async def test_requires_cleaning_downgrades_to_allowed_when_cleaned(self):
        """Req 7.2.4: fresh cleaning lets the gated load proceed."""

        agent, deps = _make_agent()
        # Same pair, but cleaning is newer than the last load.
        _install_compartments(
            deps,
            _compartment_hit(
                last_loaded_product="GASOLINE_REG",
                last_loaded_at=NOW - timedelta(days=1),
                last_cleaned_at=NOW,
                state="clean",
            ),
        )
        agent._priority_buffer.append(_priority_list(fuel_grade=FuelGrade.AGO))

        proposals = await agent.evaluate([])

        # Plan went through — one proposal, one mvp_load_plans write,
        # no violations, no RiskSignal.
        assert len(proposals) == 1
        assert len(_load_plan_index_calls(deps["es_service"])) == 1
        assert _violation_index_calls(deps["es_service"]) == []
        assert _contamination_signals(deps["signal_bus"]) == []


# ---------------------------------------------------------------------------
# Tests — allowed transitions short-circuit without side effects
# ---------------------------------------------------------------------------


class TestAllowedTransitions:
    @pytest.mark.asyncio
    async def test_same_product_chain_is_allowed(self):
        """Identical prev/next products never trip the guard."""

        agent, deps = _make_agent()
        _install_compartments(
            deps,
            _compartment_hit(
                last_loaded_product="DIESEL_2",
                last_loaded_at=NOW - timedelta(hours=4),
                last_cleaned_at=None,
                state="loaded",
            ),
        )
        agent._priority_buffer.append(_priority_list(fuel_grade=FuelGrade.AGO))

        proposals = await agent.evaluate([])

        assert len(proposals) == 1
        assert _violation_index_calls(deps["es_service"]) == []
        assert _contamination_signals(deps["signal_bus"]) == []

    @pytest.mark.asyncio
    async def test_empty_compartment_allows_any_load(self):
        """``last_loaded_product=None`` is the bootstrap case — always allowed."""

        agent, deps = _make_agent()
        _install_compartments(
            deps,
            _compartment_hit(
                last_loaded_product=None,
                last_loaded_at=None,
                last_cleaned_at=None,
                state="clean",
            ),
        )
        agent._priority_buffer.append(_priority_list(fuel_grade=FuelGrade.AGO))

        proposals = await agent.evaluate([])

        assert len(proposals) == 1
        assert _violation_index_calls(deps["es_service"]) == []
        assert _contamination_signals(deps["signal_bus"]) == []


# ---------------------------------------------------------------------------
# Tests — partial rejection preserves the remaining assignments
# ---------------------------------------------------------------------------


class TestPartialRejection:
    @pytest.mark.asyncio
    async def test_blocked_assignment_stripped_unserved_preserved(self):
        """One rejected compartment does not torch the whole plan."""

        agent, deps = _make_agent()
        # Two compartments on the same truck. c1 previously held
        # HEATING_OIL so PMS (→GASOLINE_REG) into it is blocked; c2 is
        # fresh so PMS is allowed.
        _install_compartments(
            deps,
            _compartment_hit(
                compartment_id="c1",
                last_loaded_product="HEATING_OIL",
                last_loaded_at=NOW - timedelta(days=2),
                state="loaded",
                allowed_grades=["PMS"],
                capacity_liters=5000.0,
                position_index=0,
            ),
            _compartment_hit(
                compartment_id="c2",
                last_loaded_product=None,
                state="clean",
                allowed_grades=["PMS"],
                capacity_liters=5000.0,
                position_index=1,
            ),
        )
        # Large PMS demand so both compartments get filled.
        agent._priority_buffer.append(
            _priority_list(
                fuel_grade=FuelGrade.PMS,
                priority_score=0.95,
            )
        )

        proposals = await agent.evaluate([])

        # Plan survived because c2 was allowed; c1 was stripped and the
        # blocked volume migrated to unserved_demand_liters.
        assert len(proposals) == 1
        plan_calls = _load_plan_index_calls(deps["es_service"])
        assert len(plan_calls) == 1
        persisted_plan = plan_calls[0].args[2]
        persisted_compartments = [
            a["compartment_id"] for a in persisted_plan["assignments"]
        ]
        assert "c1" not in persisted_compartments
        assert "c2" in persisted_compartments
        assert persisted_plan["unserved_demand_liters"] > 0

        # Exactly one violation logged, naming c1 only.
        violations = _violation_index_calls(deps["es_service"])
        assert len(violations) == 1
        violation_doc = violations[0].args[2]
        assert violation_doc["compartment_id"] == "truck-1_c1"

        signals = _contamination_signals(deps["signal_bus"])
        assert len(signals) == 1
        assert signals[0].context["compartment_id"] == "truck-1_c1"


# ---------------------------------------------------------------------------
# Tests — tenant-override integration via load_tenant_compatibility_rules
# ---------------------------------------------------------------------------


class TestTenantOverride:
    @pytest.mark.asyncio
    async def test_tenant_override_relaxes_default_block(self):
        """A tenant override that allows HEATING_OIL→GASOLINE_REG is honored."""

        class _AllowHeatingOilToGasoline:
            """Relaxes the default HEATING_OIL → GASOLINE_REG block.

            The default matrix blocks this pair (dyed-heating-oil residue
            contaminates gasoline), but a tenant with sanitize-certified
            fleets may opt in to ``requires_cleaning`` so the gate
            downgrades to allowed when a fresh Cleaning_Event exists.
            """

            async def get(self, key: str):
                assert key == "compatibility_matrix_config:tenant-1"
                return '{"HEATING_OIL->GASOLINE_REG": "requires_cleaning"}'

        agent, deps = _make_agent()
        agent._tenant_config = _AllowHeatingOilToGasoline()
        # Cleaning is newer than the last load, so the downgraded rule
        # resolves to ``allowed``.
        _install_compartments(
            deps,
            _compartment_hit(
                last_loaded_product="HEATING_OIL",
                last_loaded_at=NOW - timedelta(days=2),
                last_cleaned_at=NOW - timedelta(hours=1),
                state="clean",
            ),
        )
        agent._priority_buffer.append(_priority_list(fuel_grade=FuelGrade.PMS))

        proposals = await agent.evaluate([])

        # Override turned an otherwise-hard-block into an allowed load.
        assert len(proposals) == 1
        assert _violation_index_calls(deps["es_service"]) == []
        assert _contamination_signals(deps["signal_bus"]) == []


# ---------------------------------------------------------------------------
# Tests — ES failure on violation write is tolerated
# ---------------------------------------------------------------------------


class TestResilientWrites:
    @pytest.mark.asyncio
    async def test_signal_still_fires_when_es_write_fails(self):
        """A flaky cross_contamination_events write must not swallow the RiskSignal."""

        agent, deps = _make_agent()

        # Make index_document raise for the violation index but succeed
        # for the loading plan index (the plan will not be persisted in
        # this case — the blocked assignment is the only one — but we
        # keep the stub generic).
        async def _maybe_raise(index: str, doc_id: str, doc: Dict[str, Any]):
            if index == CROSS_CONTAMINATION_EVENTS_INDEX:
                raise RuntimeError("ES down")
            return None

        deps["es_service"].index_document = AsyncMock(side_effect=_maybe_raise)

        _install_compartments(
            deps,
            _compartment_hit(
                last_loaded_product="HEATING_OIL",
                last_loaded_at=NOW - timedelta(days=1),
                state="loaded",
            ),
        )
        agent._priority_buffer.append(_priority_list(fuel_grade=FuelGrade.PMS))

        # Should not raise — the guard must tolerate an ES write failure.
        proposals = await agent.evaluate([])
        assert proposals == []

        # RiskSignal still fired despite the ES failure.
        signals = _contamination_signals(deps["signal_bus"])
        assert len(signals) == 1
        assert signals[0].context["reason"] == REASON_CROSS_CONTAMINATION_BLOCKED
