#!/usr/bin/env python3
"""
Order Intake Pipeline Migration 001 — Rename Shipment to Order.

Two-phase migration script per design §14 of the order-intake-pipeline spec.

Purpose
-------
Migrates existing ``shipments_current`` documents into ``fuel_orders_current``
and ``riders_current`` documents into ``drivers_current``. This is the one-time
data migration that enables the ``active_gated`` → ``active_auto`` transition
for a tenant.

The migration:
1. Reads every ``shipments_current`` doc for the tenant.
2. Synthesizes a FuelOrder with:
   - ``order_id`` = original ``shipment_id`` (preserves external refs)
   - ``intake_channel="legacy"``
   - ``intake_channel_id="pre-migration"``
   - ``intake_metadata.legacy_shipment_id=<shipment_id>``
   - ``call_type="one_off"``
   - ``fill_to_full=true`` (so validator accepts null gallons_requested)
   - ``product_code=null`` (legacy channel exempts it)
   - ``source_schema_version="legacy"``
   - ``legacy_origin_snapshot=<original shipments_current.origin>``
   - ``status`` mapped from the source shipment's status
3. Preserves ``rider_id`` → ``driver_id`` in the drivers_current index.
4. Idempotent: re-running finds the target already exists and skips.
5. Unmappable shipments route to ``ops_poison_queue`` with reason
   ``legacy_shipment_unmappable``.

Usage
-----
Phase 1 — dry-run::

    python -m scripts.migrations.order_intake_pipeline_001_rename_shipment_to_order \\
        --tenant-id tenant-a --dry-run

Phase 2 — execute (requires --confirm)::

    python -m scripts.migrations.order_intake_pipeline_001_rename_shipment_to_order \\
        --tenant-id tenant-a --execute --confirm

CLI Args
---------
--tenant-id     Target tenant (required).
--dry-run       Phase 1: report counts, validation failures, poison-queue candidates.
--execute       Phase 2: perform the actual migration.
--confirm       Safety flag required alongside --execute.
--batch-size    Number of documents to process per ES scroll page (default: 500).

Validates: Requirements 9.4.1, 9.4.2, 9.4.3, 9.4.4, 9.4.5, 9.4.6.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone as _dt_timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure the project root is on sys.path so imports resolve when the script
# is executed as ``python -m scripts.migrations.order_intake_pipeline_001_...``
# or as a standalone ``python scripts/migrations/...``.
_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("order_intake_pipeline_001")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SHIPMENTS_CURRENT_INDEX = "shipments_current"
RIDERS_CURRENT_INDEX = "riders_current"
FUEL_ORDERS_CURRENT_INDEX = "fuel_orders_current"
DRIVERS_CURRENT_INDEX = "drivers_current"
POISON_QUEUE_INDEX = "ops_poison_queue"

DEFAULT_BATCH_SIZE = 500

#: Valid shipment statuses that map directly to FuelOrder statuses.
SHIPMENT_STATUS_MAP: Dict[str, str] = {
    "placed": "placed",
    "confirmed": "confirmed",
    "scheduled": "scheduled",
    "dispatched": "dispatched",
    "in_transit": "in_transit",
    "delivered": "delivered",
    "failed": "failed",
    "cancelled": "cancelled",
    "on_hold": "on_hold",
    # Legacy statuses that don't map cleanly default to "placed"
    "pending": "placed",
    "assigned": "confirmed",
    "picked_up": "in_transit",
    "completed": "delivered",
}

#: Rider statuses that map to driver statuses.
RIDER_STATUS_MAP: Dict[str, str] = {
    "active": "active",
    "inactive": "inactive",
    "on_break": "on_break",
    "off_duty": "off_duty",
    # Legacy fallbacks
    "available": "active",
    "busy": "active",
    "offline": "off_duty",
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ValidationFailure:
    """A single validation failure for a shipment that cannot be migrated."""

    shipment_id: str
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MigrationResult:
    """Per-tenant migration outcome."""

    tenant_id: str
    shipments_found: int = 0
    shipments_migrated: int = 0
    shipments_skipped_existing: int = 0
    shipments_poisoned: int = 0
    riders_found: int = 0
    riders_migrated: int = 0
    riders_skipped_existing: int = 0
    validation_failures: List[ValidationFailure] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def as_log_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "shipments_found": self.shipments_found,
            "shipments_migrated": self.shipments_migrated,
            "shipments_skipped_existing": self.shipments_skipped_existing,
            "shipments_poisoned": self.shipments_poisoned,
            "riders_found": self.riders_found,
            "riders_migrated": self.riders_migrated,
            "riders_skipped_existing": self.riders_skipped_existing,
            "validation_failure_count": len(self.validation_failures),
            "errors": list(self.errors),
        }

    def poison_queue_summary(self) -> Dict[str, int]:
        """Group validation failures by reason for dry-run output."""
        summary: Dict[str, int] = {}
        for vf in self.validation_failures:
            summary[vf.reason] = summary.get(vf.reason, 0) + 1
        return summary


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_shipment_for_migration(
    shipment: Dict[str, Any],
) -> Optional[ValidationFailure]:
    """Validate a shipment document can be migrated.

    Returns a ValidationFailure if the shipment is unmappable, or None
    if it passes validation.
    """
    shipment_id = shipment.get("shipment_id", "<unknown>")

    # Must have a tenant_id
    tenant_id = shipment.get("tenant_id")
    if not tenant_id or not isinstance(tenant_id, str) or not tenant_id.strip():
        return ValidationFailure(
            shipment_id=shipment_id,
            reason="missing_tenant_id",
            details={"field": "tenant_id", "value": tenant_id},
        )

    # Must have a shipment_id
    if not shipment_id or not isinstance(shipment_id, str) or not shipment_id.strip():
        return ValidationFailure(
            shipment_id=str(shipment_id),
            reason="missing_shipment_id",
            details={"field": "shipment_id"},
        )

    # Must have valid coordinates (current_location)
    current_location = shipment.get("current_location")
    if current_location:
        if isinstance(current_location, dict):
            lat = current_location.get("lat")
            lon = current_location.get("lon")
        elif isinstance(current_location, str) and "," in current_location:
            parts = current_location.split(",", 1)
            try:
                lat = float(parts[0].strip())
                lon = float(parts[1].strip())
            except (ValueError, TypeError):
                lat = lon = None
        else:
            lat = lon = None

        if lat is not None and lon is not None:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= lon_f <= 180.0):
                    return ValidationFailure(
                        shipment_id=shipment_id,
                        reason="malformed_coordinates",
                        details={
                            "lat": lat_f,
                            "lon": lon_f,
                            "issue": "out_of_range",
                        },
                    )
            except (TypeError, ValueError):
                return ValidationFailure(
                    shipment_id=shipment_id,
                    reason="malformed_coordinates",
                    details={"current_location": str(current_location)},
                )
    # If no current_location, we'll use defaults (0.0, 0.0) — not ideal
    # but acceptable for legacy data that may not have coordinates.

    # Must have a destination (maps to ship_to_address)
    destination = shipment.get("destination")
    if not destination or not isinstance(destination, str) or not destination.strip():
        return ValidationFailure(
            shipment_id=shipment_id,
            reason="missing_destination",
            details={"field": "destination"},
        )

    return None


def _extract_coordinates(
    shipment: Dict[str, Any],
) -> Tuple[float, float]:
    """Extract lat/lon from a shipment's current_location field.

    Returns (0.0, 0.0) as a safe default when coordinates are absent
    or unparseable — legacy data may not have location data.
    """
    current_location = shipment.get("current_location")
    if not current_location:
        return (0.0, 0.0)

    if isinstance(current_location, dict):
        lat = current_location.get("lat")
        lon = current_location.get("lon")
    elif isinstance(current_location, str) and "," in current_location:
        parts = current_location.split(",", 1)
        try:
            lat = float(parts[0].strip())
            lon = float(parts[1].strip())
        except (ValueError, TypeError):
            return (0.0, 0.0)
    else:
        return (0.0, 0.0)

    try:
        return (float(lat), float(lon))
    except (TypeError, ValueError):
        return (0.0, 0.0)


def _map_shipment_status(status: Optional[str]) -> str:
    """Map a legacy shipment status to a FuelOrder status."""
    if not status:
        return "placed"
    return SHIPMENT_STATUS_MAP.get(status.lower().strip(), "placed")


def _map_rider_status(status: Optional[str]) -> str:
    """Map a legacy rider status to a Driver status."""
    if not status:
        return "active"
    return RIDER_STATUS_MAP.get(status.lower().strip(), "active")


# ---------------------------------------------------------------------------
# Document transformation
# ---------------------------------------------------------------------------


def transform_shipment_to_fuel_order(
    shipment: Dict[str, Any],
    now: datetime,
) -> Dict[str, Any]:
    """Transform a validated shipment document into a FuelOrder document.

    Preserves the shipment_id as order_id so external references keep
    resolving. Synthesizes all required FuelOrder fields per design §14.
    """
    shipment_id = shipment["shipment_id"]
    tenant_id = shipment["tenant_id"]
    lat, lon = _extract_coordinates(shipment)
    status = _map_shipment_status(shipment.get("status"))

    # Preserve the original origin for rollback from active_auto
    legacy_origin = shipment.get("origin") or ""

    # Use existing timestamps or fall back to now
    created_at = shipment.get("created_at") or now.isoformat()
    updated_at = shipment.get("updated_at") or now.isoformat()
    last_event_ts = shipment.get("last_event_timestamp") or now.isoformat()

    fuel_order = {
        "order_id": shipment_id,
        "tenant_id": tenant_id,
        "customer_id": f"legacy_{tenant_id}",
        "customer_name": "Legacy Customer",
        "customer_phone": None,
        "customer_email": None,
        "ship_to_address": shipment.get("destination", "Unknown"),
        "ship_to_lat": lat,
        "ship_to_lon": lon,
        "customer_tank_id": None,
        "product_code": None,
        "gallons_requested": None,
        "fill_to_full": True,
        "call_type": "one_off",
        "delivery_window_start": None,
        "delivery_window_end": None,
        "hold_reason": None,
        "po_number": None,
        "special_instructions": None,
        "intake_channel": "legacy",
        "intake_channel_id": "pre-migration",
        "intake_metadata": {
            "legacy_shipment_id": shipment_id,
        },
        "status": status,
        "assigned_driver_id": shipment.get("rider_id"),
        "assigned_run_id": None,
        "legacy_origin_snapshot": legacy_origin,
        "source_schema_version": "legacy",
        "trace_id": shipment.get("trace_id") or f"migration_{shipment_id}",
        "created_at": created_at,
        "updated_at": updated_at,
        "last_event_timestamp": last_event_ts,
    }

    return fuel_order


def transform_rider_to_driver(
    rider: Dict[str, Any],
    now: datetime,
) -> Dict[str, Any]:
    """Transform a validated rider document into a Driver document.

    Preserves the rider_id as driver_id so external references keep
    resolving.
    """
    rider_id = rider["rider_id"]
    tenant_id = rider["tenant_id"]
    status = _map_rider_status(rider.get("status"))

    # Use existing timestamps or fall back to now
    created_at = rider.get("ingested_at") or now.isoformat()
    updated_at = rider.get("last_event_timestamp") or now.isoformat()
    last_event_ts = rider.get("last_event_timestamp") or now.isoformat()
    last_seen = rider.get("last_seen")

    # Extract current_location
    current_location = rider.get("current_location")
    if isinstance(current_location, dict):
        driver_location = current_location
    elif isinstance(current_location, str) and "," in current_location:
        parts = current_location.split(",", 1)
        try:
            driver_location = {
                "lat": float(parts[0].strip()),
                "lon": float(parts[1].strip()),
            }
        except (ValueError, TypeError):
            driver_location = None
    else:
        driver_location = None

    driver = {
        "driver_id": rider_id,
        "tenant_id": tenant_id,
        "driver_name": rider.get("rider_name") or f"Driver {rider_id}",
        "phone": None,
        "status": status,
        "availability": rider.get("availability"),
        "assigned_truck_id": None,
        "cdl_class": None,
        "hazmat_endorsement": None,
        "medical_card_expiry": None,
        "current_location": driver_location,
        "last_seen": last_seen,
        "active_order_count": rider.get("active_shipment_count", 0) or 0,
        "completed_today": rider.get("completed_today", 0) or 0,
        "last_event_timestamp": last_event_ts,
        "source_schema_version": "legacy",
        "trace_id": rider.get("trace_id") or f"migration_{rider_id}",
        "created_at": created_at,
        "updated_at": updated_at,
    }

    return driver


# ---------------------------------------------------------------------------
# Elasticsearch helpers
# ---------------------------------------------------------------------------


async def _scan_index(
    es_service: Any,
    index: str,
    tenant_id: str,
    batch_size: int,
) -> List[Dict[str, Any]]:
    """Scan all documents for a tenant from an index using pagination."""
    documents: List[Dict[str, Any]] = []
    offset = 0

    while True:
        query = {
            "query": {"term": {"tenant_id": tenant_id}},
            "from": offset,
            "size": batch_size,
            "sort": [{"_id": {"order": "asc"}}],
        }
        try:
            resp = await es_service.search_documents(index, query, batch_size)
        except Exception as exc:
            logger.warning(
                "scan_index: index=%s tenant=%s offset=%d failed: %s",
                index, tenant_id, offset, exc,
            )
            break

        hits = ((resp or {}).get("hits") or {}).get("hits") or []
        if not hits:
            break

        for hit in hits:
            src = hit.get("_source") or {}
            documents.append(src)

        if len(hits) < batch_size:
            break
        offset += batch_size

    return documents


async def _document_exists(
    es_service: Any,
    index: str,
    doc_id: str,
) -> bool:
    """Check if a document with the given ID already exists in the index."""
    try:
        resp = await es_service.search_documents(
            index,
            {"query": {"term": {"_id": doc_id}}, "size": 1},
            1,
        )
        hits = ((resp or {}).get("hits") or {}).get("hits") or []
        return len(hits) > 0
    except Exception:
        # Also try a direct get-style query by the domain ID field
        pass

    # Fallback: search by the domain-level ID field
    if index == FUEL_ORDERS_CURRENT_INDEX:
        id_field = "order_id"
    elif index == DRIVERS_CURRENT_INDEX:
        id_field = "driver_id"
    else:
        return False

    try:
        resp = await es_service.search_documents(
            index,
            {"query": {"term": {id_field: doc_id}}, "size": 1},
            1,
        )
        hits = ((resp or {}).get("hits") or {}).get("hits") or []
        return len(hits) > 0
    except Exception:
        return False


async def _index_document(
    es_service: Any,
    index: str,
    doc_id: str,
    document: Dict[str, Any],
) -> bool:
    """Index a document with the given ID. Returns True on success."""
    try:
        es = es_service.es_service if hasattr(es_service, "es_service") else es_service
        client = es.client if hasattr(es, "client") else es
        await client.index(index=index, id=doc_id, document=document)
        return True
    except Exception as exc:
        logger.error(
            "index_document: index=%s id=%s failed: %s", index, doc_id, exc,
        )
        return False


async def _store_poison_queue_entry(
    poison_queue_service: Any,
    shipment: Dict[str, Any],
    reason: str,
    tenant_id: str,
) -> None:
    """Route an unmappable shipment to the poison queue."""
    try:
        await poison_queue_service.store_failed_event(
            payload=shipment,
            error=f"Legacy shipment unmappable: {reason}",
            error_type="legacy_shipment_unmappable",
            tenant_id=tenant_id,
            trace_id=shipment.get("trace_id", ""),
        )
    except Exception as exc:
        logger.error(
            "Failed to store poison queue entry for shipment=%s: %s",
            shipment.get("shipment_id", "<unknown>"),
            exc,
        )


# ---------------------------------------------------------------------------
# Per-tenant migration
# ---------------------------------------------------------------------------


async def migrate_tenant(
    *,
    tenant_id: str,
    es_service: Any,
    poison_queue_service: Any,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> MigrationResult:
    """Run the migration for a single tenant.

    Parameters are passed explicitly so tests can substitute mocks.
    """
    result = MigrationResult(tenant_id=tenant_id)
    now = datetime.now(_dt_timezone.utc)

    # --- Phase: Migrate shipments → fuel_orders ---
    logger.info(
        "tenant=%s scanning shipments_current dry_run=%s",
        tenant_id, dry_run,
    )
    shipments = await _scan_index(
        es_service, SHIPMENTS_CURRENT_INDEX, tenant_id, batch_size,
    )
    result.shipments_found = len(shipments)

    for shipment in shipments:
        shipment_id = shipment.get("shipment_id", "<unknown>")

        # Validate
        failure = validate_shipment_for_migration(shipment)
        if failure is not None:
            result.validation_failures.append(failure)
            result.shipments_poisoned += 1
            if not dry_run:
                await _store_poison_queue_entry(
                    poison_queue_service, shipment, failure.reason, tenant_id,
                )
            continue

        # Idempotency check — skip if already migrated
        if not dry_run:
            exists = await _document_exists(
                es_service, FUEL_ORDERS_CURRENT_INDEX, shipment_id,
            )
            if exists:
                result.shipments_skipped_existing += 1
                continue

        # Transform
        fuel_order = transform_shipment_to_fuel_order(shipment, now)

        # Write (only in execute mode)
        if not dry_run:
            success = await _index_document(
                es_service, FUEL_ORDERS_CURRENT_INDEX, shipment_id, fuel_order,
            )
            if success:
                result.shipments_migrated += 1
            else:
                result.errors.append(
                    f"Failed to index fuel_order for shipment={shipment_id}"
                )
        else:
            result.shipments_migrated += 1

    # --- Phase: Migrate riders → drivers ---
    logger.info(
        "tenant=%s scanning riders_current dry_run=%s",
        tenant_id, dry_run,
    )
    riders = await _scan_index(
        es_service, RIDERS_CURRENT_INDEX, tenant_id, batch_size,
    )
    result.riders_found = len(riders)

    for rider in riders:
        rider_id = rider.get("rider_id", "<unknown>")

        # Basic validation for riders
        if not rider.get("tenant_id"):
            result.validation_failures.append(
                ValidationFailure(
                    shipment_id=rider_id,
                    reason="missing_tenant_id",
                    details={"entity": "rider"},
                )
            )
            continue

        # Idempotency check
        if not dry_run:
            exists = await _document_exists(
                es_service, DRIVERS_CURRENT_INDEX, rider_id,
            )
            if exists:
                result.riders_skipped_existing += 1
                continue

        # Transform
        driver = transform_rider_to_driver(rider, now)

        # Write (only in execute mode)
        if not dry_run:
            success = await _index_document(
                es_service, DRIVERS_CURRENT_INDEX, rider_id, driver,
            )
            if success:
                result.riders_migrated += 1
            else:
                result.errors.append(
                    f"Failed to index driver for rider={rider_id}"
                )
        else:
            result.riders_migrated += 1

    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_migration(
    *,
    tenant_id: str,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    es_service: Optional[Any] = None,
    poison_queue_service: Optional[Any] = None,
) -> MigrationResult:
    """Run the migration for a tenant.

    All dependencies are injectable so tests can drive the migration
    with mocks or in-memory fakes.

    Args:
        tenant_id: The tenant to migrate.
        dry_run: When True, report what would happen without writing.
        batch_size: ES pagination size.
        es_service: Optional ES service (lazy-loaded if None).
        poison_queue_service: Optional poison queue service.

    Returns:
        MigrationResult with counts and any failures.
    """
    # Lazy-import production dependencies
    if es_service is None:
        from services.elasticsearch_service import (
            elasticsearch_service as _es,
        )
        es_service = _es

    if poison_queue_service is None:
        from ops.services.ops_es_service import OpsElasticsearchService
        ops_es = OpsElasticsearchService(es_service)
        from ops.ingestion.poison_queue import PoisonQueueService
        poison_queue_service = PoisonQueueService(ops_es)

    result = await migrate_tenant(
        tenant_id=tenant_id,
        es_service=es_service,
        poison_queue_service=poison_queue_service,
        dry_run=dry_run,
        batch_size=batch_size,
    )

    # Log the result
    mode = "DRY-RUN" if dry_run else "EXECUTE"
    logger.info(
        "order_intake_pipeline_001 [%s]: tenant=%s result=%s",
        mode,
        tenant_id,
        json.dumps(result.as_log_dict(), default=str),
    )

    # Print human-readable summary
    if dry_run:
        print(f"\n{'='*60}")
        print(f"DRY-RUN REPORT — Tenant: {tenant_id}")
        print(f"{'='*60}")
        print(f"  Shipments found:          {result.shipments_found}")
        print(f"  Would migrate:            {result.shipments_migrated}")
        print(f"  Would go to poison queue: {result.shipments_poisoned}")
        print(f"  Riders found:             {result.riders_found}")
        print(f"  Would migrate riders:     {result.riders_migrated}")
        if result.validation_failures:
            print(f"\n  Poison-queue candidates ({len(result.validation_failures)}):")
            for reason, count in result.poison_queue_summary().items():
                print(f"    {reason}: {count}")
        print(f"{'='*60}\n")
    else:
        print(f"\n{'='*60}")
        print(f"EXECUTION SUMMARY — Tenant: {tenant_id}")
        print(f"{'='*60}")
        print(f"  Shipments found:          {result.shipments_found}")
        print(f"  Migrated:                 {result.shipments_migrated}")
        print(f"  Skipped (already exist):  {result.shipments_skipped_existing}")
        print(f"  Routed to poison queue:   {result.shipments_poisoned}")
        print(f"  Riders found:             {result.riders_found}")
        print(f"  Riders migrated:          {result.riders_migrated}")
        print(f"  Riders skipped:           {result.riders_skipped_existing}")
        if result.errors:
            print(f"\n  Errors ({len(result.errors)}):")
            for err in result.errors:
                print(f"    - {err}")
        print(f"{'='*60}\n")

        # Emit audit log entry
        audit_entry = {
            "migration": "order_intake_pipeline_001",
            "tenant_id": tenant_id,
            "executed_at": datetime.now(_dt_timezone.utc).isoformat(),
            "result": result.as_log_dict(),
        }
        logger.info(
            "AUDIT: migration_complete %s",
            json.dumps(audit_entry, default=str),
        )

    return result


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Order Intake Pipeline Migration 001 — "
            "Rename shipments to orders, riders to drivers."
        ),
    )
    parser.add_argument(
        "--tenant-id",
        required=True,
        help="Target tenant ID to migrate.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Phase 1: report counts and validation failures without writing.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Phase 2: perform the actual migration.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Safety flag required alongside --execute.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"ES pagination batch size (default: {DEFAULT_BATCH_SIZE}).",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        parser.error("Must specify either --dry-run or --execute.")
        return 1

    if args.dry_run and args.execute:
        parser.error("Cannot specify both --dry-run and --execute.")
        return 1

    if args.execute and not args.confirm:
        parser.error("--execute requires --confirm for safety.")
        return 1

    dry_run = args.dry_run

    try:
        result = asyncio.run(
            run_migration(
                tenant_id=args.tenant_id,
                dry_run=dry_run,
                batch_size=args.batch_size,
            )
        )
    except Exception as exc:
        logger.exception("Migration failed: %s", exc)
        return 1

    if result.errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
