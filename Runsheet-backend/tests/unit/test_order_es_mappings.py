"""
Unit tests for order_es_mappings — strict mappings + setup function.

Validates that every order-intake index mapping is strict, carries a tenant_id
keyword field, uses the default 1-shard/1-replica settings, includes a strict
intake_metadata sub-mapping, and that ``setup_order_intake_indices`` creates
missing indices while skipping existing ones.

Requirements: 1.1, 1.1.3, 10.3
"""
import sys
from unittest.mock import MagicMock, patch

import pytest

from fuel.services.order_es_mappings import DRIVER_REPORTS_INDEX, DRIVER_REPORTS_MAPPING, DRIVERS_CURRENT_INDEX, DRIVERS_CURRENT_MAPPING, FUEL_ORDER_EVENTS_INDEX, FUEL_ORDER_EVENTS_MAPPING, FUEL_ORDERS_CURRENT_INDEX, FUEL_ORDERS_CURRENT_MAPPING, INTAKE_CHANNELS_INDEX, INTAKE_CHANNELS_MAPPING, ORDER_INTAKE_INDEX_MAPPINGS

EXPECTED_INDICES = {
    FUEL_ORDERS_CURRENT_INDEX,
    FUEL_ORDER_EVENTS_INDEX,
    DRIVERS_CURRENT_INDEX,
    INTAKE_CHANNELS_INDEX,
    DRIVER_REPORTS_INDEX,
}
# NB: ``pending_legacy_mirrors`` was dropped from this set with the legacy
# mirror retry queue it backed.

# Indices whose documents carry the pipeline's closed ``intake_metadata``
# sub-mapping. ``driver_reports`` is written directly by the
# DriverReportRepository with its own closed field set (no intake_metadata),
# so it is scoped out of the intake_metadata convention check.
INTAKE_METADATA_INDICES = EXPECTED_INDICES - {DRIVER_REPORTS_INDEX}


# ---------------------------------------------------------------------------
# Tests: mapping shape
# ---------------------------------------------------------------------------


class TestOrderIntakeMappingShape:
    """Every mapping is strict, tenant-scoped, and uses canonical settings."""

    def test_catalog_contains_all_indices(self):
        assert set(ORDER_INTAKE_INDEX_MAPPINGS.keys()) == EXPECTED_INDICES
        assert len(ORDER_INTAKE_INDEX_MAPPINGS) == len(EXPECTED_INDICES)

    @pytest.mark.parametrize("index_name", sorted(EXPECTED_INDICES))
    def test_each_mapping_is_strict(self, index_name):
        mapping = ORDER_INTAKE_INDEX_MAPPINGS[index_name]
        assert mapping["mappings"]["dynamic"] == "strict", (
            f"{index_name} mapping must use dynamic: strict"
        )

    @pytest.mark.parametrize("index_name", sorted(EXPECTED_INDICES))
    def test_each_mapping_has_tenant_id_keyword(self, index_name):
        props = ORDER_INTAKE_INDEX_MAPPINGS[index_name]["mappings"]["properties"]
        assert "tenant_id" in props, f"{index_name} must define tenant_id"
        assert props["tenant_id"]["type"] == "keyword", (
            f"{index_name}.tenant_id must be keyword for tenant isolation"
        )

    @pytest.mark.parametrize("index_name", sorted(EXPECTED_INDICES))
    def test_each_mapping_has_1_shard_1_replica(self, index_name):
        settings = ORDER_INTAKE_INDEX_MAPPINGS[index_name]["settings"]
        assert settings["number_of_shards"] == 1
        assert settings["number_of_replicas"] == 1

    @pytest.mark.parametrize("index_name", sorted(INTAKE_METADATA_INDICES))
    def test_each_mapping_has_strict_intake_metadata(self, index_name):
        props = ORDER_INTAKE_INDEX_MAPPINGS[index_name]["mappings"]["properties"]
        assert "intake_metadata" in props, (
            f"{index_name} must define intake_metadata sub-mapping"
        )
        im = props["intake_metadata"]
        assert im["dynamic"] == "strict", (
            f"{index_name}.intake_metadata must use dynamic: strict"
        )
        assert "properties" in im, (
            f"{index_name}.intake_metadata must have properties"
        )

    def test_fuel_orders_current_has_all_required_fields(self):
        props = FUEL_ORDERS_CURRENT_MAPPING["mappings"]["properties"]
        required_fields = [
            "order_id", "tenant_id", "customer_id", "customer_name",
            "customer_phone", "customer_email", "ship_to_address",
            "ship_to_lat", "ship_to_lon", "ship_to_geo", "customer_tank_id",
            "product_code", "gallons_requested", "fill_to_full", "call_type",
            "delivery_window_start", "delivery_window_end", "hold_reason",
            "po_number", "special_instructions", "intake_channel",
            "intake_channel_id", "intake_metadata", "status",
            "assigned_driver_id", "assigned_run_id", "legacy_origin_snapshot",
            "source_schema_version", "trace_id", "created_at", "updated_at",
            "last_event_timestamp",
        ]
        for field in required_fields:
            assert field in props, f"fuel_orders_current missing field: {field}"

    def test_fuel_order_events_has_all_required_fields(self):
        props = FUEL_ORDER_EVENTS_MAPPING["mappings"]["properties"]
        required_fields = [
            "event_id", "order_id", "tenant_id", "event_type",
            "event_payload", "event_timestamp", "ingested_at",
            "source_schema_version", "trace_id", "location",
        ]
        for field in required_fields:
            assert field in props, f"fuel_order_events missing field: {field}"

    def test_drivers_current_has_all_required_fields(self):
        props = DRIVERS_CURRENT_MAPPING["mappings"]["properties"]
        required_fields = [
            "driver_id", "tenant_id", "driver_name", "phone", "status",
            "availability", "assigned_truck_id", "cdl_class",
            "hazmat_endorsement", "medical_card_expiry", "current_location",
            "last_seen", "active_order_count", "completed_today",
            "last_event_timestamp", "source_schema_version", "trace_id",
            "created_at", "updated_at",
        ]
        for field in required_fields:
            assert field in props, f"drivers_current missing field: {field}"

    def test_drivers_current_declares_duty_status_projection_fields(self):
        """The projection bookkeeping fields exist and status stays a keyword.

        ``drivers_current`` is ``dynamic: strict``, so the duty-status service
        cannot write either bookkeeping field until it is declared here. The
        ``status`` field must stay a single keyword so existing readers work
        unchanged.
        """
        props = DRIVERS_CURRENT_MAPPING["mappings"]["properties"]

        assert props["duty_status_event_id"]["type"] == "keyword"
        assert props["duty_status_updated_at"]["type"] == "date"
        assert props["status"] == {"type": "keyword"}

    def test_intake_channels_has_all_required_fields(self):
        props = INTAKE_CHANNELS_MAPPING["mappings"]["properties"]
        required_fields = [
            "channel_id", "tenant_id", "channel_type", "display_name",
            "hmac_secret_ref", "supported_schema_versions",
            "rate_limit_per_minute", "secret_version", "enabled",
            "created_at", "updated_at",
        ]
        for field in required_fields:
            assert field in props, f"intake_channels missing field: {field}"

    # ``test_pending_legacy_mirrors_has_all_required_fields`` was removed
    # here along with the PENDING_LEGACY_MIRRORS mapping it asserted on.

    def test_driver_reports_has_all_required_fields(self):
        props = DRIVER_REPORTS_MAPPING["mappings"]["properties"]
        required_fields = [
            "report_id", "tenant_id", "driver_id", "assignment_id",
            "kind", "detail", "eta_minutes", "created_at",
        ]
        for field in required_fields:
            assert field in props, (
                f"driver_reports missing field: {field}"
            )
        # Tenant-scoping and category fields must be keyword for term filters.
        assert props["tenant_id"]["type"] == "keyword"
        assert props["driver_id"]["type"] == "keyword"
        assert props["assignment_id"]["type"] == "keyword"
        assert props["kind"]["type"] == "keyword"

    def test_date_fields_are_typed_correctly(self):
        """All date fields across all indices use type 'date'."""
        date_fields_by_index = {
            FUEL_ORDERS_CURRENT_INDEX: [
                "delivery_window_start", "delivery_window_end",
                "created_at", "updated_at", "last_event_timestamp",
            ],
            FUEL_ORDER_EVENTS_INDEX: [
                "event_timestamp", "ingested_at",
            ],
            DRIVERS_CURRENT_INDEX: [
                "medical_card_expiry", "last_seen", "last_event_timestamp",
                "duty_status_updated_at", "created_at", "updated_at",
            ],
            INTAKE_CHANNELS_INDEX: [
                "created_at", "updated_at",
            ],
            DRIVER_REPORTS_INDEX: [
                "created_at",
            ],
        }
        for index_name, fields in date_fields_by_index.items():
            props = ORDER_INTAKE_INDEX_MAPPINGS[index_name]["mappings"]["properties"]
            for field in fields:
                assert props[field]["type"] == "date", (
                    f"{index_name}.{field} must be type 'date'"
                )


# ---------------------------------------------------------------------------
# Tests: setup_order_intake_indices
# ---------------------------------------------------------------------------


class TestSetupOrderIntakeIndices:
    """setup_order_intake_indices creates missing indices, skips existing."""







