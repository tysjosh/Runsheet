"""
V1.0 schema handler for Dinee webhook payloads.

Maps Dinee shipment and rider events to normalized Elasticsearch documents
conforming to the strict index mappings.

Requirements: 2.1-2.3
"""

import logging
from typing import Optional

from ops.ingestion.adapter import SchemaHandler, TransformResult

logger = logging.getLogger(__name__)

# Shipment event types that produce a shipments_current doc
SHIPMENT_EVENT_TYPES = {
    "shipment_created",
    "shipment_updated",
    "shipment_delivered",
    "shipment_failed",
}

# Rider event types that produce a riders_current doc
RIDER_EVENT_TYPES = {
    "rider_assigned",
    "rider_status_changed",
}


class V1SchemaHandler(SchemaHandler):
    """Handler for Dinee schema version 1.0."""

    def transform(self, payload: dict, request_id: str) -> TransformResult:
        event_type = payload.get("event_type", "")
        data = payload.get("data", {})
        tenant_id = payload.get("tenant_id", "")
        event_id = payload.get("event_id", "")
        timestamp = payload.get("timestamp", "")

        shipment_doc: Optional[dict] = None
        rider_doc: Optional[dict] = None

        # Build shipment current doc for shipment events
        if event_type in SHIPMENT_EVENT_TYPES:
            shipment_doc = self._build_shipment_current(data, tenant_id, timestamp, event_type)

        # Build rider current doc for rider events
        if event_type in RIDER_EVENT_TYPES:
            rider_doc = self._build_rider_current(data, tenant_id, timestamp)

        # Always build event doc for shipment_events append
        event_doc = self._build_event_doc(
            event_id=event_id,
            event_type=event_type,
            tenant_id=tenant_id,
            timestamp=timestamp,
            data=data,
        )

        return TransformResult(
            event_doc=event_doc,
            shipment_current_doc=shipment_doc,
            rider_current_doc=rider_doc,
        )

    @staticmethod
    def _build_shipment_current(data: dict, tenant_id: str, timestamp: str, event_type: str) -> dict:
        doc: dict = {
            "tenant_id": tenant_id,
            "last_event_timestamp": timestamp,
            "updated_at": timestamp,
        }
        if "shipment_id" in data:
            doc["shipment_id"] = data["shipment_id"]
        if "status" in data:
            doc["status"] = data["status"]
        elif event_type == "shipment_created":
            doc["status"] = "pending"
        elif event_type == "shipment_delivered":
            doc["status"] = "delivered"
        elif event_type == "shipment_failed":
            doc["status"] = "failed"
        if "rider_id" in data:
            doc["rider_id"] = data["rider_id"]
        if "origin" in data:
            doc["origin"] = data["origin"]
        if "destination" in data:
            doc["destination"] = data["destination"]
        if "estimated_delivery" in data:
            doc["estimated_delivery"] = data["estimated_delivery"]
        if "created_at" in data:
            doc["created_at"] = data["created_at"]
        if "current_location" in data:
            doc["current_location"] = data["current_location"]
        if "failure_reason" in data:
            doc["failure_reason"] = data["failure_reason"]
        return doc

    @staticmethod
    def _build_rider_current(data: dict, tenant_id: str, timestamp: str) -> dict:
        doc: dict = {
            "tenant_id": tenant_id,
            "last_event_timestamp": timestamp,
            "last_seen": timestamp,
        }
        if "rider_id" in data:
            doc["rider_id"] = data["rider_id"]
        if "rider_name" in data:
            doc["rider_name"] = data["rider_name"]
        if "status" in data:
            doc["status"] = data["status"]
        if "availability" in data:
            doc["availability"] = data["availability"]
        if "current_location" in data:
            doc["current_location"] = data["current_location"]
        if "active_shipment_count" in data:
            doc["active_shipment_count"] = data["active_shipment_count"]
        if "completed_today" in data:
            doc["completed_today"] = data["completed_today"]
        return doc

    @staticmethod
    def _build_event_doc(event_id: str, event_type: str, tenant_id: str, timestamp: str, data: dict) -> dict:
        doc: dict = {
            "event_id": event_id,
            "event_type": event_type,
            "tenant_id": tenant_id,
            "event_timestamp": timestamp,
            "event_payload": data,
        }
        if "shipment_id" in data:
            doc["shipment_id"] = data["shipment_id"]
        if "location" in data:
            doc["location"] = data["location"]
        elif "current_location" in data:
            doc["location"] = data["current_location"]
        return doc
