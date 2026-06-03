"""Rebuild an Elasticsearch index from the PostgreSQL source-of-truth.

This is the REVERSE of :mod:`persistence.backfill` (which copied ES → PG) and
the reversibility safety net for Phases 5–6 of the migration. Once reads are
cut over to Postgres, the ES ``*_current`` / config / master-data indices are
disposable *projections*. This tool reconstructs any of them from the PG rows:
it reads every row for an aggregate, runs it through the SAME projector the
outbox relay uses (:data:`persistence.projections.PROJECTORS`), (re)creates the
index mapping if it is missing, and indexes each projected document under its
aggregate id.

Because the projection is byte-identical to what the relay writes, a
rebuild-from-PG produces the same index contents the live projection would —
so dropping an index is safe: it can always be reconstructed here.

Usage::

    ENVIRONMENT=development \\
        ./venv/bin/python -m persistence.rebuild_from_postgres \\
            --aggregate shipment --tenant demo-tenant

    # Rebuild every migrated aggregate's index:
    ./venv/bin/python -m persistence.rebuild_from_postgres --all --tenant demo-tenant

``--dry-run`` reports the row counts that WOULD be indexed without writing.
Without ``--tenant`` every tenant's rows are rebuilt.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import selectinload

logger = logging.getLogger("persistence.rebuild_from_postgres")


# aggregate_type -> (ORM attribute name in persistence.models, pk attr,
#                    tenant attr, optional relationship attr to eager-load)
# Order follows dependency / convenience; every entry here has a projector in
# persistence.projections.PROJECTORS.
_REBUILD_SPECS: Dict[str, Tuple[str, str, str, Optional[str]]] = {
    # Commerce (typed-column projectors)
    "customer": ("CustomerORM", "customer_id", "tenant_id", None),
    "account": ("AccountORM", "account_id", "tenant_id", None),
    "invoice": ("InvoiceORM", "invoice_id", "tenant_id", "line_items"),
    "payment": ("PaymentORM", "payment_id", "tenant_id", None),
    "price_book": ("PriceBookORM", "price_book_id", "tenant_id", None),
    "pricing_rule": ("PricingRuleORM", "rule_id", "tenant_id", None),
    "invoice_event": ("InvoiceEventORM", "event_id", "tenant_id", None),
    "account_event": ("AccountEventORM", "event_id", "tenant_id", None),
    "dunning_event": ("DunningEventORM", "event_id", "tenant_id", None),
    "ar_aging_snapshot": ("ArAgingSnapshotORM", "snapshot_id", "tenant_id", None),
    # Compliance config (hybrid document tables)
    "tax_jurisdiction": ("TaxJurisdictionORM", "jurisdiction_id", "tenant_id", None),
    "tax_exemption": ("TaxExemptionORM", "exemption_id", "tenant_id", None),
    "price_protection_contract": ("PriceProtectionContractORM", "contract_id", "tenant_id", None),
    "compliance_pricing_rule": ("CompliancePricingRuleORM", "rule_id", "tenant_id", None),
    "supplier_contract": ("SupplierContractORM", "contract_id", "tenant_id", None),
    # Orders / jobs current-state (hybrid)
    "fuel_order": ("FuelOrderCurrentORM", "order_id", "tenant_id", None),
    "job": ("JobCurrentORM", "job_id", "tenant_id", None),
    "shipment": ("ShipmentCurrentORM", "shipment_id", "tenant_id", None),
    "tenant_job_policy": ("TenantJobPolicyORM", "policy_id", "tenant_id", None),
    # Master data (hybrid)
    "driver": ("DriverMasterORM", "driver_id", "tenant_id", None),
    "depot": ("DepotORM", "depot_id", "tenant_id", None),
    "terminal": ("TerminalORM", "terminal_id", "tenant_id", None),
    "asset_certification": ("AssetCertificationORM", "cert_id", "tenant_id", None),
    "intake_channel": ("IntakeChannelORM", "channel_id", "tenant_id", None),
    "truck": ("TruckORM", "truck_id", "tenant_id", None),
    "location": ("LocationORM", "location_id", "tenant_id", None),
}


def _ensure_index(es_client, index: str) -> None:
    """(Re)create the index with its mapping if it does not already exist.

    Looks the mapping up from the app's central index registry so a rebuilt
    index gets the exact same mapping the app expects. If no mapping is known
    we let ES create it dynamically on first index (still correct, just not
    strict-mapped).
    """
    try:
        if es_client.indices.exists(index=index):
            return
    except Exception:  # noqa: BLE001 — exists check best-effort
        pass

    mapping = _lookup_mapping(index)
    try:
        from services.elasticsearch_service import ElasticsearchService

        if mapping is not None:
            if hasattr(ElasticsearchService, "strip_serverless_incompatible_settings"):
                mapping = ElasticsearchService.strip_serverless_incompatible_settings(mapping)
            es_client.indices.create(index=index, body=mapping)
        else:
            es_client.indices.create(index=index)
        logger.info("Created index %s for rebuild", index)
    except Exception as exc:  # noqa: BLE001
        # An "already exists" race is fine; anything else is worth surfacing.
        logger.info("Index create for %s skipped/failed (continuing): %s", index, exc)


def _lookup_mapping(index: str) -> Optional[Dict[str, Any]]:
    """Best-effort lookup of an index's mapping body from the known registries.

    A dropped index that is recreated WITHOUT its mapping gets ES dynamic
    typing (e.g. ``tenant_id`` becomes ``text`` instead of ``keyword``), which
    silently breaks the ``term`` queries the app and parity check rely on. So
    we consult every domain's index-mapping registry to recreate the index
    with its exact strict mapping.
    """
    registries: List[Dict[str, Any]] = []

    def _try(import_path: str, attr: str) -> None:
        try:
            module = __import__(import_path, fromlist=[attr])
            reg = getattr(module, attr, None)
            if isinstance(reg, dict):
                registries.append(reg)
        except Exception:  # noqa: BLE001 — registry optional
            pass

    _try("scheduling.services.scheduling_es_mappings", "SCHEDULING_INDEX_MAPPINGS")
    _try("fuel.services.order_es_mappings", "ORDER_INTAKE_INDEX_MAPPINGS")
    _try("fuel.services.fuel_ops_es_mappings", "FUEL_OPS_INDEX_MAPPINGS")
    _try("commerce.services.commerce_es_mappings", "COMMERCE_INDEX_MAPPINGS")
    _try("compliance.services.compliance_es_mappings", "COMPLIANCE_INDEX_MAPPINGS")

    for body in registries:
        if index in body:
            return body[index]
    return None


async def rebuild(
    aggregate_type: str,
    tenant_id: Optional[str] = None,
    *,
    dry_run: bool = False,
    batch_size: int = 500,
) -> int:
    """Rebuild one aggregate's ES index from Postgres. Returns docs indexed."""
    from persistence.database import is_persistence_enabled, session_scope
    from persistence.projections import PROJECTORS
    import persistence.models as models
    from services.elasticsearch_service import elasticsearch_service

    if not is_persistence_enabled():
        raise RuntimeError(
            "Rebuild requires DATABASE_URL to be set (persistence layer dormant)."
        )
    if aggregate_type not in _REBUILD_SPECS:
        raise ValueError(f"Unknown aggregate_type: {aggregate_type!r}")
    if aggregate_type not in PROJECTORS:
        raise ValueError(f"No projector registered for {aggregate_type!r}")

    model_name, pk_attr, tenant_attr, eager_attr = _REBUILD_SPECS[aggregate_type]
    model = getattr(models, model_name)
    index, projector = PROJECTORS[aggregate_type]
    es = elasticsearch_service

    if not dry_run:
        _ensure_index(es.client, index)

    indexed = 0
    async with session_scope() as session:
        stmt = select(model)
        if tenant_id:
            stmt = stmt.where(getattr(model, tenant_attr) == tenant_id)
        if eager_attr:
            stmt = stmt.options(selectinload(getattr(model, eager_attr)))
        rows = list((await session.execute(stmt)).scalars().all())

        for row in rows:
            doc = projector(row)
            doc_id = getattr(row, pk_attr)
            if not dry_run:
                # Index the projected document VERBATIM via the raw client.
                # ElasticsearchService.index_document overwrites ``updated_at``
                # with now() on every call, which would rewrite the field and
                # diverge from the PG-stored value; the projection already
                # carries the correct created_at/updated_at, so we write it
                # unchanged to keep a rebuild byte-identical to the source row.
                es.client.index(index=index, id=doc_id, body=doc, refresh=False)
            indexed += 1

    if not dry_run and indexed:
        # Make the rebuilt docs immediately searchable (the per-doc writes use
        # refresh=False for throughput; one refresh at the end is enough).
        try:
            es.client.indices.refresh(index=index)
        except Exception as exc:  # noqa: BLE001
            logger.info("Refresh of %s after rebuild skipped: %s", index, exc)

    verb = "Would index" if dry_run else "Indexed"
    logger.info("%s %d %s doc(s) into %s%s", verb, indexed, aggregate_type, index,
                f" (tenant={tenant_id})" if tenant_id else "")
    return indexed


async def rebuild_all(
    tenant_id: Optional[str] = None, *, dry_run: bool = False
) -> Dict[str, int]:
    """Rebuild every migrated aggregate's ES index from Postgres."""
    counts: Dict[str, int] = {}
    for aggregate_type in _REBUILD_SPECS:
        try:
            counts[aggregate_type] = await rebuild(
                aggregate_type, tenant_id, dry_run=dry_run
            )
        except Exception:  # noqa: BLE001 — keep going across aggregates
            logger.exception("Rebuild failed for %s", aggregate_type)
            counts[aggregate_type] = -1
    return counts


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild an Elasticsearch index from the Postgres source-of-truth."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--aggregate",
        choices=sorted(_REBUILD_SPECS),
        help="Aggregate type whose ES index should be rebuilt.",
    )
    group.add_argument(
        "--all", action="store_true", help="Rebuild every migrated aggregate's index.",
    )
    parser.add_argument("--tenant", default=None, help="Limit to one tenant.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report counts without indexing anything.",
    )
    return parser


async def _amain() -> None:
    args = _build_arg_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.all:
        counts = await rebuild_all(args.tenant, dry_run=args.dry_run)
        total = sum(c for c in counts.values() if c >= 0)
        logger.info("Rebuild complete — %d doc(s) across %d aggregate(s): %s",
                    total, len(counts), counts)
    else:
        n = await rebuild(args.aggregate, args.tenant, dry_run=args.dry_run)
        logger.info("Rebuild complete — %d doc(s) for %s", n, args.aggregate)


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
