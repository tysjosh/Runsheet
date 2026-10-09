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


