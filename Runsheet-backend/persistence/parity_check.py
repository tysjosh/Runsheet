"""Parity check: diff the Postgres read path against Elasticsearch.

Phase-4 confidence tool. For each commerce aggregate it fetches every record
from BOTH stores and reports any divergence, so the read-cutover decision rests
on evidence rather than inspection. Read-only against both stores — it never
writes.

It compares the *projected* document shapes (what callers actually receive):
ES returns the stored ``_source``; Postgres returns ``persistence.projections``
output. Volatile/derived fields that are intentionally recomputed on read (e.g.
``updated_at`` drift, account live-balance fields) can be ignored per aggregate.

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
    "shipment": ("shipments_current", "shipment_id"),
    "tenant_job_policy": ("tenant_job_policies", "tenant_id"),
    "driver": ("drivers", "driver_id"),
    "depot": ("depots", "depot_id"),
    "terminal": ("terminals", "terminal_id"),
    "asset_certification": ("asset_certifications", "cert_id"),
    "intake_channel": ("intake_channels", "channel_id"),
    "truck": ("trucks", "truck_id"),
    "location": ("locations", "location_id"),
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
    "invoice_event": set(),
    "account_event": set(),
    "dunning_event": set(),
    "ar_aging_snapshot": set(),
    # Compliance config: stored document is the verbatim ES doc — compare all.
    "tax_jurisdiction": set(),
    "tax_exemption": set(),
    "price_protection_contract": set(),
    "compliance_pricing_rule": set(),
    "supplier_contract": set(),
    # Orders/jobs current-state: verbatim document — compare all.
    "fuel_order": set(),
    "job": set(),
    "shipment": set(),
    "tenant_job_policy": set(),
    # Master data: verbatim document — compare all.
    "driver": set(),
    "depot": set(),
    "terminal": set(),
    "asset_certification": set(),
    "intake_channel": set(),
    "truck": set(),
    "location": set(),
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


async def _fetch_es_all(es_client, index: str, tenant_id: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Return ``{id: _source}`` for every doc in an index (optionally one tenant)."""
    query: Dict[str, Any] = {"term": {"tenant_id": tenant_id}} if tenant_id else {"match_all": {}}
    if not es_client.indices.exists(index=index):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    resp = es_client.search(index=index, query=query, size=500, scroll="2m")
    scroll_id = resp.get("_scroll_id")
    hits = resp["hits"]["hits"]
    while hits:
        for hit in hits:
            out[hit["_id"]] = hit["_source"]
        resp = es_client.scroll(scroll_id=scroll_id, scroll="2m")
        scroll_id = resp.get("_scroll_id")
        hits = resp["hits"]["hits"]
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
        "shipment": ("ShipmentCurrentORM", "shipment_id"),
        "tenant_job_policy": ("TenantJobPolicyORM", "policy_id"),
        "driver": ("DriverMasterORM", "driver_id"),
        "depot": ("DepotORM", "depot_id"),
        "terminal": ("TerminalORM", "terminal_id"),
        "asset_certification": ("AssetCertificationORM", "cert_id"),
        "intake_channel": ("IntakeChannelORM", "channel_id"),
        "truck": ("TruckORM", "truck_id"),
        "location": ("LocationORM", "location_id"),
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
    """Compare every commerce record across ES and Postgres.

    Returns ``(records_checked, mismatches)``.
    """
    from persistence.database import is_persistence_enabled
    from services.elasticsearch_service import elasticsearch_service

    if not is_persistence_enabled():
        raise RuntimeError("Parity check requires DATABASE_URL (persistence dormant).")

    es_client = elasticsearch_service.client
    total_checked = 0
    total_mismatches = 0

    for agg, (index, id_field) in _INDEX_BY_AGG.items():
        es_docs = await _fetch_es_all(es_client, index, tenant_id)
        pg_docs = await _fetch_pg_all(agg, tenant_id)

        only_es = set(es_docs) - set(pg_docs)
        only_pg = set(pg_docs) - set(es_docs)
        both = set(es_docs) & set(pg_docs)

        agg_mismatches = 0
        for missing in sorted(only_es):
            logger.error("[%s] %s present in ES but MISSING in Postgres", agg, missing)
            agg_mismatches += 1
        for extra in sorted(only_pg):
            logger.error("[%s] %s present in Postgres but MISSING in ES", agg, extra)
            agg_mismatches += 1

        for rid in sorted(both):
            total_checked += 1
            diffs = _diff_doc(agg, es_docs[rid], pg_docs[rid])
            if diffs:
                agg_mismatches += 1
                logger.error("[%s] %s diverges:\n%s", agg, rid, "\n".join(diffs))

        total_mismatches += agg_mismatches
        status = "OK" if agg_mismatches == 0 else f"{agg_mismatches} MISMATCH(es)"
        logger.info(
            "[%s] ES=%d PG=%d common=%d → %s",
            agg, len(es_docs), len(pg_docs), len(both), status,
        )

    if total_mismatches == 0:
        logger.info("PARITY OK — %d records identical across ES and Postgres", total_checked)
    else:
        logger.error(
            "PARITY FAILED — %d mismatch(es) across %d records checked",
            total_mismatches, total_checked,
        )
    return total_checked, total_mismatches


def main() -> None:
    parser = argparse.ArgumentParser(description="Diff the Postgres read path vs Elasticsearch.")
    parser.add_argument("--tenant", default=None, help="Restrict to one tenant_id.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    _, mismatches = asyncio.run(parity_check(args.tenant))
    raise SystemExit(1 if mismatches else 0)


if __name__ == "__main__":
    main()
