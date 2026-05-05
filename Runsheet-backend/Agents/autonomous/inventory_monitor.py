"""
Inventory Monitor Agent — autonomous background agent for inventory level monitoring.

Monitors the ``inventory`` Elasticsearch index for items with status
``low_stock`` or ``out_of_stock``. For each detected item (not on cooldown)
the agent publishes a RiskSignal to the SignalBus and broadcasts an
``inventory_alert`` WebSocket event.

Default configuration:
    - poll_interval: 300 seconds (5 minutes)
    - cooldown: 60 minutes per item

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8
"""

import logging
from typing import Any, Dict, List, Tuple

from Agents.autonomous.base_agent import AutonomousAgentBase
from Agents.overlay.data_contracts import RiskSignal, Severity
from inventory.es_mappings import INVENTORY_INDEX

logger = logging.getLogger(__name__)


class InventoryMonitorAgent(AutonomousAgentBase):
    """Monitors inventory levels and publishes RiskSignals for degraded items.

    Polls the ``inventory`` index for items where ``status`` is ``low_stock``
    or ``out_of_stock``. For each detected item the agent:

    1. Checks tenant feature flags — skips disabled tenants (Req 2.7).
    2. Checks per-item cooldown — skips recently signaled items (Req 2.1).
    3. Publishes a RiskSignal to the SignalBus with severity derived from
       the item status (Req 2.3, 2.4, 2.5, 2.6).
    4. Broadcasts an ``inventory_alert`` WebSocket event (Req 2.3).

    Args:
        es_service: Elasticsearch service for querying indices.
        activity_log_service: Service for logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: Protocol for routing mutation requests.
        feature_flag_service: Optional service for tenant feature flags.
        signal_bus: Optional SignalBus instance for publishing RiskSignals.
        poll_interval: Seconds between polling cycles (default 300).
        cooldown_minutes: Minutes to suppress duplicate signals per item
            (default 60).
    """

    def __init__(
        self,
        es_service,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        feature_flag_service=None,
        signal_bus=None,
        poll_interval: int = 300,
        cooldown_minutes: int = 60,
    ):
        super().__init__(
            agent_id="inventory_monitor",
            poll_interval_seconds=poll_interval,
            cooldown_minutes=cooldown_minutes,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            feature_flag_service=feature_flag_service,
        )
        self._es = es_service
        self._signal_bus = signal_bus

    # ------------------------------------------------------------------
    # Core monitoring cycle
    # ------------------------------------------------------------------

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Execute one monitoring cycle.

        Queries Elasticsearch for inventory items with degraded status
        (``low_stock`` or ``out_of_stock``), then publishes RiskSignals
        and WebSocket alerts for each item not on cooldown.

        Returns:
            A ``(detections, actions)`` tuple where *detections* is a
            list of item IDs detected as degraded and *actions* is a
            list of dicts describing the signals published.
        """
        detections: List[str] = []
        actions: List[Dict[str, Any]] = []

        try:
            # Query for items with status low_stock or out_of_stock (Req 2.2)
            query = {
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"status": "low_stock"}},
                            {"term": {"status": "out_of_stock"}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                "size": 200,
            }

            resp = await self._es.search_documents(INVENTORY_INDEX, query, 200)
            degraded_items = [h["_source"] for h in resp["hits"]["hits"]]
        except Exception as e:
            # Req 2.8: Never crash on ES query failure
            self.logger.error(
                f"Failed to query inventory for degraded items: {e}",
                exc_info=True,
            )
            return detections, actions

        for item in degraded_items:
            item_id = item.get("item_id")
            tenant_id = item.get("tenant_id", "default")

            # Respect tenant feature flags (Req 2.7)
            if self._feature_flags:
                try:
                    enabled = await self._feature_flags.is_enabled(tenant_id)
                    if not enabled:
                        continue
                except Exception as e:
                    # Fail-open: if feature flag check fails, process the item
                    self.logger.warning(
                        f"Feature flag check failed for tenant {tenant_id}: {e}"
                    )

            detections.append(item_id)

            # Respect cooldown (Req 2.1)
            if self._is_on_cooldown(item_id):
                continue

            # Derive severity from status (Req 2.4, 2.5)
            status = item.get("status", "")
            severity = self._derive_severity(status)

            # Publish RiskSignal to SignalBus (Req 2.3, 2.6)
            if self._signal_bus:
                try:
                    signal = RiskSignal(
                        source_agent=self.agent_id,
                        entity_id=item_id,
                        entity_type="inventory_item",
                        severity=severity,
                        confidence=1.0,
                        ttl_seconds=1800,
                        tenant_id=tenant_id,
                        context={
                            "item_name": item.get("name", ""),
                            "category": item.get("category", ""),
                            "location": item.get("location", ""),
                            "current_quantity": item.get("quantity", 0),
                            "min_threshold": item.get("min_threshold", 0),
                            "compatible_assets": item.get("compatible_assets", []),
                        },
                    )
                    await self._signal_bus.publish(signal)
                except Exception as e:
                    self.logger.error(
                        f"Failed to publish RiskSignal for item {item_id}: {e}"
                    )

            # Broadcast inventory_alert WebSocket event (Req 2.3)
            try:
                await self._ws.broadcast_event("inventory_alert", {
                    "item_id": item_id,
                    "item_name": item.get("name", ""),
                    "category": item.get("category", ""),
                    "status": status,
                    "location": item.get("location", ""),
                    "current_quantity": item.get("quantity", 0),
                    "min_threshold": item.get("min_threshold", 0),
                    "severity": severity.value,
                    "tenant_id": tenant_id,
                })
            except Exception as e:
                self.logger.error(
                    f"Failed to broadcast inventory_alert for item {item_id}: {e}"
                )

            actions.append({
                "item_id": item_id,
                "action": "risk_signal_published",
                "severity": severity.value,
                "status": status,
            })

            # Set cooldown regardless of outcome (Req 2.1)
            self._set_cooldown(item_id)

        return detections, actions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_severity(status: str) -> Severity:
        """Map inventory item status to signal severity.

        Args:
            status: The inventory item status string.

        Returns:
            ``Severity.CRITICAL`` for ``out_of_stock``,
            ``Severity.HIGH`` for ``low_stock``.
        """
        if status == "out_of_stock":
            return Severity.CRITICAL
        return Severity.HIGH
