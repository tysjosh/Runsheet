"""
Unit tests for agent Elasticsearch index mappings, ILM policy, and setup helper.

Validates: Requirements 2.1, 8.1, 8.6, 11.1, 12.3
"""

import sys
from unittest.mock import MagicMock, PropertyMock, patch

from Agents.agent_es_mappings import (
    AGENT_ACTIVITY_LOG_ILM_POLICY,
    AGENT_ACTIVITY_LOG_ILM_POLICY_NAME,
    AGENT_ACTIVITY_LOG_INDEX,
    AGENT_ACTIVITY_LOG_MAPPING,
    AGENT_APPROVAL_QUEUE_INDEX,
    AGENT_APPROVAL_QUEUE_MAPPING,
    AGENT_FEEDBACK_INDEX,
    AGENT_FEEDBACK_MAPPING,
    AGENT_MEMORY_INDEX,
    AGENT_MEMORY_MAPPING,
    setup_agent_indices,
)


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
            "impact_summary", "execution_result", "tenant_id",
            "created_at", "updated_at",
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

    def _make_es_service(self, existing_indices=None):
        """Create a mock ElasticsearchService with a mock client."""
        existing = existing_indices or set()
        es_service = MagicMock()
        client = MagicMock()
        client.indices.exists.side_effect = lambda index: index in existing
        es_service.client = client
        type(es_service).is_serverless = PropertyMock(return_value=False)
        return es_service

    def _patch_es_module(self):
        """Return a patch context manager that stubs the ES service module.

        The ``setup_agent_indices`` function does a deferred
        ``from services.elasticsearch_service import ElasticsearchService``
        which triggers a real ES connection at module level.  We inject a
        fake module into ``sys.modules`` so the import succeeds without
        network access.
        """
        fake_module = MagicMock()
        fake_module.ElasticsearchService = MagicMock()
        fake_module.ElasticsearchService.strip_serverless_incompatible_settings = (
            lambda mapping: mapping
        )
        return patch.dict(sys.modules, {"services.elasticsearch_service": fake_module})

    def test_creates_all_four_indices_when_missing(self):
        es_service = self._make_es_service()
        with self._patch_es_module():
            setup_agent_indices(es_service)

        create_calls = es_service.client.indices.create.call_args_list
        created_indices = {c.kwargs["index"] for c in create_calls}
        assert AGENT_APPROVAL_QUEUE_INDEX in created_indices
        assert AGENT_ACTIVITY_LOG_INDEX in created_indices
        assert AGENT_MEMORY_INDEX in created_indices
        assert AGENT_FEEDBACK_INDEX in created_indices

    def test_skips_existing_indices(self):
        existing = {
            AGENT_APPROVAL_QUEUE_INDEX,
            AGENT_ACTIVITY_LOG_INDEX,
            AGENT_MEMORY_INDEX,
            AGENT_FEEDBACK_INDEX,
        }
        es_service = self._make_es_service(existing_indices=existing)
        with self._patch_es_module():
            setup_agent_indices(es_service)
        es_service.client.indices.create.assert_not_called()

    def test_creates_ilm_policy(self):
        es_service = self._make_es_service()
        with self._patch_es_module():
            setup_agent_indices(es_service)

        es_service.client.ilm.put_lifecycle.assert_called_once_with(
            name=AGENT_ACTIVITY_LOG_ILM_POLICY_NAME,
            body=AGENT_ACTIVITY_LOG_ILM_POLICY,
        )

    def test_applies_ilm_policy_to_activity_log_index(self):
        es_service = self._make_es_service()
        # After creation the index exists for the put_settings call
        es_service.client.indices.exists.side_effect = None
        es_service.client.indices.exists.return_value = True

        with self._patch_es_module():
            setup_agent_indices(es_service)

        es_service.client.indices.put_settings.assert_called_once_with(
            index=AGENT_ACTIVITY_LOG_INDEX,
            body={
                "index": {
                    "lifecycle": {
                        "name": AGENT_ACTIVITY_LOG_ILM_POLICY_NAME,
                    }
                }
            },
        )

    def test_ilm_failure_does_not_raise(self):
        es_service = self._make_es_service()
        es_service.client.ilm.put_lifecycle.side_effect = Exception("ILM unavailable")

        with self._patch_es_module():
            # Should not raise
            setup_agent_indices(es_service)

    def test_ilm_failure_skips_put_settings(self):
        """When ILM policy creation fails, put_settings should not be called."""
        es_service = self._make_es_service()
        es_service.client.ilm.put_lifecycle.side_effect = Exception("ILM unavailable")

        with self._patch_es_module():
            setup_agent_indices(es_service)

        es_service.client.indices.put_settings.assert_not_called()

    def test_index_creation_failure_does_not_stop_others(self):
        """A failure creating one index should not prevent creating the rest."""
        es_service = self._make_es_service()

        def create_side_effect(**kwargs):
            if kwargs.get("index") == AGENT_APPROVAL_QUEUE_INDEX:
                raise Exception("creation failed")

        es_service.client.indices.create.side_effect = create_side_effect

        with self._patch_es_module():
            setup_agent_indices(es_service)

        # All four indices should have been attempted
        assert es_service.client.indices.create.call_count == 4
