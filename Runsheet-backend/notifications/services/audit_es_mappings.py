"""Elasticsearch index mappings for the Audit Timeline.

Defines the job_audit_timeline index mapping for immutable, append-only
audit events. Uses strict dynamic mapping to prevent unintended field
additions. The payload field uses enabled=False to allow flexible schema
without strict mapping constraints.

Validates: Requirements 12.1
"""

import logging

logger = logging.getLogger(__name__)

JOB_AUDIT_TIMELINE_INDEX = "job_audit_timeline"

JOB_AUDIT_TIMELINE_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "timeline_event_id": {"type": "keyword"},
            "job_id":            {"type": "keyword"},
            "event_type":        {"type": "keyword"},
            "actor_type":        {"type": "keyword"},
            "actor_id":          {"type": "keyword"},
            "timestamp":         {"type": "date"},
            "payload":           {"type": "object", "enabled": False},
            "tenant_id":         {"type": "keyword"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}


def setup_audit_indices(es_service):
    """
    Create audit timeline indices if they don't already exist.

    Creates the job_audit_timeline index with strict mappings and
    append-only semantics. Handles serverless-incompatible settings
    via the ElasticsearchService helper.

    Args:
        es_service: An ElasticsearchService instance (uses es_service.client).

    Validates: Requirements 12.1
    """
    from services.elasticsearch_service import ElasticsearchService

    es_client = es_service.client
    is_serverless = es_service.is_serverless

    indices = {
        JOB_AUDIT_TIMELINE_INDEX: JOB_AUDIT_TIMELINE_MAPPING,
    }

    for index_name, mapping in indices.items():
        try:
            if not es_client.indices.exists(index=index_name):
                if is_serverless:
                    mapping = ElasticsearchService.strip_serverless_incompatible_settings(mapping)
                es_client.indices.create(index=index_name, body=mapping)
                logger.info(f"✅ Created audit index: {index_name}")
            else:
                logger.info(f"📋 Audit index already exists: {index_name}")
        except Exception as e:
            logger.error(f"❌ Failed to create audit index {index_name}: {e}")
