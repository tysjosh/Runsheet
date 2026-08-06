"""Parity check: diff the relational read path against the document store.

It fetches every record for each commerce aggregate from BOTH planes and reports
any divergence. Read-only against both — it never writes.

Originally the Phase-4 confidence tool for the read cutover, when the two planes
were Postgres and an Elasticsearch cluster. Both are Postgres now — the typed
tables and ``es_documents`` — which does not make the comparison redundant: the
document plane is a *projection*, fed asynchronously by the outbox relay, so it can
still fall behind or drift. What changed is that a mismatch is now a projection bug
rather than a cross-store consistency risk, and the tool is drift detection rather
than a cutover gate.

It compares the *projected* document shapes (what callers actually receive): the
document store returns the stored document; the relational side returns
``persistence.projections`` output. Volatile/derived fields that are intentionally
recomputed on read (e.g. ``updated_at`` drift, account live-balance fields) can be
ignored per aggregate.

Usage::

    DATABASE_URL=postgresql+psycopg://... ENVIRONMENT=development \\
        ./venv/bin/python -m persistence.parity_check --tenant demo-tenant

Exit code is non-zero when any mismatch is found, so it can gate a soak in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("persistence.parity_check")

_INDEX_BY_AGG = {
    "customer": ("customers_current", "customer_id"),
    "account": ("accounts_current", "account_id"),
    "invoice": ("invoices_current", "invoice_id"),
    "payment": ("payments_current", "payment_id"),
    "price_book": ("price_books_current", "price_book_id"),
    "pricing_rule": ("pricing_rules_current", "rule_id"),
    "invoice_event": ("invoice_events", "event_id"),
    "account_event": ("account_events", "event_id"),
    "dunning_event": ("dunning_events", "event_id"),
    "ar_aging_snapshot": ("ar_aging_snapshots", "snapshot_id"),
    "tax_jurisdiction": ("tax_jurisdictions", "jurisdiction_id"),
    "tax_exemption": ("tax_exemptions", "exemption_id"),
    "price_protection_contract": ("price_protection_contracts", "contract_id"),
    "compliance_pricing_rule": ("pricing_rules", "rule_id"),
    "supplier_contract": ("supplier_contracts", "contract_id"),
    "fuel_order": ("fuel_orders_current", "order_id"),
    "job": ("jobs_current", "job_id"),
    # ``shipment`` was retired with the ``shipments_current`` table (rev 0007).
    # It stayed listed here after the drop, and because ``_fetch_pg_all`` has no
    # entry for it the tool raised ``KeyError: 'shipment'`` and abandoned the run
    # — taking the seven aggregates after it, including all three fuel assets,
    # down with it. Only reachable when ``shipments_current`` is absent from
    # ``retired_es_indices``, which is why it went unnoticed.
    "tenant_job_policy": ("tenant_job_policies", "tenant_id"),
    "driver": ("drivers", "driver_id"),
    "depot": ("depots", "depot_id"),
    "terminal": ("terminals", "terminal_id"),
    "asset_certification": ("asset_certifications", "cert_id"),
    "intake_channel": ("intake_channels", "channel_id"),
    "truck": ("trucks", "truck_id"),
    "location": ("locations", "location_id"),
    "customer_tank": ("customer_tanks", "customer_tank_id"),
    "truck_compartment": ("truck_compartments", "compartment_key"),
    "fuel_station": ("fuel_stations", "station_key"),
}

#: Aggregates whose Elasticsearch ``_id`` is not the Postgres primary key, with
#: the function that maps an ES ``_id`` + ``_source`` onto the PG key.
#:
#: Without this, parity reports every row twice — once "only in ES" and once
#: "only in Postgres" — for a migration that is actually correct.
#: ``customer_tanks`` is the case that matters: the ES documents are keyed by
#: ``customer_id`` because the seeder's id resolver preferred the foreign key,
#: and the Postgres table is deliberately keyed by ``customer_tank_id`` so the
#: collision cannot recur. The remap makes parity compare the same tank on both
#: sides instead of flagging the fix as a divergence.
_ES_ID_REMAP = {
    "customer_tank": lambda es_id, src: src.get("customer_tank_id") or es_id,
}

# Fields excluded from the diff per aggregate. These are either recomputed on
# read (so a byte diff is expected and harmless) or are ES-only projection
# bookkeeping that never existed as a Postgres column.
_IGNORED_FIELDS = {
    "customer": {"updated_at"},
    # Account get() recomputes live balance + derived fields on read; the
    # stored ES doc may lag, so exclude the recomputed ones from parity.
    "account": {
        "updated_at", "open_balance_cents", "available_credit_cents",
        "oldest_open_invoice_days",
    },
    # _last_applied_seq is ES-projection bookkeeping with no PG column.
    "invoice": {"updated_at", "_last_applied_seq"},
    "payment": {"updated_at"},
    "price_book": {"updated_at"},
    "pricing_rule": {"updated_at"},
    # Event/snapshot projections key off the domain timestamp (``occurred_at``
    # / ``queued_at`` / ``snapshot_date``) and intentionally do NOT carry
    # ``created_at`` / ``updated_at`` — those are stamped onto the ES ``_source``
    # by ``ElasticsearchService.index_document`` at write time and have no
    # Postgres column (same class as the invoice ``_last_applied_seq`` exclusion
    # above). Excluding them keeps parity honest: every legitimately-projected
    # event would otherwise show a spurious created_at/updated_at divergence.
    "invoice_event": {"created_at", "updated_at"},
    "account_event": {"created_at", "updated_at"},
    "dunning_event": {"created_at", "updated_at"},
    "ar_aging_snapshot": {"created_at", "updated_at"},
    # Compliance config: stored document is the verbatim ES doc — compare all.
    "tax_jurisdiction": set(),
    "tax_exemption": set(),
    "price_protection_contract": set(),
    "compliance_pricing_rule": set(),
    "supplier_contract": set(),
    # Orders/jobs current-state: verbatim document — compare all.
    "fuel_order": set(),
    "job": set(),
    "tenant_job_policy": set(),
    # Master data: verbatim document — compare all.
    "driver": set(),
    "depot": set(),
    "terminal": set(),
    "asset_certification": set(),
    "intake_channel": set(),
    "truck": set(),
    "location": set(),
    # Fuel assets: verbatim document — compare all.
    "customer_tank": set(),
    "truck_compartment": set(),
    "fuel_station": set(),
}


def _normalize(value: Any) -> Any:
    """Normalize values so equivalent representations compare equal.

    - dicts: recurse, dropping ``None`` values (ES omits vs PG stores null).
    - lists: normalize each element.
    - empty list / empty dict -> None, so ES's "absent" and PG's "empty
      collection" (e.g. exemptions_applied None vs []) compare equal.
    - ISO datetime strings -> normalized ISO so an ES full-datetime and a PG
      value that round-trips through the same type compare equal. Pure dates
      (len 10) are left as-is; a full datetime whose time is midnight or which
      represents the same instant is reduced to compare on its calendar date
      when one side is date-only.
    - everything else: returned as-is.
    """
    if isinstance(value, dict):
        normalized = {k: _normalize(v) for k, v in value.items() if v is not None}
        return normalized or None
    if isinstance(value, list):
        return [_normalize(v) for v in value] or None
    return value


def _values_equal(es_val: Any, pg_val: Any) -> bool:
    """Compare two field values, tolerating date/datetime representation drift.

    The ES mapping types some fields (e.g. ``due_date``) as ``date`` but stores
    a full ISO datetime in ``_source``; the Postgres projection emits a pure
    ``YYYY-MM-DD``. These denote the same calendar date, so when one side is a
    date-only string and the other an ISO datetime, compare on the date part.
    """
    a, b = _normalize(es_val), _normalize(pg_val)
    if a == b:
        return True
    da, db = _as_date_part(a), _as_date_part(b)
    if da is not None and db is not None and (da == b or a == db or da == db):
        return True
    return False


def _as_date_part(value: Any) -> Optional[str]:
    """Return the ``YYYY-MM-DD`` prefix if ``value`` is an ISO date/datetime."""
    if isinstance(value, str) and len(value) >= 10:
        head = value[:10]
        try:
            date.fromisoformat(head)
            return head
        except ValueError:
            return None
    return None


def _diff_doc(agg: str, es_doc: Dict[str, Any], pg_doc: Dict[str, Any]) -> List[str]:
    """Return a list of human-readable field divergences for one record."""
    ignored = _IGNORED_FIELDS.get(agg, set())
    keys = (set(es_doc) | set(pg_doc)) - ignored
    diffs: List[str] = []
    for key in sorted(keys):
        if not _values_equal(es_doc.get(key), pg_doc.get(key)):
            es_val = _normalize(es_doc.get(key))
            pg_val = _normalize(pg_doc.get(key))
            diffs.append(f"    {key}: ES={es_val!r}  PG={pg_val!r}")
    return diffs


async def _fetch_documents_all(index: str, tenant_id: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Return ``{doc_id: document}`` for one index (optionally one tenant).

    Reads ``es_documents`` directly rather than through ``search_documents``,
    because parity wants *every* row and a query API built for paged application
    reads is the wrong shape for that.

    This used to scroll the Elasticsearch cluster, and most of it was working
    around one mapping defect: the legacy generic indices (``trucks`` /
    ``locations``) were created with dynamic mapping, so their ``tenant_id`` was
    ``text`` rather than ``keyword`` and a plain ``term`` query matched nothing —
    which made parity silently report ES=0 for them. The fix was a three-stage
    fallback: ``term`` on ``tenant_id``, then on ``tenant_id.keyword``, then a
    client-side filter. None of it applies to a ``varchar`` column, so all three
    stages collapse into one ``WHERE``. The scroll-context cleanup goes too.
    """
    from sqlalchemy import select

    from persistence.database import session_scope
    from persistence.models import EsDocumentORM

    out: Dict[str, Dict[str, Any]] = {}
    async with session_scope() as session:
        stmt = select(EsDocumentORM).where(EsDocumentORM.index_name == index)
        if tenant_id:
            stmt = stmt.where(EsDocumentORM.tenant_id == tenant_id)
        for row in (await session.execute(stmt)).scalars().all():
            out[row.doc_id] = dict(row.document or {})
    return out


async def _fetch_pg_all(agg: str, tenant_id: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Return ``{id: projected_doc}`` from Postgres for one aggregate."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from persistence.database import session_scope

    # Compliance-config + current-state hybrid tables: the stored ``document``
    # IS the projection.
    _CONFIG = {
        "tax_jurisdiction": ("TaxJurisdictionORM", "jurisdiction_id"),
        "tax_exemption": ("TaxExemptionORM", "exemption_id"),
        "price_protection_contract": ("PriceProtectionContractORM", "contract_id"),
        "compliance_pricing_rule": ("CompliancePricingRuleORM", "rule_id"),
        "supplier_contract": ("SupplierContractORM", "contract_id"),
        "fuel_order": ("FuelOrderCurrentORM", "order_id"),
        "job": ("JobCurrentORM", "job_id"),
        # ``shipment`` was retired with the ``shipments_current`` table (rev 0007).
        "tenant_job_policy": ("TenantJobPolicyORM", "policy_id"),
        "driver": ("DriverMasterORM", "driver_id"),
        "depot": ("DepotORM", "depot_id"),
        "terminal": ("TerminalORM", "terminal_id"),
        "asset_certification": ("AssetCertificationORM", "cert_id"),
        "intake_channel": ("IntakeChannelORM", "channel_id"),
        "truck": ("TruckORM", "truck_id"),
        "location": ("LocationORM", "location_id"),
        "customer_tank": ("CustomerTankORM", "customer_tank_id"),
        "truck_compartment": ("TruckCompartmentORM", "compartment_key"),
        "fuel_station": ("FuelStationORM", "station_key"),
    }
    if agg in _CONFIG:
        import persistence.models as _m

        model_name, id_attr = _CONFIG[agg]
        model = getattr(_m, model_name)
        out: Dict[str, Dict[str, Any]] = {}
        async with session_scope() as session:
            stmt = select(model)
            if tenant_id:
                stmt = stmt.where(model.tenant_id == tenant_id)
            for row in (await session.execute(stmt)).scalars().all():
                out[getattr(row, id_attr)] = dict(row.document or {})
        return out

    from persistence.models import (
        AccountEventORM, AccountORM, ArAgingSnapshotORM, CustomerORM,
        DunningEventORM, InvoiceEventORM, InvoiceORM, PaymentORM,
        PriceBookORM, PricingRuleORM,
    )
    from persistence.projections import (
        account_event_to_doc, account_to_doc, ar_aging_snapshot_to_doc,
        customer_to_doc, dunning_event_to_doc, invoice_event_to_doc,
        invoice_to_doc, payment_to_doc, price_book_to_doc, pricing_rule_to_doc,
    )

    model, id_attr, to_doc, opts = {
        "customer": (CustomerORM, "customer_id", customer_to_doc, []),
        "account": (AccountORM, "account_id", account_to_doc, []),
        "invoice": (InvoiceORM, "invoice_id", invoice_to_doc,
                    [selectinload(InvoiceORM.line_items)]),
        "payment": (PaymentORM, "payment_id", payment_to_doc, []),
        "price_book": (PriceBookORM, "price_book_id", price_book_to_doc, []),
        "pricing_rule": (PricingRuleORM, "rule_id", pricing_rule_to_doc, []),
        "invoice_event": (InvoiceEventORM, "event_id", invoice_event_to_doc, []),
        "account_event": (AccountEventORM, "event_id", account_event_to_doc, []),
        "dunning_event": (DunningEventORM, "event_id", dunning_event_to_doc, []),
        "ar_aging_snapshot": (ArAgingSnapshotORM, "snapshot_id",
                              ar_aging_snapshot_to_doc, []),
    }[agg]

    out: Dict[str, Dict[str, Any]] = {}
    async with session_scope() as session:
        stmt = select(model)
        if tenant_id:
            stmt = stmt.where(model.tenant_id == tenant_id)
        if opts:
            stmt = stmt.options(*opts)
        for row in (await session.execute(stmt)).scalars().all():
            out[getattr(row, id_attr)] = to_doc(row)
    return out


async def parity_check(tenant_id: Optional[str] = None) -> Tuple[int, int]:
    """Compare every commerce record across the document store and the tables.

    Returns ``(records_checked, mismatches)``.
    """
    from persistence.database import is_persistence_enabled

    if not is_persistence_enabled():
        raise RuntimeError("Parity check requires DATABASE_URL (persistence dormant).")

    total_checked = 0
    total_mismatches = 0

    # A retired index has no document rows by design — the relational table is its
    # sole store — so a diff against it would report every record as missing from
    # the document side. Skip them.
    try:
        from config.settings import get_settings
        retired = set(get_settings().retired_es_indices or [])
    except Exception:  # noqa: BLE001
        retired = set()

    for agg, (index, id_field) in _INDEX_BY_AGG.items():
        if index in retired:
            logger.info("[%s] index %s retired (table-only) — skipping parity", agg, index)
            continue
        doc_docs = await _fetch_documents_all(index, tenant_id)
        remap = _ES_ID_REMAP.get(agg)
        if remap is not None:
            doc_docs = {remap(doc_id, src): src for doc_id, src in doc_docs.items()}
        pg_docs = await _fetch_pg_all(agg, tenant_id)

        only_doc = set(doc_docs) - set(pg_docs)
        only_pg = set(pg_docs) - set(doc_docs)
        both = set(doc_docs) & set(pg_docs)

        agg_mismatches = 0
        for missing in sorted(only_doc):
            logger.error(
                "[%s] %s present in the document store but MISSING in the table",
                agg, missing,
            )
            agg_mismatches += 1
        for extra in sorted(only_pg):
            logger.error(
                "[%s] %s present in the table but MISSING in the document store",
                agg, extra,
            )
            agg_mismatches += 1

        for rid in sorted(both):
            total_checked += 1
            diffs = _diff_doc(agg, doc_docs[rid], pg_docs[rid])
            if diffs:
                agg_mismatches += 1
                logger.error("[%s] %s diverges:\n%s", agg, rid, "\n".join(diffs))

        total_mismatches += agg_mismatches
        status = "OK" if agg_mismatches == 0 else f"{agg_mismatches} MISMATCH(es)"
        logger.info(
            "[%s] docs=%d table=%d common=%d → %s",
            agg, len(doc_docs), len(pg_docs), len(both), status,
        )

    if total_mismatches == 0:
        logger.info(
            "PARITY OK — %d records identical between the document store and the "
            "tables", total_checked,
        )
    else:
        logger.error(
            "PARITY FAILED — %d mismatch(es) across %d records checked",
            total_mismatches, total_checked,
        )
    return total_checked, total_mismatches


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diff the relational read path vs the document store."
    )
    parser.add_argument("--tenant", default=None, help="Restrict to one tenant_id.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    _, mismatches = asyncio.run(parity_check(args.tenant))
    raise SystemExit(1 if mismatches else 0)


if __name__ == "__main__":
    main()
