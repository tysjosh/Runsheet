"""
Checkpoint 3: Verify Elasticsearch indices for Logistics Scheduling.

Definition of Done:
  - Both `jobs_current` and `job_events` indices created on startup with strict mappings
  - Indexing a document with an unmapped field is rejected (strict mapping)
  - ILM policy attached to `job_events`
  - No ERROR-level log entries during index creation

Validates: Requirements 1.1, 1.2, 1.5, 1.6
"""

import logging
import sys
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Patch the ElasticsearchService singleton BEFORE any scheduling imports so
# that importing services.elasticsearch_service doesn't trigger a real ES
# connection.
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_class = MagicMock()
_mock_es_module.ElasticsearchService = _mock_es_class
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from scheduling.services.scheduling_es_mappings import JOB_EVENTS_ILM_POLICY, JOB_EVENTS_ILM_POLICY_NAME, JOB_EVENTS_INDEX, JOB_EVENTS_MAPPING, JOBS_CURRENT_INDEX, JOBS_CURRENT_MAPPING, TENANT_JOB_POLICIES_INDEX, TENANT_JOB_POLICIES_MAPPING


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# 1. Both indices created on startup with strict mappings
# ---------------------------------------------------------------------------

class TestIndicesCreatedOnStartup:
    """Verify that setup_scheduling_indices creates both indices."""






# ---------------------------------------------------------------------------
# 2. Strict mapping enforcement (unmapped fields rejected)
# ---------------------------------------------------------------------------

class TestStrictMappingEnforcement:
    """Verify that both index mappings use dynamic: strict."""

    def test_jobs_current_mapping_is_strict(self):
        assert JOBS_CURRENT_MAPPING["mappings"]["dynamic"] == "strict"

    def test_job_events_mapping_is_strict(self):
        assert JOB_EVENTS_MAPPING["mappings"]["dynamic"] == "strict"

    def test_jobs_current_has_required_keyword_fields(self):
        props = JOBS_CURRENT_MAPPING["mappings"]["properties"]
        for field in [
            "job_id", "job_type", "status", "tenant_id",
            "asset_assigned", "created_by", "priority",
        ]:
            assert props[field]["type"] == "keyword", f"{field} should be keyword"

    def test_jobs_current_has_date_fields(self):
        props = JOBS_CURRENT_MAPPING["mappings"]["properties"]
        for field in [
            "scheduled_time", "estimated_arrival", "started_at",
            "completed_at", "created_at", "updated_at",
        ]:
            assert props[field]["type"] == "date", f"{field} should be date"

    def test_jobs_current_has_geo_point_fields(self):
        props = JOBS_CURRENT_MAPPING["mappings"]["properties"]
        assert props["origin_location"]["type"] == "geo_point"
        assert props["destination_location"]["type"] == "geo_point"

    def test_jobs_current_has_boolean_delayed_field(self):
        props = JOBS_CURRENT_MAPPING["mappings"]["properties"]
        assert props["delayed"]["type"] == "boolean"

    def test_jobs_current_has_integer_delay_duration(self):
        props = JOBS_CURRENT_MAPPING["mappings"]["properties"]
        assert props["delay_duration_minutes"]["type"] == "integer"

    def test_jobs_current_has_nested_cargo_manifest(self):
        props = JOBS_CURRENT_MAPPING["mappings"]["properties"]
        cargo = props["cargo_manifest"]
        assert cargo["type"] == "nested"
        cargo_props = cargo["properties"]
        assert "item_id" in cargo_props
        assert "description" in cargo_props
        assert "weight_kg" in cargo_props
        assert "container_number" in cargo_props
        assert "seal_number" in cargo_props
        assert "item_status" in cargo_props

    def test_job_events_has_required_keyword_fields(self):
        props = JOB_EVENTS_MAPPING["mappings"]["properties"]
        for field in ["event_id", "job_id", "event_type", "tenant_id", "actor_id"]:
            assert props[field]["type"] == "keyword", f"{field} should be keyword"

    def test_job_events_has_date_field(self):
        props = JOB_EVENTS_MAPPING["mappings"]["properties"]
        assert props["event_timestamp"]["type"] == "date"

    def test_job_events_payload_not_indexed(self):
        props = JOB_EVENTS_MAPPING["mappings"]["properties"]
        assert props["event_payload"]["type"] == "object"
        assert props["event_payload"]["enabled"] is False

    def test_both_indices_have_shard_settings(self):
        for mapping in (JOBS_CURRENT_MAPPING, JOB_EVENTS_MAPPING):
            settings = mapping["settings"]
            assert settings["number_of_shards"] == 1
            assert settings["number_of_replicas"] == 1


# ---------------------------------------------------------------------------
# 3. ILM policy attached to job_events
# ---------------------------------------------------------------------------

class TestILMPolicyAttachedToJobEvents:
    """Verify ILM policy creation and attachment to job_events."""

    def test_ilm_policy_has_all_phases(self):
        phases = JOB_EVENTS_ILM_POLICY["policy"]["phases"]
        assert set(phases.keys()) == {"hot", "warm", "cold", "delete"}

    def test_warm_phase_after_30_days(self):
        warm = JOB_EVENTS_ILM_POLICY["policy"]["phases"]["warm"]
        assert warm["min_age"] == "30d"

    def test_cold_phase_after_90_days(self):
        cold = JOB_EVENTS_ILM_POLICY["policy"]["phases"]["cold"]
        assert cold["min_age"] == "90d"

    def test_delete_phase_after_365_days(self):
        delete = JOB_EVENTS_ILM_POLICY["policy"]["phases"]["delete"]
        assert delete["min_age"] == "365d"
        assert "delete" in delete["actions"]





# ---------------------------------------------------------------------------
# 4. No ERROR-level log entries during index creation
# ---------------------------------------------------------------------------

class TestNoErrorLogsDuringSetup:
    """Verify that a clean setup produces no ERROR-level log entries."""


