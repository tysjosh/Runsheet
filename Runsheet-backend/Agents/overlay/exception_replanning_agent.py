"""
Exception Replanning Agent — overlay agent for live plan patching.

Subscribes to disruption RiskSignals from delay_response_agent,
sla_guardian_agent, and exception_commander. Detects disruption type
(truck_breakdown, station_outage, demand_spike, delay), loads the
current plan snapshot from ES, attempts replanning (stop reorder,
volume reallocation, truck swap), produces patched plans or escalates
with HIGH-severity RiskSignals, and persists replan events to
mvp_replan_events.

Routes all plan mutations through ConfirmationProtocol with MEDIUM
risk classification (truck swaps classified as HIGH).

For truck breakdowns, queries inventory for compatible repair parts
at the nearest depot. If parts are in stock, proposes a repair with
ETA; otherwise falls back to truck swap only.

Default configuration:
    - decision_cycle: 30 seconds (continuous monitor)
    - cooldown: 5 minutes per entity

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.confidence_utils import compute_confidence_score
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.overlay.signal_bus import SignalBus
from Agents.support.fuel_distribution_models import (
    ReplanDiff,
    ReplanEvent,
    RoutePlan,
    RouteStop,
)
from Agents.support.mvp_es_mappings import (
    MVP_LOAD_PLANS_INDEX,
    MVP_REPLAN_EVENTS_INDEX,
    MVP_ROUTES_INDEX,
)
from Agents.support.replan_diff_models import (
    ReplanDiff as StructuredReplanDiff,
    compute_replan_diff,
)
from inventory.es_mappings import INVENTORY_INDEX

logger = logging.getLogger(__name__)

# Disruption type detection keywords
DISRUPTION_KEYWORDS: Dict[str, List[str]] = {
    "truck_breakdown": ["breakdown", "vehicle_failure", "truck_down", "mechanical"],
    "station_outage": ["outage", "station_closed", "station_offline", "power_failure"],
    "demand_spike": ["demand_spike", "surge", "unexpected_demand", "high_demand"],
    "delay": ["delay", "late", "behind_schedule", "traffic", "sla_breach"],
}

# Source agents that this agent subscribes to (Req 5.1)
DISRUPTION_SOURCE_AGENTS = {
    "delay_response_agent",
    "sla_guardian_agent",
    "exception_commander",
}

# Repair part categories queried during breakdown handling (Req 4.1)
REPAIR_PART_CATEGORIES = ["tires", "brake_parts", "engine_parts"]

# Default estimated repair time in minutes per part category
DEFAULT_REPAIR_ETA_MINUTES = 45


class ExceptionReplanningAgent(OverlayAgentBase):
    """Patches plans when disruptions occur.

    Subscribes to disruption RiskSignals from delay_response_agent,
    sla_guardian_agent, and exception_commander. Detects disruption type,
    loads the current plan snapshot, attempts replanning, and produces
    patched plans or escalates.

    For truck breakdowns, queries inventory for compatible repair parts
    at the nearest depot. If parts are in stock, proposes a repair with
    ETA and parts list; otherwise falls back to truck swap only.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service for querying indices.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        inventory_service: Optional InventoryService for parts consumption.
        poll_interval: Decision cycle interval in seconds (default 30).
        cooldown_minutes: Per-entity cooldown in minutes (default 5).
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
        inventory_service=None,
        fuel_planning_ws_manager=None,
        poll_interval: int = 30,
        cooldown_minutes: int = 5,
    ):
        super().__init__(
            agent_id="exception_replanning",
            signal_bus=signal_bus,
            subscriptions=[
                {
                    "message_type": RiskSignal,
                    "filters": {
                        "source_agent": list(DISRUPTION_SOURCE_AGENTS),
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
        self._inventory_service = inventory_service

        #: Dedicated fuel-planning WS manager used to emit the
        #: ``replan_diff_ready`` event (Req 2.5.4) on ``/ws/fuel-planning``.
        #: Distinct from ``self._ws`` (the generic agent-activity manager
        #: inherited from :class:`AutonomousAgentBase`) so dispatcher UIs
        #: listening on the fuel-planning channel receive the diff summary
        #: in real time. Optional so existing tests and bootstrap paths that
        #: haven't wired the manager yet continue to work — broadcasts are
        #: silently skipped when the manager is ``None``.
        self._fuel_planning_ws = fuel_planning_ws_manager

    # ------------------------------------------------------------------
    # Core evaluation (Req 5.1–5.8)
    # ------------------------------------------------------------------

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Detect disruptions and produce patched plans or escalate.

        Steps:
        1. For each disruption signal: detect disruption type (Req 5.1).
        2. Load current plan snapshot from ES (Req 5.2).
        3. Attempt replan based on disruption type (Req 5.3–5.5).
        4. If no feasible replan: escalate with HIGH-severity RiskSignal (Req 5.6).
        5. Persist replan events to mvp_replan_events (Req 5.7).
        6. Route mutations through ConfirmationProtocol (Req 5.8).

        Returns:
            List of InterventionProposals with replan actions.
        """
        if not signals:
            return []

        tenant_id = signals[0].tenant_id
        proposals: List[InterventionProposal] = []

        for signal in signals:
            # Step 1: Detect disruption type
            disruption_type = self._detect_disruption_type(signal)

            # Step 2: Load current plan snapshot
            plan_snapshot = await self._load_plan_snapshot(tenant_id)
            if not plan_snapshot:
                logger.info(
                    "ExceptionReplanningAgent: no active plan found for "
                    "tenant %s, skipping signal %s",
                    tenant_id,
                    signal.signal_id,
                )
                continue

            # Step 3: Attempt replan based on disruption type
            replan_result = await self._attempt_replan(
                disruption_type=disruption_type,
                signal=signal,
                plan_snapshot=plan_snapshot,
                tenant_id=tenant_id,
            )

            if replan_result is not None:
                proposals.append(replan_result)

        logger.info(
            "ExceptionReplanningAgent: processed %d signals, produced %d "
            "replan proposals for tenant %s",
            len(signals),
            len(proposals),
            tenant_id,
        )

        return proposals

    # ------------------------------------------------------------------
    # Disruption type detection (Req 5.1)
    # ------------------------------------------------------------------

    def _detect_disruption_type(self, signal: RiskSignal) -> str:
        """Detect disruption type from signal context and source.

        Checks signal context fields and entity_type against known
        disruption keywords. Falls back to 'delay' if unrecognized.
        """
        context = signal.context or {}

        # Check explicit disruption_type in context
        explicit_type = context.get("disruption_type", "")
        if explicit_type in DISRUPTION_KEYWORDS:
            return explicit_type

        # Check entity_type
        entity_type = signal.entity_type.lower()
        for dtype, keywords in DISRUPTION_KEYWORDS.items():
            if entity_type in keywords:
                return dtype

        # Check context values for keyword matches
        context_str = str(context).lower()
        for dtype, keywords in DISRUPTION_KEYWORDS.items():
            for keyword in keywords:
                if keyword in context_str:
                    return dtype

        # Check source agent for hints
        if signal.source_agent == "delay_response_agent":
            return "delay"
        if signal.source_agent == "sla_guardian_agent":
            return "delay"

        return "delay"  # Default fallback

    # ------------------------------------------------------------------
    # Load plan snapshot (Req 5.2)
    # ------------------------------------------------------------------

    async def _load_plan_snapshot(
        self, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """Load the most recent active plan (loading + route) from ES.

        Returns a dict with 'loading_plan' and 'route_plan' keys,
        or None if no active plan exists.
        """
        # Query most recent loading plan
        loading_query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"status": "proposed"}},
                    ],
                },
            },
            "sort": [{"created_at": {"order": "desc"}}],
            "size": 1,
        }

        loading_plan = None
        try:
            resp = await self._es.search_documents(
                MVP_LOAD_PLANS_INDEX, loading_query, 1
            )
            hits = resp.get("hits", {}).get("hits", [])
            if hits:
                loading_plan = hits[0]["_source"]
        except Exception as e:
            logger.error(
                "ExceptionReplanningAgent: failed to query loading plans: %s", e
            )

        if not loading_plan:
            return None

        # Query most recent route plan
        route_query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"status": "proposed"}},
                    ],
                },
            },
            "sort": [{"timestamp": {"order": "desc"}}],
            "size": 1,
        }

        route_plan = None
        try:
            resp = await self._es.search_documents(
                MVP_ROUTES_INDEX, route_query, 1
            )
            hits = resp.get("hits", {}).get("hits", [])
            if hits:
                route_plan = hits[0]["_source"]
        except Exception as e:
            logger.error(
                "ExceptionReplanningAgent: failed to query route plans: %s", e
            )

        return {
            "loading_plan": loading_plan,
            "route_plan": route_plan,
        }

    # ------------------------------------------------------------------
    # Attempt replan (Req 5.3–5.6)
    # ------------------------------------------------------------------

    async def _attempt_replan(
        self,
        disruption_type: str,
        signal: RiskSignal,
        plan_snapshot: Dict[str, Any],
        tenant_id: str,
    ) -> Optional[InterventionProposal]:
        """Attempt to replan based on disruption type.

        Dispatches to type-specific handlers. If no feasible replan
        exists, escalates with a HIGH-severity RiskSignal (Req 5.6).
        """
        replan_handlers = {
            "truck_breakdown": self._handle_truck_breakdown,
            "station_outage": self._handle_station_outage,
            "demand_spike": self._handle_demand_spike,
            "delay": self._handle_delay,
        }

        handler = replan_handlers.get(disruption_type, self._handle_delay)
        result = await handler(signal, plan_snapshot)

        if result is None:
            # Step 4: No feasible replan — escalate (Req 5.6)
            await self._escalate(signal, tenant_id)

            # Persist failed replan event (Req 5.7)
            replan_event = ReplanEvent(
                original_plan_id=plan_snapshot.get("loading_plan", {}).get(
                    "plan_id", ""
                ),
                trigger_signal_id=signal.signal_id,
                replan_type=disruption_type,
                status="escalated",
                tenant_id=tenant_id,
            )
            await self._persist_replan_event(replan_event)
            return None

        diff, patched_plan_id, risk_class = result

        # Persist replan event (Req 5.7)
        replan_event = ReplanEvent(
            original_plan_id=plan_snapshot.get("loading_plan", {}).get(
                "plan_id", ""
            ),
            patched_plan_id=patched_plan_id,
            trigger_signal_id=signal.signal_id,
            replan_type=disruption_type,
            diff=diff,
            status="applied",
            tenant_id=tenant_id,
        )

        # Req 2.5.1–2.5.4 (Task 4.10): compute a structured Replan_Diff for
        # every replan so the dispatcher UI gets a typed "what changed"
        # document alongside the legacy free-form ReplanEvent.diff. The
        # structured diff is derived from a patched-route projection (what
        # the route_plan looked like before the replan vs. what it would
        # look like after applying ``diff``) rather than a real
        # RoutePlan-vs-RoutePlan comparison because the handler chain here
        # only emits the high-level change description; the solver-produced
        # ``patched`` route is computed downstream. Attaching the diff to
        # the event doc keeps the two representations consistent and makes
        # the REST fetch endpoint trivially ``event.replan_diff``.
        structured_diff = self._build_structured_replan_diff(
            plan_snapshot=plan_snapshot,
            legacy_diff=diff,
            disruption_type=disruption_type,
            signal=signal,
        )

        await self._persist_replan_event(
            replan_event, structured_diff=structured_diff
        )

        # Req 2.5.4: broadcast replan_diff_ready on /ws/fuel-planning after
        # the event has been written so dispatcher UIs can fetch the full
        # diff via GET /api/fuel/mvp/replans/{event_id}/diff. Broadcast
        # failures never block the replan path; the persisted event is the
        # source of truth.
        if structured_diff is not None:
            await self._broadcast_replan_diff_ready(
                event_id=replan_event.event_id,
                structured_diff=structured_diff,
                tenant_id=tenant_id,
                disruption_type=disruption_type,
            )

        # Build proposal (Req 5.8)
        proposal = self._build_replan_proposal(
            replan_event=replan_event,
            disruption_type=disruption_type,
            risk_class=risk_class,
            tenant_id=tenant_id,
            signal=signal,
        )

        return proposal

    # ------------------------------------------------------------------
    # Disruption handlers (Req 5.3–5.5)
    # ------------------------------------------------------------------

    async def _handle_truck_breakdown(
        self,
        signal: RiskSignal,
        plan_snapshot: Dict[str, Any],
    ) -> Optional[tuple]:
        """Handle truck breakdown with inventory-aware repair/swap decision.

        1. Query inventory for repair parts compatible with broken truck
           at the nearest depot (Req 4.1).
        2. If parts available: include repair_proposal with ETA (Req 4.2).
        3. If parts unavailable: propose truck_swap only (Req 4.3).
        4. Include parts availability in proposal context (Req 4.4).

        Returns (ReplanDiff, patched_plan_id, RiskClass) or None.
        """
        loading_plan = plan_snapshot.get("loading_plan", {})
        route_plan = plan_snapshot.get("route_plan")
        broken_truck = signal.entity_id

        # Check if the broken truck is in the current plan
        plan_truck = loading_plan.get("truck_id", "")
        if plan_truck != broken_truck:
            # Truck not in current plan — no replan needed
            return None

        # Determine asset type and depot location from context
        context = signal.context or {}
        asset_type = context.get("asset_type", "truck")
        depot_location = context.get("depot_location", "")

        # Collect remaining stops that need reassignment
        remaining_stops = []
        if route_plan:
            remaining_stops = [
                s.get("station_id", "")
                for s in route_plan.get("stops", [])
                if s.get("station_id")
            ]

        # Query repair parts availability (Req 4.1)
        tenant_id = signal.tenant_id
        repair_parts = await self._query_repair_parts(
            asset_type=asset_type,
            depot_location=depot_location,
            tenant_id=tenant_id,
        )

        # Determine if all needed parts are in stock
        parts_in_stock = (
            len(repair_parts) > 0
            and all(p.get("status") == "in_stock" for p in repair_parts)
        )

        if parts_in_stock:
            # Req 4.2: Include repair_proposal action with ETA and parts list
            diff = ReplanDiff(
                truck_swapped=None,
                stops_reordered=remaining_stops,
            )

            # Attach repair context for proposal building
            diff_context = {
                "repair_proposal": True,
                "repair_parts": repair_parts,
                "depot_location": depot_location,
                "estimated_repair_minutes": DEFAULT_REPAIR_ETA_MINUTES,
                "parts_availability": "in_stock",
                "broken_truck": broken_truck,
            }
            # Store context on the signal for downstream use
            if signal.context is None:
                signal.context = {}
            signal.context["_repair_context"] = diff_context

            return diff, None, RiskClass.MEDIUM
        else:
            # Req 4.3: Parts out of stock — propose truck_swap only
            diff = ReplanDiff(
                truck_swapped=broken_truck,
                stops_reordered=remaining_stops,
            )

            # Attach parts availability context (Req 4.4)
            parts_context = {
                "repair_proposal": False,
                "repair_parts": repair_parts,
                "depot_location": depot_location,
                "parts_availability": "out_of_stock",
                "broken_truck": broken_truck,
                "reason": "repair_parts_unavailable",
            }
            if signal.context is None:
                signal.context = {}
            signal.context["_repair_context"] = parts_context

            # Truck swaps are HIGH risk (Req 5.8)
            return diff, None, RiskClass.HIGH

    async def _handle_station_outage(
        self,
        signal: RiskSignal,
        plan_snapshot: Dict[str, Any],
    ) -> Optional[tuple]:
        """Handle station outage: remove station, reoptimize (Req 5.4).

        Returns (ReplanDiff, patched_plan_id, RiskClass) or None.
        """
        route_plan = plan_snapshot.get("route_plan")
        outage_station = signal.entity_id

        if not route_plan:
            return None

        # Check if the station is in the current route
        stops = route_plan.get("stops", [])
        station_in_route = any(
            s.get("station_id") == outage_station for s in stops
        )

        if not station_in_route:
            return None

        # Remove station from route, defer its volume
        remaining_stops = [
            s.get("station_id", "")
            for s in stops
            if s.get("station_id") != outage_station
        ]

        diff = ReplanDiff(
            stations_deferred=[outage_station],
            stops_reordered=remaining_stops,
        )

        return diff, None, RiskClass.MEDIUM

    async def _handle_demand_spike(
        self,
        signal: RiskSignal,
        plan_snapshot: Dict[str, Any],
    ) -> Optional[tuple]:
        """Handle demand spike: increase delivery quantity (Req 5.5).

        Returns (ReplanDiff, patched_plan_id, RiskClass) or None.
        """
        spike_station = signal.entity_id
        context = signal.context or {}
        additional_liters = context.get("additional_liters", 1000.0)

        loading_plan = plan_snapshot.get("loading_plan", {})
        assignments = loading_plan.get("assignments", [])

        # Check if station is in current plan
        station_in_plan = any(
            a.get("station_id") == spike_station for a in assignments
        )

        if not station_in_plan:
            return None

        diff = ReplanDiff(
            volumes_reallocated={spike_station: additional_liters},
        )

        return diff, None, RiskClass.MEDIUM

    async def _handle_delay(
        self,
        signal: RiskSignal,
        plan_snapshot: Dict[str, Any],
    ) -> Optional[tuple]:
        """Handle delay: reorder stops to minimize impact (Req 5.2).

        Returns (ReplanDiff, patched_plan_id, RiskClass) or None.
        """
        route_plan = plan_snapshot.get("route_plan")
        if not route_plan:
            return None

        stops = route_plan.get("stops", [])
        if len(stops) < 2:
            return None

        # Simple reorder: move delayed entity's stop to the end
        delayed_entity = signal.entity_id
        reordered = [
            s.get("station_id", "")
            for s in stops
            if s.get("station_id") != delayed_entity
        ]
        reordered.append(delayed_entity)

        diff = ReplanDiff(
            stops_reordered=reordered,
        )

        return diff, None, RiskClass.MEDIUM

    # ------------------------------------------------------------------
    # Inventory-aware repair parts (Req 4.1, 4.4, 4.5)
    # ------------------------------------------------------------------

    async def _query_repair_parts(
        self, asset_type: str, depot_location: str, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """Query inventory for repair parts compatible with asset type at depot.

        Searches the inventory index for items in critical repair categories
        (tires, brake_parts, engine_parts) that are compatible with the
        given asset type and located at the specified depot.

        Args:
            asset_type: The broken truck's asset type (e.g., "truck", "tanker").
            depot_location: The nearest depot location to check.
            tenant_id: Tenant scope.

        Returns:
            List of dicts with item_id, name, category, status, quantity,
            min_threshold, and location for each matching part.
            Returns empty list on query failure (fail-open).
        """
        # Build query for repair parts at the depot
        must_clauses: List[Dict[str, Any]] = [
            {"terms": {"category": REPAIR_PART_CATEGORIES}},
            {"term": {"tenant_id": tenant_id}},
        ]

        # Filter by compatible_assets if asset_type is provided
        if asset_type:
            must_clauses.append({"term": {"compatible_assets": asset_type}})

        # Filter by depot location if provided
        if depot_location:
            must_clauses.append(
                {"match": {"location": depot_location}}
            )

        query = {
            "query": {
                "bool": {
                    "must": must_clauses,
                },
            },
            "size": 100,
        }

        try:
            resp = await self._es.search_documents(
                INVENTORY_INDEX, query, 100
            )
            hits = resp.get("hits", {}).get("hits", [])

            parts: List[Dict[str, Any]] = []
            for hit in hits:
                source = hit["_source"]
                parts.append({
                    "item_id": source.get("item_id", ""),
                    "name": source.get("name", ""),
                    "category": source.get("category", ""),
                    "status": source.get("status", ""),
                    "quantity": source.get("quantity", 0),
                    "min_threshold": source.get("min_threshold", 0),
                    "location": source.get("location", ""),
                })

            return parts

        except Exception as e:
            # Fail-open: on inventory query failure, return empty list
            # so the agent falls back to truck_swap (Req 4.3)
            logger.warning(
                "ExceptionReplanningAgent: inventory query failed for "
                "repair parts (asset_type=%s, depot=%s): %s",
                asset_type,
                depot_location,
                e,
            )
            return []

    async def _consume_repair_parts(
        self, parts: List[Dict[str, Any]], tenant_id: str
    ) -> None:
        """Trigger stock consumption for used repair parts via InventoryService.

        Calls InventoryService.adjust_stock() for each part with a negative
        quantity change of 1 unit, reason 'used_for_repair'.

        Args:
            parts: List of part dicts (must contain 'item_id').
            tenant_id: Tenant scope.

        Req 4.5: When a repair proposal is accepted and executed,
        trigger parts consumption.
        """
        if not self._inventory_service:
            logger.warning(
                "ExceptionReplanningAgent: cannot consume repair parts — "
                "InventoryService not wired"
            )
            return

        from inventory.models import StockAdjustment

        for part in parts:
            item_id = part.get("item_id", "")
            if not item_id:
                continue

            try:
                adjustment = StockAdjustment(
                    quantity_change=-1,
                    reason="used_for_repair",
                    reference_id=f"repair_{item_id}",
                    notes=f"Consumed for truck repair (tenant: {tenant_id})",
                )
                await self._inventory_service.adjust_stock(
                    item_id=item_id,
                    adjustment=adjustment,
                    tenant_id=tenant_id,
                    actor_id=self.agent_id,
                )
                logger.info(
                    "ExceptionReplanningAgent: consumed repair part %s "
                    "for tenant %s",
                    item_id,
                    tenant_id,
                )
            except Exception as e:
                logger.warning(
                    "ExceptionReplanningAgent: failed to consume repair "
                    "part %s: %s",
                    item_id,
                    e,
                )

    def set_inventory_service(self, inventory_service) -> None:
        """Wire the InventoryService reference via setter.

        Allows late-binding when the service is not available at
        construction time.

        Args:
            inventory_service: An InventoryService instance.
        """
        self._inventory_service = inventory_service

    def set_fuel_planning_ws_manager(self, manager) -> None:
        """Wire the fuel-planning WebSocket manager post-construction.

        ``None`` disables the ``replan_diff_ready`` broadcasts; the agent
        continues to persist diffs to ``mvp_replan_events`` either way so
        the REST fetch-by-event endpoint keeps working.

        Args:
            manager: A :class:`FuelPlanningWSManager` instance, or None.
        """
        self._fuel_planning_ws = manager

    # ------------------------------------------------------------------
    # Escalation (Req 5.6)
    # ------------------------------------------------------------------

    async def _escalate(self, signal: RiskSignal, tenant_id: str) -> None:
        """Escalate by publishing a HIGH-severity RiskSignal (Req 5.6)."""
        escalation_signal = RiskSignal(
            source_agent=self.agent_id,
            entity_id=signal.entity_id,
            entity_type="plan_escalation",
            severity=Severity.HIGH,
            confidence=0.9,
            ttl_seconds=3600,
            tenant_id=tenant_id,
            context={
                "original_signal_id": signal.signal_id,
                "reason": "no_feasible_replan",
                "escalation_required": True,
            },
        )
        await self._signal_bus.publish(escalation_signal)

    # ------------------------------------------------------------------
    # Build replan proposal (Req 5.8)
    # ------------------------------------------------------------------

    def _build_replan_proposal(
        self,
        replan_event: ReplanEvent,
        disruption_type: str,
        risk_class: RiskClass,
        tenant_id: str,
        signal: Optional[RiskSignal] = None,
    ) -> InterventionProposal:
        """Build an InterventionProposal for a replan event.

        Routes through ConfirmationProtocol with MEDIUM risk
        (truck swaps as HIGH) per Req 5.8.

        For truck breakdowns, includes repair_proposal or truck_swap
        action based on parts availability (Req 4.2, 4.3, 4.4).

        Computes confidence_score and confidence_rationale per Req 17.1–17.3.
        When confidence_score < 0.5, overrides risk_class to HIGH.
        """
        # Check for repair context from breakdown handler (Req 4.2, 4.3)
        repair_context = None
        if signal and signal.context:
            repair_context = signal.context.get("_repair_context")

        actions = []

        if repair_context and repair_context.get("repair_proposal"):
            # Req 4.2: Include repair_proposal action with ETA and parts list
            actions.append({
                "tool_name": "repair_proposal",
                "parameters": {
                    "event_id": replan_event.event_id,
                    "original_plan_id": replan_event.original_plan_id,
                    "broken_truck": repair_context.get("broken_truck", ""),
                    "depot_location": repair_context.get("depot_location", ""),
                    "estimated_repair_minutes": repair_context.get(
                        "estimated_repair_minutes", DEFAULT_REPAIR_ETA_MINUTES
                    ),
                    "repair_parts": repair_context.get("repair_parts", []),
                    "parts_availability": repair_context.get(
                        "parts_availability", "unknown"
                    ),
                    "checked_item_ids": [
                        p.get("item_id", "")
                        for p in repair_context.get("repair_parts", [])
                    ],
                },
                "description": (
                    f"Repair proposal for truck "
                    f"{repair_context.get('broken_truck', '')} — "
                    f"ETA {repair_context.get('estimated_repair_minutes', DEFAULT_REPAIR_ETA_MINUTES)} min, "
                    f"{len(repair_context.get('repair_parts', []))} parts available"
                ),
            })
        else:
            # Default replan action (truck_swap or other)
            action_params: Dict[str, Any] = {
                "event_id": replan_event.event_id,
                "original_plan_id": replan_event.original_plan_id,
                "replan_type": disruption_type,
                "diff": replan_event.diff.model_dump(mode="json"),
            }
            # Req 4.4: Include parts availability in proposal context
            if repair_context:
                action_params["parts_availability"] = repair_context.get(
                    "parts_availability", "unknown"
                )
                action_params["depot_location"] = repair_context.get(
                    "depot_location", ""
                )
                action_params["checked_item_ids"] = [
                    p.get("item_id", "")
                    for p in repair_context.get("repair_parts", [])
                ]

            actions.append({
                "tool_name": "apply_replan",
                "parameters": action_params,
                "description": (
                    f"Replan ({disruption_type}) for plan "
                    f"{replan_event.original_plan_id}"
                ),
            })

        # Build expected KPI delta
        expected_kpi_delta: Dict[str, Any] = {
            "replan_count": 1,
            "disruption_mitigated": 1,
        }

        # Compute confidence score (Req 17.1, 17.2)
        signal_confidence = signal.confidence if signal else 0.5
        # Count affected entities from the diff
        affected_count = len(replan_event.diff.stops_reordered or [])
        affected_count += len(replan_event.diff.stations_deferred or [])
        affected_count += len(replan_event.diff.volumes_reallocated or {})
        if replan_event.diff.truck_swapped:
            affected_count += 1
        affected_count = max(1, affected_count)

        # Data freshness: seconds since the signal was emitted
        data_freshness_seconds = 0.0
        if signal:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            delta = (now - signal.timestamp).total_seconds()
            data_freshness_seconds = max(0.0, delta)

        confidence_score, confidence_rationale = compute_confidence_score(
            signal_confidence=signal_confidence,
            historical_success_rate=0.7,  # Default; future: query from OutcomeTracker
            data_freshness_seconds=data_freshness_seconds,
            affected_entity_count=affected_count,
        )

        # Req 17.3: override risk_class to HIGH when confidence < 0.5
        effective_risk_class = risk_class
        if confidence_score < 0.5:
            effective_risk_class = RiskClass.HIGH
            confidence_rationale.append(
                "risk_class overridden to HIGH due to low confidence (<0.5)"
            )

        return InterventionProposal(
            source_agent=self.agent_id,
            actions=actions,
            expected_kpi_delta=expected_kpi_delta,
            risk_class=effective_risk_class,
            confidence=signal_confidence,
            priority=2,
            tenant_id=tenant_id,
            confidence_score=confidence_score,
            confidence_rationale=confidence_rationale,
        )

    # ------------------------------------------------------------------
    # Persistence (Req 5.7)
    # ------------------------------------------------------------------

    async def _persist_replan_event(
        self,
        replan_event: ReplanEvent,
        structured_diff: Optional[StructuredReplanDiff] = None,
    ) -> None:
        """Persist a ReplanEvent to the mvp_replan_events ES index.

        When ``structured_diff`` is supplied (Req 2.5.2), the serialized
        Replan_Diff is merged into the persisted document under the
        ``replan_diff`` key. The legacy ``diff`` field (MVP pipeline
        contract) is preserved so downstream consumers that still read
        the free-form shape continue to work.
        """
        try:
            doc = replan_event.model_dump(mode="json")
            if structured_diff is not None:
                # ``model_dump(mode="json")`` on StructuredReplanDiff
                # produces ES-friendly primitives (ISO timestamps for
                # ``generated_at`` and plain dicts for nested rows) so we
                # can write it straight into the ES document.
                doc["replan_diff"] = structured_diff.model_dump(mode="json")
            await self._es.index_document(
                MVP_REPLAN_EVENTS_INDEX,
                replan_event.event_id,
                doc,
            )
        except Exception as e:
            logger.error(
                "ExceptionReplanningAgent: failed to persist replan event %s: %s",
                replan_event.event_id,
                e,
            )

    # ------------------------------------------------------------------
    # Structured Replan_Diff construction and broadcast (Req 2.5.1–2.5.4)
    # ------------------------------------------------------------------

    def _build_structured_replan_diff(
        self,
        *,
        plan_snapshot: Dict[str, Any],
        legacy_diff: ReplanDiff,
        disruption_type: str,
        signal: RiskSignal,
    ) -> Optional[StructuredReplanDiff]:
        """Derive a :class:`StructuredReplanDiff` from the current replan.

        Constructs a "before / after" route view from ``plan_snapshot`` and
        the legacy :class:`ReplanDiff` returned by the disruption handler,
        then delegates to
        :func:`Agents.support.replan_diff_models.compute_replan_diff` so the
        overlay agent and the emergency-stop insertion path (Task 4.9) emit
        identical diff shapes. When the original route cannot be located we
        return ``None`` so the caller skips the broadcast — the persisted
        legacy ``diff`` field still contains the change summary.

        Args:
            plan_snapshot: The ``{"loading_plan", "route_plan"}`` view
                loaded from ES.
            legacy_diff: The :class:`ReplanDiff` produced by the
                disruption handler (truck_breakdown / station_outage /
                demand_spike / delay).
            disruption_type: Used only to pick a sensible synthesized
                ``patched_route_id`` so the diff is self-describing.
            signal: The originating RiskSignal, consulted for the spike
                station's additional volume on demand-spike replans.

        Returns:
            A validated :class:`StructuredReplanDiff`, or ``None`` when
            the snapshot lacks a route plan.
        """
        route_plan = plan_snapshot.get("route_plan") or {}
        original_stops = route_plan.get("stops") or []
        original_route_id = route_plan.get("route_id")
        if not original_route_id or not original_stops:
            return None

        original_truck_id = route_plan.get("truck_id") or ""

        # Step 1: normalize the original stops into a shape compatible
        # with ``compute_replan_diff``. The helper already understands
        # ``station_id`` / ``drop`` / ``eta`` so we only need to copy.
        normalized_original: List[Dict[str, Any]] = []
        for idx, stop in enumerate(original_stops):
            if not isinstance(stop, dict):
                continue
            normalized_original.append(
                {
                    "stop_id": stop.get("station_id")
                    or stop.get("customer_tank_id")
                    or stop.get("stop_id")
                    or f"stop_{idx}",
                    "station_id": stop.get("station_id"),
                    "customer_tank_id": stop.get("customer_tank_id"),
                    "eta": stop.get("eta"),
                    "drop": dict(stop.get("drop") or {}),
                    "planned_gallons": stop.get("planned_gallons"),
                    "product_code": stop.get("product_code")
                    or stop.get("fuel_grade"),
                    "sequence": stop.get("sequence", idx),
                }
            )

        # Step 2: project a patched stop list from the legacy diff.
        patched_stops = self._project_patched_stops(
            normalized_original=normalized_original,
            legacy_diff=legacy_diff,
            disruption_type=disruption_type,
            signal=signal,
        )

        # Step 3: derive a deterministic patched_route_id so the diff
        # document is self-describing even when no real patched Route_Plan
        # has been persisted yet (the solver-produced patched route is
        # written later in the replan pipeline).
        patched_truck_id = legacy_diff.truck_swapped or original_truck_id
        patched_route_id = f"{original_route_id}:{disruption_type}:patched"

        original_view = {
            "route_id": original_route_id,
            "truck_id": original_truck_id,
            "stops": normalized_original,
        }
        patched_view = {
            "route_id": patched_route_id,
            "truck_id": patched_truck_id,
            "stops": patched_stops,
        }

        try:
            return compute_replan_diff(original_view, patched_view)
        except ValueError as exc:
            logger.warning(
                "ExceptionReplanningAgent: unable to build structured "
                "replan diff (disruption=%s, route=%s): %s",
                disruption_type,
                original_route_id,
                exc,
            )
            return None

    def _project_patched_stops(
        self,
        *,
        normalized_original: List[Dict[str, Any]],
        legacy_diff: ReplanDiff,
        disruption_type: str,
        signal: RiskSignal,
    ) -> List[Dict[str, Any]]:
        """Project the patched stop list the legacy diff implies.

        The disruption handlers return high-level change hints (a reorder,
        a deferral, a volume reallocation) rather than a fully-formed
        patched route. To feed :func:`compute_replan_diff` we reconstruct
        the stop-level view those hints imply:

        * ``stations_deferred`` → remove the matching stops.
        * ``stops_reordered`` → reorder the *remaining* stops by the
          ids in the list (unknown ids are ignored, known ids not in
          the list keep their relative order at the end).
        * ``volumes_reallocated`` → for demand-spike replans the signal
          carries ``additional_liters`` that the handler reallocates to
          the spike station; mirror that here so ``quantity_changes``
          fires.

        The projection only mutates a *copy* of the normalized original —
        the caller keeps the original intact.
        """
        by_id: Dict[str, Dict[str, Any]] = {}
        for stop in normalized_original:
            by_id[str(stop["stop_id"])] = dict(stop)

        deferred = set(legacy_diff.stations_deferred or [])
        # Remove deferred stops up front; they also drop out of any
        # reorder hint since that list is produced from the remaining
        # stops anyway.
        for stop_id in list(by_id.keys()):
            if stop_id in deferred:
                by_id.pop(stop_id, None)

        # Apply quantity reallocations as a delta on planned_gallons so the
        # downstream helper surfaces a QuantityChange entry for the spike
        # station. Liters are not rescaled into gallons here — the diff
        # consumer already tracks gallons in its own unit system and we
        # pass whatever unit the legacy diff uses, which is the same one
        # the stop drop dict uses (liters for MVP routes, gallons for
        # fuel-ops-hardened routes).
        for stop_id, delta in (legacy_diff.volumes_reallocated or {}).items():
            stop = by_id.get(str(stop_id))
            if stop is None:
                continue
            # Prefer ``planned_gallons`` when the route uses that field;
            # otherwise fold the delta into ``drop`` for MVP routes.
            if stop.get("planned_gallons") is not None:
                try:
                    stop["planned_gallons"] = (
                        float(stop["planned_gallons"]) + float(delta)
                    )
                except (TypeError, ValueError):
                    continue
            else:
                drop = dict(stop.get("drop") or {})
                if drop:
                    # Distribute the delta across the existing grades
                    # proportionally; for single-grade drops this is a
                    # straight add.
                    total = sum(
                        float(v) for v in drop.values() if v is not None
                    )
                    if total > 0:
                        for grade, vol in list(drop.items()):
                            try:
                                share = float(vol) / total
                                drop[grade] = float(vol) + (
                                    float(delta) * share
                                )
                            except (TypeError, ValueError):
                                continue
                    else:
                        # Empty drop dict — record the delta under an
                        # unknown grade so quantity_changes still fires.
                        drop["_spike_delta"] = float(delta)
                    stop["drop"] = drop

        # Apply reordering if the legacy diff carries an explicit list.
        ordered_ids: List[str] = [
            str(sid) for sid in (legacy_diff.stops_reordered or []) if sid
        ]
        if ordered_ids:
            seen: List[Dict[str, Any]] = []
            used: set[str] = set()
            for sid in ordered_ids:
                stop = by_id.get(sid)
                if stop is not None and sid not in used:
                    seen.append(stop)
                    used.add(sid)
            # Append any remaining unreferenced stops (defensive: the
            # handlers already produce a complete list of remaining stops).
            for sid, stop in by_id.items():
                if sid in used:
                    continue
                seen.append(stop)
                used.add(sid)
            # Re-stamp the sequence so ``_index_stops_by_id`` sees the
            # updated ordering — the helper itself indexes by enumeration,
            # but keeping ``sequence`` consistent helps downstream tools.
            for idx, stop in enumerate(seen):
                stop["sequence"] = idx
            return seen

        return list(by_id.values())

    async def _broadcast_replan_diff_ready(
        self,
        *,
        event_id: str,
        structured_diff: StructuredReplanDiff,
        tenant_id: str,
        disruption_type: str,
    ) -> None:
        """Fire ``replan_diff_ready`` on ``/ws/fuel-planning`` (Req 2.5.4).

        Skips silently when no fuel-planning WS manager is wired so
        existing bootstrap paths that run without it continue to work.
        Any exception from the WS manager is logged and swallowed — the
        persisted event remains the source of truth.
        """
        if self._fuel_planning_ws is None:
            return
        try:
            await self._fuel_planning_ws.broadcast_replan_diff_ready(
                event_id=event_id,
                diff_id=structured_diff.diff_id,
                tenant_id=tenant_id,
                summary=structured_diff.summary_counts(),
                replan_type=disruption_type,
                original_route_id=structured_diff.original_route_id,
                patched_route_id=structured_diff.patched_route_id,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "ExceptionReplanningAgent: replan_diff_ready broadcast "
                "failed for event=%s: %s",
                event_id,
                exc,
            )
