"""
Unit tests for fuel Elasticsearch index mappings, ILM policy, and setup helper.

Validates: Requirements 8.2, 8.3, 8.4
"""

from unittest.mock import MagicMock, call

from fuel.services.fuel_es_mappings import (
    FUEL_EVENTS_ILM_POLICY,
    FUEL_EVENTS_ILM_POLICY_NAME,
    FUEL_EVENTS_INDEX,
    FUEL_EVENTS_MAPPING,
    FUEL_STATIONS_INDEX,
    FUEL_STATIONS_MAPPING,
    setup_fuel_indices,
)


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

    def test_creates_both_indices_when_missing(self):
        client = self._make_es_client()
        setup_fuel_indices(client)

        create_calls = client.indices.create.call_args_list
        created_indices = {c.kwargs["index"] for c in create_calls}
        assert FUEL_STATIONS_INDEX in created_indices
        assert FUEL_EVENTS_INDEX in created_indices

    def test_skips_existing_indices(self):
        client = self._make_es_client(
            existing_indices={FUEL_STATIONS_INDEX, FUEL_EVENTS_INDEX}
        )
        setup_fuel_indices(client)
        client.indices.create.assert_not_called()

    def test_creates_ilm_policy(self):
        client = self._make_es_client()
        setup_fuel_indices(client)

        client.ilm.put_lifecycle.assert_called_once_with(
            name=FUEL_EVENTS_ILM_POLICY_NAME,
            body=FUEL_EVENTS_ILM_POLICY,
        )

    def test_applies_ilm_policy_to_fuel_events_index(self):
        client = self._make_es_client()
        # After creation, the index exists for the put_settings call
        client.indices.exists.side_effect = (
            lambda index: index == FUEL_EVENTS_INDEX
            if client.indices.create.called
            else False
        )
        # Simplify: just make exists return True after create
        client.indices.exists.side_effect = None
        client.indices.exists.return_value = True

        setup_fuel_indices(client)

        client.indices.put_settings.assert_called_once_with(
            index=FUEL_EVENTS_INDEX,
            body={
                "index": {
                    "lifecycle": {
                        "name": FUEL_EVENTS_ILM_POLICY_NAME,
                    }
                }
            },
        )

    def test_ilm_failure_does_not_raise(self):
        client = self._make_es_client()
        client.ilm.put_lifecycle.side_effect = Exception("ILM unavailable")

        # Should not raise
        setup_fuel_indices(client)
