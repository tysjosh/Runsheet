"""
Core inventory service handling item CRUD, stock adjustments,
low-stock alerting, and summary aggregation.

Provides the business logic layer between the API endpoints and
Elasticsearch persistence for the Fleet Inventory & Maintenance
Supplies module.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from errors.exceptions import resource_not_found, validation_error
from inventory.es_mappings import INVENTORY_EVENTS_INDEX, INVENTORY_INDEX
from inventory.models import (
    CreateInventoryItem,
    InventoryCategory,
    InventoryItem,
    InventoryStatus,
    InventorySummary,
    StockAdjustment,
    StockAdjustmentResult,
    UpdateInventoryItem,
)
from services.elasticsearch_service import ElasticsearchService
from fuel.services.fuel_product_catalog import canonicalize_or_warn

logger = logging.getLogger(__name__)


def _canonicalize_compatible_assets(
    compatible_assets: Optional[List[str]],
) -> Optional[List[str]]:
    """Best-effort canonicalize ``compatible_assets`` fuel-grade entries.

    ``compatible_assets`` is a mixed-use field: for ``fuel_equipment``
    inventory items it often carries fuel-grade aliases (e.g. ``AGO`` /
    ``PMS``) identifying which products a hose or nozzle can carry, while
    for other categories it carries asset types like ``heavy_truck`` or
    ``boat`` that have no fuel meaning. Req 6.1.4 asks us to normalize
    fuel-grade values on write; asset-type values must pass through
    untouched so downstream equipment lookups keep working.

    ``canonicalize_or_warn`` does exactly this: when the string resolves
    to a catalog entry (e.g., ``AGO`` -> ``DIESEL_2``) the canonical code
    is returned, otherwise the original string is preserved and a
    warning is logged. None and empty inputs are passed through.
    """
    if compatible_assets is None:
        return None
    return [
        canonicalize_or_warn(
            value,
            context="inventory.compatible_assets",
            logger_=logger,
        )
        for value in compatible_assets
    ]


class InventoryService:
    """Manages inventory item state, stock adjustments, and alerting."""

    #: Cap on items scanned to total inventory value in :meth:`get_summary`. The
    #: largest index in the cluster holds 988 documents, so this cannot bind on
    #: today's data; past it the total is understated and a warning says so, rather
    #: than the number quietly shrinking.
    MAX_SUMMARY_ITEMS: int = 10_000

    def __init__(self, es_service: ElasticsearchService, ws_manager=None):
        self._es = es_service
        self._ws_manager = ws_manager

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_item_id() -> str:
        """Generate a unique item ID in INV_{hex8} format."""
        return f"INV_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _derive_status(quantity: int, min_threshold: int) -> InventoryStatus:
        """Derive inventory status from quantity and threshold."""
        if quantity <= 0:
            return InventoryStatus.OUT_OF_STOCK
        if quantity <= min_threshold:
            return InventoryStatus.LOW_STOCK
        return InventoryStatus.IN_STOCK

    # ------------------------------------------------------------------
    # List items (paginated with filters)
    # ------------------------------------------------------------------

    async def list_items(
        self,
        tenant_id: str,
        category: Optional[str] = None,
        status: Optional[str] = None,
        location: Optional[str] = None,
        page: int = 1,
        size: int = 50,
    ) -> Dict[str, Any]:
        """
        List inventory items with optional filtering and pagination.

        Returns a dict with items list, total count, page, and size.
        """
        filters: List[dict] = [{"term": {"tenant_id": tenant_id}}]

        if category:
            filters.append({"term": {"category": category}})
        if status:
            filters.append({"term": {"status": status}})
        if location:
            filters.append(
                {"match": {"location": {"query": location, "fuzziness": "AUTO"}}}
            )

        from_offset = (page - 1) * size
        query: dict = {
            "query": {"bool": {"must": filters}},
            "from": from_offset,
            "size": size,
            "sort": [{"updated_at": {"order": "desc", "unmapped_type": "date"}}],
        }

        response = await self._es.search_documents(
            INVENTORY_INDEX, query, size=size
        )

        total = response["hits"]["total"]["value"]
        items = [
            InventoryItem(**hit["_source"]) for hit in response["hits"]["hits"]
        ]

        return {"items": items, "total": total, "page": page, "size": size}

    # ------------------------------------------------------------------
    # Get single item
    # ------------------------------------------------------------------

    async def get_item(self, item_id: str, tenant_id: str) -> InventoryItem:
        """Fetch a single inventory item by ID, scoped to tenant."""
        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"item_id": item_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }
        response = await self._es.search_documents(
            INVENTORY_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"Inventory item '{item_id}' not found",
                details={"item_id": item_id},
            )

        return InventoryItem(**hits[0]["_source"])

    # ------------------------------------------------------------------
    # Create item
    # ------------------------------------------------------------------

    async def create_item(
        self, data: CreateInventoryItem, tenant_id: str
    ) -> InventoryItem:
        """Register a new inventory item."""
        item_id = self._generate_item_id()
        status = self._derive_status(data.quantity, data.min_threshold)
        now = datetime.now(timezone.utc).isoformat()

        doc = {
            "item_id": item_id,
            "name": data.name,
            "category": data.category.value,
            "quantity": data.quantity,
            "unit": data.unit,
            "min_threshold": data.min_threshold,
            "max_capacity": data.max_capacity,
            "location": data.location,
            "status": status.value,
            "unit_cost": data.unit_cost,
            "supplier": data.supplier,
            "compatible_assets": _canonicalize_compatible_assets(
                data.compatible_assets
            ),
            "last_restocked": now if data.quantity > 0 else None,
            "tenant_id": tenant_id,
        }

        await self._es.index_document(INVENTORY_INDEX, item_id, doc)

        return InventoryItem(**doc)

    # ------------------------------------------------------------------
    # Update item
    # ------------------------------------------------------------------

    async def update_item(
        self, item_id: str, data: UpdateInventoryItem, tenant_id: str
    ) -> InventoryItem:
        """Partially update an inventory item."""
        # Verify item exists and belongs to tenant
        existing = await self.get_item(item_id, tenant_id)

        # Build partial update from non-None fields
        update_fields: Dict[str, Any] = {}
        for field, value in data.model_dump(exclude_unset=True).items():
            if value is not None:
                # Convert enums to their string value
                if isinstance(value, InventoryCategory):
                    value = value.value
                # Canonicalize fuel-grade entries in compatible_assets on
                # update so the persisted list stays consistent with the
                # catalog (Req 6.1.4). Non-fuel values are preserved.
                if field == "compatible_assets":
                    value = _canonicalize_compatible_assets(value)
                update_fields[field] = value

        if not update_fields:
            return existing

        # If quantity or min_threshold changed, re-derive status
        new_quantity = update_fields.get("quantity", existing.quantity)
        new_threshold = update_fields.get("min_threshold", existing.min_threshold)

        # Only auto-derive status if it's not currently ON_ORDER
        if existing.status != InventoryStatus.ON_ORDER:
            new_status = self._derive_status(new_quantity, new_threshold)
            update_fields["status"] = new_status.value

        await self._es.update_document(INVENTORY_INDEX, item_id, update_fields)

        # Fetch and return the updated item
        return await self.get_item(item_id, tenant_id)

    # ------------------------------------------------------------------
    # Delete item
    # ------------------------------------------------------------------

    async def delete_item(self, item_id: str, tenant_id: str) -> bool:
        """Delete an inventory item by ID, scoped to tenant.

        Returns True if the item was deleted, raises if not found.
        """
        # Verify item exists and belongs to tenant (raises 404 if not)
        await self.get_item(item_id, tenant_id)

        # Find the ES document _id for this item
        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"item_id": item_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }
        response = await self._es.search_documents(INVENTORY_INDEX, query, size=1)
        hits = response["hits"]["hits"]
        if not hits:
            return False

        doc_id = hits[0]["_id"]
        await self._es.delete_document(INVENTORY_INDEX, doc_id)
        return True

    # ------------------------------------------------------------------
    # Stock adjustment
    # ------------------------------------------------------------------

    async def adjust_stock(
        self,
        item_id: str,
        adjustment: StockAdjustment,
        tenant_id: str,
        actor_id: str,
    ) -> StockAdjustmentResult:
        """
        Record a stock movement and update item quantity/status.

        Steps:
        1. Fetch the item
        2. Apply quantity change
        3. Validate new quantity >= 0
        4. Auto-derive status
        5. Update the item document
        6. Append a stock movement event to inventory_events
        7. Broadcast WebSocket alert if status changed to low/out_of_stock
        """
        existing = await self.get_item(item_id, tenant_id)

        previous_quantity = existing.quantity
        previous_status = existing.status
        new_quantity = previous_quantity + adjustment.quantity_change

        # Validate: quantity cannot go below zero
        if new_quantity < 0:
            raise validation_error(
                "Stock adjustment would result in negative quantity",
                details={
                    "item_id": item_id,
                    "current_quantity": previous_quantity,
                    "adjustment": adjustment.quantity_change,
                    "resulting_quantity": new_quantity,
                },
            )

        # Derive new status
        new_status = self._derive_status(new_quantity, existing.min_threshold)

        # Build update
        now = datetime.now(timezone.utc).isoformat()
        update_fields: Dict[str, Any] = {
            "quantity": new_quantity,
            "status": new_status.value,
        }
        # If restocking, update last_restocked
        if adjustment.quantity_change > 0:
            update_fields["last_restocked"] = now

        await self._es.update_document(INVENTORY_INDEX, item_id, update_fields)

        # Record the stock movement event
        event_id = f"EVT_{uuid.uuid4().hex[:8]}"
        event_doc = {
            "event_id": event_id,
            "item_id": item_id,
            "quantity_change": adjustment.quantity_change,
            "quantity_before": previous_quantity,
            "quantity_after": new_quantity,
            "reason": adjustment.reason,
            "reference_id": adjustment.reference_id,
            "notes": adjustment.notes,
            "actor_id": actor_id,
            "status_before": previous_status.value,
            "status_after": new_status.value,
            "tenant_id": tenant_id,
            "event_timestamp": now,
        }

        await self._es.index_document(
            INVENTORY_EVENTS_INDEX, event_id, event_doc
        )

        # Broadcast WebSocket alert if status degraded
        if new_status in (
            InventoryStatus.LOW_STOCK,
            InventoryStatus.OUT_OF_STOCK,
        ) and new_status != previous_status:
            await self._broadcast_stock_alert(
                item_id=item_id,
                name=existing.name,
                category=existing.category.value if isinstance(existing.category, InventoryCategory) else existing.category,
                location=existing.location,
                new_status=new_status.value,
                quantity=new_quantity,
                min_threshold=existing.min_threshold,
                tenant_id=tenant_id,
            )

        return StockAdjustmentResult(
            item_id=item_id,
            previous_quantity=previous_quantity,
            new_quantity=new_quantity,
            previous_status=previous_status,
            new_status=new_status,
            event_id=event_id,
        )

    # ------------------------------------------------------------------
    # Low-stock alerts
    # ------------------------------------------------------------------

    async def get_low_stock_alerts(self, tenant_id: str) -> List[InventoryItem]:
        """Return items that are below their min_threshold or out of stock."""
        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {
                            "terms": {
                                "status": [
                                    InventoryStatus.LOW_STOCK.value,
                                    InventoryStatus.OUT_OF_STOCK.value,
                                ]
                            }
                        },
                    ]
                }
            },
            "size": 200,
            "sort": [{"quantity": {"order": "asc"}}],
        }

        response = await self._es.search_documents(
            INVENTORY_INDEX, query, size=200
        )

        return [
            InventoryItem(**hit["_source"]) for hit in response["hits"]["hits"]
        ]

    # ------------------------------------------------------------------
    # Summary / aggregation
    # ------------------------------------------------------------------

    async def get_summary(self, tenant_id: str) -> InventorySummary:
        """Return aggregated inventory counts and total value."""
        # ``total_value`` was a script-valued ``sum`` multiplying ``quantity`` by
        # ``unit_cost`` in painless. The Postgres document store refuses
        # script-valued metrics — emulating a painless expression means guessing at
        # a language's semantics — so it raised, and unlike the notification metrics
        # this method has no ``except``, meaning the inventory summary endpoint
        # returned a 500. A product of two stored fields does not need a script; it
        # is computed below over the same documents.
        query: dict = {
            "query": {"term": {"tenant_id": tenant_id}},
            "size": self.MAX_SUMMARY_ITEMS,
            "_source": ["quantity", "unit_cost"],
            "aggs": {
                "status_counts": {"terms": {"field": "status", "size": 10}},
                "category_counts": {"terms": {"field": "category", "size": 20}},
            },
        }

        response = await self._es.search_documents(
            INVENTORY_INDEX, query, size=self.MAX_SUMMARY_ITEMS
        )

        total_items = response["hits"]["total"]["value"]
        aggs = response.get("aggregations", {})

        # Parse status counts
        status_buckets = {
            b["key"]: b["doc_count"]
            for b in aggs.get("status_counts", {}).get("buckets", [])
        }
        # Parse category counts
        category_buckets = {
            b["key"]: b["doc_count"]
            for b in aggs.get("category_counts", {}).get("buckets", [])
        }

        # ``quantity * (unit_cost or 0)`` summed, which is what the painless script
        # did — including treating a missing ``unit_cost`` as zero rather than
        # skipping the row, so an item priced at nothing still counts as an item.
        total_value = 0.0
        for hit in response["hits"]["hits"]:
            source = hit.get("_source") or {}
            quantity = source.get("quantity") or 0
            unit_cost = source.get("unit_cost") or 0
            try:
                total_value += float(quantity) * float(unit_cost)
            except (TypeError, ValueError):
                logger.warning(
                    "inventory summary: skipping item with non-numeric "
                    "quantity=%r unit_cost=%r",
                    quantity, unit_cost,
                )
        if total_items > self.MAX_SUMMARY_ITEMS:
            # Said out loud rather than reported as a smaller total, which would
            # look like inventory shrinking.
            logger.warning(
                "inventory summary for tenant covers %d of %d items (scan cap); "
                "total_value is understated",
                self.MAX_SUMMARY_ITEMS, total_items,
            )

        return InventorySummary(
            total_items=total_items,
            total_value=total_value,
            in_stock=status_buckets.get(InventoryStatus.IN_STOCK.value, 0),
            low_stock=status_buckets.get(InventoryStatus.LOW_STOCK.value, 0),
            out_of_stock=status_buckets.get(InventoryStatus.OUT_OF_STOCK.value, 0),
            on_order=status_buckets.get(InventoryStatus.ON_ORDER.value, 0),
            categories=category_buckets,
        )

    # ------------------------------------------------------------------
    # Stock movement history
    # ------------------------------------------------------------------

    async def get_item_history(
        self,
        item_id: str,
        tenant_id: str,
        page: int = 1,
        size: int = 50,
    ) -> Dict[str, Any]:
        """Fetch stock movement history for an item from inventory_events."""
        # Verify item exists
        await self.get_item(item_id, tenant_id)

        from_offset = (page - 1) * size
        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"item_id": item_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "from": from_offset,
            "size": size,
            "sort": [{"event_timestamp": {"order": "desc"}}],
        }

        response = await self._es.search_documents(
            INVENTORY_EVENTS_INDEX, query, size=size
        )

        total = response["hits"]["total"]["value"]
        events = [hit["_source"] for hit in response["hits"]["hits"]]

        return {"items": events, "total": total, "page": page, "size": size}

    # ------------------------------------------------------------------
    # WebSocket broadcasting
    # ------------------------------------------------------------------

    async def _broadcast_stock_alert(
        self,
        item_id: str,
        name: str,
        category: str,
        location: str,
        new_status: str,
        quantity: int,
        min_threshold: int,
        tenant_id: str,
    ) -> None:
        """Broadcast a low-stock or out-of-stock alert via WebSocket."""
        if self._ws_manager is None:
            return

        message = {
            "type": "inventory_alert",
            "data": {
                "item_id": item_id,
                "name": name,
                "category": category,
                "location": location,
                "status": new_status,
                "quantity": quantity,
                "min_threshold": min_threshold,
                "tenant_id": tenant_id,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        try:
            await self._ws_manager.broadcast(message)
            logger.info(
                "Broadcast inventory alert: item=%s status=%s",
                item_id, new_status,
            )
        except Exception as e:
            # Log but don't fail the operation if broadcast fails
            logger.warning(
                "Failed to broadcast inventory alert for %s: %s",
                item_id, e,
            )
