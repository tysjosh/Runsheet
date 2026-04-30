"""
Fuel Management Agent — autonomous background agent for fuel station monitoring.

Monitors the ``fuel_stations`` Elasticsearch index for stations with
``status == "critical"`` or ``days_until_empty`` below a configured threshold
(default 5 days). For each detected station the agent calculates the refill
quantity (to restore stock to 80% capacity) and priority, then creates a
refill request via the Confirmation Protocol and broadcasts a ``fuel_alert``
via WebSocket.

Default configuration:
    - poll_interval: 300 seconds (5 minutes)
    - cooldown: 120 minutes (2 hours) per station

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7
"""

import logging
from typing import Any, Dict, List, Tuple

from Agents.autonomous.base_agent import AutonomousAgentBase
from Agents.autonomous.fuel_calculations import (
    calculate_refill_priority,
    calculate_refill_quantity,
)
from Agents.confirmation_protocol import MutationRequest

logger = logging.getLogger(__name__)

# Elasticsearch index name
FUEL_STATIONS_INDEX = "fuel_stations"

# Default threshold for days_until_empty below which a station is flagged
DEFAULT_DAYS_THRESHOLD = 5


class FuelManagementAgent(AutonomousAgentBase):
    """Monitors fuel stations and creates refill requests for critical stations.

    Polls ``fuel_stations`` for stations where ``status == "critical"`` or
    ``days_until_empty`` is below a configurable threshold. For each detected
    station the agent:

    1. Checks per-station cooldown — skips recently processed stations.
    2. Calculates the refill quantity to restore stock to 80% capacity.
    3. Calculates the refill priority based on ``days_until_empty``.
    4. Creates a refill request via the Confirmation Protocol
       (``request_fuel_refill`` mutation tool).
    5. Broadcasts a ``fuel_alert`` via WebSocket with station details and
       urgency level.

    Args:
        es_service: Elasticsearch service for querying indices.
        activity_log_service: Service for logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: Protocol for routing mutation requests.
        feature_flag_service: Optional service for tenant feature flags.
        poll_interval: Seconds between polling cycles (default 300).
        cooldown_minutes: Minutes to suppress duplicate actions per station
            (default 120).
        days_threshold: Days-until-empty threshold for flagging stations
            (default 5).
    """

    def __init__(
        self,
        es_service,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        feature_flag_service=None,
        poll_interval: int = 300,
        cooldown_minutes: int = 120,
        days_threshold: float = DEFAULT_DAYS_THRESHOLD,
    ):
        super().__init__(
            agent_id="fuel_management_agent",
            poll_interval_seconds=poll_interval,
            cooldown_minutes=cooldown_minutes,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            feature_flag_service=feature_flag_service,
        )
        self._es = es_service
        self._days_threshold = days_threshold

    # ------------------------------------------------------------------
    # Core monitoring cycle
    # ------------------------------------------------------------------

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one monitoring cycle.

        Queries Elasticsearch for fuel stations that are critical or have
        low ``days_until_empty``, then creates refill requests for each.

        Returns:
            A ``(detections, actions)`` tuple where *detections* is a
            list of station IDs flagged and *actions* is a list of dicts
            describing the action taken for each station.
        """
        detections: List[str] = []
        actions: List[Dict[str, Any]] = []

        # Query for critical stations or stations with low days_until_empty (Req 4.2)
        query = {
            "query": {
                "bool": {
                    "should": [
                        {"term": {"status": "critical"}},
                        {"range": {"days_until_empty": {"lt": self._days_threshold}}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            "size": 50,
        }

        resp = await self._es.search_documents(FUEL_STATIONS_INDEX, query, 50)
        flagged_stations = [h["_source"] for h in resp["hits"]["hits"]]

        for station in flagged_stations:
            station_id = station.get("station_id")
            tenant_id = station.get("tenant_id", "default")

            detections.append(station_id)

            # Respect cooldown (Req 4.4)
            if self._is_on_cooldown(station_id):
                continue

            # Calculate refill quantity and priority (Req 4.3, 4.7)
            capacity = station.get("capacity_liters", 0)
            current_stock = station.get("current_stock_liters", 0)
            days_until_empty = station.get("days_until_empty", 0)

            refill_quantity = calculate_refill_quantity(capacity, current_stock)
            priority = calculate_refill_priority(days_until_empty)

            # Skip if no refill needed (station already above 80%)
            if refill_quantity <= 0:
                continue

            # Create refill request via Confirmation Protocol (Req 4.3)
            request = MutationRequest(
                tool_name="request_fuel_refill",
                parameters={
                    "station_id": station_id,
                    "quantity_liters": refill_quantity,
                    "priority": priority.value,
                },
                tenant_id=tenant_id,
                agent_id=self.agent_id,
            )
            result = await self._confirmation_protocol.process_mutation(request)

            # Broadcast fuel_alert via WebSocket (Req 4.5)
            await self._ws.broadcast_event("fuel_alert", {
                "station_id": station_id,
                "station_name": station.get("name", "Unknown"),
                "fuel_type": station.get("fuel_type", "N/A"),
                "current_stock_liters": current_stock,
                "capacity_liters": capacity,
                "days_until_empty": days_until_empty,
                "refill_quantity": refill_quantity,
                "priority": priority.value,
                "status": station.get("status", "unknown"),
                "tenant_id": tenant_id,
            })

            actions.append({
                "station_id": station_id,
                "action": "refill_request",
                "quantity_liters": refill_quantity,
                "priority": priority.value,
                "result": result,
            })

            # Set cooldown regardless of outcome (Req 4.4)
            self._set_cooldown(station_id)

        return detections, actions
