"""Elasticsearch index mappings for Fuel Ops Hardening indices.

Defines strict mappings for the 22 new indices introduced by the fuel-ops-hardening spec
covering customer tanks, weather/HDD ingestion, tenant-configurable depots, combinable
groups, POD + reconciliation artifacts, integration framework, terminal sourcing,
storm mode, and the per-tenant credentials vault.

All mappings use ``dynamic: strict`` to reject unexpected fields and every index carries a
``tenant_id`` keyword field so tenant isolation can be enforced at the query layer.

Validates: Requirements 1.1, 1.2, 2.2, 3.2, 4.1, 4.3, 4.4, 5.1, 7.1, 8.1, 8.2, 8.3, 8.4,
9.1, 9.3, 10.1
"""
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Index name constants
# ---------------------------------------------------------------------------

# Capability 1 — Forecasting
CUSTOMER_TANKS_INDEX = "customer_tanks"
WEATHER_OBSERVATIONS_INDEX = "weather_observations"

# Capability 2 — Dispatch & Replanning
DEPOTS_INDEX = "depots"

# Capability 3 — Prioritization
MVP_COMBINABLE_GROUPS_INDEX = "mvp_combinable_groups"

# Capability 4 — POD + Reconciliation
METER_TICKET_OCR_RESULTS_INDEX = "meter_ticket_ocr_results"
BILL_OF_LADING_INDEX = "bill_of_lading"
MVP_RECONCILIATION_INDEX = "mvp_reconciliation"

# Capability 5 — Integration Layer
INTEGRATION_INSTANCES_INDEX = "integration_instances"
INTEGRATION_SYNC_RUNS_INDEX = "integration_sync_runs"
ATG_READINGS_INDEX = "atg_readings"
TRUCK_TELEMETRY_INDEX = "truck_telemetry"
TENANT_CREDENTIALS_INDEX = "tenant_credentials"

# Capability 7 — Contamination Prevention
COMPARTMENT_CLEANING_EVENTS_INDEX = "compartment_cleaning_events"
CROSS_CONTAMINATION_EVENTS_INDEX = "cross_contamination_events"

# Capability 8 — Terminal / Rack Sourcing
TERMINALS_INDEX = "terminals"
RACK_PRICES_INDEX = "rack_prices"
SUPPLIER_CONTRACTS_INDEX = "supplier_contracts"
TERMINAL_WAIT_REPORTS_INDEX = "terminal_wait_reports"
SOURCING_RECOMMENDATIONS_INDEX = "sourcing_recommendations"

# Capability 9 — Storm Mode
WEATHER_ALERTS_INDEX = "weather_alerts"
STORM_ROAD_RESTRICTIONS_INDEX = "storm_road_restrictions"
STORM_MODE_OVERRIDES_INDEX = "storm_mode_overrides"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DEFAULT_SETTINGS = {
    "number_of_shards": 1,
    "number_of_replicas": 1,
}


# ---------------------------------------------------------------------------
# Capability 1 — Forecasting mappings
# ---------------------------------------------------------------------------

CUSTOMER_TANKS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "customer_tank_id":       {"type": "keyword"},
            "tenant_id":              {"type": "keyword"},
            "source_system":          {"type": "keyword"},
            "external_tank_id":       {"type": "keyword"},
            "customer_id":            {"type": "keyword"},
            "last_refill_order_id":   {"type": "keyword"},
            "customer_type":          {"type": "keyword"},
            "fuel_type":              {"type": "keyword"},
            "fuel_product_code":      {"type": "keyword"},
            "capacity_gallons":       {"type": "double"},
            "current_level_gallons":  {"type": "double"},
            "last_reading_at":        {"type": "date"},
            "location":               {"type": "geo_point"},
            "location_lat":           {"type": "double"},
            "location_lon":           {"type": "double"},
            "zip_code":               {"type": "keyword"},
            "k_factor":               {"type": "double"},
            "use_case":               {"type": "keyword"},
            "status":                 {"type": "keyword"},
            "updated_at":             {"type": "date"},
            "created_at":             {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

WEATHER_OBSERVATIONS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "tenant_id":    {"type": "keyword"},
            "zip_code":     {"type": "keyword"},
            "date":         {"type": "date"},
            "avg_temp_f":   {"type": "float"},
            "hdd":          {"type": "float"},
            "provider":     {"type": "keyword"},
            "retrieved_at": {"type": "date"},
            "updated_at":   {"type": "date"},
            "created_at":   {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Capability 2 — Depots mapping
# ---------------------------------------------------------------------------

DEPOTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "depot_id":              {"type": "keyword"},
            "tenant_id":             {"type": "keyword"},
            "name":                  {"type": "text"},
            "location":              {"type": "geo_point"},
            "location_lat":          {"type": "double"},
            "location_lon":          {"type": "double"},
            "address":               {"type": "text"},
            "timezone":              {"type": "keyword"},
            "fuel_types_supported":  {"type": "keyword"},
            "status":                {"type": "keyword"},
            "is_default":            {"type": "boolean"},
            "updated_at":            {"type": "date"},
            "created_at":            {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Capability 3 — Combinable groups mapping
# ---------------------------------------------------------------------------

MVP_COMBINABLE_GROUPS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "group_id":                   {"type": "keyword"},
            "tenant_id":                  {"type": "keyword"},
            "run_id":                     {"type": "keyword"},
            "members": {
                "type": "nested",
                "properties": {
                    "destination_type":    {"type": "keyword"},
                    "destination_id":      {"type": "keyword"},
                    "station_id":          {"type": "keyword"},
                    "customer_tank_id":    {"type": "keyword"},
                    "fuel_grade":          {"type": "keyword"},
                    "product_code":        {"type": "keyword"},
                    "estimated_gallons":   {"type": "double"},
                    "location":            {"type": "geo_point"},
                },
            },
            "fuel_grades":                 {"type": "keyword"},
            "estimated_combined_gallons":  {"type": "double"},
            "centroid":                    {"type": "geo_point"},
            "generated_at":                {"type": "date"},
            "updated_at":                  {"type": "date"},
            "created_at":                  {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Capability 4 — POD + Reconciliation mappings
# ---------------------------------------------------------------------------

METER_TICKET_OCR_RESULTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "ocr_result_id":          {"type": "keyword"},
            "tenant_id":              {"type": "keyword"},
            "pod_id":                 {"type": "keyword"},
            "file_ref":               {"type": "keyword"},
            "extracted_gallons":      {"type": "double"},
            "confidence":             {"type": "float"},
            "raw_text":               {"type": "text"},
            "requires_manual_review": {"type": "boolean"},
            "provider":               {"type": "keyword"},
            "processed_at":           {"type": "date"},
            "error_details":          {"type": "text"},
            "updated_at":             {"type": "date"},
            "created_at":             {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

BILL_OF_LADING_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "bol_id":       {"type": "keyword"},
            "tenant_id":    {"type": "keyword"},
            "pod_id":       {"type": "keyword"},
            "order_id":     {"type": "keyword"},
            "file_ref":     {"type": "keyword"},
            "hash":         {"type": "keyword"},
            "status":       {"type": "keyword"},
            "fields":       {"type": "object", "enabled": True},
            "generated_at": {"type": "date"},
            "updated_at":   {"type": "date"},
            "created_at":   {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

MVP_RECONCILIATION_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "reconciliation_id":                     {"type": "keyword"},
            "tenant_id":                             {"type": "keyword"},
            "order_id":                              {"type": "keyword"},
            "plan_id":                               {"type": "keyword"},
            "pod_id":                                {"type": "keyword"},
            "invoice_id":                            {"type": "keyword"},
            "customer_id":                           {"type": "keyword"},
            "assigned_asset_id":                     {"type": "keyword"},
            "assigned_driver_id":                    {"type": "keyword"},
            "ordered_gallons":                       {"type": "double"},
            "loaded_gallons":                        {"type": "double"},
            "delivered_gallons":                     {"type": "double"},
            "invoiced_gallons":                      {"type": "double"},
            "variance_load_vs_order_pct":            {"type": "float"},
            "variance_delivered_vs_loaded_pct":      {"type": "float"},
            "variance_invoiced_vs_delivered_pct":    {"type": "float"},
            "alert_flags":                           {"type": "keyword"},
            "payment_status":                        {"type": "keyword"},
            "payment_intent_id":                     {"type": "keyword"},
            "generated_at":                          {"type": "date"},
            "updated_at":                            {"type": "date"},
            "created_at":                            {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Capability 5 — Integration mappings
# ---------------------------------------------------------------------------

INTEGRATION_INSTANCES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "instance_id":      {"type": "keyword"},
            "tenant_id":        {"type": "keyword"},
            "provider_name":    {"type": "keyword"},
            "category":         {"type": "keyword"},
            "status":           {"type": "keyword"},
            "enabled":          {"type": "boolean"},
            "credentials_ref":  {"type": "keyword"},
            "schedule_cron":    {"type": "keyword"},
            "config":           {"type": "object", "enabled": True},
            "last_sync_at":     {"type": "date"},
            "last_error":       {"type": "text"},
            "retry_count":      {"type": "integer"},
            "updated_at":       {"type": "date"},
            "created_at":       {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

INTEGRATION_SYNC_RUNS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "run_id":         {"type": "keyword"},
            "tenant_id":      {"type": "keyword"},
            "instance_id":    {"type": "keyword"},
            "provider_name":  {"type": "keyword"},
            "operation":      {"type": "keyword"},
            "started_at":     {"type": "date"},
            "finished_at":    {"type": "date"},
            "status":         {"type": "keyword"},
            "record_counts":  {"type": "object", "dynamic": True},
            "result_metadata":{"type": "object", "enabled": False},
            "error_details":  {"type": "text"},
            "duration_ms":    {"type": "integer"},
            "updated_at":     {"type": "date"},
            "created_at":     {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

ATG_READINGS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "reading_id":       {"type": "keyword"},
            "tenant_id":        {"type": "keyword"},
            "instance_id":      {"type": "keyword"},
            "source_system":    {"type": "keyword"},
            "external_tank_id": {"type": "keyword"},
            "source_reading_id":{"type": "keyword"},
            "tank_ref":         {"type": "keyword"},
            "customer_tank_id": {"type": "keyword"},
            "station_id":       {"type": "keyword"},
            "volume_gallons":   {"type": "double"},
            "water_level_in":   {"type": "float"},
            "temperature_f":    {"type": "float"},
            "product_code":     {"type": "keyword"},
            "reading_at":       {"type": "date"},
            "retrieved_at":     {"type": "date"},
            "updated_at":       {"type": "date"},
            "created_at":       {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

TRUCK_TELEMETRY_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "telemetry_id":   {"type": "keyword"},
            "tenant_id":      {"type": "keyword"},
            "truck_id":       {"type": "keyword"},
            "driver_id":      {"type": "keyword"},
            "location":       {"type": "geo_point"},
            "location_lat":   {"type": "double"},
            "location_lon":   {"type": "double"},
            "speed_kph":      {"type": "float"},
            "engine_on":      {"type": "boolean"},
            "odometer_km":    {"type": "double"},
            "hos_status":     {"type": "keyword"},
            "recorded_at":    {"type": "date"},
            "retrieved_at":   {"type": "date"},
            "updated_at":     {"type": "date"},
            "created_at":     {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

TENANT_CREDENTIALS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "ref":             {"type": "keyword"},
            "tenant_id":       {"type": "keyword"},
            "key":             {"type": "keyword"},
            "provider_name":   {"type": "keyword"},
            "wrapped_dek":     {"type": "binary"},
            "ciphertext":      {"type": "binary"},
            "kms_key_id":      {"type": "keyword"},
            "algorithm":       {"type": "keyword"},
            "rotated_at":      {"type": "date"},
            "updated_at":      {"type": "date"},
            "created_at":      {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Capability 7 — Contamination mappings
# ---------------------------------------------------------------------------

COMPARTMENT_CLEANING_EVENTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "cleaning_event_id": {"type": "keyword"},
            "tenant_id":         {"type": "keyword"},
            "compartment_id":    {"type": "keyword"},
            "truck_id":          {"type": "keyword"},
            "method":            {"type": "keyword"},
            "actor_id":          {"type": "keyword"},
            # --- cross-module-entity-linkage Req 8.2 ---
            # Canonical driver reference for the actor that performed the
            # cleaning. Nullable/additive: legacy events that only carried
            # the free-text ``actor_id`` continue to index cleanly. The
            # ``actor_id`` above is retained as a DEPRECATED alias for
            # backward compatibility and is never removed.
            "driver_id":         {"type": "keyword"},
            "notes":             {"type": "text"},
            "evidence_refs":     {"type": "keyword"},
            "cleaned_at":        {"type": "date"},
            "updated_at":        {"type": "date"},
            "created_at":        {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

CROSS_CONTAMINATION_EVENTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "event_id":           {"type": "keyword"},
            "tenant_id":          {"type": "keyword"},
            "compartment_id":     {"type": "keyword"},
            "truck_id":           {"type": "keyword"},
            "previous_product":   {"type": "keyword"},
            "attempted_product":  {"type": "keyword"},
            "governing_rule":     {"type": "keyword"},
            "decision":           {"type": "keyword"},
            "reason":             {"type": "keyword"},
            "actor_id":           {"type": "keyword"},
            "plan_id":            {"type": "keyword"},
            "timestamp":          {"type": "date"},
            "updated_at":         {"type": "date"},
            "created_at":         {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Capability 8 — Terminal / Rack sourcing mappings
# ---------------------------------------------------------------------------

TERMINALS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "terminal_id":        {"type": "keyword"},
            "tenant_id":          {"type": "keyword"},
            "name":               {"type": "text"},
            "operator":           {"type": "keyword"},
            "location":           {"type": "geo_point"},
            "location_lat":       {"type": "double"},
            "location_lon":       {"type": "double"},
            "address":            {"type": "text"},
            "timezone":           {"type": "keyword"},
            "operating_hours": {
                "type": "nested",
                "properties": {
                    "day_of_week": {"type": "keyword"},
                    "open":        {"type": "keyword"},
                    "close":       {"type": "keyword"},
                },
            },
            "supported_products": {"type": "keyword"},
            "branded":            {"type": "boolean"},
            "supplier_brand":     {"type": "keyword"},
            "status":             {"type": "keyword"},
            "updated_at":         {"type": "date"},
            "created_at":         {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

RACK_PRICES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "rack_price_id":           {"type": "keyword"},
            "tenant_id":               {"type": "keyword"},
            "terminal_id":             {"type": "keyword"},
            "product_code":            {"type": "keyword"},
            "price_per_gallon_usd":    {"type": "double"},
            "branded_flag":            {"type": "boolean"},
            "supplier_brand":          {"type": "keyword"},
            "provider":                {"type": "keyword"},
            "effective_at":            {"type": "date"},
            "retrieved_at":            {"type": "date"},
            "updated_at":              {"type": "date"},
            "created_at":              {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

SUPPLIER_CONTRACTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "contract_id":                       {"type": "keyword"},
            "tenant_id":                         {"type": "keyword"},
            "supplier_name":                     {"type": "keyword"},
            "product_code":                      {"type": "keyword"},
            "preferred_terminal_ids":            {"type": "keyword"},
            "contract_price_per_gallon_usd":     {"type": "double"},
            "branded_required":                  {"type": "boolean"},
            "minimum_lift_gallons_per_month":    {"type": "double"},
            "rebate_terms":                      {"type": "text"},
            "effective_from":                    {"type": "date"},
            "effective_to":                      {"type": "date"},
            "status":                            {"type": "keyword"},
            "updated_at":                        {"type": "date"},
            "created_at":                        {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

TERMINAL_WAIT_REPORTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "report_id":     {"type": "keyword"},
            "tenant_id":     {"type": "keyword"},
            "terminal_id":   {"type": "keyword"},
            "wait_minutes":  {"type": "float"},
            "source":        {"type": "keyword"},
            "reporter_id":   {"type": "keyword"},
            "truck_id":      {"type": "keyword"},
            "observed_at":   {"type": "date"},
            "retrieved_at":  {"type": "date"},
            "notes":         {"type": "text"},
            "updated_at":    {"type": "date"},
            "created_at":    {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

SOURCING_RECOMMENDATIONS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "recommendation_id":  {"type": "keyword"},
            "request_id":         {"type": "keyword"},
            "tenant_id":          {"type": "keyword"},
            "truck_id":           {"type": "keyword"},
            "run_id":             {"type": "keyword"},
            "product_code":       {"type": "keyword"},
            "volume_gallons":     {"type": "double"},
            "origin":             {"type": "geo_point"},
            "origin_lat":         {"type": "double"},
            "origin_lon":         {"type": "double"},
            "candidates": {
                "type": "nested",
                "properties": {
                    "terminal_id":              {"type": "keyword"},
                    "price_per_gallon_usd":     {"type": "double"},
                    "branded_flag":             {"type": "boolean"},
                    "contract_id":              {"type": "keyword"},
                    "avg_wait_minutes":         {"type": "float"},
                    "distance_km_from_start":   {"type": "float"},
                    "score":                    {"type": "float"},
                    "reasons":                  {"type": "keyword"},
                    "wait_warning":             {"type": "boolean"},
                },
            },
            "rack_price_fallback":  {"type": "boolean"},
            # Top-level wait-warning summary (Task 7.11 / Req 8.4.5).
            # Mirrors the ``TerminalCandidate.wait_warning`` flags so
            # the dispatcher UI can render a wait-warning banner and
            # audit queries can filter on wait-warning exposure without
            # unwrapping the nested ``candidates`` array.
            "wait_warning_terminal_ids": {"type": "keyword"},
            "generated_at":         {"type": "date"},
            "updated_at":           {"type": "date"},
            "created_at":           {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Capability 9 — Storm mode mappings
# ---------------------------------------------------------------------------

WEATHER_ALERTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "alert_id":            {"type": "keyword"},
            "tenant_id":           {"type": "keyword"},
            "region_code":         {"type": "keyword"},
            "alert_type":          {"type": "keyword"},
            "severity":            {"type": "keyword"},
            "headline":            {"type": "text"},
            "description":         {"type": "text"},
            "expected_start_at":   {"type": "date"},
            "expected_end_at":     {"type": "date"},
            "affected_zip_codes":  {"type": "keyword"},
            "source":              {"type": "keyword"},
            "ingested_at":         {"type": "date"},
            "activation_status":   {"type": "keyword"},
            "updated_at":          {"type": "date"},
            "created_at":          {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

STORM_ROAD_RESTRICTIONS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "restriction_id":  {"type": "keyword"},
            "tenant_id":       {"type": "keyword"},
            "polygon":         {"type": "geo_shape"},
            "effective_from":  {"type": "date"},
            "effective_to":    {"type": "date"},
            "source":          {"type": "keyword"},
            "severity":        {"type": "keyword"},
            "reason":          {"type": "text"},
            "updated_at":      {"type": "date"},
            "created_at":      {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

STORM_MODE_OVERRIDES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "override_id":  {"type": "keyword"},
            "tenant_id":    {"type": "keyword"},
            "action":       {"type": "keyword"},
            "reason":       {"type": "text"},
            "actor_id":     {"type": "keyword"},
            "expires_at":   {"type": "date"},
            "updated_at":   {"type": "date"},
            "created_at":   {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Index setup function
# ---------------------------------------------------------------------------

FUEL_OPS_INDEX_MAPPINGS = {
    # Capability 1
    CUSTOMER_TANKS_INDEX:                CUSTOMER_TANKS_MAPPING,
    WEATHER_OBSERVATIONS_INDEX:          WEATHER_OBSERVATIONS_MAPPING,
    # Capability 2
    DEPOTS_INDEX:                        DEPOTS_MAPPING,
    # Capability 3
    MVP_COMBINABLE_GROUPS_INDEX:         MVP_COMBINABLE_GROUPS_MAPPING,
    # Capability 4
    METER_TICKET_OCR_RESULTS_INDEX:      METER_TICKET_OCR_RESULTS_MAPPING,
    BILL_OF_LADING_INDEX:                BILL_OF_LADING_MAPPING,
    MVP_RECONCILIATION_INDEX:            MVP_RECONCILIATION_MAPPING,
    # Capability 5
    INTEGRATION_INSTANCES_INDEX:         INTEGRATION_INSTANCES_MAPPING,
    INTEGRATION_SYNC_RUNS_INDEX:         INTEGRATION_SYNC_RUNS_MAPPING,
    ATG_READINGS_INDEX:                  ATG_READINGS_MAPPING,
    TRUCK_TELEMETRY_INDEX:               TRUCK_TELEMETRY_MAPPING,
    TENANT_CREDENTIALS_INDEX:            TENANT_CREDENTIALS_MAPPING,
    # Capability 7
    COMPARTMENT_CLEANING_EVENTS_INDEX:   COMPARTMENT_CLEANING_EVENTS_MAPPING,
    CROSS_CONTAMINATION_EVENTS_INDEX:    CROSS_CONTAMINATION_EVENTS_MAPPING,
    # Capability 8
    TERMINALS_INDEX:                     TERMINALS_MAPPING,
    RACK_PRICES_INDEX:                   RACK_PRICES_MAPPING,
    SUPPLIER_CONTRACTS_INDEX:            SUPPLIER_CONTRACTS_MAPPING,
    TERMINAL_WAIT_REPORTS_INDEX:         TERMINAL_WAIT_REPORTS_MAPPING,
    SOURCING_RECOMMENDATIONS_INDEX:      SOURCING_RECOMMENDATIONS_MAPPING,
    # Capability 9
    WEATHER_ALERTS_INDEX:                WEATHER_ALERTS_MAPPING,
    STORM_ROAD_RESTRICTIONS_INDEX:       STORM_ROAD_RESTRICTIONS_MAPPING,
    STORM_MODE_OVERRIDES_INDEX:          STORM_MODE_OVERRIDES_MAPPING,
}


def setup_fuel_ops_indices(es_service) -> None:
    """Create Fuel Ops hardening ES indices if they don't already exist.

    Follows the same pattern as setup_mvp_indices in mvp_es_mappings.py and
    setup_overlay_indices in overlay_es_mappings.py. On Elasticsearch Serverless
    deployments, shard/replica settings are stripped before creation via
    ``ElasticsearchService.strip_serverless_incompatible_settings``.

    Args:
        es_service: An ElasticsearchService instance with ``.client`` and
            ``.is_serverless`` attributes.
    """
    from services.elasticsearch_service import ElasticsearchService

    es_client = es_service.client
    is_serverless = es_service.is_serverless

    # Skip indices that have been retired (migrated to Postgres + dropped in
    # Phase 6) so startup does not silently recreate a dropped index.
    try:
        from config.settings import get_settings
        retired = set(get_settings().retired_es_indices or [])
    except Exception:  # noqa: BLE001
        retired = set()

    for index_name, mapping in FUEL_OPS_INDEX_MAPPINGS.items():
        if index_name in retired:
            logger.info("Skipping retired fuel-ops index: %s", index_name)
            continue
        try:
            if not es_client.indices.exists(index=index_name):
                body = mapping
                if is_serverless:
                    body = ElasticsearchService.strip_serverless_incompatible_settings(body)
                es_client.indices.create(index=index_name, body=body)
                logger.info(f"Created fuel-ops index: {index_name}")
            else:
                # Index exists — reconcile additively so new fields added to a
                # mapping definition (e.g. depots.is_default) are pushed to the
                # live index without a destructive reindex. ES ``put_mapping``
                # adds new properties to a strict mapping; identical fields are
                # no-ops and only an incompatible type change would raise
                # (logged, non-fatal).
                properties = (mapping.get("mappings") or {}).get("properties")
                if properties:
                    try:
                        es_client.indices.put_mapping(
                            index=index_name, body={"properties": properties}
                        )
                    except Exception:
                        logger.exception(
                            "Failed to reconcile fuel-ops index mapping %s",
                            index_name,
                        )
                else:
                    logger.info(f"Fuel-ops index already exists: {index_name}")
        except Exception:
            logger.exception("Failed to create fuel-ops index %s", index_name)
