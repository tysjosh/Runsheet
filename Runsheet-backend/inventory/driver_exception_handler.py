"""
Driver Exception Handler.

Handles inventory lookup and restock request creation for driver-reported
exceptions. When a driver reports an exception (flat tire, brake failure,
engine issue, electrical fault), this handler:

1. Maps the exception category to an inventory category.
2. Queries compatible parts at the nearest depot.
3. If parts are available: returns part info (item_id, name, quantity, depot).
4. If parts are unavailable: creates a Restock_Request in ES with priority
   ``urgent`` and publishes a RiskSignal with severity ``high``.

Follows fail-open design: on any inventory query failure, the exception is
processed normally and the failure is logged without blocking the workflow.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from Agents.overlay.data_contracts import RiskSignal, Severity
from inventory.es_mappings import INVENTORY_INDEX, RESTOCK_REQUESTS_INDEX

logger = logging.getLogger(__name__)

# Requirement 6.6: Map driver exception categories to inventory categories
EXCEPTION_TO_INVENTORY_CATEGORY: Dict[str, str] = {
    "flat_tire": "tires",
    "brake_failure": "brake_parts",
    "engine_issue": "engine_parts",
    "electrical_fault": "electrical",
}


class DriverExceptionHandler:
    """Handles inventory lookup and restock request creation for driver exceptions.

    When a driver reports an exception matching an inventory category, this
    handler queries for compatible parts at the nearest depot. If parts are
    available, it returns their details. If unavailable, it creates a
    Restock_Request and publishes a RiskSignal.

    Args:
        es_service: An ElasticsearchService instance for querying inventory.
        inventory_service: An InventoryService instance (used for future
            stock operations if needed).
        signal_bus: Optional SignalBus instance for publishing RiskSignals.

    Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6
    """

    def __init__(self, es_service, inventory_service=None, signal_bus=None):
        self._es = es_service
        self._inventory = inventory_service
        self._signal_bus = signal_bus

    async def handle_exception(
        self,
        exception_category: str,
        asset_type: str,
        driver_location: Dict[str, float],
        tenant_id: str,
    ) -> Dict[str, Any]:
        """Process a driver exception with inventory awareness.

        1. Map exception category to inventory category.
        2. Query compatible parts at nearest depot.
        3. If available: return part info.
        4. If unavailable: create Restock_Request, publish RiskSignal.

        Args:
            exception_category: The driver-reported exception category
                (e.g., "flat_tire", "brake_failure").
            asset_type: The asset type for compatibility matching.
            driver_location: Dict with lat/lng of the driver's location.
            tenant_id: Tenant scope.

        Returns:
            Dict with keys:
                - ``inventory_category``: The mapped inventory category.
                - ``parts_available``: bool indicating if parts were found.
                - ``parts``: List of available part dicts (if available).
                - ``restock_request_id``: ID of created restock request
                  (if parts unavailable).
        """
        # Step 1: Map exception category to inventory category (Req 6.6)
        inventory_category = EXCEPTION_TO_INVENTORY_CATEGORY.get(
            exception_category
        )

        if inventory_category is None:
            logger.warning(
                "DriverExceptionHandler: unknown exception category '%s' — "
                "processing without inventory lookup.",
                exception_category,
            )
            return {
                "inventory_category": None,
                "parts_available": False,
                "parts": [],
                "restock_request_id": None,
            }

        # Step 2: Query compatible parts at nearest depot (Req 6.1)
        try:
            parts = await self._find_nearest_depot_parts(
                inventory_category, asset_type, tenant_id
            )
        except Exception as e:
            # Req 6.5: Fail gracefully — process exception normally, log failure
            logger.warning(
                "DriverExceptionHandler: inventory query failed for "
                "exception_category=%s, asset_type=%s, tenant_id=%s — "
                "processing exception without inventory enrichment. Error: %s",
                exception_category,
                asset_type,
                tenant_id,
                e,
            )
            return {
                "inventory_category": inventory_category,
                "parts_available": False,
                "parts": [],
                "restock_request_id": None,
            }

        # Step 3: Check availability (Req 6.2)
        available_parts = [
            p for p in parts if p.get("status") == "in_stock"
        ]

        if available_parts:
            # Req 6.2: Return part info
            return {
                "inventory_category": inventory_category,
                "parts_available": True,
                "parts": [
                    {
                        "item_id": p.get("item_id", ""),
                        "name": p.get("name", ""),
                        "quantity": p.get("quantity", 0),
                        "depot_location": p.get("location", ""),
                    }
                    for p in available_parts
                ],
                "restock_request_id": None,
            }

        # Step 4: Parts unavailable — create Restock_Request (Req 6.3)
        # Use min_threshold from the first part found (or default to 1)
        min_threshold = 1
        if parts:
            min_threshold = parts[0].get("min_threshold", 1) or 1

        restock_request_id = None
        try:
            restock_request_id = await self._create_restock_request(
                category=inventory_category,
                asset_type=asset_type,
                min_threshold=min_threshold,
                depot_location=driver_location,
                tenant_id=tenant_id,
            )
        except Exception as e:
            logger.error(
                "DriverExceptionHandler: failed to create restock request "
                "for category=%s, tenant_id=%s. Error: %s",
                inventory_category,
                tenant_id,
                e,
            )

        # Publish RiskSignal on restock creation (Req 6.4)
        if restock_request_id and self._signal_bus:
            try:
                signal = RiskSignal(
                    source_agent="driver_exception_handler",
                    entity_id=restock_request_id,
                    entity_type="restock_request",
                    severity=Severity.HIGH,
                    confidence=1.0,
                    ttl_seconds=3600,
                    tenant_id=tenant_id,
                    context={
                        "item_category": inventory_category,
                        "compatible_asset_type": asset_type,
                        "requested_quantity": min_threshold,
                        "priority": "urgent",
                        "driver_location": driver_location,
                    },
                )
                await self._signal_bus.publish(signal)
            except Exception as e:
                logger.error(
                    "DriverExceptionHandler: failed to publish RiskSignal "
                    "for restock request %s. Error: %s",
                    restock_request_id,
                    e,
                )

        return {
            "inventory_category": inventory_category,
            "parts_available": False,
            "parts": [],
            "restock_request_id": restock_request_id,
        }

    async def _find_nearest_depot_parts(
        self,
        inventory_category: str,
        asset_type: str,
        tenant_id: str,
    ) -> List[Dict[str, Any]]:
        """Query inventory for compatible parts across depots.

        Searches the inventory index for items that:
        - Belong to the given tenant
        - Match the inventory category
        - Have the asset_type in their compatible_assets list

        Args:
            inventory_category: The inventory category to search for.
            asset_type: The asset type for compatibility matching.
            tenant_id: Tenant scope.

        Returns:
            List of part dicts from ES hits.

        Raises:
            Exception: If the ES query fails (caller handles fail-open).
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"category": inventory_category}},
                        {"term": {"compatible_assets": asset_type}},
                    ]
                }
            },
            "size": 50,
        }

        response = await self._es.search_documents(
            INVENTORY_INDEX, query, size=50
        )

        parts = []
        for hit in response["hits"]["hits"]:
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

    async def _create_restock_request(
        self,
        category: str,
        asset_type: str,
        min_threshold: int,
        depot_location: Any,
        tenant_id: str,
    ) -> str:
        """Create a restock request record in ES.

        Creates a document in the restock_requests index with priority
        ``urgent`` and requested_quantity equal to min_threshold.

        Args:
            category: The inventory category needing restock.
            asset_type: The compatible asset type.
            min_threshold: The requested quantity (equal to min_threshold).
            depot_location: The depot location (dict or string).
            tenant_id: Tenant scope.

        Returns:
            The generated request_id.
        """
        request_id = f"RST_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()

        # Convert depot_location to string if it's a dict (lat/lng)
        if isinstance(depot_location, dict):
            depot_location_str = (
                f"lat:{depot_location.get('lat', 0)},"
                f"lng:{depot_location.get('lng', 0)}"
            )
        else:
            depot_location_str = str(depot_location)

        doc = {
            "request_id": request_id,
            "item_category": category,
            "compatible_asset_type": asset_type,
            "requested_quantity": min_threshold,
            "priority": "urgent",
            "status": "pending",
            "requested_by": "driver_exception_handler",
            "depot_location": depot_location_str,
            "tenant_id": tenant_id,
            "created_at": now,
        }

        await self._es.index_document(
            RESTOCK_REQUESTS_INDEX, request_id, doc
        )

        logger.info(
            "DriverExceptionHandler: created restock request %s for "
            "category=%s, asset_type=%s, tenant_id=%s",
            request_id,
            category,
            asset_type,
            tenant_id,
        )

        return request_id
