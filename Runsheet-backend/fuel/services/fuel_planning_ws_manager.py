"""
Fuel Planning WebSocket Manager.

Manages WebSocket connections for the ``/ws/fuel-planning`` channel and
broadcasts fuel-ops-hardening planning events (Capability 1 tank forecasts,
Capability 2 emergency stop insertions and replan diffs, Capability 8
sourcing recommendations).

This manager is introduced by Task 3.6 of the fuel-ops-hardening spec to
fulfil Requirement 1.6.4: ``customer_tank_forecast_ready`` must be emitted
on ``/ws/fuel-planning`` whenever a per-tank forecast completes. The manager
is deliberately scoped to the fuel planning surface — driver/execution
updates continue to use ``/ws/plan-execution`` and general agent activity
uses ``/ws/agent-activity``.

Extends :class:`websocket.base_ws_manager.BaseWSManager` so it inherits the
platform-standard connection registry, metric counters, backpressure, and
stale-client handling that every other WS manager in the codebase follows.

Design notes:

* The per-event convenience broadcasters wrap ``broadcast_event`` with a
  strict payload shape so callers in overlay agents don't have to remember
  the field list for every event type. This mirrors the pattern used by
  :class:`Agents.support.plan_execution_ws_manager.PlanExecutionWSManager`.
* ``broadcast_customer_tank_forecast_ready`` accepts the strict payload
  mandated by Req 1.6.4 (run_id, tenant_id, customer_tank_id, fuel_type,
  runout_risk_24h, model_name). Extra context is passed through the
  optional ``extra`` parameter so downstream consumers can opt-in without
  the core schema shifting.
* ``broadcast_replan_diff_ready`` emits the Task 4.10 / Req 2.5.4 event
  (event_id, diff_id, tenant_id, summary, diff_url) after the
  Exception_Replanning_Agent has persisted a structured diff to
  ``mvp_replan_events``. Optional ``replan_type``, ``original_route_id``,
  and ``patched_route_id`` fields are surfaced when available so
  dispatcher UIs can badge the event without a follow-up fetch.
* ``broadcast_sourcing_recommendation_ready`` emits the Task 7.10 /
  Req 8.5.4 event after ``GET /api/fuel/sourcing/recommendations``
  persists a ranked list to ``sourcing_recommendations`` for audit.
  The payload carries ``recommendation_id``, a compact top-pick
  summary, and the ``wait_warning_terminal_ids`` list so dispatcher
  UIs can render a sourcing banner without fetching the full record.
* Broadcasts never raise — the ``BaseWSManager.broadcast`` infrastructure
  prunes dead clients and logs send failures. Callers in the agent's
  decision cycle should still defend against exceptions (e.g. if the
  manager is ``None`` because bootstrap hasn't wired it yet).

Validates: Requirements 1.6.4, 2.5.4, 8.5.4.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import WebSocket

from websocket.base_ws_manager import BaseWSManager

logger = logging.getLogger(__name__)


class FuelPlanningWSManager(BaseWSManager):
    """WebSocket manager for the ``/ws/fuel-planning`` channel.

    Broadcasts fuel-ops-hardening planning events to dispatcher UIs. At
    the time of Task 3.6 the only wired event is
    ``customer_tank_forecast_ready``; Capability 2 (``emergency_stop_inserted``,
    ``replan_diff_ready``) and Capability 8 (``sourcing_recommendation_ready``)
    will attach their convenience helpers in later tasks.

    Args:
        max_pending_messages: Per-client backpressure threshold inherited
            from the base manager. Defaults to 100 outstanding messages,
            which matches the other managers in the platform.
    """

    #: Envelope ``type`` emitted on Req 1.6.4's customer_tank_forecast_ready.
    CUSTOMER_TANK_FORECAST_READY = "customer_tank_forecast_ready"

    #: Envelope ``type`` emitted on Req 2.4.6's emergency_stop_inserted event.
    EMERGENCY_STOP_INSERTED = "emergency_stop_inserted"

    #: Envelope ``type`` emitted on Req 2.5.4's replan_diff_ready.
    REPLAN_DIFF_READY = "replan_diff_ready"

    #: Envelope ``type`` emitted on Req 8.5.4's sourcing_recommendation_ready.
    SOURCING_RECOMMENDATION_READY = "sourcing_recommendation_ready"

    def __init__(self, max_pending_messages: int = 100) -> None:
        super().__init__(
            manager_name="fuel_planning",
            max_pending_messages=max_pending_messages,
        )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, websocket: WebSocket, tenant_id: str = "") -> None:
        """Accept a client and register it under the tenant scope."""
        await super().connect(websocket, tenant_id=tenant_id)

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a client from the registry."""
        await super().disconnect(websocket)

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def broadcast_event(self, event_type: str, data: Dict[str, Any]) -> int:
        """Broadcast a generic event envelope to every connected client.

        Matches the envelope shape used by ``AgentActivityWSManager`` and
        ``PlanExecutionWSManager`` (``{type, data, timestamp}``) so UI
        clients can reuse the same dispatcher/parser.

        Args:
            event_type: The event name (e.g. ``customer_tank_forecast_ready``).
            data: The event payload.

        Returns:
            The number of clients that successfully received the message.
        """
        message = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast(message)

    async def broadcast_customer_tank_forecast_ready(
        self,
        *,
        run_id: str,
        tenant_id: str,
        customer_tank_id: str,
        fuel_type: str,
        runout_risk_24h: float,
        model_name: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Emit the Req 1.6.4 ``customer_tank_forecast_ready`` event.

        The payload carries exactly the fields required by Requirement
        1.6.4. Additional context (weather_fallback, customer_type, etc.)
        is supported through the optional ``extra`` mapping; downstream
        UIs must continue to work when the extra fields are absent.

        Args:
            run_id: Forecast run identifier (stamped on every TankForecast).
            tenant_id: Owning tenant, re-emitted on the envelope for
                client-side multi-tenant demuxing.
            customer_tank_id: The Customer_Tank the forecast was produced
                for (Req 1.1.2).
            fuel_type: The tank's fuel-type family (propane, heating_oil,
                diesel, generator_fuel, farm_fuel, gasoline).
            runout_risk_24h: The computed probability (0.0–1.0) that the
                tank will run out within the next 24 hours.
            model_name: The Consumption_Model strategy that produced the
                per-tank gallons-per-day estimate (Req 1.5.6).
            extra: Optional auxiliary fields (e.g. ``customer_type``,
                ``weather_fallback``) to surface in the payload without
                widening the core schema.

        Returns:
            The number of clients that successfully received the event.
        """
        payload: Dict[str, Any] = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "customer_tank_id": customer_tank_id,
            "fuel_type": fuel_type,
            "runout_risk_24h": float(runout_risk_24h),
            "model_name": model_name,
        }
        if extra:
            # Preserve caller-supplied keys verbatim but never let them
            # overwrite the mandatory Req 1.6.4 fields.
            for key, value in extra.items():
                if key in payload:
                    continue
                payload[key] = value
        return await self.broadcast_event(self.CUSTOMER_TANK_FORECAST_READY, payload)


    async def broadcast_emergency_stop_inserted(
        self,
        *,
        run_id: str,
        tenant_id: str,
        route_id: str,
        diff_summary: Dict[str, Any],
        extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Emit the Req 2.4.6 ``emergency_stop_inserted`` event.

        Carries a compact diff summary (added/removed/reordered/reassigned/
        quantity/eta shift counts plus ``diff_id`` and the original and
        patched route ids) so dispatcher UIs can render a "what changed"
        banner without an immediate follow-up fetch. The full Replan_Diff
        is persisted to ``mvp_replan_events`` and retrievable via the
        ``GET /api/fuel/mvp/replans/{event_id}/diff`` endpoint (Task 4.10).

        Args:
            run_id: Pipeline run identifier associated with the route
                being patched. Empty string when the route has no
                ``run_id`` on record; the dispatcher UI tolerates this.
            tenant_id: Owning tenant, re-emitted on the envelope for
                client-side multi-tenant demuxing.
            route_id: The route id whose stop sequence changed.
            diff_summary: The compact summary payload. Expected keys:
                ``diff_id``, ``original_route_id``, ``patched_route_id``,
                plus the six counts returned by
                :meth:`Agents.support.replan_diff_models.ReplanDiff.summary_counts`.
                Callers construct this from the ReplanDiff they just
                persisted so the event and the persisted record agree.
            extra: Optional auxiliary fields (e.g. ``risk_level``,
                ``approval_id``, ``insert_index``) that downstream UIs
                may surface in a detail tooltip. Never allowed to
                overwrite the mandatory keys above.

        Returns:
            The number of clients that successfully received the event.

        Validates: Requirement 2.4.6.
        """
        payload: Dict[str, Any] = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "route_id": route_id,
            "diff_summary": dict(diff_summary),
        }
        if extra:
            for key, value in extra.items():
                if key in payload:
                    continue
                payload[key] = value
        return await self.broadcast_event(self.EMERGENCY_STOP_INSERTED, payload)


    async def broadcast_replan_diff_ready(
        self,
        *,
        event_id: str,
        diff_id: str,
        tenant_id: str,
        summary: Dict[str, int],
        replan_type: Optional[str] = None,
        original_route_id: Optional[str] = None,
        patched_route_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Emit the Req 2.5.4 ``replan_diff_ready`` event.

        Fires after the Exception_Replanning_Agent (Task 4.10) or the
        emergency-stop insertion path (Task 4.9) persist a Replan_Diff to
        ``mvp_replan_events``. The payload carries the event_id, diff_id,
        summary counts, and a link hint so dispatcher UIs can fetch the
        full diff via ``GET /api/fuel/mvp/replans/{event_id}/diff``.

        Args:
            event_id: The Replan_Event id on ``mvp_replan_events``.
            diff_id: The :class:`ReplanDiff.diff_id` persisted under the
                event's ``replan_diff`` field.
            tenant_id: Owning tenant, re-emitted on the envelope for
                client-side multi-tenant demuxing.
            summary: Counts of changes (``{added, removed, reordered,
                reassigned, quantity_changes, eta_shifts}``), typically
                sourced from :meth:`ReplanDiff.summary_counts`.
            replan_type: The disruption type that triggered the replan
                (``delay``, ``truck_breakdown``, ...). Optional; included
                when available so the FE can badge the event correctly.
            original_route_id: Route id that was being replaced. Optional.
            patched_route_id: Route id produced by the replan. Optional.
            extra: Optional auxiliary fields to surface alongside the core
                payload without widening the core schema.

        Returns:
            The number of clients that successfully received the event.
        """
        payload: Dict[str, Any] = {
            "event_id": event_id,
            "diff_id": diff_id,
            "tenant_id": tenant_id,
            "summary": dict(summary),
            # Provide a ready-to-use link to the full-diff endpoint so
            # dispatcher UIs don't have to reassemble the URL. Omitting
            # the ``/api`` prefix is intentional so the FE's API base
            # can prefix it consistently.
            "diff_url": f"/api/fuel/mvp/replans/{event_id}/diff",
        }
        if replan_type:
            payload["replan_type"] = replan_type
        if original_route_id:
            payload["original_route_id"] = original_route_id
        if patched_route_id:
            payload["patched_route_id"] = patched_route_id
        if extra:
            for key, value in extra.items():
                if key in payload:
                    continue
                payload[key] = value
        return await self.broadcast_event(self.REPLAN_DIFF_READY, payload)


    async def broadcast_sourcing_recommendation_ready(
        self,
        *,
        recommendation_id: str,
        request_id: str,
        tenant_id: str,
        product_code: str,
        volume_gallons: float,
        candidate_count: int,
        top_terminal_id: Optional[str] = None,
        top_score: Optional[float] = None,
        rack_price_fallback: bool = False,
        wait_warning_terminal_ids: Optional[list] = None,
        truck_id: Optional[str] = None,
        run_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Emit the Req 8.5.4 / Task 7.10 ``sourcing_recommendation_ready`` event.

        Fires after ``GET /api/fuel/sourcing/recommendations`` persists a
        :class:`fuel.terminal_models.SourcingRecommendation` to the
        ``sourcing_recommendations`` ES index. The payload carries the
        recommendation_id, a compact top-pick summary, and the
        wait-warning terminal-id list so dispatcher UIs can render a
        sourcing banner without fetching the full audit record.

        Args:
            recommendation_id: Persisted :attr:`SourcingRecommendation.recommendation_id`
                so the dispatcher UI can pull the full ranked list from the
                ``sourcing_recommendations`` index.
            request_id: Idempotency key stamped on the recommendation so
                retries surface the same event envelope.
            tenant_id: Owning tenant, re-emitted on the envelope for
                client-side multi-tenant demuxing.
            product_code: Canonical product_code the recommendation
                ranked (aliases are canonicalized before emission).
            volume_gallons: Load volume the request addressed.
            candidate_count: Number of ranked terminal candidates (may
                be zero when every terminal was disqualified).
            top_terminal_id: The first-ranked terminal id when present;
                ``None`` when no candidates survived disqualification.
            top_score: The first-ranked terminal's 0.0–1.0 score;
                ``None`` when no candidates survived.
            rack_price_fallback: True when the recommender fell back to
                cached rack prices (Req 8.2.5). The dispatcher UI can
                badge the event so the operator knows the price figure
                is historical rather than live.
            wait_warning_terminal_ids: Terminal ids whose rolling 2-hour
                avg_wait_minutes exceeded the tenant threshold
                (Req 8.4.5). Mirrors
                :attr:`SourcingRecommendation.wait_warning_terminal_ids`.
            truck_id / run_id: Optional traceability context surfaced in
                the dispatcher UI.
            extra: Optional auxiliary fields (never allowed to overwrite
                the core payload above).

        Returns:
            The number of clients that successfully received the event.

        Validates: Requirement 8.5.4.
        """
        payload: Dict[str, Any] = {
            "recommendation_id": recommendation_id,
            "request_id": request_id,
            "tenant_id": tenant_id,
            "product_code": product_code,
            "volume_gallons": float(volume_gallons),
            "candidate_count": int(candidate_count),
            "rack_price_fallback": bool(rack_price_fallback),
            "wait_warning_terminal_ids": list(wait_warning_terminal_ids or []),
        }
        if top_terminal_id is not None:
            payload["top_terminal_id"] = top_terminal_id
        if top_score is not None:
            payload["top_score"] = float(top_score)
        if truck_id:
            payload["truck_id"] = truck_id
        if run_id:
            payload["run_id"] = run_id
        if extra:
            for key, value in extra.items():
                if key in payload:
                    continue
                payload[key] = value
        return await self.broadcast_event(
            self.SOURCING_RECOMMENDATION_READY, payload
        )


# ---------------------------------------------------------------------------
# Singleton accessor (mirrors PlanExecutionWSManager.get_plan_execution_ws_manager)
# ---------------------------------------------------------------------------


_fuel_planning_ws_manager: Optional[FuelPlanningWSManager] = None


def get_fuel_planning_ws_manager() -> FuelPlanningWSManager:
    """Return the module-level :class:`FuelPlanningWSManager` singleton.

    Created lazily on first access so tests and small scripts that only
    need the class definition do not incur manager construction cost.
    """
    global _fuel_planning_ws_manager
    if _fuel_planning_ws_manager is None:
        _fuel_planning_ws_manager = FuelPlanningWSManager()
    return _fuel_planning_ws_manager


__all__ = [
    "FuelPlanningWSManager",
    "get_fuel_planning_ws_manager",
]
