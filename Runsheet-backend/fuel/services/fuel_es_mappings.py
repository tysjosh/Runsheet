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
            "capacity_liters": {"type": "float"},
            "current_stock_liters": {"type": "float"},
            "daily_consumption_rate": {"type": "float"},
            "days_until_empty": {"type": "float"},
            "alert_threshold_pct": {"type": "float"},
            "status": {"type": "keyword"},
            "location": {"type": "geo_point"},
            "location_name": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "tenant_id": {"type": "keyword"},
            "created_at": {"type": "date"},
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
            "tenant_id": {"type": "keyword"},
            "event_timestamp": {"type": "date"},
            "ingested_at": {"type": "date"},
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


def setup_fuel_indices(es_client, es_service=None):
    """
    Create fuel indices and apply ILM policy.

    Creates the fuel_stations and fuel_events indices with strict mappings
    if they don't already exist, then sets up and applies the ILM policy
    for fuel_events.

    Args:
        es_client: An Elasticsearch client instance.
        es_service: Optional ElasticsearchService for serverless detection.
    """
    from services.elasticsearch_service import ElasticsearchService

    is_serverless = es_service.is_serverless if es_service else False

    indices = {
        FUEL_STATIONS_INDEX: FUEL_STATIONS_MAPPING,
        FUEL_EVENTS_INDEX: FUEL_EVENTS_MAPPING,
    }

    for index_name, mapping in indices.items():
        try:
            if not es_client.indices.exists(index=index_name):
                if is_serverless:
                    mapping = ElasticsearchService.strip_serverless_incompatible_settings(mapping)
                es_client.indices.create(index=index_name, body=mapping)
                logger.info(f"✅ Created fuel index: {index_name}")
            else:
                logger.info(f"📋 Fuel index already exists: {index_name}")
        except Exception as e:
            logger.error(f"❌ Failed to create fuel index {index_name}: {e}")

    # Apply ILM policy for fuel_events
    try:
        es_client.ilm.put_lifecycle(
            name=FUEL_EVENTS_ILM_POLICY_NAME,
            body=FUEL_EVENTS_ILM_POLICY,
        )
        logger.info(f"✅ Created/updated ILM policy: {FUEL_EVENTS_ILM_POLICY_NAME}")
    except Exception as e:
        logger.warning(
            f"⚠️ Failed to create/update ILM policy "
            f"{FUEL_EVENTS_ILM_POLICY_NAME}: {e}"
        )
        return

    # Apply the ILM policy to the fuel_events index
    try:
        if es_client.indices.exists(index=FUEL_EVENTS_INDEX):
            es_client.indices.put_settings(
                index=FUEL_EVENTS_INDEX,
                body={
                    "index": {
                        "lifecycle": {
                            "name": FUEL_EVENTS_ILM_POLICY_NAME,
                        }
                    }
                },
            )
            logger.info(
                f"✅ Applied ILM policy '{FUEL_EVENTS_ILM_POLICY_NAME}' "
                f"to index '{FUEL_EVENTS_INDEX}'"
            )
    except Exception as e:
        logger.warning(
            f"⚠️ Failed to apply ILM policy to {FUEL_EVENTS_INDEX}: {e}"
        )
