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

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

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

# Average speed in km/h for ETA estimation
DEFAULT_SPEED_KMH = 40.0

# Default depot location (fallback)
DEFAULT_DEPOT = {"lat": 6.5244, "lon": 3.3792}  # Lagos, Nigeria


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

            # Step 4: Run route optimization (Req 4.5)
            optimized_order, total_distance = optimize_route(
                locations, start_index=0
            )

            # Check SLA window violations (Req 4.4)
            sla_violations = check_sla_windows(
                order=optimized_order,
                distance_matrix=build_distance_matrix(locations),
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
            )

            # Compute objective value (Req 4.6)
            route_plan.objective_value = self._compute_objective_value(
                route_plan=route_plan,
                utilization_pct=loading_plan.get("total_utilization_pct", 0.0),
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
            "_source": ["station_id", "latitude", "longitude"],
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
                lat = source.get("latitude", 0.0)
                lon = source.get("longitude", 0.0)
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
    # Persistence (Req 4.7)
    # ------------------------------------------------------------------

    async def _persist_route_plan(self, route_plan: RoutePlan) -> None:
        """Persist a RoutePlan to the mvp_routes ES index."""
        try:
            doc = route_plan.model_dump(mode="json")
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
