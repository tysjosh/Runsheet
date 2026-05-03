"""Elasticsearch index mappings for the Customer Notification Pipeline.

Defines index mappings for notifications_current, notification_preferences,
notification_templates, and notification_rules indices. Each index uses strict
dynamic mapping to prevent unintended field additions.

Validates: Requirements 3.1, 3.4, 3.5, 4.1, 5.1, 7.3
"""

import logging

logger = logging.getLogger(__name__)

NOTIFICATIONS_CURRENT_INDEX = "notifications_current"
NOTIFICATION_PREFERENCES_INDEX = "notification_preferences"
NOTIFICATION_TEMPLATES_INDEX = "notification_templates"
NOTIFICATION_RULES_INDEX = "notification_rules"
DEAD_LETTER_QUEUE_INDEX = "dead_letter_queue"

NOTIFICATIONS_CURRENT_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "notification_id":     {"type": "keyword"},
            "notification_type":   {"type": "keyword"},
            "channel":             {"type": "keyword"},
            "recipient_reference": {"type": "keyword"},
            "recipient_name":      {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "subject":             {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "message_body":        {"type": "text"},
            "related_entity_type": {"type": "keyword"},
            "related_entity_id":   {"type": "keyword"},
            "delivery_status":     {"type": "keyword"},
            "created_at":          {"type": "date"},
            "updated_at":          {"type": "date"},
            "sent_at":             {"type": "date"},
            "delivered_at":        {"type": "date"},
            "failed_at":           {"type": "date"},
            "failure_reason":      {"type": "text"},
            "retry_count":         {"type": "integer"},
            "proposal_id":         {"type": "keyword"},
            "provider_message_id": {"type": "keyword"},
            "scheduled_retry_at":  {"type": "date"},
            "tenant_id":           {"type": "keyword"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

NOTIFICATION_PREFERENCES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "preference_id":     {"type": "keyword"},
            "tenant_id":         {"type": "keyword"},
            "customer_id":       {"type": "keyword"},
            "customer_name":     {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "channels":          {"type": "object", "enabled": False},
            "event_preferences": {
                "type": "nested",
                "properties": {
                    "event_type":       {"type": "keyword"},
                    "enabled_channels": {"type": "keyword"},
                }
            },
            "created_at":        {"type": "date"},
            "updated_at":        {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

NOTIFICATION_TEMPLATES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "template_id":      {"type": "keyword"},
            "tenant_id":        {"type": "keyword"},
            "event_type":       {"type": "keyword"},
            "channel":          {"type": "keyword"},
            "subject_template": {"type": "text"},
            "body_template":    {"type": "text"},
            "placeholders":     {"type": "keyword"},
            "created_at":       {"type": "date"},
            "updated_at":       {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

NOTIFICATION_RULES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "rule_id":          {"type": "keyword"},
            "tenant_id":        {"type": "keyword"},
            "event_type":       {"type": "keyword"},
            "enabled":          {"type": "boolean"},
            "default_channels": {"type": "keyword"},
            "template_id":      {"type": "keyword"},
            "created_at":       {"type": "date"},
            "updated_at":       {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}

DEAD_LETTER_QUEUE_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "notification_id":       {"type": "keyword"},
            "original_notification": {"type": "object", "enabled": False},
            "failure_reasons":       {"type": "text"},
            "moved_at":              {"type": "date"},
            "tenant_id":             {"type": "keyword"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    }
}


def setup_notification_indices(es_service):
    """
    Create notification indices if they don't already exist.

    Creates the notifications_current, notification_preferences,
    notification_templates, and notification_rules indices with strict
    mappings. Handles serverless-incompatible settings via the
    ElasticsearchService helper.

    Args:
        es_service: An ElasticsearchService instance (uses es_service.client).

    Validates: Requirements 3.1, 3.4, 3.5, 4.1, 5.1, 7.3
    """
    from services.elasticsearch_service import ElasticsearchService

    es_client = es_service.client
    is_serverless = es_service.is_serverless

    indices = {
        NOTIFICATIONS_CURRENT_INDEX: NOTIFICATIONS_CURRENT_MAPPING,
        NOTIFICATION_PREFERENCES_INDEX: NOTIFICATION_PREFERENCES_MAPPING,
        NOTIFICATION_TEMPLATES_INDEX: NOTIFICATION_TEMPLATES_MAPPING,
        NOTIFICATION_RULES_INDEX: NOTIFICATION_RULES_MAPPING,
        DEAD_LETTER_QUEUE_INDEX: DEAD_LETTER_QUEUE_MAPPING,
    }

    for index_name, mapping in indices.items():
        try:
            if not es_client.indices.exists(index=index_name):
                if is_serverless:
                    mapping = ElasticsearchService.strip_serverless_incompatible_settings(mapping)
                es_client.indices.create(index=index_name, body=mapping)
                logger.info(f"✅ Created notification index: {index_name}")
            else:
                logger.info(f"📋 Notification index already exists: {index_name}")
        except Exception as e:
            logger.error(f"❌ Failed to create notification index {index_name}: {e}")
