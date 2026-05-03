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
            }
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


def setup_scheduling_indices(es_service):
    """
    Create scheduling indices and apply ILM policy.

    Creates the jobs_current and job_events indices with strict mappings
    if they don't already exist, then sets up and applies the ILM policy
    for job_events.

    Args:
        es_service: An ElasticsearchService instance (uses es_service.client).

    Validates: Requirements 1.5, 1.6
    """
    from services.elasticsearch_service import ElasticsearchService

    es_client = es_service.client
    is_serverless = es_service.is_serverless

    indices = {
        JOBS_CURRENT_INDEX: JOBS_CURRENT_MAPPING,
        JOB_EVENTS_INDEX: JOB_EVENTS_MAPPING,
        TENANT_JOB_POLICIES_INDEX: TENANT_JOB_POLICIES_MAPPING,
    }

    for index_name, mapping in indices.items():
        try:
            if not es_client.indices.exists(index=index_name):
                if is_serverless:
                    mapping = ElasticsearchService.strip_serverless_incompatible_settings(mapping)
                es_client.indices.create(index=index_name, body=mapping)
                logger.info(f"✅ Created scheduling index: {index_name}")
            else:
                logger.info(f"📋 Scheduling index already exists: {index_name}")
        except Exception as e:
            logger.error(f"❌ Failed to create scheduling index {index_name}: {e}")

    # Apply ILM policy for job_events
    try:
        es_client.ilm.put_lifecycle(
            name=JOB_EVENTS_ILM_POLICY_NAME,
            body=JOB_EVENTS_ILM_POLICY,
        )
        logger.info(f"✅ Created/updated ILM policy: {JOB_EVENTS_ILM_POLICY_NAME}")
    except Exception as e:
        logger.warning(
            f"⚠️ Failed to create/update ILM policy "
            f"{JOB_EVENTS_ILM_POLICY_NAME}: {e}"
        )
        return

    # Apply the ILM policy to the job_events index
    try:
        if es_client.indices.exists(index=JOB_EVENTS_INDEX):
            es_client.indices.put_settings(
                index=JOB_EVENTS_INDEX,
                body={
                    "index": {
                        "lifecycle": {
                            "name": JOB_EVENTS_ILM_POLICY_NAME,
                        }
                    }
                },
            )
            logger.info(
                f"✅ Applied ILM policy '{JOB_EVENTS_ILM_POLICY_NAME}' "
                f"to index '{JOB_EVENTS_INDEX}'"
            )
    except Exception as e:
        logger.warning(
            f"⚠️ Failed to apply ILM policy to {JOB_EVENTS_INDEX}: {e}"
        )
