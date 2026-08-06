"""Elasticsearch index mappings for the Logistics Scheduling module.

Validates: Requirements 1.1, 1.2, 1.5, 1.6, 10.3
"""

import logging

logger = logging.getLogger(__name__)

JOBS_CURRENT_INDEX = "jobs_current"
JOB_EVENTS_INDEX = "job_events"
TENANT_JOB_POLICIES_INDEX = "tenant_job_policies"
JOB_EVENTS_ILM_POLICY_NAME = "job-events-policy"

JOBS_CURRENT_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "job_id":              {"type": "keyword"},
            "job_type":            {"type": "keyword"},
            "status":              {"type": "keyword"},
            "tenant_id":           {"type": "keyword"},
            "asset_assigned":      {"type": "keyword"},
            "order_id":            {"type": "keyword"},
            "customer_id":         {"type": "keyword"},
            "driver_id":           {"type": "keyword"},
            # Canonical drivers_current identifier recorded alongside the
            # SuperTokens user_id in asset_assigned on job acceptance
            # (Requirement 1.13). Nullable: an absent value means a
            # pre-migration document and no backfill runs (Requirement 15.12).
            "assigned_driver_id":  {"type": "keyword"},
            "origin":              {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "destination":         {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "origin_location":     {"type": "geo_point"},
            "destination_location": {"type": "geo_point"},
            "scheduled_time":      {"type": "date"},
            "estimated_arrival":   {"type": "date"},
            "started_at":          {"type": "date"},
            "completed_at":        {"type": "date"},
            "created_at":          {"type": "date"},
            "updated_at":          {"type": "date"},
            "created_by":          {"type": "keyword"},
            "priority":            {"type": "keyword"},
            "delayed":             {"type": "boolean"},
            "delay_duration_minutes": {"type": "integer"},
            "failure_reason":      {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "notes":               {"type": "text"},
            "cargo_manifest": {
                "type": "nested",
                "properties": {
                    "item_id":           {"type": "keyword"},
                    "description":       {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    "weight_kg":         {"type": "float"},
                    "container_number":  {"type": "keyword"},
                    "seal_number":       {"type": "keyword"},
                    "item_status":       {"type": "keyword"}
                }
            },
            "readiness_flags":   {"type": "object", "enabled": False}
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1
    }
}


JOB_EVENTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "event_id":        {"type": "keyword"},
            "job_id":          {"type": "keyword"},
            "event_type":      {"type": "keyword"},
            "tenant_id":       {"type": "keyword"},
            "actor_id":        {"type": "keyword"},
            "event_timestamp": {"type": "date"},
            "event_payload":   {"type": "object", "enabled": False}
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1
    }
}

JOB_EVENTS_ILM_POLICY = {
    "policy": {
        "phases": {
            "hot":    {"actions": {}},
            "warm":   {"min_age": "30d", "actions": {}},
            "cold":   {"min_age": "90d", "actions": {}},
            "delete": {"min_age": "365d", "actions": {"delete": {}}}
        }
    }
}

TENANT_JOB_POLICIES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "tenant_id":              {"type": "keyword"},
            "pod_required":           {"type": "boolean"},
            "pod_radius_meters":      {"type": "integer"},
            "otp_required":           {"type": "boolean"},
            "nudge_timeout_minutes":  {"type": "integer"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}


# Module-level registry: index_name -> mapping. Single source of truth for
# both setup_scheduling_indices and the seed script's recreate path.
SCHEDULING_INDEX_MAPPINGS = {
    JOBS_CURRENT_INDEX: JOBS_CURRENT_MAPPING,
    JOB_EVENTS_INDEX: JOB_EVENTS_MAPPING,
    TENANT_JOB_POLICIES_INDEX: TENANT_JOB_POLICIES_MAPPING,
}


