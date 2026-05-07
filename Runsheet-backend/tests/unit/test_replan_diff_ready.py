"""
Unit tests for Task 4.10 — Replan_Diff persistence, WebSocket event,
and REST fetch endpoint.

Covers:

* :meth:`ExceptionReplanningAgent._build_structured_replan_diff` derives
  a :class:`StructuredReplanDiff` from a plan snapshot plus the legacy
  handler diff (Req 2.5.1, 2.5.2).
* :meth:`ExceptionReplanningAgent._persist_replan_event` folds the
  structured diff into the ES document under ``replan_diff`` (Req 2.5.2).
* ``evaluate()`` broadcasts ``replan_diff_ready`` on the fuel-planning WS
  manager whenever a structured diff is produced (Req 2.5.4).
* :func:`FuelPlanningWSManager.broadcast_replan_diff_ready` emits the
  required payload shape (Req 2.5.4).
* ``GET /api/fuel/mvp/replans/{event_id}/diff`` returns the persisted
  diff for a tenant-owned event and enforces cross-tenant isolation
  (Req 2.5.3).

Validates: Requirements 2.5.1, 2.5.2, 2.5.3, 2.5.4.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskSignal,
    Severity,
)
from Agents.overlay.exception_replanning_agent import ExceptionReplanningAgent
from Agents.support.fuel_distribution_models import ReplanDiff as LegacyReplanDiff
from Agents.support.mvp_es_mappings import MVP_REPLAN_EVENTS_INDEX
from Agents.support.replan_diff_models import (
    ReplanDiff as StructuredReplanDiff,
)
from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    mvp_router,
)
from fuel.services.fuel_planning_ws_manager import FuelPlanningWSManager
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_signal(
    source_agent: str = "delay_response_agent",
    entity_id: str = "station-1",
    entity_type: str = "station",
    tenant_id: str = "tenant-1",
    context=None,
) -> RiskSignal:
    return RiskSignal(
        source_agent=source_agent,
        entity_id=entity_id,
        entity_type=entity_type,
        severity=Severity.HIGH,
        confidence=0.8,
        ttl_seconds=300,
        tenant_id=tenant_id,
        context=context or {},
    )


def _make_snapshot(
    station_ids=("station-1", "station-2", "station-3"),
    route_id: str = "route-1",
    truck_id: str = "truck-1",
):
    """Build a plan_snapshot shaped like ``_load_plan_snapshot`` returns."""
    stops = [
        {
            "station_id": sid,
            "eta": f"2024-01-01T{10 + idx:02d}:00:00+00:00",
            "drop": {"AGO": 5000},
            "sequence": idx,
        }
        for idx, sid in enumerate(station_ids)
    ]
    return {
        "loading_plan": {
            "plan_id": "plan-1",
            "truck_id": truck_id,
            "tenant_id": "tenant-1",
            "assignments": [
                {
                    "station_id": sid,
                    "fuel_grade": "AGO",
                    "quantity_liters": 5000,
                }
                for sid in station_ids
            ],
        },
        "route_plan": {
            "route_id": route_id,
            "truck_id": truck_id,
            "plan_id": "plan-1",
            "tenant_id": "tenant-1",
            "stops": stops,
        },
    }


def _make_agent(fuel_planning_ws_manager=None):
    deps = {
        "signal_bus": MagicMock(subscribe=AsyncMock(), publish=AsyncMock(return_value=1)),
        "es_service": MagicMock(
            search_documents=AsyncMock(return_value={"hits": {"hits": []}}),
            index_document=AsyncMock(),
        ),
        "activity_log_service": MagicMock(
            log_monitoring_cycle=AsyncMock(return_value="log-id"),
            log=AsyncMock(),
        ),
        "ws_manager": MagicMock(broadcast_activity=AsyncMock()),
        "confirmation_protocol": MagicMock(process_mutation=AsyncMock()),
        "autonomy_config_service": MagicMock(),
        "feature_flag_service": MagicMock(is_enabled=AsyncMock(return_value=True)),
        "fuel_planning_ws_manager": fuel_planning_ws_manager,
    }
    return ExceptionReplanningAgent(**deps), deps


# ---------------------------------------------------------------------------
# _build_structured_replan_diff (Req 2.5.1)
# ---------------------------------------------------------------------------


class TestBuildStructuredReplanDiff:
    def test_reorder_diff_is_derived(self):
        """Delay handler reorders station-1 to end; structured diff
        should surface station-1 as a reordered_stop."""
        agent, _ = _make_agent()
        snapshot = _make_snapshot()
        legacy = LegacyReplanDiff(
            stops_reordered=["station-2", "station-3", "station-1"]
        )
        signal = _make_signal(entity_id="station-1")

        diff = agent._build_structured_replan_diff(
            plan_snapshot=snapshot,
            legacy_diff=legacy,
            disruption_type="delay",
            signal=signal,
        )

        assert diff is not None
        assert diff.original_route_id == "route-1"
        assert diff.patched_route_id.startswith("route-1:")
        # station-1 moved from index 0 → index 2
        reordered_ids = {r.stop_id for r in diff.reordered_stops}
        assert "station-1" in reordered_ids
        # All three stops are retained — none are added/removed
        assert diff.added_stops == []
        assert diff.removed_stops == []

    def test_deferral_surfaces_as_removed_stop(self):
        """Station outage defers a station; structured diff shows it removed."""
        agent, _ = _make_agent()
        snapshot = _make_snapshot()
        legacy = LegacyReplanDiff(
            stations_deferred=["station-2"],
            stops_reordered=["station-1", "station-3"],
        )
        signal = _make_signal(entity_id="station-2")

        diff = agent._build_structured_replan_diff(
            plan_snapshot=snapshot,
            legacy_diff=legacy,
            disruption_type="station_outage",
            signal=signal,
        )

        assert diff is not None
        removed_ids = {r.stop_id for r in diff.removed_stops}
        assert "station-2" in removed_ids
        assert diff.added_stops == []

    def test_demand_spike_surfaces_quantity_change(self):
        """Demand spike reallocates liters; structured diff surfaces a
        QuantityChange entry for the spike station."""
        agent, _ = _make_agent()
        snapshot = _make_snapshot()
        legacy = LegacyReplanDiff(
            volumes_reallocated={"station-1": 2000.0},
        )
        signal = _make_signal(entity_id="station-1")

        diff = agent._build_structured_replan_diff(
            plan_snapshot=snapshot,
            legacy_diff=legacy,
            disruption_type="demand_spike",
            signal=signal,
        )

        assert diff is not None
        changes = {q.stop_id: q for q in diff.quantity_changes}
        assert "station-1" in changes
        # Original drop was 5000 liters; delta +2000 → after_gallons 7000
        change = changes["station-1"]
        assert change.before_gallons == pytest.approx(5000.0)
        assert change.after_gallons == pytest.approx(7000.0)

    def test_no_route_plan_returns_none(self):
        agent, _ = _make_agent()
        snapshot = {"loading_plan": {"plan_id": "plan-1"}, "route_plan": None}
        legacy = LegacyReplanDiff()
        signal = _make_signal()

        diff = agent._build_structured_replan_diff(
            plan_snapshot=snapshot,
            legacy_diff=legacy,
            disruption_type="delay",
            signal=signal,
        )
        assert diff is None

    def test_truck_swap_marks_stops_reassigned(self):
        agent, _ = _make_agent()
        snapshot = _make_snapshot(station_ids=("station-1", "station-2"))
        legacy = LegacyReplanDiff(
            truck_swapped="truck-2",
            stops_reordered=["station-1", "station-2"],
        )
        signal = _make_signal(entity_id="truck-1")

        diff = agent._build_structured_replan_diff(
            plan_snapshot=snapshot,
            legacy_diff=legacy,
            disruption_type="truck_breakdown",
            signal=signal,
        )
        assert diff is not None
        reassigned = {r.stop_id: r for r in diff.reassigned_stops}
        assert reassigned  # both stops reassigned from truck-1 → truck-2
        for r in reassigned.values():
            assert r.from_truck_id == "truck-1"
            assert r.to_truck_id == "truck-2"


# ---------------------------------------------------------------------------
# _persist_replan_event (Req 2.5.2)
# ---------------------------------------------------------------------------


class TestPersistReplanEvent:
    @pytest.mark.asyncio
    async def test_structured_diff_is_merged_into_doc(self):
        agent, deps = _make_agent()
        from Agents.support.fuel_distribution_models import ReplanEvent

        event = ReplanEvent(
            original_plan_id="plan-1",
            trigger_signal_id="sig-1",
            replan_type="delay",
            tenant_id="tenant-1",
        )
        structured = StructuredReplanDiff(
            original_route_id="r1",
            patched_route_id="r2",
        )

        await agent._persist_replan_event(event, structured_diff=structured)

        deps["es_service"].index_document.assert_awaited_once()
        args = deps["es_service"].index_document.await_args.args
        assert args[0] == MVP_REPLAN_EVENTS_INDEX
        doc = args[2]
        # Legacy diff is still in the doc under ``diff``.
        assert "diff" in doc
        # Structured diff is folded in under ``replan_diff`` with the
        # expected top-level keys.
        assert "replan_diff" in doc
        assert doc["replan_diff"]["original_route_id"] == "r1"
        assert doc["replan_diff"]["patched_route_id"] == "r2"
        # generated_at is serialized as an ISO string (mode="json").
        assert isinstance(doc["replan_diff"]["generated_at"], str)


# ---------------------------------------------------------------------------
# End-to-end evaluate() → broadcast (Req 2.5.4)
# ---------------------------------------------------------------------------


class TestReplanDiffReadyBroadcast:
    @pytest.mark.asyncio
    async def test_broadcast_fires_after_successful_replan(self):
        """Evaluate → broadcast_replan_diff_ready called with expected args."""
        ws_manager = MagicMock(spec=FuelPlanningWSManager)
        ws_manager.broadcast_replan_diff_ready = AsyncMock(return_value=1)

        agent, deps = _make_agent(fuel_planning_ws_manager=ws_manager)

        # Prime ES search to return the plan snapshot (loading + route).
        snapshot = _make_snapshot()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [{"_source": snapshot["loading_plan"]}]}},
                {"hits": {"hits": [{"_source": snapshot["route_plan"]}]}},
            ]
        )

        signal = _make_signal(
            source_agent="delay_response_agent",
            entity_id="station-1",
            context={"disruption_type": "delay"},
        )

        proposals = await agent.evaluate([signal])
        assert len(proposals) == 1

        ws_manager.broadcast_replan_diff_ready.assert_awaited_once()
        kwargs = ws_manager.broadcast_replan_diff_ready.await_args.kwargs
        assert kwargs["tenant_id"] == "tenant-1"
        assert kwargs["replan_type"] == "delay"
        assert kwargs["original_route_id"] == "route-1"
        # summary contains the six counts from summary_counts()
        assert set(kwargs["summary"].keys()) == {
            "added",
            "removed",
            "reordered",
            "reassigned",
            "quantity_changes",
            "eta_shifts",
        }

    @pytest.mark.asyncio
    async def test_no_broadcast_when_manager_not_wired(self):
        """Without a fuel-planning WS manager the replan still runs but
        no broadcast is attempted (no crash)."""
        agent, deps = _make_agent(fuel_planning_ws_manager=None)
        snapshot = _make_snapshot()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [{"_source": snapshot["loading_plan"]}]}},
                {"hits": {"hits": [{"_source": snapshot["route_plan"]}]}},
            ]
        )

        signal = _make_signal(
            source_agent="delay_response_agent",
            entity_id="station-1",
            context={"disruption_type": "delay"},
        )

        proposals = await agent.evaluate([signal])
        assert len(proposals) == 1  # replan succeeded without broadcast


# ---------------------------------------------------------------------------
# FuelPlanningWSManager.broadcast_replan_diff_ready (Req 2.5.4)
# ---------------------------------------------------------------------------


class TestReplanDiffReadyEnvelope:
    @pytest.mark.asyncio
    async def test_envelope_shape(self):
        mgr = FuelPlanningWSManager()
        mgr.broadcast = AsyncMock(return_value=0)

        await mgr.broadcast_replan_diff_ready(
            event_id="evt-1",
            diff_id="d-1",
            tenant_id="tenant-a",
            summary={
                "added": 0,
                "removed": 1,
                "reordered": 2,
                "reassigned": 0,
                "quantity_changes": 0,
                "eta_shifts": 0,
            },
            replan_type="delay",
            original_route_id="r-o",
            patched_route_id="r-p",
        )

        mgr.broadcast.assert_awaited_once()
        envelope = mgr.broadcast.await_args.args[0]
        assert envelope["type"] == "replan_diff_ready"
        data = envelope["data"]
        assert data["event_id"] == "evt-1"
        assert data["diff_id"] == "d-1"
        assert data["tenant_id"] == "tenant-a"
        assert data["summary"]["removed"] == 1
        assert data["diff_url"] == "/api/fuel/mvp/replans/evt-1/diff"
        assert data["replan_type"] == "delay"

    @pytest.mark.asyncio
    async def test_optional_fields_omitted_when_none(self):
        mgr = FuelPlanningWSManager()
        mgr.broadcast = AsyncMock(return_value=0)

        await mgr.broadcast_replan_diff_ready(
            event_id="evt-2",
            diff_id="d-2",
            tenant_id="tenant-a",
            summary={},
        )
        data = mgr.broadcast.await_args.args[0]["data"]
        assert "replan_type" not in data
        assert "original_route_id" not in data
        assert "patched_route_id" not in data

    @pytest.mark.asyncio
    async def test_extra_cannot_overwrite_required_fields(self):
        mgr = FuelPlanningWSManager()
        mgr.broadcast = AsyncMock(return_value=0)

        await mgr.broadcast_replan_diff_ready(
            event_id="evt-3",
            diff_id="d-3",
            tenant_id="tenant-a",
            summary={},
            extra={"event_id": "EVIL", "bonus": "hi"},
        )
        data = mgr.broadcast.await_args.args[0]["data"]
        assert data["event_id"] == "evt-3"
        assert data["bonus"] == "hi"


# ---------------------------------------------------------------------------
# REST endpoint GET /api/fuel/mvp/replans/{event_id}/diff (Req 2.5.3)
# ---------------------------------------------------------------------------


def _build_fastapi_app(es_service) -> FastAPI:
    """Assemble a FastAPI app with only the diff endpoint under test.

    Tenant context is stubbed via dependency override so we don't need a
    real JWT for the test.
    """
    configure_fuel_ops_endpoints(es_service=es_service)

    app = FastAPI()
    app.include_router(mvp_router)

    async def _stub_tenant():
        return TenantContext(
            tenant_id="tenant-1",
            user_id="user-1",
            has_pii_access=True,
            roles=["dispatcher"],
            region="US",
        )

    app.dependency_overrides[get_tenant_context] = _stub_tenant
    return app


def _sample_event_doc(
    event_id: str = "evt-1",
    tenant_id: str = "tenant-1",
    include_diff: bool = True,
):
    diff = {
        "diff_id": "d-1",
        "original_route_id": "r-o",
        "patched_route_id": "r-p",
        "added_stops": [],
        "removed_stops": [
            {
                "stop_id": "station-2",
                "index": 1,
                "gallons": 5000.0,
                "product_code": None,
                "eta": "2024-01-01T11:00:00+00:00",
            }
        ],
        "reordered_stops": [],
        "reassigned_stops": [],
        "quantity_changes": [],
        "eta_shifts": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    doc = {
        "event_id": event_id,
        "original_plan_id": "plan-1",
        "trigger_signal_id": "sig-1",
        "replan_type": "station_outage",
        "status": "applied",
        "tenant_id": tenant_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if include_diff:
        doc["replan_diff"] = diff
    return doc


class TestReplanDiffEndpoint:
    def test_returns_diff_for_tenant_owned_event(self):
        doc = _sample_event_doc()
        es = MagicMock()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [{"_source": doc}]}}
        )
        app = _build_fastapi_app(es)

        with TestClient(app) as client:
            resp = client.get("/api/fuel/mvp/replans/evt-1/diff")
        assert resp.status_code == 200
        body = resp.json()
        assert body["event_id"] == "evt-1"
        assert body["replan_type"] == "station_outage"
        assert body["status"] == "applied"
        assert body["diff"]["diff_id"] == "d-1"
        assert body["diff"]["removed_stops"][0]["stop_id"] == "station-2"

    def test_missing_event_returns_404(self):
        es = MagicMock()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        app = _build_fastapi_app(es)

        with TestClient(app) as client:
            resp = client.get("/api/fuel/mvp/replans/missing/diff")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "replan_event_not_found"

    def test_cross_tenant_event_returns_404(self):
        """Simulate a misconfigured ES that returns a row belonging to
        another tenant; the endpoint must mask it as 404."""
        doc = _sample_event_doc(tenant_id="tenant-other")
        es = MagicMock()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [{"_source": doc}]}}
        )
        app = _build_fastapi_app(es)

        with TestClient(app) as client:
            resp = client.get("/api/fuel/mvp/replans/evt-1/diff")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "replan_event_not_found"

    def test_event_without_structured_diff_returns_distinct_404(self):
        doc = _sample_event_doc(include_diff=False)
        es = MagicMock()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [{"_source": doc}]}}
        )
        app = _build_fastapi_app(es)

        with TestClient(app) as client:
            resp = client.get("/api/fuel/mvp/replans/evt-1/diff")
        assert resp.status_code == 404
        assert (
            resp.json()["detail"]["error_code"] == "replan_diff_not_available"
        )

    def test_es_failure_returns_502(self):
        es = MagicMock()
        es.search_documents = AsyncMock(side_effect=RuntimeError("boom"))
        app = _build_fastapi_app(es)

        with TestClient(app) as client:
            resp = client.get("/api/fuel/mvp/replans/evt-1/diff")
        assert resp.status_code == 502
        assert resp.json()["detail"]["error_code"] == "replan_events_unavailable"
