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
            # Storm_Mode metadata (Task 10.9, Req 9.2.6): set when the
            # notification used the severe-weather template variant.
            "storm_mode_active":   {"type": "boolean"},
            "weather_alert_ref":   {
                "type": "object",
                "properties": {
                    "alert_id":          {"type": "keyword"},
                    "alert_type":        {"type": "keyword"},
                    "severity":          {"type": "keyword"},
                    "headline":          {"type": "text"},
                    "source":            {"type": "keyword"},
                    "region_code":       {"type": "keyword"},
                    "expected_start_at": {"type": "date"},
                    "expected_end_at":   {"type": "date"},
                    "affected_zip_codes": {"type": "keyword"},
                },
            },
            "storm_variant_reason": {"type": "keyword"},
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
            "template_opt_outs": {"type": "keyword"},
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
            # Fuel notification rules (seed_data.seed_fuel_notification_rules)
            # carry these extra fields wiring a template_key to its trigger.
            "template_key":      {"type": "keyword"},
            "trigger_condition": {"type": "keyword"},
            "description":       {"type": "text"},
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


def _reconcile_notification_index_mapping(es_client, index_name, mapping) -> None:
    """Additively put any mapping fields missing from a live notification index.

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
            logger.info(f"📋 Notification index already up to date: {index_name}")
            return
        es_client.indices.put_mapping(
            index=index_name, body={"properties": missing}
        )
        logger.info(
            "✅ Reconciled notification index %s — added fields: %s",
            index_name,
            ", ".join(sorted(missing)),
        )
    except Exception:
        logger.exception(
            "Failed to reconcile notification index mapping for %s", index_name
        )


