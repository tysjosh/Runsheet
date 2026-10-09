"""Elasticsearch index mappings for overlay agent indices.

Defines mappings for agent_signals, agent_shadow_proposals,
agent_outcomes, agent_revenue_reports, and agent_policy_experiments.

Validates: Requirements 2.6, 3.4, 6.6, 8.6, 11.4
"""
import logging

logger = logging.getLogger(__name__)

AGENT_SIGNALS_INDEX = "agent_signals"
AGENT_SHADOW_PROPOSALS_INDEX = "agent_shadow_proposals"
AGENT_OUTCOMES_INDEX = "agent_outcomes"
AGENT_REVENUE_REPORTS_INDEX = "agent_revenue_reports"
AGENT_POLICY_EXPERIMENTS_INDEX = "agent_policy_experiments"

AGENT_SIGNALS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "signal_id":      {"type": "keyword"},
            "_signal_type":   {"type": "keyword"},
            "source_agent":   {"type": "keyword"},
            "entity_id":      {"type": "keyword"},
            "entity_type":    {"type": "keyword"},
            "severity":       {"type": "keyword"},
            "confidence":     {"type": "float"},
            "ttl_seconds":    {"type": "integer"},
            "timestamp":      {"type": "date"},
            "context":        {"type": "object", "dynamic": True},
            "tenant_id":      {"type": "keyword"},
            "schema_version": {"type": "keyword"},
            # InterventionProposal fields (shared index)
            "proposal_id":        {"type": "keyword"},
            "actions":            {"type": "object", "enabled": True},
            "expected_kpi_delta": {"type": "object", "enabled": True},
            "risk_class":         {"type": "keyword"},
            "priority":           {"type": "integer"},
            # OutcomeRecord fields
            "outcome_id":           {"type": "keyword"},
            "intervention_id":      {"type": "keyword"},
            "before_kpis":          {"type": "object", "enabled": True},
            "after_kpis":           {"type": "object", "enabled": True},
            "realized_delta":       {"type": "object", "enabled": True},
            "execution_duration_ms": {"type": "float"},
            "status":               {"type": "keyword"},
            # PolicyChangeProposal fields
            "parameter":     {"type": "keyword"},
            "old_value":     {"type": "object", "enabled": True},
            "new_value":     {"type": "object", "enabled": True},
            "evidence":      {"type": "keyword"},
            "rollback_plan": {"type": "object", "enabled": True},
            # Timestamp fields (auto-injected by ElasticsearchService)
            "updated_at":    {"type": "date"},
            "created_at":    {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

AGENT_SHADOW_PROPOSALS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "proposal_id":        {"type": "keyword"},
            "source_agent":       {"type": "keyword"},
            "shadow_agent":       {"type": "keyword"},
            "shadow_timestamp":   {"type": "date"},
            "actions":            {"type": "object", "enabled": True},
            "expected_kpi_delta": {"type": "object", "enabled": True},
            "risk_class":         {"type": "keyword"},
            "confidence":         {"type": "float"},
            "priority":           {"type": "integer"},
            "tenant_id":          {"type": "keyword"},
            "timestamp":          {"type": "date"},
            "schema_version":     {"type": "keyword"},
            # PolicyChangeProposal fields (for RevenueGuard shadow)
            "parameter":     {"type": "keyword"},
            "old_value":     {"type": "object", "enabled": True},
            "new_value":     {"type": "object", "enabled": True},
            "evidence":      {"type": "keyword"},
            "rollback_plan": {"type": "object", "enabled": True},
            # Timestamp fields (auto-injected by ElasticsearchService)
            "updated_at":    {"type": "date"},
            "created_at":    {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

AGENT_OUTCOMES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "outcome_id":           {"type": "keyword"},
            "intervention_id":      {"type": "keyword"},
            "before_kpis":          {"type": "object", "enabled": True},
            "after_kpis":           {"type": "object", "enabled": True},
            "realized_delta":       {"type": "object", "enabled": True},
            "execution_duration_ms": {"type": "float"},
            "tenant_id":            {"type": "keyword"},
            "timestamp":            {"type": "date"},
            "status":               {"type": "keyword"},
            "schema_version":       {"type": "keyword"},
            "notification_ids":     {"type": "keyword"},
            "confidence_score":     {"type": "float"},
            "confidence_rationale": {"type": "keyword"},
            # Timestamp fields (auto-injected by ElasticsearchService)
            "updated_at":    {"type": "date"},
            "created_at":    {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

AGENT_REVENUE_REPORTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "report_type":               {"type": "keyword"},
            "tenant_id":                 {"type": "keyword"},
            "period_start":              {"type": "date"},
            "period_end":                {"type": "date"},
            "total_routes_analyzed":     {"type": "integer"},
            "leakage_patterns_detected": {"type": "integer"},
            "generated_at":              {"type": "date"},
            # Timestamp fields (auto-injected by ElasticsearchService)
            "updated_at":    {"type": "date"},
            "created_at":    {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

AGENT_POLICY_EXPERIMENTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "experiment_id":  {"type": "keyword"},
            "proposal_id":    {"type": "keyword"},
            "parameter":      {"type": "keyword"},
            "old_value":      {"type": "object", "enabled": True},
            "new_value":      {"type": "object", "enabled": True},
            "rollout_pct":    {"type": "integer"},
            "status":         {"type": "keyword"},
            "tenant_id":      {"type": "keyword"},
            "created_at":     {"type": "date"},
            "updated_at":     {"type": "date"},
            "rollback_reason": {"type": "text"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

JOB_PRIORITIES_INDEX = "job_priorities"

JOB_PRIORITIES_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "priority_list_id": {"type": "keyword"},
            "priorities": {
                "type": "nested",
                "properties": {
                    "job_id":               {"type": "keyword"},
                    "job_type":             {"type": "keyword"},
                    "priority_score":       {"type": "float"},
                    "priority_bucket":      {"type": "keyword"},
                    "sla_urgency":          {"type": "float"},
                    "cargo_priority_score": {"type": "float"},
                    "customer_tier_score":  {"type": "float"},
                    "reasons":              {"type": "keyword"},
                },
            },
            "scoring_weights": {"type": "flattened"},
            "tenant_id":       {"type": "keyword"},
            "timestamp":       {"type": "date"},
            # index_document auto-stamps created_at/updated_at on every write;
            # a strict mapping without them rejects all priority-list persists.
            "created_at":      {"type": "date"},
            "updated_at":      {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}


# Module-level registry: index_name -> mapping. Single source of truth for
# both setup_overlay_indices and the seed script's recreate path.
OVERLAY_INDEX_MAPPINGS = {
    AGENT_SIGNALS_INDEX: AGENT_SIGNALS_MAPPING,
    AGENT_SHADOW_PROPOSALS_INDEX: AGENT_SHADOW_PROPOSALS_MAPPING,
    AGENT_OUTCOMES_INDEX: AGENT_OUTCOMES_MAPPING,
    AGENT_REVENUE_REPORTS_INDEX: AGENT_REVENUE_REPORTS_MAPPING,
    AGENT_POLICY_EXPERIMENTS_INDEX: AGENT_POLICY_EXPERIMENTS_MAPPING,
    JOB_PRIORITIES_INDEX: JOB_PRIORITIES_MAPPING,
}


