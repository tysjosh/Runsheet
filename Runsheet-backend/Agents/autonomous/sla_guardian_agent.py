"""
SLA Guardian Agent — autonomous background agent for SLA breach prevention.

Monitors the ``shipments_current`` Elasticsearch index for in-transit
shipments approaching their SLA deadline (within a configurable threshold
of ``estimated_delivery``, default 30 minutes). For each at-risk shipment
the agent evaluates the assigned rider's workload and either proposes a
rider reassignment (when the rider has more than a configurable number of
active shipments, default 3) or escalates breached shipments by updating
priority to "critical" and broadcasting an ``sla_breach`` event via
WebSocket.

Default configuration:
    - poll_interval: 120 seconds (2 minutes)
    - cooldown: 10 minutes per shipment

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from Agents.autonomous.base_agent import AutonomousAgentBase
from Agents.confirmation_protocol import MutationRequest

logger = logging.getLogger(__name__)

# Elasticsearch index name
SHIPMENTS_CURRENT_INDEX = "shipments_current"

# Default SLA breach threshold in minutes
DEFAULT_SLA_THRESHOLD_MINUTES = 30

# Default maximum active shipments per rider before reassignment is proposed
DEFAULT_MAX_RIDER_SHIPMENTS = 3


class SLAGuardianAgent(AutonomousAgentBase):
    """Monitors shipments approaching SLA breach and takes corrective action.

    Polls ``shipments_current`` for shipments where ``status == "in_transit"``
    and ``estimated_delivery`` is within a configurable threshold of the
    current time. For each detected shipment the agent:

    1. Checks per-shipment cooldown — skips recently processed shipments.
    2. Evaluates the assigned rider's current workload (active shipment count).
    3. If the rider has more than ``max_rider_shipments`` active shipments,
       proposes a rider reassignment via the Confirmation Protocol.
    4. Escalates breached shipments by updating priority to "critical" via
       the Confirmation Protocol and broadcasting an ``sla_breach`` event
       via WebSocket.

    Args:
        es_service: Elasticsearch service for querying indices.
        activity_log_service: Service for logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: Protocol for routing mutation requests.
        feature_flag_service: Optional service for tenant feature flags.
        poll_interval: Seconds between polling cycles (default 120).
        cooldown_minutes: Minutes to suppress duplicate actions per shipment
            (default 10).
        sla_threshold_minutes: Minutes before estimated_delivery to flag a
            shipment as approaching SLA breach (default 30).
        max_rider_shipments: Maximum active shipments a rider can have before
            reassignment is proposed (default 3).
    """

    def __init__(
        self,
        es_service,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        feature_flag_service=None,
        poll_interval: int = 120,
        cooldown_minutes: int = 10,
        sla_threshold_minutes: int = DEFAULT_SLA_THRESHOLD_MINUTES,
        max_rider_shipments: int = DEFAULT_MAX_RIDER_SHIPMENTS,
    ):
        super().__init__(
            agent_id="sla_guardian_agent",
            poll_interval_seconds=poll_interval,
            cooldown_minutes=cooldown_minutes,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            feature_flag_service=feature_flag_service,
        )
        self._es = es_service
        self._sla_threshold_minutes = sla_threshold_minutes
        self._max_rider_shipments = max_rider_shipments

    # ------------------------------------------------------------------
    # Core monitoring cycle
    # ------------------------------------------------------------------

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one monitoring cycle.

        Queries Elasticsearch for in-transit shipments approaching their
        SLA deadline, evaluates rider workload, and takes corrective
        action for each.

        Returns:
            A ``(detections, actions)`` tuple where *detections* is a
            list of shipment IDs flagged and *actions* is a list of dicts
            describing the action taken for each shipment.
        """
        detections: List[str] = []
        actions: List[Dict[str, Any]] = []

        now = datetime.now(timezone.utc)
        threshold_time = now + timedelta(minutes=self._sla_threshold_minutes)

        # Query for in_transit shipments approaching SLA breach (Req 5.2)
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"status": "in_transit"}},
                        {
                            "range": {
                                "estimated_delivery": {
                                    "lte": threshold_time.isoformat(),
                                }
                            }
                        },
                    ]
                }
            },
            "size": 50,
        }

        resp = await self._es.search_documents(
            SHIPMENTS_CURRENT_INDEX, query, 50
        )
        at_risk_shipments = [h["_source"] for h in resp["hits"]["hits"]]

        for shipment in at_risk_shipments:
            shipment_id = shipment.get("shipment_id")
            tenant_id = shipment.get("tenant_id", "default")
            rider_id = shipment.get("rider_id")

            detections.append(shipment_id)

            # Respect cooldown (Req 5.7)
            if self._is_on_cooldown(shipment_id):
                continue

            # Evaluate rider workload (Req 5.3)
            rider_shipment_count = await self._get_rider_active_shipment_count(
                rider_id, tenant_id
            )

            # Propose reassignment if rider is overloaded (Req 5.4)
            if rider_id and rider_shipment_count > self._max_rider_shipments:
                less_loaded_rider = await self._find_less_loaded_rider(
                    rider_id, tenant_id
                )
                if less_loaded_rider:
                    request = MutationRequest(
                        tool_name="reassign_rider",
                        parameters={
                            "shipment_id": shipment_id,
                            "new_rider_id": less_loaded_rider["rider_id"],
                            "reason": (
                                f"SLA breach risk: rider {rider_id} has "
                                f"{rider_shipment_count} active shipments "
                                f"(threshold: {self._max_rider_shipments})"
                            ),
                        },
                        tenant_id=tenant_id,
                        agent_id=self.agent_id,
                    )
                    result = await self._confirmation_protocol.process_mutation(
                        request
                    )
                    actions.append({
                        "shipment_id": shipment_id,
                        "action": "reassignment_proposed",
                        "new_rider_id": less_loaded_rider["rider_id"],
                        "result": result,
                    })

            # Escalate breached shipments (Req 5.5)
            estimated_delivery = shipment.get("estimated_delivery")
            if self._is_breached(estimated_delivery, now):
                escalation_request = MutationRequest(
                    tool_name="escalate_shipment",
                    parameters={
                        "shipment_id": shipment_id,
                        "priority": "critical",
                        "reason": "SLA breach: estimated delivery time exceeded",
                    },
                    tenant_id=tenant_id,
                    agent_id=self.agent_id,
                )
                escalation_result = (
                    await self._confirmation_protocol.process_mutation(
                        escalation_request
                    )
                )

                # Broadcast sla_breach event via WebSocket (Req 5.5)
                await self._ws.broadcast_event("sla_breach", {
                    "shipment_id": shipment_id,
                    "rider_id": rider_id,
                    "estimated_delivery": estimated_delivery,
                    "priority": "critical",
                    "tenant_id": tenant_id,
                })

                actions.append({
                    "shipment_id": shipment_id,
                    "action": "escalation",
                    "priority": "critical",
                    "result": escalation_result,
                })

            # Set cooldown regardless of outcome (Req 5.7)
            self._set_cooldown(shipment_id)

        return detections, actions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_rider_active_shipment_count(
        self, rider_id: Optional[str], tenant_id: str
    ) -> int:
        """Count the number of active (in_transit) shipments for a rider.

        Args:
            rider_id: The rider's identifier. Returns 0 if ``None``.
            tenant_id: The tenant scope.

        Returns:
            The number of active shipments assigned to the rider.
        """
        if not rider_id:
            return 0

        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"rider_id": rider_id}},
                        {"term": {"status": "in_transit"}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 0,
        }
        resp = await self._es.search_documents(
            SHIPMENTS_CURRENT_INDEX, query, 0
        )
        return resp["hits"]["total"]["value"]

    async def _find_less_loaded_rider(
        self, current_rider_id: str, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        """Find a rider with fewer active shipments than the current rider.

        Queries for riders in the same tenant who have fewer active
        shipments, excluding the current rider.

        Args:
            current_rider_id: The rider to exclude from results.
            tenant_id: The tenant scope.

        Returns:
            A rider document dict, or ``None`` if no less-loaded rider
            is found.
        """
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"status": "active"}},
                    ],
                    "must_not": [
                        {"term": {"rider_id": current_rider_id}},
                    ],
                }
            },
            "sort": [{"active_shipment_count": {"order": "asc"}}],
            "size": 1,
        }
        resp = await self._es.search_documents("riders", query, 1)
        hits = [h["_source"] for h in resp["hits"]["hits"]]
        return hits[0] if hits else None

    @staticmethod
    def _is_breached(estimated_delivery: Optional[str], now: datetime) -> bool:
        """Determine whether a shipment has breached its SLA.

        A shipment is considered breached if its ``estimated_delivery``
        time is in the past relative to *now*.

        Args:
            estimated_delivery: ISO-format delivery timestamp string.
            now: The current UTC datetime.

        Returns:
            ``True`` if the shipment has breached its SLA.
        """
        if not estimated_delivery:
            return False
        try:
            delivery_dt = datetime.fromisoformat(
                estimated_delivery.replace("Z", "+00:00")
            )
            return delivery_dt <= now
        except (ValueError, AttributeError):
            return False
