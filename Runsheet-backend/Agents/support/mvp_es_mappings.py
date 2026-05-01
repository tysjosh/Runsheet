"""Elasticsearch index mappings for Fuel Distribution MVP indices.

Defines mappings for mvp_tank_forecasts, mvp_delivery_priorities,
mvp_load_plans, mvp_routes, mvp_replan_events, mvp_plan_outcomes,
and truck_compartments.

Validates: Requirements 7.1–7.9
"""
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Index name constants
# ---------------------------------------------------------------------------

MVP_TANK_FORECASTS_INDEX = "mvp_tank_forecasts"
MVP_DELIVERY_PRIORITIES_INDEX = "mvp_delivery_priorities"
MVP_LOAD_PLANS_INDEX = "mvp_load_plans"
MVP_ROUTES_INDEX = "mvp_routes"
MVP_REPLAN_EVENTS_INDEX = "mvp_replan_events"
MVP_PLAN_OUTCOMES_INDEX = "mvp_plan_outcomes"
TRUCK_COMPARTMENTS_INDEX = "truck_compartments"

# ---------------------------------------------------------------------------
# Mapping definitions
# ---------------------------------------------------------------------------

MVP_TANK_FORECASTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "forecast_id":         {"type": "keyword"},
            "station_id":          {"type": "keyword"},
            "fuel_grade":          {"type": "keyword"},
            "hours_to_runout_p50": {"type": "float"},
            "hours_to_runout_p90": {"type": "float"},
            "runout_risk_24h":     {"type": "float"},
            "confidence":          {"type": "float"},
            "feature_version":     {"type": "keyword"},
            "anomaly_flags":       {"type": "keyword"},
            "tenant_id":           {"type": "keyword"},
            "run_id":              {"type": "keyword"},
            "timestamp":           {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

MVP_DELIVERY_PRIORITIES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "priority_list_id": {"type": "keyword"},
            "priorities": {
                "type": "nested",
                "properties": {
                    "station_id":      {"type": "keyword"},
                    "fuel_grade":      {"type": "keyword"},
                    "priority_score":  {"type": "float"},
                    "priority_bucket": {"type": "keyword"},
                    "reasons":         {"type": "keyword"},
                },
            },
            "scoring_weights": {"type": "object", "enabled": True},
            "tenant_id":       {"type": "keyword"},
            "run_id":          {"type": "keyword"},
            "timestamp":       {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

MVP_LOAD_PLANS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "plan_id":  {"type": "keyword"},
            "truck_id": {"type": "keyword"},
            "assignments": {
                "type": "nested",
                "properties": {
                    "compartment_id":             {"type": "keyword"},
                    "station_id":                 {"type": "keyword"},
                    "fuel_grade":                 {"type": "keyword"},
                    "quantity_liters":            {"type": "float"},
                    "compartment_capacity_liters": {"type": "float"},
                },
            },
            "total_utilization_pct":  {"type": "float"},
            "unserved_demand_liters": {"type": "float"},
            "total_weight_kg":        {"type": "float"},
            "tenant_id":              {"type": "keyword"},
            "run_id":                 {"type": "keyword"},
            "created_at":             {"type": "date"},
            "status":                 {"type": "keyword"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

MVP_ROUTES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "route_id": {"type": "keyword"},
            "truck_id": {"type": "keyword"},
            "plan_id":  {"type": "keyword"},
            "stops": {
                "type": "nested",
                "properties": {
                    "station_id": {"type": "keyword"},
                    "eta":        {"type": "date"},
                    "drop":       {"type": "object", "enabled": True},
                    "sequence":   {"type": "integer"},
                },
            },
            "distance_km":    {"type": "float"},
            "eta_confidence":  {"type": "float"},
            "objective_value": {"type": "float"},
            "tenant_id":       {"type": "keyword"},
            "run_id":          {"type": "keyword"},
            "timestamp":       {"type": "date"},
            "status":          {"type": "keyword"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

MVP_REPLAN_EVENTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "event_id":          {"type": "keyword"},
            "original_plan_id":  {"type": "keyword"},
            "patched_plan_id":   {"type": "keyword"},
            "trigger_signal_id": {"type": "keyword"},
            "replan_type":       {"type": "keyword"},
            "diff": {
                "type": "object",
                "properties": {
                    "stops_reordered":     {"type": "keyword"},
                    "volumes_reallocated": {"type": "object", "enabled": True},
                    "truck_swapped":       {"type": "keyword"},
                    "stations_deferred":   {"type": "keyword"},
                    "stations_added":      {"type": "keyword"},
                },
            },
            "status":    {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "run_id":    {"type": "keyword"},
            "timestamp": {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

MVP_PLAN_OUTCOMES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "outcome_id":     {"type": "keyword"},
            "plan_id":        {"type": "keyword"},
            "run_id":         {"type": "keyword"},
            "before_kpis":    {"type": "object", "enabled": True},
            "after_kpis":     {"type": "object", "enabled": True},
            "realized_delta": {"type": "object", "enabled": True},
            "tenant_id":      {"type": "keyword"},
            "timestamp":      {"type": "date"},
            "status":         {"type": "keyword"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

TRUCK_COMPARTMENTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "compartment_id": {"type": "keyword"},
            "truck_id":       {"type": "keyword"},
            "capacity_liters": {"type": "float"},
            "allowed_grades":  {"type": "keyword"},
            "position_index":  {"type": "integer"},
            "tenant_id":       {"type": "keyword"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

# ---------------------------------------------------------------------------
# Index setup function
# ---------------------------------------------------------------------------

def setup_mvp_indices(es_service) -> None:
    """Create MVP ES indices if they don't already exist.

    Follows the same pattern as setup_overlay_indices in overlay_es_mappings.py.

    Args:
        es_service: An ElasticsearchService instance.
    """
    from services.elasticsearch_service import ElasticsearchService

    es_client = es_service.client
    is_serverless = es_service.is_serverless

    indices = {
        MVP_TANK_FORECASTS_INDEX: MVP_TANK_FORECASTS_MAPPING,
        MVP_DELIVERY_PRIORITIES_INDEX: MVP_DELIVERY_PRIORITIES_MAPPING,
        MVP_LOAD_PLANS_INDEX: MVP_LOAD_PLANS_MAPPING,
        MVP_ROUTES_INDEX: MVP_ROUTES_MAPPING,
        MVP_REPLAN_EVENTS_INDEX: MVP_REPLAN_EVENTS_MAPPING,
        MVP_PLAN_OUTCOMES_INDEX: MVP_PLAN_OUTCOMES_MAPPING,
        TRUCK_COMPARTMENTS_INDEX: TRUCK_COMPARTMENTS_MAPPING,
    }

    for index_name, mapping in indices.items():
        try:
            if not es_client.indices.exists(index=index_name):
                if is_serverless:
                    mapping = ElasticsearchService.strip_serverless_incompatible_settings(mapping)
                es_client.indices.create(index=index_name, body=mapping)
                logger.info(f"Created MVP index: {index_name}")
            else:
                logger.info(f"MVP index already exists: {index_name}")
        except Exception as e:
            logger.error(f"Failed to create MVP index {index_name}: {e}")
