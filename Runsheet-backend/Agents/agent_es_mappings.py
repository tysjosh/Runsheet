"""Elasticsearch index mappings for the Agentic AI indices.

Defines mappings for agent_approval_queue, agent_activity_log,
agent_memory, and agent_feedback indices, plus the ILM policy
for agent_activity_log.

Validates: Requirements 2.1, 8.1, 8.6, 11.1, 12.3
"""

import logging

logger = logging.getLogger(__name__)

AGENT_APPROVAL_QUEUE_INDEX = "agent_approval_queue"
AGENT_ACTIVITY_LOG_INDEX = "agent_activity_log"
AGENT_MEMORY_INDEX = "agent_memory"
AGENT_FEEDBACK_INDEX = "agent_feedback"
AGENT_ACTIVITY_LOG_ILM_POLICY_NAME = "agent-activity-log-policy"

AGENT_APPROVAL_QUEUE_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "action_id":        {"type": "keyword"},
            "action_type":      {"type": "keyword"},
            "tool_name":        {"type": "keyword"},
            "parameters":       {"type": "object", "dynamic": True, "enabled": True},
            "risk_level":       {"type": "keyword"},
            "proposed_by":      {"type": "keyword"},
            "proposed_at":      {"type": "date"},
            "status":           {"type": "keyword"},
            "reviewed_by":      {"type": "keyword"},
            "reviewed_at":      {"type": "date"},
            "expiry_time":      {"type": "date"},
            "impact_summary":   {"type": "text"},
            "execution_result": {"type": "object", "dynamic": True, "enabled": True},
            "rejection_reason": {"type": "text"},
            "tenant_id":        {"type": "keyword"},
            "created_at":       {"type": "date"},
            "updated_at":       {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

AGENT_ACTIVITY_LOG_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "log_id":      {"type": "keyword"},
            "agent_id":    {"type": "keyword"},
            "action_type": {"type": "keyword"},
            "tool_name":   {"type": "keyword"},
            "parameters":  {"type": "object", "dynamic": True, "enabled": True},
            "risk_level":  {"type": "keyword"},
            "outcome":     {"type": "keyword"},
            "duration_ms": {"type": "float"},
            "tenant_id":   {"type": "keyword"},
            "user_id":     {"type": "keyword"},
            "session_id":  {"type": "keyword"},
            "timestamp":   {"type": "date"},
            "created_at":  {"type": "date"},
            "updated_at":  {"type": "date"},
            "details":     {"type": "object", "dynamic": True, "enabled": True},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

AGENT_MEMORY_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "memory_id":        {"type": "keyword"},
            "memory_type":      {"type": "keyword"},
            "agent_id":         {"type": "keyword"},
            "tenant_id":        {"type": "keyword"},
            "content":          {"type": "text"},
            "confidence_score": {"type": "float"},
            "created_at":       {"type": "date"},
            "last_accessed":    {"type": "date"},
            "access_count":     {"type": "integer"},
            "tags":             {"type": "keyword"},
            "updated_at":       {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

AGENT_FEEDBACK_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "feedback_id":       {"type": "keyword"},
            "agent_id":          {"type": "keyword"},
            "action_type":       {"type": "keyword"},
            "original_proposal": {"type": "object", "enabled": True},
            "user_action":       {"type": "object", "enabled": True},
            "feedback_type":     {"type": "keyword"},
            "tenant_id":         {"type": "keyword"},
            "user_id":           {"type": "keyword"},
            "timestamp":         {"type": "date"},
            "context":           {"type": "object", "enabled": True},
            "created_at":        {"type": "date"},
            "updated_at":        {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

AGENT_ACTIVITY_LOG_ILM_POLICY = {
    "policy": {
        "phases": {
            "hot": {
                "actions": {},
            },
            "warm": {
                "min_age": "30d",
                "actions": {},
            },
            "cold": {
                "min_age": "90d",
                "actions": {},
            },
            "delete": {
                "min_age": "365d",
                "actions": {"delete": {}},
            },
        }
    }
}


def setup_agent_indices(es_service):
    """Create agent indices and apply the ILM policy for agent_activity_log.

    Creates the agent_approval_queue, agent_activity_log, agent_memory, and
    agent_feedback indices with strict mappings if they don't already exist,
    then sets up and applies the ILM policy for agent_activity_log.

    Args:
        es_service: An ElasticsearchService instance (uses es_service.client).

    Validates: Requirements 2.1, 8.1, 8.6, 11.1, 12.3
    """
    from services.elasticsearch_service import ElasticsearchService

    es_client = es_service.client
    is_serverless = es_service.is_serverless

    indices = {
        AGENT_APPROVAL_QUEUE_INDEX: AGENT_APPROVAL_QUEUE_MAPPING,
        AGENT_ACTIVITY_LOG_INDEX: AGENT_ACTIVITY_LOG_MAPPING,
        AGENT_MEMORY_INDEX: AGENT_MEMORY_MAPPING,
        AGENT_FEEDBACK_INDEX: AGENT_FEEDBACK_MAPPING,
    }

    for index_name, mapping in indices.items():
        try:
            if not es_client.indices.exists(index=index_name):
                if is_serverless:
                    mapping = ElasticsearchService.strip_serverless_incompatible_settings(mapping)
                es_client.indices.create(index=index_name, body=mapping)
                logger.info(f"✅ Created agent index: {index_name}")
            else:
                logger.info(f"📋 Agent index already exists: {index_name}")
                # Update mapping with any new fields (existing fields are unchanged)
                try:
                    current = es_client.indices.get_mapping(index=index_name)
                    current_props = (
                        current.get(index_name, {})
                        .get("mappings", {})
                        .get("properties", {})
                    )
                    expected_props = mapping.get("mappings", {}).get("properties", {})
                    missing = {k: v for k, v in expected_props.items() if k not in current_props}
                    if missing:
                        es_client.indices.put_mapping(
                            index=index_name,
                            body={"properties": missing},
                        )
                        logger.info(
                            f"📝 Updated agent index '{index_name}' with new field(s): "
                            f"{list(missing.keys())}"
                        )
                except Exception as e:
                    logger.warning(f"⚠️ Failed to update mapping for agent index '{index_name}': {e}")
        except Exception as e:
            logger.error(f"❌ Failed to create agent index {index_name}: {e}")

    # Apply ILM policy for agent_activity_log
    try:
        es_client.ilm.put_lifecycle(
            name=AGENT_ACTIVITY_LOG_ILM_POLICY_NAME,
            body=AGENT_ACTIVITY_LOG_ILM_POLICY,
        )
        logger.info(
            f"✅ Created/updated ILM policy: {AGENT_ACTIVITY_LOG_ILM_POLICY_NAME}"
        )
    except Exception as e:
        logger.warning(
            f"⚠️ Failed to create/update ILM policy "
            f"{AGENT_ACTIVITY_LOG_ILM_POLICY_NAME}: {e}"
        )
        return

    # Apply the ILM policy to the agent_activity_log index
    try:
        if es_client.indices.exists(index=AGENT_ACTIVITY_LOG_INDEX):
            es_client.indices.put_settings(
                index=AGENT_ACTIVITY_LOG_INDEX,
                body={
                    "index": {
                        "lifecycle": {
                            "name": AGENT_ACTIVITY_LOG_ILM_POLICY_NAME,
                        }
                    }
                },
            )
            logger.info(
                f"✅ Applied ILM policy '{AGENT_ACTIVITY_LOG_ILM_POLICY_NAME}' "
                f"to index '{AGENT_ACTIVITY_LOG_INDEX}'"
            )
    except Exception as e:
        logger.warning(
            f"⚠️ Failed to apply ILM policy to {AGENT_ACTIVITY_LOG_INDEX}: {e}"
        )
