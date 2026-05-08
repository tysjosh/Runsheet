"""
Unit tests for Route_Planning_Agent ↔ Sourcing_Recommender wiring
(Task 7.10 — Req 8.4.5, 8.5.4, 8.5.5).

The Route_Planning_Agent consults the already-wired
:class:`fuel.services.sourcing_recommender.SourcingRecommender` when a
Loading_Plan carries an external ``terminal_id`` and the
``overlay.terminal_sourcing`` feature flag is active for the tenant.
The winning terminal id + reasons are stamped on the persisted
Route_Plan so downstream consumers (dispatcher UI, audit trail) see the
sourcing decision that produced the route.

These tests exercise the five guarantees documented in the task
instructions:

    1. Recommender invoked when flag on AND external lift required →
       ``sourced_terminal_id`` populated on the persisted Route_Plan.
    2. Recommender NOT invoked when the flag is disabled.
    3. Recommender NOT invoked when the Loading_Plan has no external
       ``terminal_id`` (depot-local plan).
    4. Empty recommender candidates → sourcing fields remain ``None``,
       route still persists to ``mvp_routes``.
    5. Exceptions from the recommender are logged and swallowed so the
       route still persists with no sourcing fields set.

Validates: Requirements 8.4.5, 8.5.4, 8.5.5.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.data_contracts import InterventionProposal, RiskClass
from Agents.overlay.route_planning_agent import (
    TERMINAL_SOURCING_FLAG_KEY,
    RoutePlanningAgent,
)
from fuel.terminal_models import SourcingRecommendation, TerminalCandidate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_loading_proposal(
    *,
    truck_id: str = "truck-1",
    plan_id: str = "plan-1",
    tenant_id: str = "tenant-1",
    station_ids: Optional[List[str]] = None,
    terminal_id: Optional[str] = None,
    fuel_grade: str = "DIESEL_2",
    quantity_liters: float = 5000.0,
) -> InterventionProposal:
    """Build the apply_loading_plan proposal the Route_Planning_Agent consumes.

    The ``terminal_id`` parameter toggles the external-lift signal the
    agent uses to decide whether to call the recommender (Task 7.10).
    Defaults to ``None`` so depot-local plans bypass the sourcing path.
    """
    if station_ids is None:
        station_ids = ["station-1", "station-2"]

    assignments = [
        {
            "compartment_id": f"comp-{i}",
            "station_id": sid,
            "fuel_grade": fuel_grade,
            "quantity_liters": quantity_liters,
            "compartment_capacity_liters": 10000.0,
        }
        for i, sid in enumerate(station_ids)
    ]

    return InterventionProposal(
        source_agent="compartment_loading",
        actions=[
            {
                "tool_name": "apply_loading_plan",
                "parameters": {
                    "plan_id": plan_id,
                    "truck_id": truck_id,
                    "assignments": assignments,
                    "total_utilization_pct": 75.0,
                    "unserved_demand_liters": 0.0,
                    "total_weight_kg": 8500.0,
                    # Task 7.10 — the forwarded terminal_id is the
                    # external-lift signal the Route_Planning_Agent
                    # checks before consulting the recommender.
                    "terminal_id": terminal_id,
                    "contract_id": None,
                },
            }
        ],
        expected_kpi_delta={"truck_utilization_pct": 75.0},
        risk_class=RiskClass.LOW,
        confidence=0.85,
        priority=1,
        tenant_id=tenant_id,
    )


def _make_station_location_hit(station_id: str, lat: float, lon: float) -> Dict[str, Any]:
    return {
        "_source": {
            "station_id": station_id,
            "latitude": lat,
            "longitude": lon,
        }
    }


def _make_recommendation(
    *,
    tenant_id: str = "tenant-1",
    product_code: str = "DIESEL_2",
    candidates: Optional[List[TerminalCandidate]] = None,
    recommendation_id: str = "srec_unit_test",
    request_id: str = "req_unit_test",
) -> SourcingRecommendation:
    from datetime import datetime, timezone

    return SourcingRecommendation(
        recommendation_id=recommendation_id,
        request_id=request_id,
        tenant_id=tenant_id,
        truck_id=None,
        run_id=None,
        product_code=product_code,
        volume_gallons=1000.0,
        origin_lat=6.45,
        origin_lon=3.40,
        candidates=candidates or [],
        rack_price_fallback=False,
        generated_at=datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc),
    )


def _make_candidate(
    *,
    terminal_id: str = "term_a",
    score: float = 0.92,
    reasons: Optional[List[str]] = None,
) -> TerminalCandidate:
    return TerminalCandidate(
        terminal_id=terminal_id,
        price_per_gallon_usd=3.25,
        branded_flag=False,
        contract_id=None,
        avg_wait_minutes=5.0,
        distance_km_from_start=10.0,
        score=score,
        reasons=reasons or ["best_price"],
        wait_warning=False,
    )


@dataclass
class _StubRecommender:
    """Async recommender stub recording every ``recommend`` call.

    Mirrors the test harness used by the sourcing-endpoint tests but
    lives in-line here so the Route_Planning_Agent tests can cover the
    "recommender raised" / "empty candidates" / "never invoked" paths
    without standing up the full recommender dependency graph.
    """

    canned: Optional[SourcingRecommendation] = None
    raises: Optional[BaseException] = None
    calls: List[Dict[str, Any]] = field(default_factory=list)

    async def recommend(self, **kwargs: Any) -> SourcingRecommendation:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        assert self.canned is not None, "canned recommendation not set"
        return self.canned


class _StubFeatureFlags:
    """Minimal feature-flag service that returns a pinned overlay state.

    The Route_Planning_Agent treats ``active_gated`` / ``active_auto``
    as "enabled" and everything else (``shadow``, ``disabled``, errors)
    as "skip the recommender". We accept a mapping from ``flag_key`` to
    state so tests can flip the terminal-sourcing flag independently of
    the traffic-aware flag.
    """

    def __init__(self, states: Optional[Dict[str, str]] = None):
        self._states = states or {}

    async def get_overlay_state(self, flag_key: str, tenant_id: str) -> str:
        return self._states.get(flag_key, "disabled")

    async def is_enabled(self, tenant_id: str) -> bool:  # pragma: no cover
        return True


def _make_deps(
    *,
    terminal_sourcing_state: str = "active_gated",
) -> Dict[str, Any]:
    """Create mocked dependencies for the RoutePlanningAgent.

    Matches the shape used by the existing ``test_route_planning_agent``
    tests so the agent constructs cleanly. The feature-flag stub is
    pinned so ``TERMINAL_SOURCING_FLAG_KEY`` returns the requested
    overlay state for every tenant under test.
    """
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

    feature_flags = _StubFeatureFlags(
        states={TERMINAL_SOURCING_FLAG_KEY: terminal_sourcing_state}
    )

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "autonomy_config_service": autonomy_config,
        "feature_flag_service": feature_flags,
    }


def _make_agent(
    *,
    terminal_sourcing_state: str = "active_gated",
    recommender: Optional[_StubRecommender] = None,
    es_hits: Optional[List[Dict[str, Any]]] = None,
):
    """Build a Route_Planning_Agent wired to stubbed dependencies.

    ``es_hits`` are returned by ``search_documents`` so the agent can
    resolve station coordinates and the single evaluate pass produces
    a route. ``recommender`` is plumbed through
    :meth:`RoutePlanningAgent.set_sourcing_recommender` — the same
    injection point bootstrap uses at startup (Task 7.10).
    """
    deps = _make_deps(terminal_sourcing_state=terminal_sourcing_state)
    if es_hits is not None:
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": es_hits}}
        )
    agent = RoutePlanningAgent(**deps)
    if recommender is not None:
        agent.set_sourcing_recommender(recommender)
    return agent, deps


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRoutePlanningAgentSourcing:
    """Task 7.10 — Route_Planning_Agent ↔ Sourcing_Recommender wiring.

    Validates: Requirements 8.4.5, 8.5.4, 8.5.5.
    """

    @pytest.mark.asyncio
    async def test_flag_on_and_external_lift_populates_sourced_terminal(self):
        """Req 8.5.5: flag active + terminal_id set → sourced fields populated.

        When the Loading_Plan carries a ``terminal_id`` and the tenant
        has ``overlay.terminal_sourcing`` active, the agent invokes the
        recommender and stamps the top candidate's terminal id + reasons
        + recommendation_id on the persisted Route_Plan.
        """

        candidates = [
            _make_candidate(
                terminal_id="term_buckeye",
                score=0.92,
                reasons=["best_price", "contract_priority_boost"],
            ),
        ]
        recommender = _StubRecommender(
            canned=_make_recommendation(
                candidates=candidates,
                recommendation_id="srec_happy",
            )
        )
        agent, deps = _make_agent(
            terminal_sourcing_state="active_gated",
            recommender=recommender,
            es_hits=[
                _make_station_location_hit("station-1", 6.45, 3.40),
                _make_station_location_hit("station-2", 6.50, 3.35),
            ],
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                station_ids=["station-1", "station-2"],
                terminal_id="term_buckeye",
            )
        )

        route_proposals = await agent.evaluate([])

        assert len(recommender.calls) == 1
        call = recommender.calls[0]
        assert call["tenant_id"] == "tenant-1"
        # Liters → gallons conversion (two stops × 5000 L ÷ 3.785411784).
        assert call["product_code"] == "DIESEL_2"
        assert call["volume_gallons"] == pytest.approx(
            10000.0 / 3.785411784, rel=1e-6
        )

        # Route was persisted once with the sourcing fields stamped.
        es = deps["es_service"]
        assert es.index_document.await_count == 1
        _, _, persisted_doc = es.index_document.await_args.args
        assert persisted_doc["sourced_terminal_id"] == "term_buckeye"
        assert persisted_doc["sourced_terminal_reasons"] == [
            "best_price",
            "contract_priority_boost",
        ]
        assert persisted_doc["sourcing_recommendation_id"] == "srec_happy"

        # And a route proposal was returned to the caller (Req 4.1).
        assert len(route_proposals) == 1
        assert route_proposals[0].source_agent == "route_planning"

    @pytest.mark.asyncio
    async def test_flag_off_skips_recommender(self):
        """Req 8.5.5: flag disabled → recommender never called.

        Shadow-mode / disabled tenants must not consume the recommender's
        ES / rack-price / Redis traffic. The route still persists, but
        with the sourcing fields unset.
        """

        recommender = _StubRecommender(
            canned=_make_recommendation(
                candidates=[_make_candidate(terminal_id="term_a")]
            )
        )
        agent, deps = _make_agent(
            terminal_sourcing_state="disabled",
            recommender=recommender,
            es_hits=[_make_station_location_hit("station-1", 6.45, 3.40)],
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                station_ids=["station-1"],
                terminal_id="term_buckeye",
            )
        )

        await agent.evaluate([])

        assert recommender.calls == []
        es = deps["es_service"]
        assert es.index_document.await_count == 1
        _, _, persisted_doc = es.index_document.await_args.args
        # Default values from the RoutePlan model — no sourcing fields set.
        assert persisted_doc["sourced_terminal_id"] is None
        assert persisted_doc["sourced_terminal_reasons"] == []
        assert persisted_doc["sourcing_recommendation_id"] is None

    @pytest.mark.asyncio
    async def test_no_external_lift_skips_recommender(self):
        """Req 8.5.5: missing terminal_id on the Loading_Plan → skip.

        Depot-local plans (``terminal_id`` absent or empty) never need a
        sourcing pick. The agent must not call the recommender regardless
        of the feature-flag state.
        """

        recommender = _StubRecommender(
            canned=_make_recommendation(
                candidates=[_make_candidate(terminal_id="term_a")]
            )
        )
        agent, deps = _make_agent(
            terminal_sourcing_state="active_gated",
            recommender=recommender,
            es_hits=[_make_station_location_hit("station-1", 6.45, 3.40)],
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                station_ids=["station-1"],
                terminal_id=None,  # Depot-local lift — no sourcing needed.
            )
        )

        await agent.evaluate([])

        assert recommender.calls == []
        es = deps["es_service"]
        assert es.index_document.await_count == 1
        _, _, persisted_doc = es.index_document.await_args.args
        assert persisted_doc["sourced_terminal_id"] is None
        assert persisted_doc["sourced_terminal_reasons"] == []
        assert persisted_doc["sourcing_recommendation_id"] is None

    @pytest.mark.asyncio
    async def test_empty_candidates_leaves_fields_none(self):
        """Req 8.5.5: zero candidates → fields stay None, route persists.

        The recommender legitimately returns an empty candidate slate
        when every terminal is disqualified (product unsupported, hours,
        branding). The agent must log a warning and still persist the
        route so dispatch is not blocked on a sourcing miss.
        """

        recommender = _StubRecommender(
            canned=_make_recommendation(
                candidates=[],
                recommendation_id="srec_empty",
            )
        )
        agent, deps = _make_agent(
            terminal_sourcing_state="active_auto",
            recommender=recommender,
            es_hits=[_make_station_location_hit("station-1", 6.45, 3.40)],
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                station_ids=["station-1"],
                terminal_id="term_buckeye",
            )
        )

        await agent.evaluate([])

        # Recommender was invoked once.
        assert len(recommender.calls) == 1
        es = deps["es_service"]
        assert es.index_document.await_count == 1
        _, _, persisted_doc = es.index_document.await_args.args
        assert persisted_doc["sourced_terminal_id"] is None
        assert persisted_doc["sourced_terminal_reasons"] == []
        assert persisted_doc["sourcing_recommendation_id"] is None

    @pytest.mark.asyncio
    async def test_recommender_exception_logged_route_persists(self):
        """Req 8.5.5: exception → swallow, log, route still persists.

        A recommender outage (ES down, rack-price adapter failure, bug)
        must not bubble up through the agent. The route persists with
        the sourcing fields unset and the caller still receives a route
        proposal.
        """

        recommender = _StubRecommender(
            raises=RuntimeError("rack-price adapter down")
        )
        agent, deps = _make_agent(
            terminal_sourcing_state="active_gated",
            recommender=recommender,
            es_hits=[_make_station_location_hit("station-1", 6.45, 3.40)],
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                station_ids=["station-1"],
                terminal_id="term_buckeye",
            )
        )

        # Must not raise — the recommender exception is swallowed.
        route_proposals = await agent.evaluate([])

        assert len(recommender.calls) == 1
        es = deps["es_service"]
        assert es.index_document.await_count == 1
        _, _, persisted_doc = es.index_document.await_args.args
        assert persisted_doc["sourced_terminal_id"] is None
        assert persisted_doc["sourced_terminal_reasons"] == []
        assert persisted_doc["sourcing_recommendation_id"] is None
        assert len(route_proposals) == 1
