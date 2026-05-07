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
MVP_PLAN_EXECUTIONS_INDEX = "mvp_plan_executions"
MVP_COST_CONFIGS_INDEX = "mvp_cost_configs"

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
            "updated_at":          {"type": "date"},
            "created_at":          {"type": "date"},
            # --- fuel-ops hardening Capability 1 extensions ---
            # Customer-tank context (Req 1.1.2, 1.6.1). Nullable so legacy
            # retail-station forecasts continue to index without these
            # fields populated.
            "customer_tank_id":          {"type": "keyword"},
            "customer_id":               {"type": "keyword"},
            "customer_type":             {"type": "keyword"},
            "fuel_type":                 {"type": "keyword"},
            "model_name":                {"type": "keyword"},
            "customer_type_multiplier":  {"type": "float"},
            "baseline_source":           {"type": "keyword"},
            "weather_fallback":          {"type": "boolean"},
            # Scheduled_Delivery entries folded into the projected level
            # (Req 1.4.3). Nested so individual fields remain queryable.
            "scheduled_deliveries": {
                "type": "nested",
                "properties": {
                    "delivery_id":     {"type": "keyword"},
                    "scheduled_eta":   {"type": "date"},
                    "planned_gallons": {"type": "double"},
                },
            },
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
                    # --- Phase 5 extensions (fuel-ops hardening Capability 3) ---
                    # Safe-to-delay tolerance (Req 3.1.3).
                    "safe_to_delay_days":    {"type": "integer"},
                    "safe_to_delay_bucket":  {"type": "keyword"},
                    # Business-impact scoring (Req 3.3.3, 3.3.4).
                    "business_impact_score":   {"type": "float"},
                    "business_impact_reasons": {"type": "keyword"},
                    # Route-friendly DBSCAN cluster (Req 3.4.2).
                    "cluster_id":   {"type": "keyword"},
                    "cluster_size": {"type": "integer"},
                    "cluster_centroid": {
                        "type": "object",
                        "properties": {
                            "lat": {"type": "float"},
                            "lon": {"type": "float"},
                        },
                    },
                },
            },
            "scoring_weights": {"type": "object", "enabled": True},
            "tenant_id":       {"type": "keyword"},
            "run_id":          {"type": "keyword"},
            "timestamp":       {"type": "date"},
            "updated_at":      {"type": "date"},
            "created_at":      {"type": "date"},
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
            "updated_at":             {"type": "date"},
            "status":                 {"type": "keyword"},
            "approved_by":            {"type": "keyword"},
            "approved_at":            {"type": "date"},
            "rejected_by":            {"type": "keyword"},
            "rejected_at":            {"type": "date"},
            "rejection_reason":       {"type": "text"},
            "estimated_cost":         {"type": "object", "dynamic": True},
            "actual_cost":            {"type": "object", "dynamic": True},
            "cost_variance_pct":      {"type": "float"},
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
                    "drop":       {"type": "object", "dynamic": True},
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
            "updated_at":      {"type": "date"},
            "created_at":      {"type": "date"},
            # Fuel Ops Hardening Req 2.1.5 / 2.1.6 — traffic provenance
            "traffic_provider": {"type": "keyword"},
            "traffic_fallback": {"type": "boolean"},
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
            # --- fuel-ops hardening Capability 2 / Task 4.10 (Req 2.5.1–2.5.4) ---
            # Structured "what changed" diff produced by
            # :func:`Agents.support.replan_diff_models.compute_replan_diff`.
            # Nested so each array entry stays independently queryable by
            # stop_id / truck_id from the dispatcher UI. The legacy ``diff``
            # field above continues to serve the MVP pipeline; this new
            # ``replan_diff`` field is the canonical fuel-ops-hardening shape
            # consumed by the ``/api/fuel/mvp/replans/{event_id}/diff``
            # endpoint and the ``replan_diff_ready`` WebSocket event.
            "replan_diff": {
                "type": "object",
                "properties": {
                    "diff_id":           {"type": "keyword"},
                    "original_route_id": {"type": "keyword"},
                    "patched_route_id":  {"type": "keyword"},
                    "added_stops": {
                        "type": "nested",
                        "properties": {
                            "stop_id":      {"type": "keyword"},
                            "index":        {"type": "integer"},
                            "gallons":      {"type": "double"},
                            "product_code": {"type": "keyword"},
                            "eta":          {"type": "keyword"},
                        },
                    },
                    "removed_stops": {
                        "type": "nested",
                        "properties": {
                            "stop_id":      {"type": "keyword"},
                            "index":        {"type": "integer"},
                            "gallons":      {"type": "double"},
                            "product_code": {"type": "keyword"},
                            "eta":          {"type": "keyword"},
                        },
                    },
                    "reordered_stops": {
                        "type": "nested",
                        "properties": {
                            "stop_id":      {"type": "keyword"},
                            "before_index": {"type": "integer"},
                            "after_index":  {"type": "integer"},
                        },
                    },
                    "reassigned_stops": {
                        "type": "nested",
                        "properties": {
                            "stop_id":       {"type": "keyword"},
                            "from_truck_id": {"type": "keyword"},
                            "to_truck_id":   {"type": "keyword"},
                        },
                    },
                    "quantity_changes": {
                        "type": "nested",
                        "properties": {
                            "stop_id":        {"type": "keyword"},
                            "before_gallons": {"type": "double"},
                            "after_gallons":  {"type": "double"},
                            "product_code":   {"type": "keyword"},
                        },
                    },
                    "eta_shifts": {
                        "type": "nested",
                        "properties": {
                            "stop_id":       {"type": "keyword"},
                            "before_eta":    {"type": "keyword"},
                            "after_eta":     {"type": "keyword"},
                            "shift_minutes": {"type": "double"},
                        },
                    },
                    "generated_at":      {"type": "date"},
                },
            },
            "status":    {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "run_id":    {"type": "keyword"},
            "timestamp": {"type": "date"},
            "updated_at": {"type": "date"},
            "created_at": {"type": "date"},
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
            "stop_variances": {
                "type": "nested",
                "properties": {
                    "station_id":            {"type": "keyword"},
                    "sequence":              {"type": "integer"},
                    "quantity_variance_pct":  {"type": "float"},
                    "time_variance_minutes":  {"type": "float"},
                    "status":                {"type": "keyword"},
                },
            },
            "aggregate_quantity_variance_pct":  {"type": "float"},
            "aggregate_time_variance_minutes":  {"type": "float"},
            "missed_stops_count":              {"type": "integer"},
            "tenant_id":      {"type": "keyword"},
            "timestamp":      {"type": "date"},
            "status":         {"type": "keyword"},
            "updated_at":     {"type": "date"},
            "created_at":     {"type": "date"},
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
            "depot_city":      {"type": "keyword"},
            "depot_location":  {"type": "geo_point"},
            "latitude":        {"type": "float"},
            "longitude":       {"type": "float"},
            "tenant_id":       {"type": "keyword"},
            "updated_at":      {"type": "date"},
            "created_at":      {"type": "date"},
            # --- fuel-ops hardening Capability 7 (Requirement 7.1.1) ---
            # Compartment-state tracking for cross-contamination prevention.
            # All four fields are nullable in ES so legacy
            # truck_compartments documents continue to index cleanly without
            # populating them; the Compartment_Loading_Agent and Cleaning
            # Event endpoints update them atomically via
            # CompartmentStateRepository (see fuel/compartment_state_models.py).
            #   state: one of clean | loaded | needs_cleaning
            #   last_loaded_product: canonical US product_code (e.g. DIESEL_2)
            #   last_loaded_at: timestamp of the most recent Loading_Plan commit
            #   last_cleaned_at: timestamp of the most recent Cleaning_Event
            "last_loaded_product": {"type": "keyword"},
            "last_loaded_at":      {"type": "date"},
            "last_cleaned_at":     {"type": "date"},
            "state":               {"type": "keyword"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

MVP_PLAN_EXECUTIONS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "execution_id":    {"type": "keyword"},
            "plan_id":         {"type": "keyword"},
            "route_id":        {"type": "keyword"},
            "tenant_id":       {"type": "keyword"},
            "stops": {
                "type": "nested",
                "properties": {
                    "station_id":          {"type": "keyword"},
                    "sequence":            {"type": "integer"},
                    "status":              {"type": "keyword"},
                    "planned_eta":         {"type": "date"},
                    "actual_arrival":      {"type": "date"},
                    "planned_quantities":  {"type": "object", "dynamic": True},
                    "actual_quantities":   {"type": "object", "dynamic": True},
                },
            },
            "completed_stops":  {"type": "integer"},
            "total_stops":      {"type": "integer"},
            "status":           {"type": "keyword"},
            "created_at":       {"type": "date"},
            "updated_at":       {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

MVP_COST_CONFIGS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "tenant_id":              {"type": "keyword"},
            "fuel_consumption_rate":  {"type": "float"},
            "fuel_price_per_liter":   {"type": "float"},
            "driver_hourly_rate":     {"type": "float"},
            "currency":               {"type": "keyword"},
            "created_at":             {"type": "date"},
            "updated_at":             {"type": "date"},
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
        MVP_PLAN_EXECUTIONS_INDEX: MVP_PLAN_EXECUTIONS_MAPPING,
        MVP_COST_CONFIGS_INDEX: MVP_COST_CONFIGS_MAPPING,
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
