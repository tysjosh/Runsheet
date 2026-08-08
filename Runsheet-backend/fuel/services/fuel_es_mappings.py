"""
Elasticsearch index mappings for fuel monitoring indices.

Validates: Requirements 8.1, 8.2, 8.3, 8.4
"""

import logging

logger = logging.getLogger(__name__)

FUEL_STATIONS_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "station_id": {"type": "keyword"},
            "name": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "fuel_type": {"type": "keyword"},
            "fuel_grade": {"type": "keyword"},
            "capacity_liters": {"type": "float"},
            "current_stock_liters": {"type": "float"},
            "daily_consumption_rate": {"type": "float"},
            "days_until_empty": {"type": "float"},
            "stock_level_pct": {"type": "float"},
            "alert_threshold_pct": {"type": "float"},
            "status": {"type": "keyword"},
            "location": {"type": "geo_point"},
            "latitude": {"type": "float"},
            "longitude": {"type": "float"},
            "location_name": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "tenant_id": {"type": "keyword"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
            "last_updated": {"type": "date"},
        },
    },
}

FUEL_EVENTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "event_id": {"type": "keyword"},
            "station_id": {"type": "keyword"},
            "event_type": {"type": "keyword"},
            "fuel_type": {"type": "keyword"},
            "quantity_liters": {"type": "float"},
            "asset_id": {"type": "keyword"},
            "operator_id": {"type": "keyword"},
            "supplier": {"type": "keyword"},
            "delivery_reference": {"type": "keyword"},
            "odometer_reading": {"type": "float"},
            "status": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "event_timestamp": {"type": "date"},
            "ingested_at": {"type": "date"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

FUEL_EVENTS_ILM_POLICY = {
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

FUEL_STATIONS_INDEX = "fuel_stations"
FUEL_EVENTS_INDEX = "fuel_events"
FUEL_EVENTS_ILM_POLICY_NAME = "fuel-events-policy"

# Module-level registry: index_name -> mapping. Every other mappings module
# exposes one; this module did not, so callers that look a mapping up by index
# name could not find ``fuel_stations`` at all.
# ``persistence.rebuild_from_postgres._ensure_index`` is the one that matters: it
# recreates a dropped index and, finding no mapping, let Elasticsearch infer one
# dynamically — which types ``tenant_id`` as ``text`` and makes every
# ``term: {tenant_id}`` query match nothing. The rebuild reports success and the
# index is silently unqueryable.
FUEL_INDEX_MAPPINGS = {
    FUEL_STATIONS_INDEX: FUEL_STATIONS_MAPPING,
    FUEL_EVENTS_INDEX: FUEL_EVENTS_MAPPING,
}


def _reconcile_fuel_index_mapping(es_client, index_name: str, mapping: dict) -> None:
    """Additively put any mapping fields missing from the live fuel index.

    Only fields absent from the current mapping are sent via ``put_mapping``
    so an existing index gains newly-introduced fields without a reindex.
    No-op when nothing is missing.
    """
    expected_props = mapping.get("mappings", {}).get("properties", {})
    if not expected_props:
        return
    try:
        actual = es_client.indices.get_mapping(index=index_name)
        actual_props = (
            actual.get(index_name, {}).get("mappings", {}).get("properties", {})
        )
        missing = {
            name: spec
            for name, spec in expected_props.items()
            if name not in actual_props
        }
        if not missing:
            logger.info(f"📋 Fuel index already up to date: {index_name}")
            return
        es_client.indices.put_mapping(
            index=index_name, body={"properties": missing}
        )
        logger.info(
            "✅ Reconciled fuel index %s — added fields: %s",
            index_name,
            ", ".join(sorted(missing)),
        )
    except Exception:
        logger.exception("Failed to reconcile fuel index mapping for %s", index_name)


