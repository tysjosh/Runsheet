"""
Elasticsearch index mappings for the inventory module.

Defines strict mappings for:
- inventory: Item registry with current state
- inventory_events: Stock movement history (append-only)
- restock_requests: Restock request tracking
"""

import logging

logger = logging.getLogger(__name__)

INVENTORY_INDEX = "inventory"
INVENTORY_EVENTS_INDEX = "inventory_events"
RESTOCK_REQUESTS_INDEX = "restock_requests"

INVENTORY_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "item_id": {"type": "keyword"},
            "name": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "category": {"type": "keyword"},
            "quantity": {"type": "integer"},
            "unit": {"type": "keyword"},
            "min_threshold": {"type": "integer"},
            "max_capacity": {"type": "integer"},
            "location": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "status": {"type": "keyword"},
            "unit_cost": {"type": "float"},
            "supplier": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "compatible_assets": {"type": "keyword"},
            "last_restocked": {"type": "date"},
            "tenant_id": {"type": "keyword"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
        },
    },
}

INVENTORY_EVENTS_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "event_id": {"type": "keyword"},
            "item_id": {"type": "keyword"},
            "quantity_change": {"type": "integer"},
            "quantity_before": {"type": "integer"},
            "quantity_after": {"type": "integer"},
            "reason": {"type": "keyword"},
            "reference_id": {"type": "keyword"},
            "notes": {"type": "text"},
            "actor_id": {"type": "keyword"},
            "status_before": {"type": "keyword"},
            "status_after": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "event_timestamp": {"type": "date"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
        },
    },
}

RESTOCK_REQUESTS_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "request_id": {"type": "keyword"},
            "item_category": {"type": "keyword"},
            "compatible_asset_type": {"type": "keyword"},
            "requested_quantity": {"type": "integer"},
            "priority": {"type": "keyword"},
            "status": {"type": "keyword"},
            "requested_by": {"type": "keyword"},
            "depot_location": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "tenant_id": {"type": "keyword"},
            "created_at": {"type": "date"},
            # index_document auto-stamps updated_at on every write; a strict
            # mapping without it rejects all restock-request creations.
            "updated_at": {"type": "date"},
            "fulfilled_at": {"type": "date"},
        },
    },
}

INVENTORY_EVENTS_ILM_POLICY = {
    "policy": {
        "phases": {
            "hot": {
                "min_age": "0ms",
                "actions": {
                    "set_priority": {"priority": 100},
                },
            },
            "warm": {
                "min_age": "30d",
                "actions": {
                    "set_priority": {"priority": 50},
                    "forcemerge": {"max_num_segments": 1},
                    "readonly": {},
                },
            },
            "cold": {
                "min_age": "90d",
                "actions": {
                    "set_priority": {"priority": 0},
                    "allocate": {"number_of_replicas": 0},
                },
            },
            "delete": {
                "min_age": "365d",
                "actions": {"delete": {}},
            },
        }
    }
}

INVENTORY_EVENTS_ILM_POLICY_NAME = "inventory-events-policy"


