"""
Unit tests for fuel Elasticsearch index mappings, ILM policy, and setup helper.

Validates: Requirements 8.2, 8.3, 8.4
"""

from unittest.mock import MagicMock, call

from fuel.services.fuel_es_mappings import FUEL_EVENTS_ILM_POLICY, FUEL_EVENTS_ILM_POLICY_NAME, FUEL_EVENTS_INDEX, FUEL_EVENTS_MAPPING, FUEL_STATIONS_INDEX, FUEL_STATIONS_MAPPING


class TestFuelEventsMapping:
    """Tests for the FUEL_EVENTS_MAPPING structure."""

    def test_mapping_is_strict(self):
        assert FUEL_EVENTS_MAPPING["mappings"]["dynamic"] == "strict"

    def test_keyword_fields(self):
        props = FUEL_EVENTS_MAPPING["mappings"]["properties"]
        keyword_fields = [
            "event_id", "station_id", "event_type", "fuel_type",
            "asset_id", "operator_id", "supplier", "delivery_reference",
            "tenant_id",
        ]
        for field in keyword_fields:
            assert props[field]["type"] == "keyword", f"{field} should be keyword"

    def test_float_fields(self):
        props = FUEL_EVENTS_MAPPING["mappings"]["properties"]
        for field in ["quantity_liters", "odometer_reading"]:
            assert props[field]["type"] == "float", f"{field} should be float"

    def test_date_fields(self):
        props = FUEL_EVENTS_MAPPING["mappings"]["properties"]
        for field in ["event_timestamp", "ingested_at"]:
            assert props[field]["type"] == "date", f"{field} should be date"

    def test_shard_settings(self):
        settings = FUEL_EVENTS_MAPPING["settings"]
        assert settings["number_of_shards"] == 1
        assert settings["number_of_replicas"] == 1


class TestFuelEventsILMPolicy:
    """Tests for the FUEL_EVENTS_ILM_POLICY structure."""

    def test_has_all_phases(self):
        phases = FUEL_EVENTS_ILM_POLICY["policy"]["phases"]
        assert set(phases.keys()) == {"hot", "warm", "cold", "delete"}

    def test_warm_phase_after_30_days(self):
        warm = FUEL_EVENTS_ILM_POLICY["policy"]["phases"]["warm"]
        assert warm["min_age"] == "30d"

    def test_cold_phase_after_90_days(self):
        cold = FUEL_EVENTS_ILM_POLICY["policy"]["phases"]["cold"]
        assert cold["min_age"] == "90d"

    def test_delete_phase_after_365_days(self):
        delete = FUEL_EVENTS_ILM_POLICY["policy"]["phases"]["delete"]
        assert delete["min_age"] == "365d"
        assert "delete" in delete["actions"]


class TestSetupFuelIndices:
    """Tests for the setup_fuel_indices helper function."""

    def _make_es_client(self, existing_indices=None):
        existing = existing_indices or set()
        client = MagicMock()
        client.indices.exists.side_effect = lambda index: index in existing
        return client





