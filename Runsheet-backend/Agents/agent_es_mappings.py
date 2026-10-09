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


