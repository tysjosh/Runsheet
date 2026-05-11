"""
Truck Fuel Monitor — autonomous background agent for low-fuel detection.

Monitors the ``trucks`` Elasticsearch index for trucks whose
``fuel_level_pct`` has fallen below a configurable threshold. For each
flagged truck the agent publishes a RiskSignal to the SignalBus, creates
a MutationRequest through the Confirmation Protocol, and broadcasts a
``truck_fuel_low`` WebSocket event.

Default configuration:
    - poll_interval: 120 seconds
    - cooldown: 30 minutes (per truck)
    - fuel_threshold_pct: 20.0%

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8
"""

import logging
from typing import Any, Dict, List, Tuple

from Agents.autonomous.base_agent import AutonomousAgentBase
from Agents.confirmation_protocol import MutationRequest
from Agents.overlay.data_contracts import RiskSignal, Severity

logger = logging.getLogger(__name__)

# Elasticsearch index name
TRUCKS_INDEX = "trucks"


class TruckFuelMonitor(AutonomousAgentBase):
    """Monitors truck fuel levels and alerts when below threshold.

    Polls the ``trucks`` index for trucks where ``fuel_level_pct`` is
    below a configurable threshold. For each detected low-fuel truck
    the agent:

    1. Checks tenant feature flags — skips disabled tenants.
    2. Checks per-truck cooldown — skips recently alerted trucks.
    3. Derives severity from the fuel level.
    4. Publishes a ``RiskSignal`` to the SignalBus.
    5. Creates a ``MutationRequest`` via the Confirmation Protocol.
    6. Broadcasts a ``truck_fuel_low`` WebSocket event.
    7. Sets cooldown for the truck.

    Args:
        es_service: Elasticsearch service for querying indices.
        activity_log_service: Service for logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: Protocol for routing mutation requests.
        feature_flag_service: Optional service for tenant feature flags.
        signal_bus: Optional SignalBus instance for publishing RiskSignals
            to Layer 1 overlay agents.
        poll_interval: Seconds between polling cycles (default 120).
        cooldown_minutes: Minutes to suppress duplicate alerts per truck
            (default 30).
        fuel_threshold_pct: Fuel level percentage below which a truck is
            flagged (default 20.0).
    """

    def __init__(
        self,
        es_service,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        feature_flag_service=None,
        signal_bus=None,
        poll_interval: int = 120,
        cooldown_minutes: int = 30,
        fuel_threshold_pct: float = 20.0,
    ):
        super().__init__(
            agent_id="truck_fuel_monitor",
            poll_interval_seconds=poll_interval,
            cooldown_minutes=cooldown_minutes,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            feature_flag_service=feature_flag_service,
        )
        self._es = es_service
        self._signal_bus = signal_bus
        self._fuel_threshold_pct = fuel_threshold_pct

    # ------------------------------------------------------------------
    # Core monitoring cycle
    # ------------------------------------------------------------------

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one monitoring cycle.

        Queries Elasticsearch for trucks with fuel below the threshold,
        then processes each flagged truck.

        Returns:
            A ``(detections, actions)`` tuple where *detections* is a
            list of flagged truck IDs and *actions* is a list of dicts
            describing the action taken for each truck.
        """
        detections: List[str] = []
        actions: List[Dict[str, Any]] = []

        # Query for trucks with low fuel (Req 1.2)
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"range": {"fuel_level_pct": {"lt": self._fuel_threshold_pct}}},
                    ]
                }
            },
            "size": 100,
        }

        try:
            resp = await self._es.search_documents(TRUCKS_INDEX, query, 100)
        except Exception:
            # Req 1.8: log error and continue without crashing
            self.logger.exception("Failed to query trucks index")
            return detections, actions

        flagged_trucks = [h["_source"] for h in resp.get("hits", {}).get("hits", [])]

        for truck in flagged_trucks:
            truck_id = truck.get("truck_id")
            tenant_id = truck.get("tenant_id")
            fuel_level_pct = truck.get("fuel_level_pct", 0.0)
            if not truck_id or not tenant_id:
                self.logger.warning(
                    "TruckFuelMonitor: skipping truck missing truck_id or tenant_id: "
                    "truck_id=%s tenant_id=%s",
                    truck_id,
                    tenant_id,
                )
                continue

            # Respect tenant feature flags (Req 1.7)
            if self._feature_flags:
                enabled = await self._feature_flags.is_enabled(tenant_id)
                if not enabled:
                    continue

            detections.append(truck_id)

            # Respect per-truck cooldown (Req 1.6)
            if self._is_on_cooldown(truck_id):
                continue

            # Derive severity (Req 1.5)
            severity = self._derive_severity(fuel_level_pct)

            # Publish RiskSignal to SignalBus (Req 1.5)
            if self._signal_bus:
                try:
                    signal = RiskSignal(
                        source_agent=self.agent_id,
                        entity_id=truck_id,
                        entity_type="truck",
                        severity=severity,
                        confidence=0.9,
                        ttl_seconds=600,
                        tenant_id=tenant_id,
                        context={
                            "fuel_level_pct": fuel_level_pct,
                            "current_location": truck.get("current_location"),
                        },
                    )
                    await self._signal_bus.publish(signal)
                except Exception:
                    self.logger.exception("Failed to publish RiskSignal")

            # Create MutationRequest via Confirmation Protocol (Req 1.3)
            request = MutationRequest(
                tool_name="truck_fuel_alert",
                parameters={
                    "truck_id": truck_id,
                    "fuel_level_pct": fuel_level_pct,
                    "tenant_id": tenant_id,
                },
                tenant_id=tenant_id,
                agent_id=self.agent_id,
            )
            result = await self._confirmation_protocol.process_mutation(request)
            actions.append({
                "truck_id": truck_id,
                "action": "truck_fuel_alert",
                "result": result,
            })

            # Broadcast WebSocket event (Req 1.4)
            await self._ws.broadcast_event("truck_fuel_low", {
                "truck_id": truck_id,
                "fuel_level_pct": fuel_level_pct,
                "current_location": truck.get("current_location"),
                "tenant_id": tenant_id,
            })

            # Set cooldown (Req 1.6)
            self._set_cooldown(truck_id)

        return detections, actions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_severity(fuel_level_pct: float) -> Severity:
        """Derive severity from the fuel level percentage.

        Args:
            fuel_level_pct: The truck's current fuel level as a
                percentage (0.0–100.0).

        Returns:
            A ``Severity`` enum value: ``CRITICAL`` when below 10%,
            ``HIGH`` when below 20%.
        """
        if fuel_level_pct < 10.0:
            return Severity.CRITICAL
        return Severity.HIGH
