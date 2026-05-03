"""Elasticsearch index mappings for the Driver Communication module.

Defines index mappings for job_messages, proof_of_delivery, driver_presence,
driver_exceptions, and idempotency_keys indices. Each index uses strict
dynamic mapping to prevent unintended field additions.

Validates: Requirements 6.1, 8.1, 9.5, 7.1, 14.2
"""

import logging

logger = logging.getLogger(__name__)

JOB_MESSAGES_INDEX = "job_messages"
PROOF_OF_DELIVERY_INDEX = "proof_of_delivery"
DRIVER_PRESENCE_INDEX = "driver_presence"
DRIVER_EXCEPTIONS_INDEX = "driver_exceptions"
IDEMPOTENCY_KEYS_INDEX = "idempotency_keys"

JOB_MESSAGES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "message_id":  {"type": "keyword"},
            "job_id":      {"type": "keyword"},
            "sender_id":   {"type": "keyword"},
            "sender_role": {"type": "keyword"},
            "body":        {"type": "text"},
            "timestamp":   {"type": "date"},
            "tenant_id":   {"type": "keyword"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

PROOF_OF_DELIVERY_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "pod_id":             {"type": "keyword"},
            "job_id":             {"type": "keyword"},
            "recipient_name":     {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "signature_url":      {"type": "keyword"},
            "photo_urls":         {"type": "keyword"},
            "geotag":             {"type": "geo_point"},
            "timestamp":          {"type": "date"},
            "otp_verified":       {"type": "boolean"},
            "location_mismatch":  {"type": "boolean"},
            "status":             {"type": "keyword"},
            "tenant_id":          {"type": "keyword"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

DRIVER_PRESENCE_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "driver_id":     {"type": "keyword"},
            "tenant_id":     {"type": "keyword"},
            "status":        {"type": "keyword"},
            "last_seen":     {"type": "date"},
            "last_location": {"type": "geo_point"},
            "connected_at":  {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

DRIVER_EXCEPTIONS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "exception_id":   {"type": "keyword"},
            "job_id":         {"type": "keyword"},
            "exception_type": {"type": "keyword"},
            "severity":       {"type": "keyword"},
            "note":           {"type": "text"},
            "location":       {"type": "geo_point"},
            "media_refs":     {"type": "keyword"},
            "tenant_id":      {"type": "keyword"},
            "timestamp":      {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

IDEMPOTENCY_KEYS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "idempotency_key": {"type": "keyword"},
            "tenant_id":       {"type": "keyword"},
            "response":        {"type": "object", "enabled": False},
            "created_at":      {"type": "date"},
            "expires_at":      {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}


def setup_driver_indices(es_service):
    """
    Create driver communication indices if they don't already exist.

    Creates the job_messages, proof_of_delivery, driver_presence,
    driver_exceptions, and idempotency_keys indices with strict mappings.
    Handles serverless-incompatible settings via the ElasticsearchService
    helper.

    Args:
        es_service: An ElasticsearchService instance (uses es_service.client).

    Validates: Requirements 6.1, 8.1, 9.5, 7.1, 14.2
    """
    from services.elasticsearch_service import ElasticsearchService

    es_client = es_service.client
    is_serverless = es_service.is_serverless

    indices = {
        JOB_MESSAGES_INDEX: JOB_MESSAGES_MAPPING,
        PROOF_OF_DELIVERY_INDEX: PROOF_OF_DELIVERY_MAPPING,
        DRIVER_PRESENCE_INDEX: DRIVER_PRESENCE_MAPPING,
        DRIVER_EXCEPTIONS_INDEX: DRIVER_EXCEPTIONS_MAPPING,
        IDEMPOTENCY_KEYS_INDEX: IDEMPOTENCY_KEYS_MAPPING,
    }

    for index_name, mapping in indices.items():
        try:
            if not es_client.indices.exists(index=index_name):
                if is_serverless:
                    mapping = ElasticsearchService.strip_serverless_incompatible_settings(mapping)
                es_client.indices.create(index=index_name, body=mapping)
                logger.info(f"✅ Created driver index: {index_name}")
            else:
                logger.info(f"📋 Driver index already exists: {index_name}")
        except Exception as e:
            logger.error(f"❌ Failed to create driver index {index_name}: {e}")
