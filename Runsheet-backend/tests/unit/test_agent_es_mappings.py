"""
Unit tests for agent Elasticsearch index mappings, ILM policy, and setup helper.

Validates: Requirements 2.1, 8.1, 8.6, 11.1, 12.3
"""

import sys
from unittest.mock import MagicMock, PropertyMock, patch

from Agents.agent_es_mappings import AGENT_ACTIVITY_LOG_ILM_POLICY, AGENT_ACTIVITY_LOG_ILM_POLICY_NAME, AGENT_ACTIVITY_LOG_INDEX, AGENT_ACTIVITY_LOG_MAPPING, AGENT_APPROVAL_QUEUE_INDEX, AGENT_APPROVAL_QUEUE_MAPPING, AGENT_FEEDBACK_INDEX, AGENT_FEEDBACK_MAPPING, AGENT_MEMORY_INDEX, AGENT_MEMORY_MAPPING


# ---------------------------------------------------------------------------
# agent_approval_queue mapping tests
# ---------------------------------------------------------------------------

class TestAgentApprovalQueueMapping:
    """Tests for the AGENT_APPROVAL_QUEUE_MAPPING structure."""

    def test_mapping_is_strict(self):
        assert AGENT_APPROVAL_QUEUE_MAPPING["mappings"]["dynamic"] == "strict"

    def test_keyword_fields(self):
        props = AGENT_APPROVAL_QUEUE_MAPPING["mappings"]["properties"]
        keyword_fields = [
            "action_id", "action_type", "tool_name", "risk_level",
            "proposed_by", "status", "reviewed_by", "tenant_id",
        ]
        for field in keyword_fields:
            assert props[field]["type"] == "keyword", f"{field} should be keyword"

    def test_date_fields(self):
        props = AGENT_APPROVAL_QUEUE_MAPPING["mappings"]["properties"]
        for field in ["proposed_at", "reviewed_at", "expiry_time"]:
            assert props[field]["type"] == "date", f"{field} should be date"

    def test_text_fields(self):
        props = AGENT_APPROVAL_QUEUE_MAPPING["mappings"]["properties"]
        assert props["impact_summary"]["type"] == "text"

    def test_object_fields(self):
        props = AGENT_APPROVAL_QUEUE_MAPPING["mappings"]["properties"]
        for field in ["parameters", "execution_result"]:
            assert props[field]["type"] == "object", f"{field} should be object"
            assert props[field]["enabled"] is True, f"{field} should be enabled"

    def test_shard_settings(self):
        settings = AGENT_APPROVAL_QUEUE_MAPPING["settings"]
        assert settings["number_of_shards"] == 1
        assert settings["number_of_replicas"] == 1

    def test_all_design_fields_present(self):
        """Verify every field from the design document is present."""
        props = AGENT_APPROVAL_QUEUE_MAPPING["mappings"]["properties"]
        expected_fields = {
            "action_id", "action_type", "tool_name", "parameters",
            "risk_level", "proposed_by", "proposed_at", "status",
            "reviewed_by", "reviewed_at", "expiry_time",
            "impact_summary", "execution_result", "rejection_reason",
            "tenant_id", "created_at", "updated_at",
        }
        assert set(props.keys()) == expected_fields


# ---------------------------------------------------------------------------
# agent_activity_log mapping tests
# ---------------------------------------------------------------------------

class TestAgentActivityLogMapping:
    """Tests for the AGENT_ACTIVITY_LOG_MAPPING structure."""

    def test_mapping_is_strict(self):
        assert AGENT_ACTIVITY_LOG_MAPPING["mappings"]["dynamic"] == "strict"

    def test_keyword_fields(self):
        props = AGENT_ACTIVITY_LOG_MAPPING["mappings"]["properties"]
        keyword_fields = [
            "log_id", "agent_id", "action_type", "tool_name",
            "risk_level", "outcome", "tenant_id", "user_id", "session_id",
        ]
        for field in keyword_fields:
            assert props[field]["type"] == "keyword", f"{field} should be keyword"

    def test_float_fields(self):
        props = AGENT_ACTIVITY_LOG_MAPPING["mappings"]["properties"]
        assert props["duration_ms"]["type"] == "float"

    def test_date_fields(self):
        props = AGENT_ACTIVITY_LOG_MAPPING["mappings"]["properties"]
        assert props["timestamp"]["type"] == "date"

    def test_object_fields(self):
        props = AGENT_ACTIVITY_LOG_MAPPING["mappings"]["properties"]
        for field in ["parameters", "details"]:
            assert props[field]["type"] == "object", f"{field} should be object"
            assert props[field]["enabled"] is True, f"{field} should be enabled"

    def test_all_design_fields_present(self):
        props = AGENT_ACTIVITY_LOG_MAPPING["mappings"]["properties"]
        expected_fields = {
            "log_id", "agent_id", "action_type", "tool_name",
            "parameters", "risk_level", "outcome", "duration_ms",
            "tenant_id", "user_id", "session_id", "timestamp", "details",
            "created_at", "updated_at",
        }
        assert set(props.keys()) == expected_fields


# ---------------------------------------------------------------------------
# agent_memory mapping tests
# ---------------------------------------------------------------------------

class TestAgentMemoryMapping:
    """Tests for the AGENT_MEMORY_MAPPING structure."""

    def test_mapping_is_strict(self):
        assert AGENT_MEMORY_MAPPING["mappings"]["dynamic"] == "strict"

    def test_keyword_fields(self):
        props = AGENT_MEMORY_MAPPING["mappings"]["properties"]
        keyword_fields = [
            "memory_id", "memory_type", "agent_id", "tenant_id", "tags",
        ]
        for field in keyword_fields:
            assert props[field]["type"] == "keyword", f"{field} should be keyword"

    def test_text_fields(self):
        props = AGENT_MEMORY_MAPPING["mappings"]["properties"]
        assert props["content"]["type"] == "text"

    def test_float_fields(self):
        props = AGENT_MEMORY_MAPPING["mappings"]["properties"]
        assert props["confidence_score"]["type"] == "float"

    def test_integer_fields(self):
        props = AGENT_MEMORY_MAPPING["mappings"]["properties"]
        assert props["access_count"]["type"] == "integer"

    def test_date_fields(self):
        props = AGENT_MEMORY_MAPPING["mappings"]["properties"]
        for field in ["created_at", "last_accessed"]:
            assert props[field]["type"] == "date", f"{field} should be date"

    def test_all_design_fields_present(self):
        props = AGENT_MEMORY_MAPPING["mappings"]["properties"]
        expected_fields = {
            "memory_id", "memory_type", "agent_id", "tenant_id",
            "content", "confidence_score", "created_at",
            "last_accessed", "access_count", "tags",
            "updated_at",
        }
        assert set(props.keys()) == expected_fields


# ---------------------------------------------------------------------------
# agent_feedback mapping tests
# ---------------------------------------------------------------------------

class TestAgentFeedbackMapping:
    """Tests for the AGENT_FEEDBACK_MAPPING structure."""

    def test_mapping_is_strict(self):
        assert AGENT_FEEDBACK_MAPPING["mappings"]["dynamic"] == "strict"

    def test_keyword_fields(self):
        props = AGENT_FEEDBACK_MAPPING["mappings"]["properties"]
        keyword_fields = [
            "feedback_id", "agent_id", "action_type",
            "feedback_type", "tenant_id", "user_id",
        ]
        for field in keyword_fields:
            assert props[field]["type"] == "keyword", f"{field} should be keyword"

    def test_date_fields(self):
        props = AGENT_FEEDBACK_MAPPING["mappings"]["properties"]
        assert props["timestamp"]["type"] == "date"

    def test_object_fields(self):
        props = AGENT_FEEDBACK_MAPPING["mappings"]["properties"]
        for field in ["original_proposal", "user_action", "context"]:
            assert props[field]["type"] == "object", f"{field} should be object"
            assert props[field]["enabled"] is True, f"{field} should be enabled"

    def test_all_design_fields_present(self):
        props = AGENT_FEEDBACK_MAPPING["mappings"]["properties"]
        expected_fields = {
            "feedback_id", "agent_id", "action_type",
            "original_proposal", "user_action", "feedback_type",
            "tenant_id", "user_id", "timestamp", "context",
            "created_at", "updated_at",
        }
        assert set(props.keys()) == expected_fields


# ---------------------------------------------------------------------------
# ILM policy tests
# ---------------------------------------------------------------------------

class TestAgentActivityLogILMPolicy:
    """Tests for the AGENT_ACTIVITY_LOG_ILM_POLICY structure."""

    def test_has_all_phases(self):
        phases = AGENT_ACTIVITY_LOG_ILM_POLICY["policy"]["phases"]
        assert set(phases.keys()) == {"hot", "warm", "cold", "delete"}

    def test_warm_phase_after_30_days(self):
        warm = AGENT_ACTIVITY_LOG_ILM_POLICY["policy"]["phases"]["warm"]
        assert warm["min_age"] == "30d"

    def test_cold_phase_after_90_days(self):
        cold = AGENT_ACTIVITY_LOG_ILM_POLICY["policy"]["phases"]["cold"]
        assert cold["min_age"] == "90d"

    def test_delete_phase_after_365_days(self):
        delete = AGENT_ACTIVITY_LOG_ILM_POLICY["policy"]["phases"]["delete"]
        assert delete["min_age"] == "365d"
        assert "delete" in delete["actions"]


# ---------------------------------------------------------------------------
# setup_agent_indices tests
# ---------------------------------------------------------------------------

class TestSetupAgentIndices:
    """Tests for the setup_agent_indices helper function."""









