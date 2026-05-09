"""
Unit tests for the order_intake_pipeline_001_rename_shipment_to_order migration.

Covers:
    * Seed legacy fixture → dry-run reports expected counts.
    * Execute once → assert new docs exist and preserve IDs.
    * Execute again → assert no duplicates (idempotency).
    * Seed malformed fixture → assert poison-queue routing.
    * Shipment status mapping.
    * Rider → Driver transformation.
    * Coordinate extraction.
    * Validation failure detection.

Validates: Requirements 9.4.1, 9.4.2, 9.4.4, 9.4.6.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from scripts.migrations.order_intake_pipeline_001_rename_shipment_to_order import (
    DEFAULT_BATCH_SIZE,
    DRIVERS_CURRENT_INDEX,
    FUEL_ORDERS_CURRENT_INDEX,
    POISON_QUEUE_INDEX,
    RIDERS_CURRENT_INDEX,
    SHIPMENTS_CURRENT_INDEX,
    MigrationResult,
    ValidationFailure,
    _extract_coordinates,
    _map_rider_status,
    _map_shipment_status,
    migrate_tenant,
    run_migration,
    transform_rider_to_driver,
    transform_shipment_to_fuel_order,
    validate_shipment_for_migration,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-test-001"
NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _make_shipment(
    shipment_id: str = "shp_001",
    tenant_id: str = TENANT_ID,
    status: str = "placed",
    rider_id: Optional[str] = "rider_001",
    origin: str = "Depot A",
    destination: str = "123 Main St",
    current_location: Optional[Dict] = None,
    trace_id: str = "trace_001",
) -> Dict[str, Any]:
    """Create a valid legacy shipment fixture."""
    if current_location is None:
        current_location = {"lat": 32.7767, "lon": -96.7970}
    return {
        "shipment_id": shipment_id,
        "tenant_id": tenant_id,
        "status": status,
        "rider_id": rider_id,
        "origin": origin,
        "destination": destination,
        "current_location": current_location,
        "trace_id": trace_id,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "last_event_timestamp": "2026-01-02T00:00:00Z",
        "source_schema_version": "1.0",
    }


def _make_rider(
    rider_id: str = "rider_001",
    tenant_id: str = TENANT_ID,
    status: str = "active",
    rider_name: str = "John Driver",
) -> Dict[str, Any]:
    """Create a valid legacy rider fixture."""
    return {
        "rider_id": rider_id,
        "tenant_id": tenant_id,
        "status": status,
        "rider_name": rider_name,
        "availability": "available",
        "current_location": {"lat": 32.7767, "lon": -96.7970},
        "last_seen": "2026-01-02T00:00:00Z",
        "last_event_timestamp": "2026-01-02T00:00:00Z",
        "active_shipment_count": 3,
        "completed_today": 5,
        "trace_id": "trace_rider_001",
        "source_schema_version": "1.0",
    }


class FakeEsService:
    """In-memory fake ES service for testing the migration."""

    def __init__(self):
        self._indices: Dict[str, Dict[str, Dict[str, Any]]] = {
            SHIPMENTS_CURRENT_INDEX: {},
            RIDERS_CURRENT_INDEX: {},
            FUEL_ORDERS_CURRENT_INDEX: {},
            DRIVERS_CURRENT_INDEX: {},
            POISON_QUEUE_INDEX: {},
        }
        # Expose a nested es_service.client for _index_document
        self.es_service = self
        self.client = self

    def seed_shipment(self, doc: Dict[str, Any]) -> None:
        doc_id = doc.get("shipment_id", "unknown")
        self._indices[SHIPMENTS_CURRENT_INDEX][doc_id] = doc

    def seed_rider(self, doc: Dict[str, Any]) -> None:
        doc_id = doc.get("rider_id", "unknown")
        self._indices[RIDERS_CURRENT_INDEX][doc_id] = doc

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int = 10
    ) -> Dict[str, Any]:
        """Simulate ES search."""
        docs = self._indices.get(index, {})
        results = []

        # Handle term queries for tenant_id filtering
        q = query.get("query", {})
        term_filter = q.get("term", {})
        tenant_filter = term_filter.get("tenant_id")
        id_filter = term_filter.get("_id")
        order_id_filter = term_filter.get("order_id")
        driver_id_filter = term_filter.get("driver_id")

        for doc_id, doc in docs.items():
            if tenant_filter and doc.get("tenant_id") != tenant_filter:
                continue
            if id_filter and doc_id != id_filter:
                continue
            if order_id_filter and doc.get("order_id") != order_id_filter:
                continue
            if driver_id_filter and doc.get("driver_id") != driver_id_filter:
                continue
            results.append({"_id": doc_id, "_source": doc})

        # Handle pagination
        offset = query.get("from", 0)
        page_size = query.get("size", size)
        page = results[offset:offset + page_size]

        return {
            "hits": {
                "total": {"value": len(results)},
                "hits": page,
            }
        }

    async def index(
        self, *, index: str, id: str, document: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Simulate ES index operation."""
        if index not in self._indices:
            self._indices[index] = {}
        self._indices[index][id] = document
        return {"result": "created", "_id": id}

    def get_indexed_docs(self, index: str) -> Dict[str, Dict[str, Any]]:
        """Helper to inspect indexed documents in tests."""
        return self._indices.get(index, {})


class FakePoisonQueueService:
    """In-memory fake poison queue service for testing."""

    def __init__(self):
        self.entries: List[Dict[str, Any]] = []

    async def store_failed_event(
        self,
        payload: dict,
        error: str,
        error_type: str,
        tenant_id: str = "",
        trace_id: str = "",
    ) -> None:
        self.entries.append({
            "payload": payload,
            "error": error,
            "error_type": error_type,
            "tenant_id": tenant_id,
            "trace_id": trace_id,
        })


# ---------------------------------------------------------------------------
# Tests: Validation
# ---------------------------------------------------------------------------


class TestValidateShipmentForMigration:
    def test_valid_shipment_passes(self) -> None:
        shipment = _make_shipment()
        assert validate_shipment_for_migration(shipment) is None

    def test_missing_tenant_id_fails(self) -> None:
        shipment = _make_shipment(tenant_id="")
        failure = validate_shipment_for_migration(shipment)
        assert failure is not None
        assert failure.reason == "missing_tenant_id"

    def test_none_tenant_id_fails(self) -> None:
        shipment = _make_shipment()
        shipment["tenant_id"] = None
        failure = validate_shipment_for_migration(shipment)
        assert failure is not None
        assert failure.reason == "missing_tenant_id"

    def test_missing_destination_fails(self) -> None:
        shipment = _make_shipment(destination="")
        failure = validate_shipment_for_migration(shipment)
        assert failure is not None
        assert failure.reason == "missing_destination"

    def test_malformed_coordinates_out_of_range(self) -> None:
        shipment = _make_shipment(
            current_location={"lat": 100.0, "lon": -96.0}
        )
        failure = validate_shipment_for_migration(shipment)
        assert failure is not None
        assert failure.reason == "malformed_coordinates"

    def test_no_current_location_passes(self) -> None:
        """Shipments without coordinates are still valid (use defaults)."""
        shipment = _make_shipment(current_location=None)
        # Remove the key entirely
        shipment.pop("current_location", None)
        assert validate_shipment_for_migration(shipment) is None


# ---------------------------------------------------------------------------
# Tests: Transformation
# ---------------------------------------------------------------------------


class TestTransformShipmentToFuelOrder:
    def test_preserves_shipment_id_as_order_id(self) -> None:
        """Task 17.2: shipment_id → order_id preservation."""
        shipment = _make_shipment(shipment_id="shp_abc123")
        order = transform_shipment_to_fuel_order(shipment, NOW)
        assert order["order_id"] == "shp_abc123"

    def test_preserves_rider_id_as_assigned_driver_id(self) -> None:
        """Task 17.2: rider_id → driver_id preservation."""
        shipment = _make_shipment(rider_id="rider_xyz")
        order = transform_shipment_to_fuel_order(shipment, NOW)
        assert order["assigned_driver_id"] == "rider_xyz"

    def test_synthesized_fields(self) -> None:
        """Task 17.2: All synthesized FuelOrder fields are correct."""
        shipment = _make_shipment(origin="Depot B")
        order = transform_shipment_to_fuel_order(shipment, NOW)

        assert order["intake_channel"] == "legacy"
        assert order["intake_channel_id"] == "pre-migration"
        assert order["intake_metadata"]["legacy_shipment_id"] == "shp_001"
        assert order["call_type"] == "one_off"
        assert order["fill_to_full"] is True
        assert order["product_code"] is None
        assert order["gallons_requested"] is None
        assert order["source_schema_version"] == "legacy"
        assert order["legacy_origin_snapshot"] == "Depot B"

    def test_status_mapping(self) -> None:
        """Task 17.2: Status is mapped from source shipment."""
        for src_status, expected in [
            ("placed", "placed"),
            ("delivered", "delivered"),
            ("in_transit", "in_transit"),
            ("pending", "placed"),
            ("completed", "delivered"),
        ]:
            shipment = _make_shipment(status=src_status)
            order = transform_shipment_to_fuel_order(shipment, NOW)
            assert order["status"] == expected, (
                f"Expected {src_status} → {expected}, got {order['status']}"
            )

    def test_coordinates_extracted(self) -> None:
        shipment = _make_shipment(
            current_location={"lat": 40.7128, "lon": -74.0060}
        )
        order = transform_shipment_to_fuel_order(shipment, NOW)
        assert order["ship_to_lat"] == 40.7128
        assert order["ship_to_lon"] == -74.0060

    def test_preserves_timestamps(self) -> None:
        shipment = _make_shipment()
        order = transform_shipment_to_fuel_order(shipment, NOW)
        assert order["created_at"] == "2026-01-01T00:00:00Z"
        assert order["updated_at"] == "2026-01-02T00:00:00Z"


class TestTransformRiderToDriver:
    def test_preserves_rider_id_as_driver_id(self) -> None:
        """Task 17.2: rider_id → driver_id preservation."""
        rider = _make_rider(rider_id="rider_abc")
        driver = transform_rider_to_driver(rider, NOW)
        assert driver["driver_id"] == "rider_abc"

    def test_maps_rider_name_to_driver_name(self) -> None:
        rider = _make_rider(rider_name="Jane Smith")
        driver = transform_rider_to_driver(rider, NOW)
        assert driver["driver_name"] == "Jane Smith"

    def test_maps_active_shipment_count(self) -> None:
        rider = _make_rider()
        rider["active_shipment_count"] = 7
        driver = transform_rider_to_driver(rider, NOW)
        assert driver["active_order_count"] == 7

    def test_status_mapping(self) -> None:
        for src, expected in [
            ("active", "active"),
            ("inactive", "inactive"),
            ("available", "active"),
            ("offline", "off_duty"),
        ]:
            rider = _make_rider(status=src)
            driver = transform_rider_to_driver(rider, NOW)
            assert driver["status"] == expected


# ---------------------------------------------------------------------------
# Tests: Coordinate extraction
# ---------------------------------------------------------------------------


class TestExtractCoordinates:
    def test_dict_format(self) -> None:
        shipment = {"current_location": {"lat": 10.5, "lon": 20.5}}
        assert _extract_coordinates(shipment) == (10.5, 20.5)

    def test_string_format(self) -> None:
        shipment = {"current_location": "10.5,20.5"}
        assert _extract_coordinates(shipment) == (10.5, 20.5)

    def test_missing_returns_defaults(self) -> None:
        assert _extract_coordinates({}) == (0.0, 0.0)
        assert _extract_coordinates({"current_location": None}) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Tests: Status mapping
# ---------------------------------------------------------------------------


class TestStatusMapping:
    def test_known_shipment_statuses(self) -> None:
        assert _map_shipment_status("placed") == "placed"
        assert _map_shipment_status("delivered") == "delivered"
        assert _map_shipment_status("pending") == "placed"
        assert _map_shipment_status("completed") == "delivered"

    def test_unknown_shipment_status_defaults_to_placed(self) -> None:
        assert _map_shipment_status("unknown_status") == "placed"
        assert _map_shipment_status(None) == "placed"

    def test_known_rider_statuses(self) -> None:
        assert _map_rider_status("active") == "active"
        assert _map_rider_status("inactive") == "inactive"
        assert _map_rider_status("available") == "active"
        assert _map_rider_status("offline") == "off_duty"

    def test_unknown_rider_status_defaults_to_active(self) -> None:
        assert _map_rider_status("unknown") == "active"
        assert _map_rider_status(None) == "active"


# ---------------------------------------------------------------------------
# Tests: Full migration flow
# ---------------------------------------------------------------------------


class TestMigrateTenantDryRun:
    """Test dry-run mode reports expected counts."""

    @pytest.mark.asyncio
    async def test_dry_run_reports_counts(self) -> None:
        """Seed legacy fixture → dry-run reports expected counts."""
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        # Seed 3 valid shipments
        es.seed_shipment(_make_shipment(shipment_id="shp_001"))
        es.seed_shipment(_make_shipment(shipment_id="shp_002"))
        es.seed_shipment(_make_shipment(shipment_id="shp_003"))

        # Seed 2 riders
        es.seed_rider(_make_rider(rider_id="rider_001"))
        es.seed_rider(_make_rider(rider_id="rider_002"))

        result = await migrate_tenant(
            tenant_id=TENANT_ID,
            es_service=es,
            poison_queue_service=poison_queue,
            dry_run=True,
            batch_size=100,
        )

        assert result.shipments_found == 3
        assert result.shipments_migrated == 3
        assert result.shipments_poisoned == 0
        assert result.riders_found == 2
        assert result.riders_migrated == 2
        assert len(result.validation_failures) == 0

    @pytest.mark.asyncio
    async def test_dry_run_reports_poison_candidates(self) -> None:
        """Dry-run surfaces unmappable shipments."""
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        # Seed 1 valid + 2 invalid shipments (all with correct tenant_id
        # so the ES scan returns them, but with other validation issues)
        es.seed_shipment(_make_shipment(shipment_id="shp_valid"))
        es.seed_shipment(
            _make_shipment(
                shipment_id="shp_bad_coords",
                current_location={"lat": 200.0, "lon": -96.0},
            )
        )
        es.seed_shipment(_make_shipment(shipment_id="shp_no_dest", destination=""))

        result = await migrate_tenant(
            tenant_id=TENANT_ID,
            es_service=es,
            poison_queue_service=poison_queue,
            dry_run=True,
            batch_size=100,
        )

        assert result.shipments_found == 3
        assert result.shipments_migrated == 1
        assert result.shipments_poisoned == 2
        assert len(result.validation_failures) == 2

        # Dry-run does NOT write to poison queue
        assert len(poison_queue.entries) == 0

        # Check poison queue summary
        summary = result.poison_queue_summary()
        assert "malformed_coordinates" in summary
        assert "missing_destination" in summary


class TestMigrateTenantExecute:
    """Test execute mode writes documents."""

    @pytest.mark.asyncio
    async def test_execute_creates_documents(self) -> None:
        """Execute once → assert new docs exist and preserve IDs."""
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        es.seed_shipment(_make_shipment(shipment_id="shp_001"))
        es.seed_shipment(_make_shipment(shipment_id="shp_002"))
        es.seed_rider(_make_rider(rider_id="rider_001"))

        result = await migrate_tenant(
            tenant_id=TENANT_ID,
            es_service=es,
            poison_queue_service=poison_queue,
            dry_run=False,
            batch_size=100,
        )

        assert result.shipments_migrated == 2
        assert result.riders_migrated == 1

        # Verify documents exist in target indices
        orders = es.get_indexed_docs(FUEL_ORDERS_CURRENT_INDEX)
        assert "shp_001" in orders
        assert "shp_002" in orders
        assert orders["shp_001"]["order_id"] == "shp_001"
        assert orders["shp_002"]["order_id"] == "shp_002"

        drivers = es.get_indexed_docs(DRIVERS_CURRENT_INDEX)
        assert "rider_001" in drivers
        assert drivers["rider_001"]["driver_id"] == "rider_001"

    @pytest.mark.asyncio
    async def test_execute_preserves_ids(self) -> None:
        """Task 17.2: IDs are preserved across migration."""
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        es.seed_shipment(
            _make_shipment(shipment_id="shp_external_ref", rider_id="rider_ext")
        )

        await migrate_tenant(
            tenant_id=TENANT_ID,
            es_service=es,
            poison_queue_service=poison_queue,
            dry_run=False,
            batch_size=100,
        )

        orders = es.get_indexed_docs(FUEL_ORDERS_CURRENT_INDEX)
        assert "shp_external_ref" in orders
        order = orders["shp_external_ref"]
        assert order["order_id"] == "shp_external_ref"
        assert order["assigned_driver_id"] == "rider_ext"

    @pytest.mark.asyncio
    async def test_idempotent_no_duplicates(self) -> None:
        """Execute again → assert no duplicates."""
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        es.seed_shipment(_make_shipment(shipment_id="shp_001"))
        es.seed_rider(_make_rider(rider_id="rider_001"))

        # First run
        result1 = await migrate_tenant(
            tenant_id=TENANT_ID,
            es_service=es,
            poison_queue_service=poison_queue,
            dry_run=False,
            batch_size=100,
        )
        assert result1.shipments_migrated == 1
        assert result1.riders_migrated == 1

        # Second run — should skip existing
        result2 = await migrate_tenant(
            tenant_id=TENANT_ID,
            es_service=es,
            poison_queue_service=poison_queue,
            dry_run=False,
            batch_size=100,
        )
        assert result2.shipments_migrated == 0
        assert result2.shipments_skipped_existing == 1
        assert result2.riders_migrated == 0
        assert result2.riders_skipped_existing == 1

        # Only one document in target
        orders = es.get_indexed_docs(FUEL_ORDERS_CURRENT_INDEX)
        assert len(orders) == 1


class TestMigrateTenantPoisonQueue:
    """Test unmappable shipments route to poison queue."""

    @pytest.mark.asyncio
    async def test_malformed_routes_to_poison_queue(self) -> None:
        """Task 17.3: Unmappable shipments route to ops_poison_queue."""
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        # Seed malformed shipments (all with valid tenant_id so ES scan
        # returns them, but with other validation issues)
        es.seed_shipment(
            _make_shipment(
                shipment_id="shp_bad_coords",
                current_location={"lat": 200.0, "lon": -96.0},
            )
        )
        es.seed_shipment(_make_shipment(shipment_id="shp_no_dest", destination=""))
        es.seed_shipment(_make_shipment(shipment_id="shp_no_dest2", destination="  "))

        result = await migrate_tenant(
            tenant_id=TENANT_ID,
            es_service=es,
            poison_queue_service=poison_queue,
            dry_run=False,
            batch_size=100,
        )

        assert result.shipments_poisoned == 3
        assert result.shipments_migrated == 0

        # Verify poison queue entries
        assert len(poison_queue.entries) == 3
        error_types = [e["error_type"] for e in poison_queue.entries]
        assert all(et == "legacy_shipment_unmappable" for et in error_types)

    @pytest.mark.asyncio
    async def test_poison_queue_surfaced_in_dry_run(self) -> None:
        """Task 17.3: Poison-queue candidates surfaced in dry-run output."""
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        es.seed_shipment(_make_shipment(shipment_id="shp_ok"))
        es.seed_shipment(_make_shipment(shipment_id="shp_bad", destination=""))

        result = await migrate_tenant(
            tenant_id=TENANT_ID,
            es_service=es,
            poison_queue_service=poison_queue,
            dry_run=True,
            batch_size=100,
        )

        assert result.shipments_poisoned == 1
        assert len(result.validation_failures) == 1
        assert result.validation_failures[0].reason == "missing_destination"
        assert result.validation_failures[0].shipment_id == "shp_bad"


# ---------------------------------------------------------------------------
# Tests: run_migration wrapper
# ---------------------------------------------------------------------------


class TestRunMigration:
    """Test the top-level run_migration function."""

    @pytest.mark.asyncio
    async def test_run_migration_dry_run(self) -> None:
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        es.seed_shipment(_make_shipment(shipment_id="shp_001"))

        result = await run_migration(
            tenant_id=TENANT_ID,
            dry_run=True,
            batch_size=100,
            es_service=es,
            poison_queue_service=poison_queue,
        )

        assert result.shipments_found == 1
        assert result.shipments_migrated == 1

    @pytest.mark.asyncio
    async def test_run_migration_execute(self) -> None:
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        es.seed_shipment(_make_shipment(shipment_id="shp_001"))
        es.seed_rider(_make_rider(rider_id="rider_001"))

        result = await run_migration(
            tenant_id=TENANT_ID,
            dry_run=False,
            batch_size=100,
            es_service=es,
            poison_queue_service=poison_queue,
        )

        assert result.shipments_migrated == 1
        assert result.riders_migrated == 1

        # Verify audit log would be emitted (no assertion on stdout,
        # just verify no errors)
        assert len(result.errors) == 0


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_tenant_no_errors(self) -> None:
        """A tenant with no shipments or riders produces a clean result."""
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        result = await migrate_tenant(
            tenant_id="empty-tenant",
            es_service=es,
            poison_queue_service=poison_queue,
            dry_run=False,
            batch_size=100,
        )

        assert result.shipments_found == 0
        assert result.riders_found == 0
        assert len(result.errors) == 0

    @pytest.mark.asyncio
    async def test_shipment_without_rider_id(self) -> None:
        """Shipments without a rider_id still migrate (driver_id = None)."""
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        es.seed_shipment(_make_shipment(shipment_id="shp_no_rider", rider_id=None))

        result = await migrate_tenant(
            tenant_id=TENANT_ID,
            es_service=es,
            poison_queue_service=poison_queue,
            dry_run=False,
            batch_size=100,
        )

        assert result.shipments_migrated == 1
        orders = es.get_indexed_docs(FUEL_ORDERS_CURRENT_INDEX)
        assert orders["shp_no_rider"]["assigned_driver_id"] is None

    @pytest.mark.asyncio
    async def test_legacy_origin_snapshot_preserved(self) -> None:
        """Task 17.2: legacy_origin_snapshot preserves original origin."""
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        es.seed_shipment(
            _make_shipment(shipment_id="shp_origin", origin="Terminal XYZ")
        )

        await migrate_tenant(
            tenant_id=TENANT_ID,
            es_service=es,
            poison_queue_service=poison_queue,
            dry_run=False,
            batch_size=100,
        )

        orders = es.get_indexed_docs(FUEL_ORDERS_CURRENT_INDEX)
        assert orders["shp_origin"]["legacy_origin_snapshot"] == "Terminal XYZ"

    @pytest.mark.asyncio
    async def test_fill_to_full_set_true(self) -> None:
        """Task 17.2: fill_to_full=true so validator accepts null gallons."""
        es = FakeEsService()
        poison_queue = FakePoisonQueueService()

        es.seed_shipment(_make_shipment(shipment_id="shp_fill"))

        await migrate_tenant(
            tenant_id=TENANT_ID,
            es_service=es,
            poison_queue_service=poison_queue,
            dry_run=False,
            batch_size=100,
        )

        orders = es.get_indexed_docs(FUEL_ORDERS_CURRENT_INDEX)
        order = orders["shp_fill"]
        assert order["fill_to_full"] is True
        assert order["gallons_requested"] is None
        assert order["product_code"] is None
