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

Default configuration:
    - decision_cycle: 30 seconds (continuous monitor)
    - cooldown: 5 minutes per entity

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8
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


class ExceptionReplanningAgent(OverlayAgentBase):
    """Patches plans when disruptions occur.

    Subscribes to disruption RiskSignals from delay_response_agent,
    sla_guardian_agent, and exception_commander. Detects disruption type,
    loads the current plan snapshot, attempts replanning, and produces
    patched plans or escalates.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service for querying indices.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
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
        result = handler(signal, plan_snapshot)

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
        await self._persist_replan_event(replan_event)

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

    def _handle_truck_breakdown(
        self,
        signal: RiskSignal,
        plan_snapshot: Dict[str, Any],
    ) -> Optional[tuple]:
        """Handle truck breakdown: attempt truck swap (Req 5.3).

        Checks if the broken truck is in the current plan. If so,
        marks the truck as swapped and captures remaining stops for
        reassignment. The actual replacement truck selection happens
        during proposal execution via the ConfirmationProtocol.

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

        # Collect remaining stops that need reassignment
        remaining_stops = []
        if route_plan:
            remaining_stops = [
                s.get("station_id", "")
                for s in route_plan.get("stops", [])
                if s.get("station_id")
            ]

        diff = ReplanDiff(
            truck_swapped=broken_truck,
            stops_reordered=remaining_stops,
        )

        # Truck swaps are HIGH risk (Req 5.8)
        return diff, None, RiskClass.HIGH

    def _handle_station_outage(
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

    def _handle_demand_spike(
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

    def _handle_delay(
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

        Computes confidence_score and confidence_rationale per Req 17.1–17.3.
        When confidence_score < 0.5, overrides risk_class to HIGH.
        """
        actions = [
            {
                "tool_name": "apply_replan",
                "parameters": {
                    "event_id": replan_event.event_id,
                    "original_plan_id": replan_event.original_plan_id,
                    "replan_type": disruption_type,
                    "diff": replan_event.diff.model_dump(mode="json"),
                },
                "description": (
                    f"Replan ({disruption_type}) for plan "
                    f"{replan_event.original_plan_id}"
                ),
            }
        ]

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
            expected_kpi_delta={
                "replan_count": 1,
                "disruption_mitigated": 1,
            },
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

    async def _persist_replan_event(self, replan_event: ReplanEvent) -> None:
        """Persist a ReplanEvent to the mvp_replan_events ES index."""
        try:
            doc = replan_event.model_dump(mode="json")
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
