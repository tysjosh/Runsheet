#!/usr/bin/env python3
"""CLI importer for Tax_Engine jurisdictional rate rows.

Loads a CSV of federal / state / county / city fuel excise + UST / SPCC /
environmental rates and indexes each row into the ``tax_jurisdictions``
Elasticsearch index consumed by
``compliance.services.tax_engine.TaxEngine`` (Requirement 1.5).

Each row is validated through :class:`compliance.models.jurisdiction_rate.JurisdictionRate`
before indexing; rows that fail validation are logged as warnings and
skipped so a single malformed row does not abort the import.

CSV schema (first line is the header — order of columns is fixed):

    fips_code, jurisdiction_level, jurisdiction_name, tax_type,
    product_codes, rate_cents_per_gallon, effective_date,
    expiry_date, source

* ``product_codes`` is pipe-separated
  (e.g. ``GASOLINE_REG|GASOLINE_PREM|ETHANOL_E85``).
* ``rate_cents_per_gallon`` is in ``RATE_SCALE`` units (tenths of a cent
  per gallon — see ``compliance.services.tax_engine.RATE_SCALE``). The
  federal gasoline rate of 18.4¢/gal is therefore stored as ``184``.
* ``effective_date`` is ISO-8601 (``YYYY-MM-DD``).
* ``expiry_date`` and ``source`` are optional — leave blank to omit.

Example usage
-------------

Seed the demo tenant with the sample US federal + 5 state rates::

    python scripts/import_tax_jurisdictions.py \\
        --csv-file scripts/data/sample_tax_jurisdictions.csv \\
        --tenant-id tenant-demo

Dry run (validate only, no writes)::

    python scripts/import_tax_jurisdictions.py \\
        --csv-file scripts/data/sample_tax_jurisdictions.csv \\
        --tenant-id tenant-demo \\
        --dry-run

Override the ES endpoint for a specific cluster::

    python scripts/import_tax_jurisdictions.py \\
        --csv-file scripts/data/sample_tax_jurisdictions.csv \\
        --tenant-id tenant-demo \\
        --elastic-url https://es.internal.example.com:9243

Validates: Requirement 1.5
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure the project root is on sys.path when running as a standalone script.
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from compliance.models.jurisdiction_rate import JurisdictionRate
from compliance.services.compliance_es_mappings import TAX_JURISDICTIONS_INDEX

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required CSV columns (header names are validated against this list).
# ---------------------------------------------------------------------------

_REQUIRED_COLUMNS = (
    "fips_code",
    "jurisdiction_level",
    "jurisdiction_name",
    "tax_type",
    "product_codes",
    "rate_cents_per_gallon",
    "effective_date",
)

_OPTIONAL_COLUMNS = (
    "expiry_date",
    "source",
)


# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------


@dataclass
class _ParsedRow:
    """Result of parsing a single CSV row into a JurisdictionRate payload."""

    row_number: int
    payload: Dict[str, Any]


def _parse_optional_date(value: Optional[str]) -> Optional[date]:
    """Parse an optional ISO-8601 date string; empty / None → None."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return date.fromisoformat(stripped)


def _parse_optional_text(value: Optional[str]) -> Optional[str]:
    """Return the stripped text or None when empty."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _parse_product_codes(raw: str) -> List[str]:
    """Split the pipe-separated product_codes column into a list.

    Empty entries are dropped; the JurisdictionRate validator will raise
    if the resulting list ends up empty.
    """
    if raw is None:
        return []
    return [code.strip() for code in raw.split("|") if code.strip()]


def _parse_row(
    row: Dict[str, str],
    row_number: int,
    tenant_id: str,
) -> Dict[str, Any]:
    """Convert a raw CSV row into a ``JurisdictionRate``-ready payload.

    Stamps ``tenant_id`` from the CLI context so the CSV cannot spoof
    cross-tenant records. Parsing is best-effort — type errors surface
    as ``ValueError`` and are caught by the caller.
    """
    rate_raw = (row.get("rate_cents_per_gallon") or "").strip()
    if not rate_raw:
        raise ValueError("rate_cents_per_gallon must not be empty")
    rate_value = int(rate_raw)

    effective_raw = (row.get("effective_date") or "").strip()
    if not effective_raw:
        raise ValueError("effective_date must not be empty")

    payload: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "fips_code": (row.get("fips_code") or "").strip(),
        "jurisdiction_level": (row.get("jurisdiction_level") or "").strip(),
        "jurisdiction_name": _parse_optional_text(row.get("jurisdiction_name")),
        "tax_type": (row.get("tax_type") or "").strip(),
        "product_codes": _parse_product_codes(row.get("product_codes", "")),
        "rate_cents_per_gallon": rate_value,
        "effective_date": date.fromisoformat(effective_raw),
        "expiry_date": _parse_optional_date(row.get("expiry_date")),
        "source": _parse_optional_text(row.get("source")),
    }
    return payload


# ---------------------------------------------------------------------------
# CSV ingestion
# ---------------------------------------------------------------------------


def _validate_header(fieldnames: Optional[List[str]]) -> None:
    """Raise ``ValueError`` if the CSV is missing any required columns."""
    if not fieldnames:
        raise ValueError("CSV file has no header row")
    seen = {name.strip() for name in fieldnames if name}
    missing = [col for col in _REQUIRED_COLUMNS if col not in seen]
    if missing:
        raise ValueError(
            f"CSV header is missing required column(s): {', '.join(missing)}"
        )


def load_jurisdiction_rates(
    csv_path: Path,
    tenant_id: str,
) -> List[JurisdictionRate]:
    """Parse and validate every row in *csv_path*.

    Returns the list of successfully-validated :class:`JurisdictionRate`
    instances. Malformed rows are logged as warnings and skipped so a
    single bad row does not abort the import.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    validated: List[JurisdictionRate] = []
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        _validate_header(reader.fieldnames)

        for row_number, row in enumerate(reader, start=2):  # row 1 is header
            # Skip blank lines (all-empty-values rows that DictReader still yields).
            if not any((value or "").strip() for value in row.values()):
                continue

            try:
                payload = _parse_row(row, row_number, tenant_id)
                rate = JurisdictionRate.model_validate(payload)
            except Exception as exc:
                logger.warning(
                    "Row %d skipped (validation failed): %s | row=%s",
                    row_number,
                    exc,
                    row,
                )
                continue

            validated.append(rate)

    return validated


# ---------------------------------------------------------------------------
# ES indexing
# ---------------------------------------------------------------------------


async def _index_rates(
    rates: List[JurisdictionRate],
    elastic_url: Optional[str],
) -> int:
    """Write each rate into the ``tax_jurisdictions`` index.

    Uses the existing :class:`services.elasticsearch_service.ElasticsearchService`
    so the script honors the same circuit-breaker wiring as the REST endpoints.

    *elastic_url* is accepted and ignored. It used to override
    ``ELASTIC_ENDPOINT`` for one invocation, pointing the import at a different
    cluster; the document store is the application database and there is no
    per-invocation endpoint to redirect. The argument stays so an existing
    ``--elastic-url ...`` command line does not fail, and ``main`` warns when it
    is supplied — silently ignoring it would let someone believe they had
    imported into a different environment.
    """
    if elastic_url:
        logger.warning(
            "--elastic-url is ignored: documents are written to the application "
            "database (DATABASE_URL), not to an Elasticsearch endpoint."
        )

    from services.elasticsearch_service import ElasticsearchService

    es_service = ElasticsearchService()

    indexed = 0
    for rate in rates:
        document = rate.model_dump(mode="json")
        try:
            await es_service.index_document(
                TAX_JURISDICTIONS_INDEX,
                rate.jurisdiction_id,
                document,
            )
        except Exception as exc:
            logger.error(
                "Failed to index jurisdiction_id=%s (fips=%s level=%s tax_type=%s): %s",
                rate.jurisdiction_id,
                rate.fips_code,
                rate.jurisdiction_level,
                rate.tax_type,
                exc,
            )
            continue

        indexed += 1
        logger.info(
            "Indexed jurisdiction_id=%s fips=%s level=%s tax_type=%s "
            "rate=%d products=%s",
            rate.jurisdiction_id,
            rate.fips_code,
            rate.jurisdiction_level,
            rate.tax_type,
            rate.rate_cents_per_gallon,
            ",".join(rate.product_codes),
        )

    return indexed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _run(
    csv_file: Path,
    tenant_id: str,
    elastic_url: Optional[str],
    dry_run: bool,
) -> int:
    """Parse the CSV, optionally index the rows, and return the indexed count."""
    logger.info(
        "Loading jurisdictional rates: csv=%s tenant=%s dry_run=%s",
        csv_file,
        tenant_id,
        dry_run,
    )
    rates = load_jurisdiction_rates(csv_file, tenant_id)
    logger.info("Parsed %d valid row(s) from %s", len(rates), csv_file)

    if dry_run:
        logger.info("Dry-run: skipping ES indexing. Parsed rows:")
        for rate in rates:
            logger.info(
                "  fips=%s level=%s tax_type=%s rate=%d products=%s "
                "effective=%s expiry=%s",
                rate.fips_code,
                rate.jurisdiction_level,
                rate.tax_type,
                rate.rate_cents_per_gallon,
                ",".join(rate.product_codes),
                rate.effective_date.isoformat(),
                rate.expiry_date.isoformat() if rate.expiry_date else "(open)",
            )
        return 0

    indexed = await _index_rates(rates, elastic_url)
    logger.info("Indexed %d / %d row(s) into %s", indexed, len(rates), TAX_JURISDICTIONS_INDEX)
    return indexed


def main() -> None:
    """Parse CLI arguments and invoke :func:`_run`."""
    parser = argparse.ArgumentParser(
        description=(
            "Import fuel-tax jurisdictional rate rows from a CSV file into "
            "the tax_jurisdictions Elasticsearch index."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Seed the demo tenant with the sample US federal + 5 state rates:
  python scripts/import_tax_jurisdictions.py \\
      --csv-file scripts/data/sample_tax_jurisdictions.csv \\
      --tenant-id tenant-demo

  # Dry run (validate only, no writes):
  python scripts/import_tax_jurisdictions.py \\
      --csv-file scripts/data/sample_tax_jurisdictions.csv \\
      --tenant-id tenant-demo \\
      --dry-run

  # Override the ES endpoint:
  python scripts/import_tax_jurisdictions.py \\
      --csv-file scripts/data/sample_tax_jurisdictions.csv \\
      --tenant-id tenant-demo \\
      --elastic-url https://es.internal.example.com:9243
""",
    )
    parser.add_argument(
        "--csv-file",
        required=True,
        type=Path,
        help="Path to the CSV file containing jurisdictional rate rows.",
    )
    parser.add_argument(
        "--tenant-id",
        required=True,
        help="Tenant identifier stamped on every imported row.",
    )
    parser.add_argument(
        "--elastic-url",
        default=None,
        help=(
            "Deprecated and ignored. Documents go to the application database "
            "(DATABASE_URL); there is no Elasticsearch endpoint to override."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate rows without writing them.",
    )

    args = parser.parse_args()

    tenant_id = args.tenant_id.strip()
    if not tenant_id:
        parser.error("--tenant-id must not be empty")

    try:
        indexed = asyncio.run(
            _run(args.csv_file, tenant_id, args.elastic_url, args.dry_run)
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        sys.exit(2)
    except ValueError as exc:
        logger.error("CSV validation error: %s", exc)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 — top-level catch for CLI
        logger.exception("Import failed: %s", exc)
        sys.exit(1)

    if not args.dry_run and indexed == 0:
        logger.warning("No rows were indexed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
