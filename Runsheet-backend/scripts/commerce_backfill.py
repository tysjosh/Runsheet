#!/usr/bin/env python3
"""CLI entry point for the Commerce Backbone backfill migration.

Accepts --tenant-id, --dry-run, and --phase flags. Instantiates
CommerceBackfill and writes a migration report to
migration_reports/commerce/{tenant_id}_{timestamp}.json.

Usage:
    python -m scripts.commerce_backfill --tenant-id tenant-abc
    python -m scripts.commerce_backfill --tenant-id tenant-abc --dry-run
    python -m scripts.commerce_backfill --tenant-id tenant-abc --phase 1
    python scripts/commerce_backfill.py --tenant-id tenant-abc --phase 2

Design reference: §9 (Migration strategy)
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

from commerce.migration.commerce_backfill import CommerceBackfill
from config.settings import get_settings
from services.elasticsearch_service import ElasticsearchService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Report output
# ---------------------------------------------------------------------------

REPORTS_DIR = Path(_project_root) / "migration_reports" / "commerce"


def _write_report(tenant_id: str, report: dict) -> Path:
    """Write the migration report to a JSON file.

    Returns the path to the written file.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{tenant_id}_{timestamp}.json"
    filepath = REPORTS_DIR / filename

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info("Migration report written to %s", filepath)
    return filepath


# ---------------------------------------------------------------------------
# QBO connector factory (optional)
# ---------------------------------------------------------------------------


def _get_qbo_connector():
    """Attempt to instantiate the QBO connector.

    Returns None if the connector is not available or not configured.
    """
    try:
        from integrations.quickbooks_online import QuickBooksOnlineConnector

        settings = get_settings()
        if hasattr(settings, "qbo_client_id") and settings.qbo_client_id:
            return QuickBooksOnlineConnector(settings)
    except (ImportError, Exception) as exc:
        logger.warning("QBO connector not available: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _run_backfill(tenant_id: str, dry_run: bool, phase: int | None) -> dict:
    """Instantiate services and run the backfill."""
    es_service = ElasticsearchService()
    es_service.connect()

    qbo_connector = _get_qbo_connector()

    backfill = CommerceBackfill(es_service, qbo_connector)
    report = await backfill.run(tenant_id, dry_run=dry_run, phase=phase)
    return report


def main() -> None:
    """Parse CLI arguments and execute the backfill."""
    parser = argparse.ArgumentParser(
        description="Commerce Backbone backfill migration script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run scan (no writes):
  python -m scripts.commerce_backfill --tenant-id tenant-abc --dry-run

  # Run all phases:
  python -m scripts.commerce_backfill --tenant-id tenant-abc

  # Run only Phase 1 (customers + accounts):
  python -m scripts.commerce_backfill --tenant-id tenant-abc --phase 1

  # Run only Phase 4 (verification):
  python -m scripts.commerce_backfill --tenant-id tenant-abc --phase 4
""",
    )
    parser.add_argument(
        "--tenant-id",
        required=True,
        help="Target tenant identifier",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Scan and report only — write no data",
    )
    parser.add_argument(
        "--phase",
        type=int,
        choices=[1, 2, 3, 4],
        default=None,
        help="Run only the specified phase (1-4). Omit to run all phases.",
    )

    args = parser.parse_args()

    logger.info(
        "Starting commerce backfill: tenant=%s dry_run=%s phase=%s",
        args.tenant_id,
        args.dry_run,
        args.phase,
    )

    report = asyncio.run(_run_backfill(args.tenant_id, args.dry_run, args.phase))

    # Write report
    report_path = _write_report(args.tenant_id, report)

    # Print summary
    status = report.get("status", "unknown")
    if status == "success":
        logger.info("Backfill completed successfully for tenant %s", args.tenant_id)
    else:
        logger.error(
            "Backfill finished with status '%s' for tenant %s. Errors: %s",
            status,
            args.tenant_id,
            report.get("errors", []),
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
