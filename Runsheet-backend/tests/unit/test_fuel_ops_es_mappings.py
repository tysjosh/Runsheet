"""
Unit tests for fuel_ops_es_mappings — strict mappings + setup function.

Validates that every new fuel-ops index mapping is strict, carries a tenant_id
keyword field, uses the default 1-shard/1-replica settings, and that
``setup_fuel_ops_indices`` creates missing indices while skipping existing ones.

Requirements: 1.1, 1.2, 2.2, 3.2, 4.1, 4.3, 4.4, 5.1, 7.1, 8.1, 8.2, 8.3, 8.4,
9.1, 9.3, 10.1
"""
import sys
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from fuel.services.fuel_ops_es_mappings import (
    ATG_READINGS_INDEX,
    BILL_OF_LADING_INDEX,
    COMPARTMENT_CLEANING_EVENTS_INDEX,
    CROSS_CONTAMINATION_EVENTS_INDEX,
    CUSTOMER_TANKS_INDEX,
    CUSTOMER_TANKS_MAPPING,
    DEPOTS_INDEX,
    DEPOTS_MAPPING,
    FUEL_OPS_INDEX_MAPPINGS,
    INTEGRATION_INSTANCES_INDEX,
    INTEGRATION_SYNC_RUNS_INDEX,
    METER_TICKET_OCR_RESULTS_INDEX,
    MVP_COMBINABLE_GROUPS_INDEX,
    MVP_RECONCILIATION_INDEX,
    RACK_PRICES_INDEX,
    SOURCING_RECOMMENDATIONS_INDEX,
    STORM_MODE_OVERRIDES_INDEX,
    STORM_ROAD_RESTRICTIONS_INDEX,
    SUPPLIER_CONTRACTS_INDEX,
    TENANT_CREDENTIALS_INDEX,
    TERMINAL_WAIT_REPORTS_INDEX,
    TERMINALS_INDEX,
    TRUCK_TELEMETRY_INDEX,
    WEATHER_ALERTS_INDEX,
    WEATHER_OBSERVATIONS_INDEX,
    setup_fuel_ops_indices,
)


# The 22 index names the spec calls for (21 domain indices + tenant_credentials).
EXPECTED_INDICES = {
    CUSTOMER_TANKS_INDEX,
    WEATHER_OBSERVATIONS_INDEX,
    DEPOTS_INDEX,
    MVP_COMBINABLE_GROUPS_INDEX,
    METER_TICKET_OCR_RESULTS_INDEX,
    BILL_OF_LADING_INDEX,
    MVP_RECONCILIATION_INDEX,
    INTEGRATION_INSTANCES_INDEX,
    INTEGRATION_SYNC_RUNS_INDEX,
    ATG_READINGS_INDEX,
    TRUCK_TELEMETRY_INDEX,
    COMPARTMENT_CLEANING_EVENTS_INDEX,
    CROSS_CONTAMINATION_EVENTS_INDEX,
    TERMINALS_INDEX,
    RACK_PRICES_INDEX,
    SUPPLIER_CONTRACTS_INDEX,
    TERMINAL_WAIT_REPORTS_INDEX,
    SOURCING_RECOMMENDATIONS_INDEX,
    WEATHER_ALERTS_INDEX,
    STORM_ROAD_RESTRICTIONS_INDEX,
    STORM_MODE_OVERRIDES_INDEX,
    TENANT_CREDENTIALS_INDEX,
}


# ---------------------------------------------------------------------------
# Tests: mapping shape
# ---------------------------------------------------------------------------


class TestFuelOpsMappingShape:
    """Every mapping is strict, tenant-scoped, and uses canonical settings."""

    def test_catalog_contains_all_22_indices(self):
        assert set(FUEL_OPS_INDEX_MAPPINGS.keys()) == EXPECTED_INDICES
        assert len(FUEL_OPS_INDEX_MAPPINGS) == 22

    @pytest.mark.parametrize("index_name", sorted(EXPECTED_INDICES))
    def test_each_mapping_is_strict(self, index_name):
        mapping = FUEL_OPS_INDEX_MAPPINGS[index_name]
        assert mapping["mappings"]["dynamic"] == "strict", (
            f"{index_name} mapping must use dynamic: strict"
        )

    @pytest.mark.parametrize("index_name", sorted(EXPECTED_INDICES))
    def test_each_mapping_has_tenant_id_keyword(self, index_name):
        props = FUEL_OPS_INDEX_MAPPINGS[index_name]["mappings"]["properties"]
        assert "tenant_id" in props, f"{index_name} must define tenant_id"
        assert props["tenant_id"]["type"] == "keyword", (
            f"{index_name}.tenant_id must be keyword for tenant isolation"
        )

    @pytest.mark.parametrize("index_name", sorted(EXPECTED_INDICES))
    def test_default_shard_and_replica_settings(self, index_name):
        settings = FUEL_OPS_INDEX_MAPPINGS[index_name]["settings"]
        assert settings["number_of_shards"] == 1
        assert settings["number_of_replicas"] == 1


# ---------------------------------------------------------------------------
# Tests: key field-type expectations per capability
# ---------------------------------------------------------------------------


class TestCapabilitySpecificFields:
    """Spot-check the most important fields per capability's acceptance criteria."""

    def test_customer_tanks_fields(self):
        """Req 1.1.1 — customer_tanks schema."""
        props = CUSTOMER_TANKS_MAPPING["mappings"]["properties"]
        assert props["customer_tank_id"]["type"] == "keyword"
        assert props["customer_id"]["type"] == "keyword"
        assert props["customer_type"]["type"] == "keyword"
        assert props["fuel_type"]["type"] == "keyword"
        assert props["capacity_gallons"]["type"] == "double"
        assert props["current_level_gallons"]["type"] == "double"
        assert props["last_reading_at"]["type"] == "date"
        assert props["zip_code"]["type"] == "keyword"
        assert props["k_factor"]["type"] == "double"
        assert props["status"]["type"] == "keyword"

    def test_depots_fields_and_coordinates(self):
        """Req 2.2.1 — depots schema with lat/lon bounds handled at model layer."""
        props = DEPOTS_MAPPING["mappings"]["properties"]
        assert props["depot_id"]["type"] == "keyword"
        assert props["timezone"]["type"] == "keyword"
        assert props["fuel_types_supported"]["type"] == "keyword"
        assert props["status"]["type"] == "keyword"
        # geo_point enables future distance queries while the lat/lon doubles
        # keep strict storage of the raw values for validation.
        assert props["location"]["type"] == "geo_point"
        assert props["location_lat"]["type"] == "double"
        assert props["location_lon"]["type"] == "double"

    def test_combinable_groups_nested_members(self):
        """Req 3.2.1 — combinable-groups nested members."""
        props = FUEL_OPS_INDEX_MAPPINGS[MVP_COMBINABLE_GROUPS_INDEX][
            "mappings"
        ]["properties"]
        assert props["members"]["type"] == "nested"
        assert props["estimated_combined_gallons"]["type"] == "double"
        assert props["centroid"]["type"] == "geo_point"

    def test_reconciliation_variance_fields(self):
        """Req 4.4 — four-way reconciliation variance percentages."""
        props = FUEL_OPS_INDEX_MAPPINGS[MVP_RECONCILIATION_INDEX][
            "mappings"
        ]["properties"]
        for field in (
            "ordered_gallons",
            "loaded_gallons",
            "delivered_gallons",
            "invoiced_gallons",
        ):
            assert props[field]["type"] == "double"
        for field in (
            "variance_load_vs_order_pct",
            "variance_delivered_vs_loaded_pct",
            "variance_invoiced_vs_delivered_pct",
        ):
            assert props[field]["type"] == "float"
        assert props["alert_flags"]["type"] == "keyword"

    def test_integration_instances_fields(self):
        """Req 5.1.1 — per-tenant integration instances."""
        props = FUEL_OPS_INDEX_MAPPINGS[INTEGRATION_INSTANCES_INDEX][
            "mappings"
        ]["properties"]
        assert props["provider_name"]["type"] == "keyword"
        assert props["category"]["type"] == "keyword"
        assert props["enabled"]["type"] == "boolean"
        assert props["credentials_ref"]["type"] == "keyword"
        assert props["schedule_cron"]["type"] == "keyword"

    def test_integration_sync_runs_fields(self):
        """Req 5.1.4 — sync run audit trail."""
        props = FUEL_OPS_INDEX_MAPPINGS[INTEGRATION_SYNC_RUNS_INDEX][
            "mappings"
        ]["properties"]
        assert props["operation"]["type"] == "keyword"
        assert props["status"]["type"] == "keyword"
        assert props["started_at"]["type"] == "date"
        assert props["finished_at"]["type"] == "date"

    def test_terminals_operating_hours_nested(self):
        """Req 8.1.1 — operating_hours is nested for per-day lookup."""
        props = FUEL_OPS_INDEX_MAPPINGS[TERMINALS_INDEX]["mappings"]["properties"]
        assert props["operating_hours"]["type"] == "nested"
        assert props["supported_products"]["type"] == "keyword"
        assert props["branded"]["type"] == "boolean"

    def test_rack_prices_fields(self):
        """Req 8.2 — rack price observations."""
        props = FUEL_OPS_INDEX_MAPPINGS[RACK_PRICES_INDEX][
            "mappings"
        ]["properties"]
        assert props["price_per_gallon_usd"]["type"] == "double"
        assert props["branded_flag"]["type"] == "boolean"
        assert props["effective_at"]["type"] == "date"

    def test_supplier_contracts_fields(self):
        """Req 8.3 — supplier contract records."""
        props = FUEL_OPS_INDEX_MAPPINGS[SUPPLIER_CONTRACTS_INDEX][
            "mappings"
        ]["properties"]
        assert props["preferred_terminal_ids"]["type"] == "keyword"
        assert props["minimum_lift_gallons_per_month"]["type"] == "double"
        assert props["branded_required"]["type"] == "boolean"

    def test_terminal_wait_reports_fields(self):
        """Req 8.4 — wait time observations."""
        props = FUEL_OPS_INDEX_MAPPINGS[TERMINAL_WAIT_REPORTS_INDEX][
            "mappings"
        ]["properties"]
        assert props["wait_minutes"]["type"] == "float"
        assert props["source"]["type"] == "keyword"
        assert props["observed_at"]["type"] == "date"

    def test_sourcing_recommendations_nested_candidates(self):
        """Req 8.5 — sourcing recommendations audit trail."""
        props = FUEL_OPS_INDEX_MAPPINGS[SOURCING_RECOMMENDATIONS_INDEX][
            "mappings"
        ]["properties"]
        assert props["candidates"]["type"] == "nested"
        assert props["origin"]["type"] == "geo_point"
        # Task 7.11 — top-level wait_warning summary for the dispatcher UI.
        assert props["wait_warning_terminal_ids"]["type"] == "keyword"
        assert props["candidates"]["properties"]["wait_warning"]["type"] == "boolean"

    def test_weather_alerts_fields(self):
        """Req 9.1.1 — weather alerts ingestion."""
        props = FUEL_OPS_INDEX_MAPPINGS[WEATHER_ALERTS_INDEX][
            "mappings"
        ]["properties"]
        assert props["alert_id"]["type"] == "keyword"
        assert props["severity"]["type"] == "keyword"
        assert props["expected_start_at"]["type"] == "date"
        assert props["affected_zip_codes"]["type"] == "keyword"

    def test_storm_road_restrictions_geo_shape(self):
        """Req 9.3.3 — road-restriction polygons stored as geo_shape."""
        props = FUEL_OPS_INDEX_MAPPINGS[STORM_ROAD_RESTRICTIONS_INDEX][
            "mappings"
        ]["properties"]
        assert props["polygon"]["type"] == "geo_shape"
        assert props["severity"]["type"] == "keyword"

    def test_storm_mode_overrides_fields(self):
        """Req 9.4.2 — manual storm-mode overrides."""
        props = FUEL_OPS_INDEX_MAPPINGS[STORM_MODE_OVERRIDES_INDEX][
            "mappings"
        ]["properties"]
        assert props["action"]["type"] == "keyword"
        assert props["actor_id"]["type"] == "keyword"
        assert props["expires_at"]["type"] == "date"

    def test_tenant_credentials_fields(self):
        """Req 5.1 — KMS-wrapped credentials vault."""
        props = FUEL_OPS_INDEX_MAPPINGS[TENANT_CREDENTIALS_INDEX][
            "mappings"
        ]["properties"]
        assert props["ref"]["type"] == "keyword"
        assert props["wrapped_dek"]["type"] == "binary"
        assert props["ciphertext"]["type"] == "binary"

    def test_atg_and_telemetry_have_geo_context(self):
        atg_props = FUEL_OPS_INDEX_MAPPINGS[ATG_READINGS_INDEX][
            "mappings"
        ]["properties"]
        assert atg_props["volume_gallons"]["type"] == "double"
        assert atg_props["water_level_in"]["type"] == "float"

        telem_props = FUEL_OPS_INDEX_MAPPINGS[TRUCK_TELEMETRY_INDEX][
            "mappings"
        ]["properties"]
        assert telem_props["location"]["type"] == "geo_point"
        assert telem_props["speed_kph"]["type"] == "float"

    def test_compartment_cleaning_events_fields(self):
        props = FUEL_OPS_INDEX_MAPPINGS[COMPARTMENT_CLEANING_EVENTS_INDEX][
            "mappings"
        ]["properties"]
        assert props["method"]["type"] == "keyword"
        assert props["evidence_refs"]["type"] == "keyword"
        assert props["cleaned_at"]["type"] == "date"

    def test_cross_contamination_events_fields(self):
        props = FUEL_OPS_INDEX_MAPPINGS[CROSS_CONTAMINATION_EVENTS_INDEX][
            "mappings"
        ]["properties"]
        assert props["previous_product"]["type"] == "keyword"
        assert props["attempted_product"]["type"] == "keyword"
        assert props["governing_rule"]["type"] == "keyword"

    def test_bill_of_lading_fields(self):
        props = FUEL_OPS_INDEX_MAPPINGS[BILL_OF_LADING_INDEX][
            "mappings"
        ]["properties"]
        assert props["bol_id"]["type"] == "keyword"
        assert props["file_ref"]["type"] == "keyword"
        assert props["hash"]["type"] == "keyword"

    def test_meter_ticket_ocr_results_fields(self):
        props = FUEL_OPS_INDEX_MAPPINGS[METER_TICKET_OCR_RESULTS_INDEX][
            "mappings"
        ]["properties"]
        assert props["extracted_gallons"]["type"] == "double"
        assert props["confidence"]["type"] == "float"
        assert props["requires_manual_review"]["type"] == "boolean"

    def test_weather_observations_fields(self):
        props = FUEL_OPS_INDEX_MAPPINGS[WEATHER_OBSERVATIONS_INDEX][
            "mappings"
        ]["properties"]
        assert props["hdd"]["type"] == "float"
        assert props["avg_temp_f"]["type"] == "float"
        assert props["date"]["type"] == "date"


# ---------------------------------------------------------------------------
# Tests: setup_fuel_ops_indices
# ---------------------------------------------------------------------------


class TestSetupFuelOpsIndices:
    """Verify the bootstrap function behaves like the existing setup helpers."""

    def _make_es_service(self, existing_indices=None):
        existing = existing_indices or set()
        es_service = MagicMock()
        client = MagicMock()
        client.indices.exists.side_effect = lambda index: index in existing
        es_service.client = client
        type(es_service).is_serverless = PropertyMock(return_value=False)
        return es_service

    def _patch_es_module(self):
        fake_module = MagicMock()
        fake_module.ElasticsearchService = MagicMock()
        fake_module.ElasticsearchService.strip_serverless_incompatible_settings = (
            lambda mapping: mapping
        )
        return patch.dict(sys.modules, {"services.elasticsearch_service": fake_module})

    def test_creates_all_missing_indices(self):
        es_service = self._make_es_service()
        with self._patch_es_module():
            setup_fuel_ops_indices(es_service)

        created = {
            call.kwargs["index"]
            for call in es_service.client.indices.create.call_args_list
        }
        assert created == EXPECTED_INDICES

    def test_skips_existing_indices(self):
        already_there = {CUSTOMER_TANKS_INDEX, DEPOTS_INDEX, TERMINALS_INDEX}
        es_service = self._make_es_service(existing_indices=already_there)
        with self._patch_es_module():
            setup_fuel_ops_indices(es_service)

        created = {
            call.kwargs["index"]
            for call in es_service.client.indices.create.call_args_list
        }
        assert created.isdisjoint(already_there)
        assert created == EXPECTED_INDICES - already_there

    def test_passes_expected_mapping_body(self):
        es_service = self._make_es_service()
        with self._patch_es_module():
            setup_fuel_ops_indices(es_service)

        bodies_by_index = {
            call.kwargs["index"]: call.kwargs["body"]
            for call in es_service.client.indices.create.call_args_list
        }
        assert bodies_by_index[CUSTOMER_TANKS_INDEX] == CUSTOMER_TANKS_MAPPING
        assert bodies_by_index[DEPOTS_INDEX] == DEPOTS_MAPPING

    def test_serverless_strips_settings(self):
        es_service = self._make_es_service()
        type(es_service).is_serverless = PropertyMock(return_value=True)

        # Stub strip_serverless_incompatible_settings to confirm it's applied.
        fake_module = MagicMock()
        fake_module.ElasticsearchService = MagicMock()
        stripped = {"mappings": {"dynamic": "strict", "properties": {}}}
        fake_module.ElasticsearchService.strip_serverless_incompatible_settings = (
            MagicMock(return_value=stripped)
        )
        with patch.dict(sys.modules, {"services.elasticsearch_service": fake_module}):
            setup_fuel_ops_indices(es_service)

        # Called once per index.
        assert (
            fake_module.ElasticsearchService
            .strip_serverless_incompatible_settings.call_count
            == len(EXPECTED_INDICES)
        )
        # Every create body is the stripped version.
        for call in es_service.client.indices.create.call_args_list:
            assert call.kwargs["body"] == stripped

    def test_errors_on_one_index_do_not_abort_others(self):
        es_service = self._make_es_service()

        def flaky_create(**kwargs):
            if kwargs["index"] == CUSTOMER_TANKS_INDEX:
                raise RuntimeError("simulated ES failure")
            return {"acknowledged": True}

        es_service.client.indices.create.side_effect = flaky_create
        with self._patch_es_module():
            setup_fuel_ops_indices(es_service)  # must not raise

        attempted = {
            call.kwargs["index"]
            for call in es_service.client.indices.create.call_args_list
        }
        assert attempted == EXPECTED_INDICES

    def test_skips_retired_indices(self, monkeypatch):
        """A Phase-6 retired index must NOT be recreated at startup."""
        from config.settings import clear_settings_cache

        monkeypatch.setenv("RETIRED_ES_INDICES", "supplier_contracts")
        clear_settings_cache()
        try:
            es_service = self._make_es_service()
            with self._patch_es_module():
                setup_fuel_ops_indices(es_service)

            created = {
                call.kwargs["index"]
                for call in es_service.client.indices.create.call_args_list
            }
            assert SUPPLIER_CONTRACTS_INDEX not in created
            # Every other index is still created.
            assert created == EXPECTED_INDICES - {SUPPLIER_CONTRACTS_INDEX}
        finally:
            clear_settings_cache()
