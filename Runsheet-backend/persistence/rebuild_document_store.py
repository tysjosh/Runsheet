"""Rebuild the document store from the PostgreSQL source-of-truth.

This is the REVERSE of :mod:`persistence.backfill` (which copied documents into
the relational tables) and the drift-repair tool for the document plane. The
``*_current`` / config / master-data documents are *projections*: this tool
reconstructs any of them from the relational rows, running each row through the
SAME projector the outbox relay uses
(:data:`persistence.projections.PROJECTORS`) and writing the projected document
under its aggregate id.

Because the projection is byte-identical to what the relay writes, a rebuild
produces the same document contents the live projection would — so any index
listed in :data:`_REBUILD_SPECS` can be discarded and reconstructed.

.. note::

   Formerly ``persistence/rebuild_from_postgres.py``, when the target was an
   Elasticsearch cluster and the name said which side the data came *from*. Both
   sides are Postgres now — the relational tables and the ``es_documents``
   document store — so the name says which side is rebuilt instead.

   Two things went with the cluster. ``_ensure_index`` / ``_lookup_mapping``
   recreated a dropped index with its declared mapping, because a dynamically
   mapped index typed ``tenant_id`` as ``text`` and every tenant-scoped ``term``
   query then matched nothing. The document store has no index to create and no
   per-index typing, so there is nothing to get wrong. The trailing
   ``indices.refresh`` went too: a write is visible to the next read.

   ``ES_ONLY_INDICES`` also went. It listed indices with no relational source of
   truth, which recreating the cluster destroyed permanently — ``customer_tanks``,
   ``truck_compartments`` and ``fuel_stations`` were the three, found the hard way
   when an end-to-end test recreated the cluster and the fuel planning stages
   silently ran on empty input. Migration ``0008_fuel_asset_tables`` gave all
   three relational tables and projectors, emptying the list, and there is no
   cluster left to recreate. What must not regress is that those three keep their
   Postgres homes; ``tests/unit/test_fuel_asset_postgres_homes.py`` holds that.

Usage::

    ENVIRONMENT=development \\
        ./venv/bin/python -m persistence.rebuild_document_store \\
            --aggregate shipment --tenant demo-tenant

    # Rebuild every migrated aggregate's documents:
    ./venv/bin/python -m persistence.rebuild_document_store --all --tenant demo-tenant

``--dry-run`` reports the row counts that WOULD be written without writing.
Without ``--tenant`` every tenant's rows are rebuilt.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import selectinload

logger = logging.getLogger("persistence.rebuild_document_store")


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
    # ``shipment`` was retired with the ``shipments_current`` table (rev 0007).
    "tenant_job_policy": ("TenantJobPolicyORM", "policy_id", "tenant_id", None),
    # Master data (hybrid)
    "driver": ("DriverMasterORM", "driver_id", "tenant_id", None),
    "depot": ("DepotORM", "depot_id", "tenant_id", None),
    "terminal": ("TerminalORM", "terminal_id", "tenant_id", None),
    "asset_certification": ("AssetCertificationORM", "cert_id", "tenant_id", None),
    "intake_channel": ("IntakeChannelORM", "channel_id", "tenant_id", None),
    "truck": ("TruckORM", "truck_id", "tenant_id", None),
    "location": ("LocationORM", "location_id", "tenant_id", None),
    # Fuel assets (hybrid). These were the three entries of the old
    # ``ES_ONLY_INDICES``; they now have Postgres tables, projectors and a
    # backfill, so ``--all`` restores them like anything else.
    "customer_tank": ("CustomerTankORM", "customer_tank_id", "tenant_id", None),
    "truck_compartment": ("TruckCompartmentORM", "compartment_key", "tenant_id", None),
    "fuel_station": ("FuelStationORM", "station_key", "tenant_id", None),
}


async def rebuild(
    aggregate_type: str,
    tenant_id: Optional[str] = None,
    *,
    dry_run: bool = False,
    batch_size: int = 500,
) -> int:
    """Rebuild one aggregate's documents from Postgres. Returns docs written."""
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
    store = elasticsearch_service

    written = 0
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
                # ``stamp_timestamps=False``: write the projected document
                # VERBATIM. The default overwrites ``updated_at`` with now() on
                # every call, which would rewrite the field and diverge from the
                # value stored on the row. The projection already carries the
                # correct created_at/updated_at, so writing it unchanged keeps a
                # rebuild byte-identical to its source row.
                await store.index_document(
                    index, doc_id, doc, stamp_timestamps=False
                )
            written += 1

    verb = "Would write" if dry_run else "Wrote"
    logger.info("%s %d %s doc(s) into %s%s", verb, written, aggregate_type, index,
                f" (tenant={tenant_id})" if tenant_id else "")
    return written


async def rebuild_all(
    tenant_id: Optional[str] = None, *, dry_run: bool = False
) -> Dict[str, int]:
    """Rebuild every migrated aggregate's documents from Postgres."""
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
        description="Rebuild the document store from the Postgres source-of-truth."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--aggregate",
        choices=sorted(_REBUILD_SPECS),
        help="Aggregate type whose documents should be rebuilt.",
    )
    group.add_argument(
        "--all", action="store_true", help="Rebuild every migrated aggregate.",
    )
    parser.add_argument("--tenant", default=None, help="Limit to one tenant.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report counts without writing anything.",
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
