"""
Replanning must change the plan the driver is executing, not just describe it.

``ExceptionReplanningAgent`` computed a diff, wrote a ``ReplanEvent`` whose
status was hardcoded to ``"applied"``, and never touched ``mvp_routes``. The
driver kept working the original sequence. Worse, both snapshot queries filtered
``status: "proposed"`` — the status a plan carries *before* dispatcher approval —
so once a plan was actually dispatched the agent could no longer see it, and
every real disruption resolved to "no active plan found" and was skipped.

These tests pin four things:

* the snapshot query targets in-flight plans,
* a replan that can be applied safely is written to ``mvp_routes`` and pushed to
  the driver,
* a replan that cannot be validated here is escalated rather than half-applied,
* the persisted event says what actually happened.

Validates: Requirements 5.2, 5.3, 5.6, 5.7
"""

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.data_contracts import RiskSignal, Severity
from Agents.overlay.exception_replanning_agent import (
    REPLAN_APPLIED,
    REPLAN_ESCALATED,
    REPLAN_PROPOSED,
    REPLANNABLE_PLAN_STATUSES,
    ExceptionReplanningAgent,
)
from Agents.support.fuel_distribution_models import ReplanDiff

TENANT = "tenant-replan"
RUN_ID = "run-77"
ROUTE_ID = "route-1"
DRIVER_ID = "driver-9"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _deps(*, mode: str = "active_auto") -> Dict[str, Any]:
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    es_service.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    es_service.index_document = AsyncMock()
    es_service.update_document = AsyncMock()

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")
    activity_log.log = AsyncMock()

    ws_manager = MagicMock()
    ws_manager.broadcast_activity = AsyncMock()
    ws_manager.broadcast_event = AsyncMock(return_value=0)

    confirmation = MagicMock()
    confirmation.process_mutation = AsyncMock()

    feature_flags = MagicMock()
    feature_flags.get_overlay_state = AsyncMock(return_value=mode)
    feature_flags.is_enabled = AsyncMock(return_value=True)

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation,
        "autonomy_config_service": MagicMock(),
        "feature_flag_service": feature_flags,
    }


def _agent(*, mode: str = "active_auto"):
    deps = _deps(mode=mode)
    agent = ExceptionReplanningAgent(**deps)
    driver_ws = MagicMock()
    driver_ws.send_new_route = AsyncMock(return_value=True)
    agent.set_driver_ws_manager(driver_ws)
    return agent, deps, driver_ws


def _stop(station_id: str, sequence: int, **extra) -> Dict[str, Any]:
    """A stop carrying fields the agent knows nothing about, to prove they survive."""
    stop = {
        "station_id": station_id,
        "sequence": sequence,
        "eta": "2026-01-01T10:00:00Z",
        "drop": {"AGO": 5000},
        "planned_liters": 5000.0,
    }
    stop.update(extra)
    return stop


def _snapshot(
    *,
    station_ids: Optional[List[str]] = None,
    stops: Optional[List[Dict[str, Any]]] = None,
    run_id: str = RUN_ID,
) -> Dict[str, Any]:
    station_ids = station_ids or ["station-1", "station-2", "station-3"]
    if stops is None:
        stops = [_stop(sid, i) for i, sid in enumerate(station_ids, start=1)]
    return {
        "loading_plan": {
            "plan_id": "plan-1",
            "truck_id": "truck-1",
            "run_id": run_id,
            "status": "dispatched",
            "tenant_id": TENANT,
            "assignments": [
                {
                    "compartment_id": f"comp-{i}",
                    "station_id": sid,
                    "fuel_grade": "AGO",
                    "quantity_liters": 5000.0,
                }
                for i, sid in enumerate(station_ids)
            ],
        },
        "route_plan": {
            "route_id": ROUTE_ID,
            "truck_id": "truck-1",
            "plan_id": "plan-1",
            "run_id": run_id,
            "status": "dispatched",
            "tenant_id": TENANT,
            "stops": stops,
            "distance_km": 150.0,
        },
    }


def _signal(entity_id: str = "station-2", **kw) -> RiskSignal:
    return RiskSignal(
        source_agent=kw.pop("source_agent", "delay_response_agent"),
        entity_id=entity_id,
        entity_type=kw.pop("entity_type", "station"),
        severity=Severity.HIGH,
        confidence=0.8,
        ttl_seconds=300,
        tenant_id=TENANT,
        context=kw.pop("context", {}),
    )


def _route_writes(deps) -> List[Any]:
    return [
        call
        for call in deps["es_service"].update_document.await_args_list
        if call.args and call.args[0] == "mvp_routes"
    ]


# ---------------------------------------------------------------------------
# The snapshot query
# ---------------------------------------------------------------------------


class TestSnapshotTargetsInFlightPlans:
    def test_proposed_is_not_replannable(self):
        """A plan the dispatcher has not approved is not what we replan.

        Regenerating an unapproved plan is cheaper and correct; patching it is
        not. The old filter selected *only* these.
        """
        assert "proposed" not in REPLANNABLE_PLAN_STATUSES
        assert set(REPLANNABLE_PLAN_STATUSES) == {"dispatched", "in_transit"}

    @pytest.mark.asyncio
    async def test_both_queries_filter_on_in_flight_statuses(self):
        """Validates: Requirement 5.2"""
        agent, deps, _ = _agent()

        await agent._load_plan_snapshot(TENANT)

        queries = [
            call.args[1]
            for call in deps["es_service"].search_documents.await_args_list
        ]
        assert queries, "no snapshot query was issued"
        for query in queries:
            clauses = query["query"]["bool"]["must"]
            status_clause = next(
                (c for c in clauses if "terms" in c and "status" in c["terms"]),
                None,
            )
            assert status_clause is not None, (
                f"snapshot query does not filter status by a terms clause: "
                f"{clauses}"
            )
            assert status_clause["terms"]["status"] == list(
                REPLANNABLE_PLAN_STATUSES
            )


# ---------------------------------------------------------------------------
# Applying a safe patch
# ---------------------------------------------------------------------------


class TestSafePatchIsApplied:
    @pytest.mark.asyncio
    async def test_delay_reorder_is_written_to_the_route(self):
        """The delayed station moves last, and the route document changes.

        Validates: Requirement 5.3
        """
        agent, deps, _ = _agent()
        snapshot = _snapshot()

        outcome = await agent._apply_replan(
            diff=ReplanDiff(
                stops_reordered=["station-1", "station-3", "station-2"]
            ),
            plan_snapshot=snapshot,
            tenant_id=TENANT,
            disruption_type="delay",
        )

        assert outcome.applied is True
        assert outcome.status == REPLAN_APPLIED
        assert outcome.patched_route_id == ROUTE_ID

        (write,) = _route_writes(deps)
        assert write.args[1] == ROUTE_ID
        patched = write.args[2]["stops"]
        assert [s["station_id"] for s in patched] == [
            "station-1",
            "station-3",
            "station-2",
        ]

    @pytest.mark.asyncio
    async def test_sequence_is_renumbered_and_other_fields_survive(self):
        """A reorder must not strip ETAs or quantities off the stops."""
        agent, deps, _ = _agent()

        await agent._apply_replan(
            diff=ReplanDiff(
                stops_reordered=["station-3", "station-1", "station-2"]
            ),
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
            disruption_type="delay",
        )

        patched = _route_writes(deps)[0].args[2]["stops"]
        assert [s["sequence"] for s in patched] == [1, 2, 3]
        assert all(s["planned_liters"] == 5000.0 for s in patched)
        assert all("eta" in s for s in patched)

    @pytest.mark.asyncio
    async def test_station_outage_drops_the_deferred_stop(self):
        """Validates: Requirement 5.4"""
        agent, deps, _ = _agent()

        outcome = await agent._apply_replan(
            diff=ReplanDiff(
                stops_reordered=["station-1", "station-3"],
                stations_deferred=["station-2"],
            ),
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
            disruption_type="station_outage",
        )

        assert outcome.applied is True
        patched = _route_writes(deps)[0].args[2]["stops"]
        assert [s["station_id"] for s in patched] == ["station-1", "station-3"]

    @pytest.mark.asyncio
    async def test_completed_stops_need_not_appear_in_the_new_sequence(self):
        """Work already done is history, so a sequence over the rest is valid."""
        agent, deps, _ = _agent()
        stops = [
            _stop("station-1", 1, status="completed"),
            _stop("station-2", 2),
            _stop("station-3", 3),
        ]

        outcome = await agent._apply_replan(
            diff=ReplanDiff(stops_reordered=["station-3", "station-2"]),
            plan_snapshot=_snapshot(stops=stops),
            tenant_id=TENANT,
            disruption_type="delay",
        )

        assert outcome.applied is True, outcome.detail
        patched = _route_writes(deps)[0].args[2]["stops"]
        assert [s["station_id"] for s in patched] == ["station-3", "station-2"]


# ---------------------------------------------------------------------------
# Refusals — a half-applied plan is worse than an escalation
# ---------------------------------------------------------------------------


class TestUnsafePatchIsEscalatedNotApplied:
    @pytest.mark.asyncio
    async def test_truck_swap_is_escalated(self):
        """``truck_swapped`` names the *broken* truck; no replacement exists.

        Writing it back onto the route would assign the route to the vehicle
        that just failed. Choosing a real replacement needs the compartment
        solver.

        Validates: Requirement 5.6
        """
        agent, deps, driver_ws = _agent()

        outcome = await agent._apply_replan(
            diff=ReplanDiff(
                truck_swapped="truck-1",
                stops_reordered=["station-1", "station-2", "station-3"],
            ),
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
            disruption_type="truck_breakdown",
        )

        assert outcome.applied is False
        assert outcome.status == REPLAN_ESCALATED
        assert outcome.reason == "needs_replacement_truck"
        assert _route_writes(deps) == []
        driver_ws.send_new_route.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_volume_reallocation_is_escalated(self):
        """Capacity and weight are the loading solver's job, not this agent's.

        Validates: Requirement 5.6
        """
        agent, deps, _ = _agent()

        outcome = await agent._apply_replan(
            diff=ReplanDiff(volumes_reallocated={"station-2": 3000.0}),
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
            disruption_type="demand_spike",
        )

        assert outcome.applied is False
        assert outcome.status == REPLAN_ESCALATED
        assert outcome.reason == "needs_capacity_revalidation"
        assert _route_writes(deps) == []

    @pytest.mark.asyncio
    async def test_a_stop_in_neither_list_blocks_the_patch(self):
        """An unaccounted stop would vanish — that is a missed delivery."""
        agent, deps, _ = _agent()

        outcome = await agent._apply_replan(
            # station-3 is neither resequenced nor deferred.
            diff=ReplanDiff(stops_reordered=["station-2", "station-1"]),
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
            disruption_type="delay",
        )

        assert outcome.applied is False
        assert outcome.status == REPLAN_ESCALATED
        assert outcome.reason == "stop_set_mismatch"
        assert _route_writes(deps) == []

    @pytest.mark.asyncio
    async def test_a_failed_write_escalates_rather_than_raising(self):
        """The disruption must still reach a human if ES rejects the update."""
        agent, deps, _ = _agent()
        deps["es_service"].update_document = AsyncMock(
            side_effect=RuntimeError("es down")
        )

        outcome = await agent._apply_replan(
            diff=ReplanDiff(
                stops_reordered=["station-1", "station-3", "station-2"]
            ),
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
            disruption_type="delay",
        )

        assert outcome.applied is False
        assert outcome.status == REPLAN_ESCALATED
        assert outcome.reason == "route_write_failed"

    @pytest.mark.asyncio
    async def test_a_snapshot_without_a_route_is_escalated(self):
        agent, deps, _ = _agent()
        snapshot = _snapshot()
        snapshot["route_plan"] = {}

        outcome = await agent._apply_replan(
            diff=ReplanDiff(stops_reordered=["station-1"]),
            plan_snapshot=snapshot,
            tenant_id=TENANT,
            disruption_type="delay",
        )

        assert outcome.applied is False
        assert outcome.reason == "no_route_to_patch"


# ---------------------------------------------------------------------------
# The mode gate
# ---------------------------------------------------------------------------


class TestShadowModeComputesButDoesNotWrite:
    @pytest.mark.asyncio
    async def test_shadow_mode_leaves_the_plan_alone(self):
        """Shadow must still produce the diff for retrospective analysis."""
        agent, deps, driver_ws = _agent(mode="shadow")

        outcome = await agent._apply_replan(
            diff=ReplanDiff(
                stops_reordered=["station-1", "station-3", "station-2"]
            ),
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
            disruption_type="delay",
        )

        assert outcome.applied is False
        assert outcome.status == REPLAN_PROPOSED
        assert outcome.reason == "shadow_mode"
        assert _route_writes(deps) == []
        driver_ws.send_new_route.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unresolvable_mode_fails_closed(self):
        """A broken flag service must not write live state."""
        agent, deps, _ = _agent()
        deps["feature_flag_service"].get_overlay_state = AsyncMock(
            side_effect=RuntimeError("redis down")
        )

        outcome = await agent._apply_replan(
            diff=ReplanDiff(
                stops_reordered=["station-1", "station-3", "station-2"]
            ),
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
            disruption_type="delay",
        )

        assert outcome.applied is False
        assert _route_writes(deps) == []


# ---------------------------------------------------------------------------
# Telling the driver
# ---------------------------------------------------------------------------


class TestDriverIsToldAboutAnAppliedReplan:
    @staticmethod
    def _es_with_dispatched_order(deps) -> None:
        async def _search(index, query, size=10, *args, **kwargs):
            if index == "fuel_orders_current":
                return {
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "order_id": "o-1",
                                    "tenant_id": TENANT,
                                    "assigned_run_id": RUN_ID,
                                    "assigned_driver_id": DRIVER_ID,
                                }
                            }
                        ]
                    }
                }
            return {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(side_effect=_search)

    @pytest.mark.asyncio
    async def test_driver_is_resolved_from_the_dispatched_orders(self):
        """Reuses the ``assigned_driver_id`` dispatch already stamped.

        Re-deriving "which driver owns this truck" here would duplicate a rule
        that lives in PlanDispatchService and would drift from it.
        """
        agent, deps, _ = _agent()
        self._es_with_dispatched_order(deps)

        driver_id = await agent._resolve_driver_for_plan(
            plan_snapshot=_snapshot(), tenant_id=TENANT
        )

        assert driver_id == DRIVER_ID

    @pytest.mark.asyncio
    async def test_a_driver_stamped_on_the_plan_needs_no_query(self):
        agent, deps, _ = _agent()
        snapshot = _snapshot()
        snapshot["route_plan"]["assigned_driver_id"] = "driver-direct"

        driver_id = await agent._resolve_driver_for_plan(
            plan_snapshot=snapshot, tenant_id=TENANT
        )

        assert driver_id == "driver-direct"
        deps["es_service"].search_documents.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_applied_replan_pushes_the_new_route(self):
        """Validates: Requirement 5.3"""
        agent, deps, driver_ws = _agent()
        self._es_with_dispatched_order(deps)
        signal = _signal()

        await agent._attempt_replan(
            disruption_type="delay",
            signal=signal,
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
        )

        driver_ws.send_new_route.assert_awaited_once()
        sent_driver, payload = driver_ws.send_new_route.await_args.args
        assert sent_driver == DRIVER_ID
        assert payload["route_id"] == ROUTE_ID
        assert payload["replan_type"] == "delay"
        assert payload["run_id"] == RUN_ID

    @pytest.mark.asyncio
    async def test_an_unapplied_replan_tells_nobody(self):
        """A driver told about a patch that never landed is out of step with ES."""
        agent, deps, driver_ws = _agent(mode="shadow")
        self._es_with_dispatched_order(deps)

        await agent._attempt_replan(
            disruption_type="delay",
            signal=_signal(),
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
        )

        driver_ws.send_new_route.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unreachable_driver_does_not_fail_the_replan(self):
        """The patch is persisted; a dropped socket must not undo that."""
        agent, deps, driver_ws = _agent()
        self._es_with_dispatched_order(deps)
        driver_ws.send_new_route = AsyncMock(
            side_effect=RuntimeError("socket closed")
        )

        await agent._attempt_replan(
            disruption_type="delay",
            signal=_signal(),
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
        )

        assert len(_route_writes(deps)) == 1


# ---------------------------------------------------------------------------
# The persisted event must not lie
# ---------------------------------------------------------------------------


class TestPersistedEventReflectsReality:
    @staticmethod
    def _event_doc(deps) -> Dict[str, Any]:
        calls = [
            call
            for call in deps["es_service"].index_document.await_args_list
            if call.args and call.args[0] == "mvp_replan_events"
        ]
        assert calls, "no replan event was persisted"
        return calls[-1].args[2]

    @pytest.mark.asyncio
    async def test_applied_replan_records_applied(self):
        """Validates: Requirement 5.7"""
        agent, deps, _ = _agent()

        await agent._attempt_replan(
            disruption_type="delay",
            signal=_signal(),
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
        )

        doc = self._event_doc(deps)
        assert doc["status"] == REPLAN_APPLIED
        assert doc["apply_outcome"]["applied"] is True
        assert doc["apply_outcome"]["patched_route_id"] == ROUTE_ID

    @pytest.mark.asyncio
    async def test_advisory_replan_does_not_claim_applied(self):
        """The status was hardcoded to "applied" no matter what happened."""
        agent, deps, _ = _agent(mode="shadow")

        await agent._attempt_replan(
            disruption_type="delay",
            signal=_signal(),
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
        )

        doc = self._event_doc(deps)
        assert doc["status"] == REPLAN_PROPOSED
        assert doc["apply_outcome"]["applied"] is False
        assert doc["apply_outcome"]["reason"] == "shadow_mode"

    @pytest.mark.asyncio
    async def test_escalated_replan_records_the_reason(self):
        agent, deps, _ = _agent()
        signal = _signal(
            entity_id="truck-1",
            entity_type="truck",
            context={"disruption_type": "truck_breakdown"},
        )

        await agent._attempt_replan(
            disruption_type="truck_breakdown",
            signal=signal,
            plan_snapshot=_snapshot(),
            tenant_id=TENANT,
        )

        doc = self._event_doc(deps)
        assert doc["status"] == REPLAN_ESCALATED
        assert doc["apply_outcome"]["reason"] == "needs_replacement_truck"
        assert "compartment solver" in doc["apply_outcome"]["detail"]
