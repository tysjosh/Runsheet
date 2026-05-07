"""
Route Planning Agent — overlay agent for optimized delivery route generation.

Subscribes to InterventionProposals from the compartment_loading agent,
extracts loading plans, queries station locations, runs route optimization
using the route_solver, computes objective values, produces
InterventionProposals with route plan actions, and persists routes to
mvp_routes.

Default configuration:
    - decision_cycle: 60 seconds
    - cooldown: 15 minutes per truck

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9,
              and Fuel Ops Hardening 2.1.3, 2.1.5, 2.1.6 (Traffic_Provider
              wiring with feature-flag gating and Haversine fallback).
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple

import httpx

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
)
from Agents.overlay.signal_bus import SignalBus
from Agents.support.fuel_distribution_models import (
    RoutePlan,
    RouteStop,
)
from Agents.support.mvp_es_mappings import MVP_ROUTES_INDEX
from Agents.support.route_solver import (
    build_distance_matrix,
    check_sla_windows,
    optimize_route,
)
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
    canonicalize_or_warn,
)
from fuel.services.sourcing_recommender import (
    InvalidBrandedPreferenceError,
    SourcingRecommender,
)
from fuel.services.traffic_provider import (
    TrafficBudgetExceeded,
    TrafficProvider,
    TravelMatrix,
    build_traffic_provider,
)
from fuel.terminal_models import SourcingRecommendation

logger = logging.getLogger(__name__)

# Elasticsearch indices consumed by this agent
FUEL_STATIONS_INDEX = "fuel_stations"

# Default objective weights (Req 4.6)
DEFAULT_OBJECTIVE_WEIGHTS: Dict[str, float] = {
    "route_cost": 0.20,
    "runout_risk_reduction": 0.30,
    "truck_utilization": 0.15,
    "late_delivery_penalty": 0.25,
    "plan_churn": 0.10,
}

# Average speed in km/h for ETA estimation and Haversine fallback
# travel-time derivation (Req 2.1.5).
DEFAULT_SPEED_KMH = 40.0

# Default depot location (legacy fallback). Task 4.2 removes the hardcoded
# Lagos coordinate from the resolver chain; this constant remains only as
# a module-level default consumed by the legacy single-depot code path
# until Depot resolution is fully wired in downstream tasks.
DEFAULT_DEPOT = {"lat": 6.5244, "lon": 3.3792}  # Lagos, Nigeria

# Redis key template for the per-tenant Traffic_Provider selection
# (Req 2.1.2). The value is a short provider name ("mapbox", "here",
# "google") or a JSON object of the form {"name": "mapbox"}.
TRAFFIC_PROVIDER_CONFIG_KEY_TEMPLATE = "overlay.traffic_provider:{tenant_id}"

# Overlay feature-flag name that gates whether the configured
# Traffic_Provider is actually consulted (Req 2.1.3).
TRAFFIC_AWARE_ROUTING_FLAG_KEY = "overlay.traffic_aware_routing"

#: Overlay feature flag name that gates whether the Route_Planning_Agent
#: consults the Sourcing_Recommender for loading plans requiring a
#: non-depot terminal lift (Req 8.5.5 / Task 7.10). When the flag is
#: anything other than ``active_gated`` / ``active_auto`` the agent
#: skips the recommender entirely and leaves
#: :attr:`RoutePlan.sourced_terminal_id` / ``sourced_terminal_reasons``
#: unset.
TERMINAL_SOURCING_FLAG_KEY = "overlay.terminal_sourcing"

# Hard timeout budget for the entire get_matrix call (Req 2.1.5). The
# provider base class already applies the same 10-second budget inside
# its own asyncio.wait_for. We set an outer timeout here as defense-in-
# depth so a misbehaving subclass cannot block the decision cycle.
TRAFFIC_MATRIX_TIMEOUT_SECONDS = 10.0

#: Exact liters-per-gallon factor used to convert Loading_Plan assignment
#: volumes (stored in liters) into the gallons unit the
#: :class:`SourcingRecommender` expects (Task 7.10). Keep in sync with
#: :data:`services.unit_conversion.GAL_TO_L`.
LITERS_PER_GALLON = 3.785411784


class TenantConfigLookup(Protocol):
    """Minimal Redis-like interface for per-tenant config reads.

    The Route_Planning_Agent uses this to resolve the
    ``overlay.traffic_provider:{tenant_id}`` key (Req 2.1.2). The same
    object is passed through to the injected Traffic_Provider factory so
    every adapter shares a single tenant-scoped config backend.
    """

    async def get(self, key: str) -> Optional[Any]:  # pragma: no cover - protocol
        ...


#: Factory signature accepted by ``set_traffic_provider_factory``. Given a
#: provider short-name (and the tenant_id so factories can inject per-
#: tenant credentials), returns a constructed :class:`TrafficProvider`.
#: Returning ``None`` tells the agent to fall back to Haversine for this
#: tenant; raising is treated the same way.
TrafficProviderFactory = Callable[[str, str], Optional[TrafficProvider]]


class RoutePlanningAgent(OverlayAgentBase):
    """Generates optimized delivery routes from loading plans.

    Consumes InterventionProposals from the compartment_loading agent,
    extracts loading plan details, queries station locations, runs
    nearest-neighbor + 2-opt route optimization, and produces
    InterventionProposals with route plan actions.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service for querying indices.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        poll_interval: Decision cycle interval in seconds (default 60).
        cooldown_minutes: Per-truck cooldown in minutes (default 15).
    """

    def __init__(
        self,
        signal_bus: SignalBus,
        es_service,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        autonomy_config_service,
        feature_flag_service,
        poll_interval: int = 60,
        cooldown_minutes: int = 15,
        *,
        traffic_provider_factory: Optional[TrafficProviderFactory] = None,
        tenant_config: Optional[TenantConfigLookup] = None,
        sourcing_recommender: Optional[SourcingRecommender] = None,
    ):
        super().__init__(
            agent_id="route_planning",
            signal_bus=signal_bus,
            subscriptions=[
                {
                    "message_type": InterventionProposal,
                    "filters": {
                        "source_agent": "compartment_loading",
                    },
                },
            ],
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            autonomy_config_service=autonomy_config_service,
            feature_flag_service=feature_flag_service,
            es_service=es_service,
            poll_interval=poll_interval,
            cooldown_minutes=cooldown_minutes,
        )
        # Buffer loading proposals between cycles
        self._proposal_buffer: List[InterventionProposal] = []

        # ---- Traffic_Provider wiring (Req 2.1.2, 2.1.3, 2.1.5, 2.1.6) ---
        # The factory is consulted once we know the provider short-name
        # for a tenant; the agent caches the resolved instance per tenant
        # so we don't rebuild the HTTP client on every cycle. Both the
        # factory and the tenant-config lookup are optional — when either
        # is missing the agent permanently falls back to Haversine and
        # stamps ``traffic_fallback: true`` on every plan for visibility.
        self._traffic_provider_factory: Optional[TrafficProviderFactory] = (
            traffic_provider_factory
        )
        self._tenant_config: Optional[TenantConfigLookup] = tenant_config
        self._traffic_provider_cache: Dict[Tuple[str, str], TrafficProvider] = {}

        # ---- Sourcing_Recommender wiring (Task 7.10 / Req 8.5.5) --------
        # When a Loading_Plan requires an external terminal lift (i.e.
        # the plan's ``terminal_id`` is set and differs from the tenant's
        # depot), the Route_Planning_Agent consults the recommender to
        # choose a loading terminal and records the pick on the
        # Route_Plan. The attribute is ``None`` until bootstrap injects
        # the singleton via :meth:`set_sourcing_recommender`; until then
        # every evaluation skips the sourcing step entirely so tests and
        # early bootstrap states fail soft instead of raising.
        self._sourcing_recommender: Optional[SourcingRecommender] = (
            sourcing_recommender
        )

    # ------------------------------------------------------------------
    # Public wiring hooks (bootstrap injects these after construction)
    # ------------------------------------------------------------------

    def set_traffic_provider_factory(
        self, factory: Optional[TrafficProviderFactory]
    ) -> None:
        """Inject the Traffic_Provider factory post-construction.

        Passing ``None`` disables the traffic-aware code path for every
        tenant and forces a Haversine fallback with ``traffic_fallback:
        true`` (Req 2.1.5).
        """
        self._traffic_provider_factory = factory
        self._traffic_provider_cache.clear()

    def set_tenant_config(
        self, lookup: Optional[TenantConfigLookup]
    ) -> None:
        """Inject the tenant-config lookup post-construction.

        Used to resolve ``overlay.traffic_provider:{tenant_id}`` per
        tenant (Req 2.1.2). ``None`` disables provider selection and
        therefore forces the Haversine fallback for every tenant.
        """
        self._tenant_config = lookup
        self._traffic_provider_cache.clear()

    def set_sourcing_recommender(
        self, recommender: Optional[SourcingRecommender]
    ) -> None:
        """Inject the :class:`SourcingRecommender` post-construction.

        Passing ``None`` disables the sourcing-recommender integration:
        the agent leaves ``sourced_terminal_id`` / ``sourced_terminal_reasons``
        / ``sourcing_recommendation_id`` on the Route_Plan unset for
        every run. The bootstrap in :mod:`bootstrap.agents` invokes this
        after constructing the recommender singleton so the Route_Plan
        carries the chosen terminal id for every Loading_Plan that
        required an external lift (Task 7.10, Req 8.5.5).
        """
        self._sourcing_recommender = recommender

    # ------------------------------------------------------------------
    # Signal handling override — buffer InterventionProposals
    # ------------------------------------------------------------------

    async def _on_signal(self, signal) -> None:
        """Buffer incoming signals. InterventionProposals from
        compartment_loading are stored separately."""
        if (
            isinstance(signal, InterventionProposal)
            and signal.source_agent == "compartment_loading"
        ):
            self._proposal_buffer.append(signal)
        else:
            await super()._on_signal(signal)

    # ------------------------------------------------------------------
    # Core evaluation (Req 4.1–4.9)
    # ------------------------------------------------------------------

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Consume loading proposals, generate optimized routes.

        Steps:
        1. Collect buffered InterventionProposals from compartment_loading.
        2. For each proposal: extract loading plan details (Req 4.1).
        3. Query station locations for route optimization (Req 4.3).
        4. Run route optimization using route_solver (Req 4.5).
        5. Compute objective value (Req 4.6).
        6. Persist route plans to mvp_routes (Req 4.7).
        7. Produce InterventionProposals with route plan actions.

        Returns:
            List of InterventionProposals with route plan actions.
        """
        # Step 1: Collect buffered proposals
        proposals = list(self._proposal_buffer)
        self._proposal_buffer.clear()

        if not proposals:
            return []

        route_proposals: List[InterventionProposal] = []

        for loading_proposal in proposals:
            tenant_id = loading_proposal.tenant_id

            # Step 2: Extract loading plan details
            loading_plan = self._extract_loading_plan(loading_proposal)
            if not loading_plan:
                continue

            truck_id = loading_plan.get("truck_id", "")
            plan_id = loading_plan.get("plan_id", "")
            assignments = loading_plan.get("assignments", [])

            # Collect unique station IDs from assignments
            station_ids = list(
                {a.get("station_id", "") for a in assignments if a.get("station_id")}
            )
            if not station_ids:
                continue

            # Step 3: Query station locations (Req 4.3)
            station_locations = await self._query_station_locations(
                tenant_id, station_ids
            )

            # Query SLA windows for stations (Req 4.4)
            station_sla_windows = await self._query_sla_windows(
                tenant_id, station_ids
            )

            # Build locations list: depot first, then stations
            locations, station_order = self._build_location_list(
                station_ids, station_locations
            )

            if len(locations) < 2:
                # Need at least depot + 1 station
                continue

            # Build SLA windows indexed by location list position
            sla_windows_by_idx = {}
            for i, sid in enumerate(station_order):
                if sid in station_sla_windows:
                    sla_windows_by_idx[i + 1] = station_sla_windows[sid]  # +1 for depot offset

            # Step 3b: Traffic-aware travel matrix (Req 2.1.3, 2.1.5, 2.1.6).
            # Returns a (distance_matrix, used_provider_name, traffic_fallback)
            # triple. When the feature flag is disabled, no provider is
            # configured, or any upstream error occurs (timeout, budget,
            # HTTP failure), we fall back to the Haversine matrix built
            # from the route-solver's shared helper and annotate the plan
            # with ``traffic_fallback: true`` (Req 2.1.5).
            (
                traffic_distance_matrix,
                used_provider_name,
                traffic_fallback,
            ) = await self._resolve_travel_matrix(
                tenant_id=tenant_id, locations=locations
            )

            # Step 4: Run route optimization (Req 4.5)
            optimized_order, total_distance = optimize_route(
                locations, start_index=0
            )

            # Check SLA window violations (Req 4.4). When we have a
            # traffic-informed matrix use it for the SLA ETA check so
            # window violations reflect real drive times; otherwise the
            # existing Haversine matrix built by the solver is used.
            sla_distance_matrix = (
                traffic_distance_matrix
                if traffic_distance_matrix is not None
                else build_distance_matrix(locations)
            )
            sla_violations = check_sla_windows(
                order=optimized_order,
                distance_matrix=sla_distance_matrix,
                sla_windows=sla_windows_by_idx if sla_windows_by_idx else None,
                speed_kmh=DEFAULT_SPEED_KMH,
            )

            # Step 5: Build route plan with ETAs
            route_plan = self._build_route_plan(
                truck_id=truck_id,
                plan_id=plan_id,
                optimized_order=optimized_order,
                station_order=station_order,
                total_distance=total_distance,
                assignments=assignments,
                tenant_id=tenant_id,
                sla_violations=sla_violations,
                traffic_provider_name=used_provider_name,
                traffic_fallback=traffic_fallback,
            )

            # Compute objective value (Req 4.6)
            route_plan.objective_value = self._compute_objective_value(
                route_plan=route_plan,
                utilization_pct=loading_plan.get("total_utilization_pct", 0.0),
            )

            # Set run_id from pipeline context if available
            route_plan.run_id = getattr(self, '_current_run_id', None) or ""

            # Step 5b: Sourcing_Recommender wiring (Task 7.10 / Req 8.5.5)
            # When the Loading_Plan requires an external terminal lift
            # (terminal_id present on the plan and sourcing flag active
            # for the tenant), consult the already-wired recommender and
            # stamp the winning terminal id + reasons on the route plan.
            # Every failure path (flag off, no recommender, zero
            # candidates, exception) leaves the sourcing fields None so
            # the route still persists.
            await self._maybe_apply_sourcing(
                route_plan=route_plan,
                loading_plan=loading_plan,
                assignments=assignments,
                locations=locations,
                tenant_id=tenant_id,
                truck_id=truck_id,
            )

            # Step 6: Persist route plan to ES (Req 4.7)
            await self._persist_route_plan(route_plan)

            # Step 7: Build InterventionProposal
            proposal = self._build_route_proposal(
                route_plan=route_plan,
                tenant_id=tenant_id,
            )
            route_proposals.append(proposal)

        logger.info(
            "RoutePlanningAgent: produced %d route plans",
            len(route_proposals),
        )

        return route_proposals

    # ------------------------------------------------------------------
    # Extract loading plan from proposal
    # ------------------------------------------------------------------

    def _extract_loading_plan(
        self, proposal: InterventionProposal
    ) -> Optional[Dict[str, Any]]:
        """Extract loading plan details from an InterventionProposal."""
        for action in proposal.actions:
            if action.get("tool_name") == "apply_loading_plan":
                return action.get("parameters", {})
        return None

    # ------------------------------------------------------------------
    # Query station locations (Req 4.3)
    # ------------------------------------------------------------------

    async def _query_station_locations(
        self, tenant_id: str, station_ids: List[str]
    ) -> Dict[str, Dict[str, float]]:
        """Query fuel_stations for lat/lon coordinates.

        Returns a dict keyed by station_id with {lat, lon} dicts.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"station_id": station_ids}},
                    ],
                },
            },
            "_source": ["station_id", "latitude", "longitude", "location"],
            "size": 200,
        }

        locations: Dict[str, Dict[str, float]] = {}
        try:
            resp = await self._es.search_documents(
                FUEL_STATIONS_INDEX, query, 200
            )
            for hit in resp.get("hits", {}).get("hits", []):
                source = hit["_source"]
                sid = source.get("station_id", "")
                # Try explicit lat/lon fields first
                lat = source.get("latitude", 0.0)
                lon = source.get("longitude", 0.0)
                # Fall back to location geo_point field
                if (lat == 0.0 and lon == 0.0) and source.get("location"):
                    loc = source["location"]
                    if isinstance(loc, dict):
                        lat = loc.get("lat", 0.0)
                        lon = loc.get("lon", 0.0)
                if sid and (lat != 0.0 or lon != 0.0):
                    locations[sid] = {"lat": lat, "lon": lon}
        except Exception as e:
            logger.error(
                "RoutePlanningAgent: failed to query station locations: %s", e
            )

        return locations

    # ------------------------------------------------------------------
    # Query SLA windows (Req 4.4)
    # ------------------------------------------------------------------

    async def _query_sla_windows(
        self, tenant_id: str, station_ids: List[str]
    ) -> Dict[str, Tuple[float, float]]:
        """Query fuel_stations for SLA delivery windows.

        Returns a dict keyed by station_id with (earliest_hour, latest_hour)
        tuples. Stations without SLA windows are omitted.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"station_id": station_ids}},
                    ],
                },
            },
            "_source": [
                "station_id", "sla_delivery_window_start",
                "sla_delivery_window_end",
            ],
            "size": 200,
        }

        sla_windows: Dict[str, Tuple[float, float]] = {}
        try:
            resp = await self._es.search_documents(
                FUEL_STATIONS_INDEX, query, 200
            )
            for hit in resp.get("hits", {}).get("hits", []):
                source = hit["_source"]
                sid = source.get("station_id", "")
                start_h = source.get("sla_delivery_window_start")
                end_h = source.get("sla_delivery_window_end")
                if sid and start_h is not None and end_h is not None:
                    sla_windows[sid] = (float(start_h), float(end_h))
        except Exception as e:
            logger.error(
                "RoutePlanningAgent: failed to query SLA windows: %s", e
            )

        return sla_windows

    # ------------------------------------------------------------------
    # Build location list for route solver
    # ------------------------------------------------------------------

    def _build_location_list(
        self,
        station_ids: List[str],
        station_locations: Dict[str, Dict[str, float]],
    ) -> Tuple[List[Dict[str, float]], List[str]]:
        """Build ordered location list with depot at index 0.

        Returns:
            (locations, station_order) where station_order[i] corresponds
            to locations[i+1] (index 0 is the depot).
        """
        locations = [dict(DEFAULT_DEPOT)]  # Depot at index 0
        station_order: List[str] = []

        for sid in station_ids:
            loc = station_locations.get(sid)
            if loc:
                locations.append(loc)
                station_order.append(sid)

        return locations, station_order

    # ------------------------------------------------------------------
    # Build route plan with ETAs (Req 4.1)
    # ------------------------------------------------------------------

    def _build_route_plan(
        self,
        truck_id: str,
        plan_id: str,
        optimized_order: List[int],
        station_order: List[str],
        total_distance: float,
        assignments: List[Dict[str, Any]],
        tenant_id: str,
        sla_violations: Optional[List[Dict]] = None,
        traffic_provider_name: Optional[str] = None,
        traffic_fallback: bool = False,
    ) -> RoutePlan:
        """Build a RoutePlan from optimized route order."""
        # Build drop quantities per station
        station_drops: Dict[str, Dict[str, float]] = {}
        for assignment in assignments:
            sid = assignment.get("station_id", "")
            grade = assignment.get("fuel_grade", "")
            qty = assignment.get("quantity_liters", 0.0)
            if sid and grade:
                station_drops.setdefault(sid, {})
                station_drops[sid][grade] = (
                    station_drops[sid].get(grade, 0.0) + qty
                )

        # Build set of at-risk stop indices from SLA violations (Req 4.4)
        at_risk_indices = set()
        if sla_violations:
            at_risk_indices = {v["stop_index"] for v in sla_violations}
            for v in sla_violations:
                logger.warning(
                    "RoutePlanningAgent: SLA at-risk — stop index %d, "
                    "ETA %.1fh exceeds window end %.1fh by %.1fh",
                    v["stop_index"], v["eta_hours"],
                    v["window_end"], v["late_by_hours"],
                )

        # Build stops from optimized order (skip depot at index 0)
        stops: List[RouteStop] = []
        now = datetime.now(timezone.utc)
        cumulative_time_hours = 0.0
        sequence = 0

        for idx in optimized_order:
            if idx == 0:
                # Skip depot
                continue
            # Map back to station_id
            station_idx = idx - 1  # Offset for depot
            if station_idx < 0 or station_idx >= len(station_order):
                continue

            station_id = station_order[station_idx]
            drop = station_drops.get(station_id, {})

            # Estimate ETA based on cumulative travel time
            cumulative_time_hours += 0.5  # Approximate 30 min between stops
            eta = now + timedelta(hours=cumulative_time_hours)

            stops.append(
                RouteStop(
                    station_id=station_id,
                    eta=eta.isoformat(),
                    drop=drop,
                    sequence=sequence,
                )
            )
            sequence += 1

        return RoutePlan(
            truck_id=truck_id,
            plan_id=plan_id,
            stops=stops,
            distance_km=round(total_distance, 2),
            eta_confidence=0.75 if not at_risk_indices else 0.4,
            tenant_id=tenant_id,
            traffic_provider=traffic_provider_name,
            traffic_fallback=traffic_fallback,
        )

    # ------------------------------------------------------------------
    # Compute objective value (Req 4.6)
    # ------------------------------------------------------------------

    def _compute_objective_value(
        self,
        route_plan: RoutePlan,
        utilization_pct: float = 0.0,
    ) -> float:
        """Compute weighted objective value for a route plan.

        Objective = w_cost * (1 - normalized_cost)
                  + w_risk * risk_reduction_estimate
                  + w_util * (utilization / 100)
                  + w_late * (1 - late_penalty_estimate)
                  - w_churn * churn_penalty

        Higher is better.
        """
        weights = DEFAULT_OBJECTIVE_WEIGHTS

        # Normalize distance cost (assume 500km is max reasonable route)
        max_distance = 500.0
        cost_score = max(0.0, 1.0 - (route_plan.distance_km / max_distance))

        # Risk reduction estimate (more stops served = more risk reduced)
        risk_score = min(1.0, len(route_plan.stops) / 10.0)

        # Utilization score
        util_score = utilization_pct / 100.0

        # Late delivery penalty (assume no late deliveries for new plans)
        late_score = 1.0

        # Churn penalty (new plans have no churn)
        churn_penalty = 0.0

        objective = (
            weights["route_cost"] * cost_score
            + weights["runout_risk_reduction"] * risk_score
            + weights["truck_utilization"] * util_score
            + weights["late_delivery_penalty"] * late_score
            - weights["plan_churn"] * churn_penalty
        )

        return round(max(0.0, min(1.0, objective)), 4)

    # ------------------------------------------------------------------
    # Build route proposal
    # ------------------------------------------------------------------

    def _build_route_proposal(
        self,
        route_plan: RoutePlan,
        tenant_id: str,
    ) -> InterventionProposal:
        """Build an InterventionProposal from a route plan."""
        actions = [
            {
                "tool_name": "apply_route_plan",
                "parameters": {
                    "route_id": route_plan.route_id,
                    "truck_id": route_plan.truck_id,
                    "plan_id": route_plan.plan_id,
                    "stops": [s.model_dump(mode="json") for s in route_plan.stops],
                    "distance_km": route_plan.distance_km,
                    "objective_value": route_plan.objective_value,
                },
                "description": (
                    f"Route for truck {route_plan.truck_id}: "
                    f"{len(route_plan.stops)} stops, "
                    f"{route_plan.distance_km:.1f}km, "
                    f"objective={route_plan.objective_value:.3f}"
                ),
            }
        ]

        return InterventionProposal(
            source_agent=self.agent_id,
            actions=actions,
            expected_kpi_delta={
                "route_distance_km": -route_plan.distance_km,
                "stops_served": len(route_plan.stops),
                "objective_value": route_plan.objective_value,
            },
            risk_class=RiskClass.LOW,
            confidence=route_plan.eta_confidence,
            priority=1,
            tenant_id=tenant_id,
        )

    # ------------------------------------------------------------------
    # Traffic_Provider orchestration (Req 2.1.3, 2.1.5, 2.1.6)
    # ------------------------------------------------------------------

    async def _resolve_travel_matrix(
        self,
        *,
        tenant_id: str,
        locations: List[Dict[str, float]],
    ) -> Tuple[Optional[List[List[float]]], Optional[str], bool]:
        """Return ``(distance_matrix, provider_name, traffic_fallback)``.

        Implements the tenant-gated Traffic_Provider flow mandated by
        Fuel Ops Hardening Requirements 2.1.3, 2.1.5, and 2.1.6:

        1. Check the ``overlay.traffic_aware_routing`` feature flag for
           ``tenant_id``. If the flag is not in an active state we skip
           the provider entirely and return ``(None, None, False)`` so
           the caller keeps using the Haversine distance matrix and
           **does not** stamp ``traffic_fallback: true`` — the plan is
           simply not traffic-aware (Req 2.1.3).
        2. Read ``overlay.traffic_provider:{tenant_id}`` from the tenant
           config (Req 2.1.2). Absent / blank / unparseable values are
           treated as a misconfiguration; we fall back to Haversine and
           annotate ``traffic_fallback: true`` per Req 2.1.5.
        3. Build (and cache) the concrete :class:`TrafficProvider` via
           the injected factory. Unknown provider names or factory
           failures also fall back to Haversine with the annotation.
        4. Call :meth:`TrafficProvider.get_matrix` under a 10-second
           outer timeout (Req 2.1.5). Any failure — asyncio timeout,
           :class:`TrafficBudgetExceeded` (Req 2.1.7), httpx.HTTPError,
           or runtime error — degrades to Haversine and stamps
           ``traffic_fallback: true`` on the Route_Plan.
        5. When the provider succeeds we return the distance matrix and
           ``traffic_fallback: False`` so consumers see a fully
           traffic-informed plan.

        Args:
            tenant_id: Owning tenant. Used for flag lookup, provider
                selection, and the provider's own budget counter.
            locations: Ordered list of ``{lat, lon}`` dicts where index 0
                is the depot and indices ``1..N`` are customer stops.

        Returns:
            ``(distance_matrix, provider_name, traffic_fallback)``.

            * ``distance_matrix`` — a ``List[List[float]]`` of km values
              or ``None`` when traffic-aware routing is disabled. When a
              matrix is returned the caller SHOULD use it for SLA ETA
              checks so window violations reflect real drive times.
            * ``provider_name`` — the short provider name actually used
              (``"mapbox"``, ``"here"``, ``"google"``) on success, or
              ``None`` when no provider was consulted.
            * ``traffic_fallback`` — ``True`` only when a provider *was*
              attempted (flag enabled AND provider configured) and the
              attempt failed. When the flag is off or no provider is
              configured, we don't consider the Haversine matrix a
              "fallback" and leave this at ``False``.
        """

        # (1) Feature-flag gate ------------------------------------------
        if not await self._traffic_aware_routing_enabled(tenant_id):
            return None, None, False

        # (2) Provider selection -----------------------------------------
        provider_name = await self._resolve_traffic_provider_name(tenant_id)
        if not provider_name:
            logger.warning(
                "RoutePlanningAgent: traffic-aware routing enabled for "
                "tenant=%s but no provider configured; falling back to "
                "Haversine + DEFAULT_SPEED_KMH",
                tenant_id,
            )
            return None, None, True

        # (3) Concrete provider ------------------------------------------
        provider = self._get_or_build_traffic_provider(tenant_id, provider_name)
        if provider is None:
            logger.warning(
                "RoutePlanningAgent: traffic_provider=%s unavailable for "
                "tenant=%s; falling back to Haversine",
                provider_name,
                tenant_id,
            )
            return None, None, True

        # (4) Call the provider under a 10-second budget -----------------
        origins = [(loc["lat"], loc["lon"]) for loc in locations]
        depart_at = datetime.now(timezone.utc)
        try:
            matrix = await asyncio.wait_for(
                provider.get_matrix(
                    origins=origins,
                    destinations=origins,
                    depart_at=depart_at,
                    tenant_id=tenant_id,
                ),
                timeout=TRAFFIC_MATRIX_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "RoutePlanningAgent: Traffic_Provider[%s] timed out after "
                "%.1fs for tenant=%s; falling back to Haversine",
                provider_name,
                TRAFFIC_MATRIX_TIMEOUT_SECONDS,
                tenant_id,
            )
            return None, None, True
        except TrafficBudgetExceeded as exc:
            logger.warning(
                "RoutePlanningAgent: Traffic_Provider[%s] budget exhausted "
                "for tenant=%s month=%s (%d/%d); falling back to Haversine",
                provider_name,
                exc.tenant_id,
                exc.month,
                exc.current,
                exc.limit,
            )
            return None, None, True
        except httpx.HTTPError as exc:
            logger.warning(
                "RoutePlanningAgent: Traffic_Provider[%s] HTTP error for "
                "tenant=%s: %s; falling back to Haversine",
                provider_name,
                tenant_id,
                exc,
            )
            return None, None, True
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning(
                "RoutePlanningAgent: Traffic_Provider[%s] unexpected "
                "failure for tenant=%s: %s; falling back to Haversine",
                provider_name,
                tenant_id,
                exc,
            )
            return None, None, True

        # (5) Success — return the provider's distance matrix ------------
        try:
            distance_matrix = [list(row) for row in matrix.distance_km]
        except AttributeError:
            logger.warning(
                "RoutePlanningAgent: Traffic_Provider[%s] returned "
                "non-TravelMatrix result for tenant=%s; falling back",
                provider_name,
                tenant_id,
            )
            return None, None, True

        return distance_matrix, matrix.provider or provider_name, False

    async def _traffic_aware_routing_enabled(self, tenant_id: str) -> bool:
        """Return True when ``overlay.traffic_aware_routing`` is active.

        The feature flag maps to an overlay state. We treat
        ``active_gated`` and ``active_auto`` as "enabled" — both modes
        route real traffic through the solver. ``shadow`` and
        ``disabled`` keep the agent on the Haversine default so the
        provider budget is never spent in shadow mode (Req 2.1.3).
        """

        if self._feature_flags is None:
            return False
        try:
            state = await self._feature_flags.get_overlay_state(
                TRAFFIC_AWARE_ROUTING_FLAG_KEY, tenant_id
            )
        except AttributeError:
            # Legacy services expose only ``is_enabled``. We treat the
            # boolean as "active" when True for backward compatibility.
            try:
                return bool(await self._feature_flags.is_enabled(tenant_id))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "RoutePlanningAgent: feature flag lookup failed for "
                    "tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                return False
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "RoutePlanningAgent: overlay state lookup failed for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )
            return False
        return state in {"active_gated", "active_auto"}

    async def _resolve_traffic_provider_name(
        self, tenant_id: str
    ) -> Optional[str]:
        """Read ``overlay.traffic_provider:{tenant_id}`` and return the name.

        Accepts either a plain string (``"mapbox"``) or a JSON object
        whose ``name`` key carries the short provider identifier. Unknown
        shapes are logged and treated as "no provider configured" — the
        caller surfaces this as a Haversine fallback with annotation.
        """

        if self._tenant_config is None:
            return None
        key = TRAFFIC_PROVIDER_CONFIG_KEY_TEMPLATE.format(tenant_id=tenant_id)
        try:
            raw = await self._tenant_config.get(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "RoutePlanningAgent: tenant_config.get(%s) failed: %s",
                key,
                exc,
            )
            return None
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return None
            # Try JSON first; fall back to treating the value as the
            # provider short-name. A config value of "mapbox" is the
            # common, idiomatic form so we don't require operators to
            # wrap it in JSON.
            if stripped.startswith("{"):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.warning(
                        "RoutePlanningAgent: traffic_provider config for "
                        "tenant=%s is not valid JSON: %r",
                        tenant_id,
                        stripped,
                    )
                    return None
                name = parsed.get("name") if isinstance(parsed, Mapping) else None
                if isinstance(name, str) and name.strip():
                    return name.strip().lower()
                return None
            return stripped.lower()
        if isinstance(raw, Mapping):
            name = raw.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip().lower()
        return None

    def _get_or_build_traffic_provider(
        self, tenant_id: str, provider_name: str
    ) -> Optional[TrafficProvider]:
        """Return a cached TrafficProvider for the tenant, or build one.

        Caches by ``(tenant_id, provider_name)`` so rotating a tenant's
        provider invalidates only that tenant's entry. The injected
        factory is tried first (so tests can stub providers); when no
        factory is configured we fall back to the module-level
        :func:`build_traffic_provider` registry.
        """

        cache_key = (tenant_id, provider_name)
        cached = self._traffic_provider_cache.get(cache_key)
        if cached is not None:
            return cached

        provider: Optional[TrafficProvider] = None
        if self._traffic_provider_factory is not None:
            try:
                provider = self._traffic_provider_factory(provider_name, tenant_id)
            except Exception as exc:
                logger.warning(
                    "RoutePlanningAgent: traffic_provider_factory(%s) failed "
                    "for tenant=%s: %s",
                    provider_name,
                    tenant_id,
                    exc,
                )
                provider = None
        if provider is None:
            try:
                provider = build_traffic_provider(provider_name)
            except ValueError as exc:
                logger.warning(
                    "RoutePlanningAgent: unknown traffic_provider=%r for "
                    "tenant=%s (%s)",
                    provider_name,
                    tenant_id,
                    exc,
                )
                return None
            except Exception as exc:
                logger.warning(
                    "RoutePlanningAgent: failed to build traffic_provider=%r "
                    "for tenant=%s: %s",
                    provider_name,
                    tenant_id,
                    exc,
                )
                return None

        if provider is not None:
            self._traffic_provider_cache[cache_key] = provider
        return provider

    # ------------------------------------------------------------------
    # Persistence (Req 4.7)
    # ------------------------------------------------------------------

    async def _persist_route_plan(self, route_plan: RoutePlan) -> None:
        """Persist a RoutePlan to the mvp_routes ES index.

        Canonicalizes every ``drop`` key (a fuel grade) on each stop
        before write so routes persisted from NG-aliased loading plans
        land in ES with canonical US codes (Req 6.1.4). Quantities from
        duplicate grade keys (e.g., both ``AGO`` and ``DIESEL_2`` on the
        same stop after canonicalization) are summed to preserve total
        drop volume. Unknown codes are preserved with a warning.
        """
        try:
            doc = route_plan.model_dump(mode="json")
            stops = doc.get("stops") or []
            for stop in stops:
                raw_drop = stop.get("drop") or {}
                if not isinstance(raw_drop, dict):
                    continue
                canonical_drop: Dict[str, float] = {}
                for grade, qty in raw_drop.items():
                    canonical_grade = canonicalize_or_warn(
                        grade,
                        context="mvp_routes.stops.drop",
                        logger_=logger,
                    )
                    # Accumulate quantities under the canonical key so a
                    # stop carrying both a US code and its NG alias does
                    # not lose volume after normalization.
                    canonical_drop[canonical_grade] = (
                        canonical_drop.get(canonical_grade, 0.0) + float(qty or 0.0)
                    )
                stop["drop"] = canonical_drop

            await self._es.index_document(
                MVP_ROUTES_INDEX,
                route_plan.route_id,
                doc,
            )
        except Exception as e:
            logger.error(
                "RoutePlanningAgent: failed to persist route plan %s: %s",
                route_plan.route_id,
                e,
            )
