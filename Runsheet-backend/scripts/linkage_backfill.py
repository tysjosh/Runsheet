#!/usr/bin/env python3
"""On-demand CLI for the cross-module linkage backfill (task 5.1).

Populates the additive linkage references introduced by tasks 2 / 3 for
*in-flight* records:

* order ``assigned_asset_id`` ← linked job's ``asset_assigned``
* job ``order_id`` / ``customer_id`` ← linked order

The links are written through the consistency-preserving order→job assignment
service, so the backfill produces the same guaranteed-consistent linkage the
live path does. Records whose links cannot be derived remain "unlinked" — the
run never fails on them (Req 6.2).

This script is **run on demand only** — it is not wired into bootstrap or any
schema migration.

Usage:
    # Dry-run (classify + count, write nothing):
    python -m scripts.linkage_backfill --tenant-id tenant-abc --dry-run

    # Apply the backfill:
    python -m scripts.linkage_backfill --tenant-id tenant-abc

    # Standalone:
    python scripts/linkage_backfill.py --tenant-id tenant-abc --dry-run

Design reference: cross-module-entity-linkage design.md §Migration / Backfill.
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on sys.path when running as a standalone script.
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from scheduling.migration.linkage_backfill import build_linkage_backfill
from services.elasticsearch_service import ElasticsearchService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

REPORTS_DIR = Path(_project_root) / "migration_reports" / "linkage"


def _write_report(tenant_id: str, report: dict) -> Path:
    """Write the migration report to a timestamped JSON file."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filepath = REPORTS_DIR / f"{tenant_id}_{timestamp}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info("Migration report written to %s", filepath)
    return filepath


async def _run_backfill(tenant_id: str, dry_run: bool, batch_size: int) -> dict:
    """Instantiate the live ES service and run the backfill."""
    es_service = ElasticsearchService()
    es_service.connect()

    backfill = build_linkage_backfill(es_service)
    return await backfill.run(tenant_id, dry_run=dry_run, batch_size=batch_size)


def main() -> None:
    """Parse CLI arguments and execute the backfill."""
    parser = argparse.ArgumentParser(
        description="Cross-module entity linkage backfill (run on demand)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run scan (no writes):
  python -m scripts.linkage_backfill --tenant-id tenant-abc --dry-run

  # Apply the backfill:
  python -m scripts.linkage_backfill --tenant-id tenant-abc
""",
    )
    parser.add_argument("--tenant-id", required=True, help="Target tenant identifier")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Classify and report only — write no data",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Max orders to load in one listing pass (default 500)",
    )

    args = parser.parse_args()

    logger.info(
        "Starting linkage backfill: tenant=%s dry_run=%s batch_size=%s",
        args.tenant_id,
        args.dry_run,
        args.batch_size,
    )

    report = asyncio.run(
        _run_backfill(args.tenant_id, args.dry_run, args.batch_size)
    )

    _write_report(args.tenant_id, report)

    logger.info(
        "Backfill %s for tenant %s — scanned=%s linked=%s skipped=%s unlinked=%s",
        report.get("status"),
        args.tenant_id,
        report.get("scanned"),
        report.get("linked"),
        report.get("skipped"),
        report.get("unlinked"),
    )

    if report.get("status") != "success":
        logger.error("Backfill errors: %s", report.get("errors", []))
        sys.exit(1)


if __name__ == "__main__":
    main()
