"""One-time backfill: copy existing ES ``*_current`` rows into PostgreSQL.

Phase 3 of the commerce migration. Before reads can be cut over to Postgres
(``commerce_read_from_postgres``) or payments promoted to authoritative, the
Postgres source-of-truth must already hold every historical record that lives
in the ES commerce indices. This script scrolls each ES index and inserts the
rows into Postgres in dependency order (customers → accounts → invoices →
payments) so foreign keys are always satisfiable.

It is **idempotent**: rows that already exist in Postgres are skipped, so the
backfill can be re-run safely (e.g. to pick up records created between runs).
It does NOT enqueue outbox events — the ES projection is the *source* here, so
re-projecting back to ES would be pointless and could clobber newer writes.

Usage::

    DATABASE_URL=postgresql+psycopg://... \\
    ENVIRONMENT=production \\
        ./venv/bin/python -m persistence.backfill --tenant demo-tenant

Without ``--tenant`` all tenants found in the indices are backfilled.
Use ``--dry-run`` to report counts without writing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger("persistence.backfill")

# ES index names (kept local to avoid importing the commerce mapping module
# during a teardown phase where it may eventually be deleted).
_CUSTOMERS_INDEX = "customers_current"
_ACCOUNTS_INDEX = "accounts_current"
_INVOICES_INDEX = "invoices_current"
_PAYMENTS_INDEX = "payments_current"
_PRICE_BOOKS_INDEX = "price_books_current"
_PRICING_RULES_INDEX = "pricing_rules_current"
_INVOICE_EVENTS_INDEX = "invoice_events"
_ACCOUNT_EVENTS_INDEX = "account_events"
_DUNNING_EVENTS_INDEX = "dunning_events"
_AR_AGING_INDEX = "ar_aging_snapshots"
# Compliance config (hybrid document tables).
_TAX_JURISDICTIONS_INDEX = "tax_jurisdictions"
_TAX_EXEMPTIONS_INDEX = "tax_exemptions"
_PRICE_PROTECTION_INDEX = "price_protection_contracts"
_COMPLIANCE_PRICING_RULES_INDEX = "pricing_rules"
_SUPPLIER_CONTRACTS_INDEX = "supplier_contracts"

# Hybrid config aggregates: (es_index, pk_field, typed columns to lift).
_CONFIG_BACKFILL = [
    ("TaxJurisdictionORM", _TAX_JURISDICTIONS_INDEX, "jurisdiction_id",
     ("fips_code", "tax_type", "status")),
    ("TaxExemptionORM", _TAX_EXEMPTIONS_INDEX, "exemption_id",
     ("customer_id", "certificate_number", "status")),
    ("PriceProtectionContractORM", _PRICE_PROTECTION_INDEX, "contract_id",
     ("customer_id", "product_code", "status", "version")),
    ("CompliancePricingRuleORM", _COMPLIANCE_PRICING_RULES_INDEX, "rule_id",
     ("customer_id", "product_code", "strategy", "status")),
    ("SupplierContractORM", _SUPPLIER_CONTRACTS_INDEX, "contract_id",
     ("supplier_name", "product_code", "status")),
]

# Orders / jobs current-state hybrid aggregates.
_FUEL_ORDERS_INDEX = "fuel_orders_current"
_JOBS_INDEX = "jobs_current"
_SHIPMENTS_INDEX = "shipments_current"
_TENANT_JOB_POLICIES_INDEX = "tenant_job_policies"

# (model, es_index, pk_field, tenant-keyed?, typed columns)
_CURRENT_STATE_BACKFILL = [
    ("FuelOrderCurrentORM", _FUEL_ORDERS_INDEX, "order_id", False,
     ("customer_id", "assigned_driver_id", "status", "last_event_timestamp")),
    ("JobCurrentORM", _JOBS_INDEX, "job_id", False,
     ("asset_id", "status", "last_event_timestamp")),
    ("ShipmentCurrentORM", _SHIPMENTS_INDEX, "shipment_id", False,
     ("status", "last_event_timestamp")),
    ("TenantJobPolicyORM", _TENANT_JOB_POLICIES_INDEX, "tenant_id", True, ()),
]

# Master-data hybrid aggregates.
_DRIVERS_INDEX = "drivers"
_DEPOTS_INDEX = "depots"
_TERMINALS_INDEX = "terminals"
_ASSET_CERTS_INDEX = "asset_certifications"
_INTAKE_CHANNELS_INDEX = "intake_channels"
_TRUCKS_INDEX = "trucks"
_LOCATIONS_INDEX = "locations"

# (model, es_index, pk_field, typed columns)
_MASTER_DATA_BACKFILL = [
    ("DriverMasterORM", _DRIVERS_INDEX, "driver_id", ("cdl_number", "status")),
    ("DepotORM", _DEPOTS_INDEX, "depot_id", ("is_default",)),
    ("TerminalORM", _TERMINALS_INDEX, "terminal_id", ("status",)),
    ("AssetCertificationORM", _ASSET_CERTS_INDEX, "cert_id", ("asset_id", "status")),
    ("IntakeChannelORM", _INTAKE_CHANNELS_INDEX, "channel_id", ()),
    ("TruckORM", _TRUCKS_INDEX, "truck_id", ()),
    ("LocationORM", _LOCATIONS_INDEX, "location_id", ()),
]

_SCROLL_SIZE = 500


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    # Accept both date (YYYY-MM-DD) and full datetime strings — some ES docs
    # store due_date as a full ISO datetime; we keep only the date part.
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.fromisoformat(value).date()
    except (ValueError, TypeError):
        return None


def _scroll(es_client, index: str, tenant_id: Optional[str]) -> Iterable[Dict[str, Any]]:
    """Yield ``_source`` dicts for every doc in ``index`` (optionally one tenant).

    Uses the synchronous Elasticsearch client's scroll API. The commerce
    indices are small enough that a simple scroll is sufficient.
    """
    query: Dict[str, Any] = {"match_all": {}}
    if tenant_id:
        query = {"term": {"tenant_id": tenant_id}}

    if not es_client.indices.exists(index=index):
        logger.warning("Index %s does not exist — nothing to backfill", index)
        return

    resp = es_client.search(
        index=index, query=query, size=_SCROLL_SIZE, scroll="2m"
    )
    scroll_id = resp.get("_scroll_id")
    hits = resp["hits"]["hits"]
    while hits:
        for hit in hits:
            yield hit["_source"]
        resp = es_client.scroll(scroll_id=scroll_id, scroll="2m")
        scroll_id = resp.get("_scroll_id")
        hits = resp["hits"]["hits"]


async def backfill(tenant_id: Optional[str] = None, *, dry_run: bool = False) -> Dict[str, int]:
    """Backfill commerce rows from ES into Postgres. Returns per-index counts."""
    from persistence.database import is_persistence_enabled, session_scope
    from persistence.models import (
        AccountEventORM,
        AccountORM,
        ArAgingSnapshotORM,
        CustomerORM,
        DunningEventORM,
        InvoiceEventORM,
        InvoiceLineItemORM,
        InvoiceORM,
        PaymentORM,
        PriceBookORM,
        PricingRuleORM,
    )
    from services.elasticsearch_service import elasticsearch_service

    if not is_persistence_enabled():
        raise RuntimeError(
            "Backfill requires DATABASE_URL to be set (persistence layer dormant)."
        )

    es_client = elasticsearch_service.client
    counts = {
        "customers": 0, "accounts": 0, "invoices": 0, "payments": 0,
        "price_books": 0, "pricing_rules": 0, "invoice_events": 0,
        "account_events": 0, "dunning_events": 0, "ar_aging_snapshots": 0,
        "tax_jurisdictions": 0, "tax_exemptions": 0,
        "price_protection_contracts": 0, "compliance_pricing_rules": 0,
        "supplier_contracts": 0,
        "fuel_orders_current": 0, "jobs_current": 0, "shipments_current": 0,
        "tenant_job_policies": 0,
        "drivers": 0, "depots": 0, "terminals": 0, "asset_certifications": 0,
        "intake_channels": 0, "trucks": 0, "locations": 0,
    }

    # --- Customers ---------------------------------------------------------
    async with session_scope() as session:
        for src in _scroll(es_client, _CUSTOMERS_INDEX, tenant_id):
            cid = src.get("customer_id")
            if not cid:
                continue
            if await session.get(CustomerORM, cid):
                continue
            counts["customers"] += 1
            if dry_run:
                continue
            session.add(CustomerORM(
                customer_id=cid,
                tenant_id=src["tenant_id"],
                display_name=src.get("display_name", ""),
                legal_name=src.get("legal_name"),
                primary_email=src.get("primary_email"),
                tax_id=src.get("tax_id"),
                status=src.get("status", "active"),
                external_refs=src.get("external_refs") or {},
                customer_metadata=src.get("metadata") or {},
                created_at=_parse_dt(src.get("created_at")) or datetime.utcnow(),
                updated_at=_parse_dt(src.get("updated_at")) or datetime.utcnow(),
            ))

    # --- Accounts ----------------------------------------------------------
    async with session_scope() as session:
        for src in _scroll(es_client, _ACCOUNTS_INDEX, tenant_id):
            aid = src.get("account_id")
            if not aid:
                continue
            if await session.get(AccountORM, aid):
                continue
            counts["accounts"] += 1
            if dry_run:
                continue
            session.add(AccountORM(
                account_id=aid,
                tenant_id=src["tenant_id"],
                customer_id=src["customer_id"],
                display_name=src.get("display_name", ""),
                status=src.get("status", "active"),
                credit_limit_cents=src.get("credit_limit_cents", 0),
                open_balance_cents=src.get("open_balance_cents", 0),
                available_credit_cents=src.get("available_credit_cents", 0),
                credit_balance_cents=src.get("credit_balance_cents", 0),
                credit_state=src.get("credit_state", "ok"),
                credit_override_expires_at=_parse_dt(src.get("credit_override_expires_at")),
                net_terms_days=src.get("net_terms_days", 30),
                tier=src.get("tier", "default"),
                billing_address=src.get("billing_address"),
                payment_method_preference=src.get("payment_method_preference", "invoice"),
                external_refs=src.get("external_refs") or {},
                created_at=_parse_dt(src.get("created_at")) or datetime.utcnow(),
                updated_at=_parse_dt(src.get("updated_at")) or datetime.utcnow(),
            ))

    # --- Invoices (+ line items) ------------------------------------------
    async with session_scope() as session:
        for src in _scroll(es_client, _INVOICES_INDEX, tenant_id):
            iid = src.get("invoice_id")
            if not iid:
                continue
            if await session.get(InvoiceORM, iid):
                continue
            counts["invoices"] += 1
            if dry_run:
                continue
            inv = InvoiceORM(
                invoice_id=iid,
                tenant_id=src["tenant_id"],
                customer_id=src["customer_id"],
                account_id=src["account_id"],
                order_id=src.get("order_id"),
                invoice_number=src.get("invoice_number"),
                status=src.get("status", "draft"),
                total_cents=src.get("total_cents", 0),
                amount_paid_cents=src.get("amount_paid_cents", 0),
                remaining_cents=src.get("remaining_cents", 0),
                tax_cents=src.get("tax_cents", 0),
                subtotal_cents=src.get("subtotal_cents", 0),
                tax_breakdown=src.get("tax_breakdown"),
                exemptions_applied=src.get("exemptions_applied"),
                issued_at=_parse_dt(src.get("issued_at")),
                due_date=_parse_date(src.get("due_date")),
                finalized_at=_parse_dt(src.get("finalized_at")),
                voided_at=_parse_dt(src.get("voided_at")),
                void_reason=src.get("void_reason"),
                qbo_push_state=src.get("qbo_push_state", "pending"),
                qbo_push_attempts=src.get("qbo_push_attempts", 0),
                qbo_push_last_error=src.get("qbo_push_last_error"),
                external_refs=src.get("external_refs") or {},
                created_at=_parse_dt(src.get("created_at")) or datetime.utcnow(),
                updated_at=_parse_dt(src.get("updated_at")) or datetime.utcnow(),
            )
            for position, li in enumerate(src.get("line_items") or []):
                inv.line_items.append(InvoiceLineItemORM(
                    line_id=li.get("line_id") or f"{iid}_line_{position}",
                    position=position,
                    product_code=li.get("product_code", ""),
                    quantity_gallons=float(li.get("quantity_gallons", li.get("quantity", 0)) or 0),
                    unit_price_cents=int(li.get("unit_price_cents", 0)),
                    subtotal_cents=int(li.get("subtotal_cents", 0)),
                ))
            session.add(inv)

    # --- Payments ----------------------------------------------------------
    async with session_scope() as session:
        for src in _scroll(es_client, _PAYMENTS_INDEX, tenant_id):
            pid = src.get("payment_id")
            if not pid:
                continue
            if await session.get(PaymentORM, pid):
                continue
            counts["payments"] += 1
            if dry_run:
                continue
            session.add(PaymentORM(
                payment_id=pid,
                tenant_id=src["tenant_id"],
                invoice_id=src["invoice_id"],
                account_id=src["account_id"],
                amount_cents=src.get("amount_cents", 0),
                source=src.get("source", "manual"),
                method=src.get("method", "other"),
                external_id=src.get("external_id"),
                reference=src.get("reference"),
                status=src.get("status", "applied"),
                received_at=_parse_dt(src.get("received_at")),
                applied_at=_parse_dt(src.get("applied_at")) or datetime.utcnow(),
                reversed_at=_parse_dt(src.get("reversed_at")),
            ))

    # --- Price books --------------------------------------------------------
    async with session_scope() as session:
        for src in _scroll(es_client, _PRICE_BOOKS_INDEX, tenant_id):
            pbid = src.get("price_book_id")
            if not pbid or await session.get(PriceBookORM, pbid):
                continue
            counts["price_books"] += 1
            if dry_run:
                continue
            session.add(PriceBookORM(
                price_book_id=pbid,
                tenant_id=src["tenant_id"],
                name=src.get("name", ""),
                description=src.get("description"),
                status=src.get("status", "draft"),
                rule_count=src.get("rule_count", 0),
                created_at=_parse_dt(src.get("created_at")) or datetime.utcnow(),
                updated_at=_parse_dt(src.get("updated_at")) or datetime.utcnow(),
            ))

    # --- Pricing rules ------------------------------------------------------
    async with session_scope() as session:
        for src in _scroll(es_client, _PRICING_RULES_INDEX, tenant_id):
            rid = src.get("rule_id")
            if not rid or await session.get(PricingRuleORM, rid):
                continue
            counts["pricing_rules"] += 1
            if dry_run:
                continue
            session.add(PricingRuleORM(
                rule_id=rid,
                price_book_id=src["price_book_id"],
                tenant_id=src["tenant_id"],
                product_code=src.get("product_code", ""),
                scope_type=src.get("scope_type", "default"),
                scope_value=src.get("scope_value", "default"),
                effective_from=_parse_dt(src.get("effective_from")),
                effective_to=_parse_dt(src.get("effective_to")),
                min_quantity_gallons=src.get("min_quantity_gallons"),
                unit_price_cents=int(src.get("unit_price_cents", 0)),
                created_at=_parse_dt(src.get("created_at")) or datetime.utcnow(),
                updated_at=_parse_dt(src.get("updated_at")) or datetime.utcnow(),
            ))

    # --- Invoice events -----------------------------------------------------
    async with session_scope() as session:
        for src in _scroll(es_client, _INVOICE_EVENTS_INDEX, tenant_id):
            eid = src.get("event_id")
            if not eid or await session.get(InvoiceEventORM, eid):
                continue
            counts["invoice_events"] += 1
            if dry_run:
                continue
            session.add(InvoiceEventORM(
                event_id=eid,
                invoice_id=src["invoice_id"],
                tenant_id=src["tenant_id"],
                event_type=src.get("event_type", "created"),
                payload=src.get("payload") or {},
                occurred_at=_parse_dt(src.get("occurred_at")) or datetime.utcnow(),
                actor=src.get("actor", "system"),
                sequence_number=int(src.get("sequence_number", 1)),
            ))

    # --- Account events -----------------------------------------------------
    async with session_scope() as session:
        for src in _scroll(es_client, _ACCOUNT_EVENTS_INDEX, tenant_id):
            eid = src.get("event_id")
            if not eid or await session.get(AccountEventORM, eid):
                continue
            counts["account_events"] += 1
            if dry_run:
                continue
            session.add(AccountEventORM(
                event_id=eid,
                account_id=src["account_id"],
                tenant_id=src["tenant_id"],
                event_type=src.get("event_type", "created"),
                payload=src.get("payload") or {},
                occurred_at=_parse_dt(src.get("occurred_at")) or datetime.utcnow(),
                actor=src.get("actor", "system"),
                sequence_number=int(src.get("sequence_number", 1)),
            ))

    # --- Dunning events -----------------------------------------------------
    async with session_scope() as session:
        for src in _scroll(es_client, _DUNNING_EVENTS_INDEX, tenant_id):
            eid = src.get("event_id")
            if not eid or await session.get(DunningEventORM, eid):
                continue
            counts["dunning_events"] += 1
            if dry_run:
                continue
            session.add(DunningEventORM(
                event_id=eid,
                invoice_id=src["invoice_id"],
                account_id=src["account_id"],
                tenant_id=src["tenant_id"],
                threshold_days=src.get("threshold_days"),
                template_key=src.get("template_key"),
                queued_at=_parse_dt(src.get("queued_at")),
                cancelled_at=_parse_dt(src.get("cancelled_at")),
                cancellation_reason=src.get("cancellation_reason"),
            ))

    # --- AR aging snapshots -------------------------------------------------
    async with session_scope() as session:
        for src in _scroll(es_client, _AR_AGING_INDEX, tenant_id):
            sid = src.get("snapshot_id")
            if not sid or await session.get(ArAgingSnapshotORM, sid):
                continue
            counts["ar_aging_snapshots"] += 1
            if dry_run:
                continue
            session.add(ArAgingSnapshotORM(
                snapshot_id=sid,
                tenant_id=src["tenant_id"],
                snapshot_date=_parse_date(src.get("snapshot_date")),
                total_open_cents=src.get("total_open_cents", 0),
                bucket_0_30_cents=src.get("bucket_0_30_cents", 0),
                bucket_31_60_cents=src.get("bucket_31_60_cents", 0),
                bucket_61_90_cents=src.get("bucket_61_90_cents", 0),
                bucket_90_plus_cents=src.get("bucket_90_plus_cents", 0),
                account_count_with_balance=src.get("account_count_with_balance", 0),
            ))

    # --- Compliance config (hybrid document tables) -------------------------
    import persistence.models as _models

    _count_key = {
        _TAX_JURISDICTIONS_INDEX: "tax_jurisdictions",
        _TAX_EXEMPTIONS_INDEX: "tax_exemptions",
        _PRICE_PROTECTION_INDEX: "price_protection_contracts",
        _COMPLIANCE_PRICING_RULES_INDEX: "compliance_pricing_rules",
        _SUPPLIER_CONTRACTS_INDEX: "supplier_contracts",
    }
    for model_name, index, pk_field, typed_cols in _CONFIG_BACKFILL:
        model = getattr(_models, model_name)
        ckey = _count_key[index]
        async with session_scope() as session:
            for src in _scroll(es_client, index, tenant_id):
                doc_id = src.get(pk_field)
                if not doc_id or await session.get(model, doc_id):
                    continue
                counts[ckey] += 1
                if dry_run:
                    continue
                typed = {c: src.get(c) for c in typed_cols if c in src}
                if "version" in typed_cols and typed.get("version") is None:
                    typed["version"] = src.get("version", 0) or 0
                session.add(model(
                    **{pk_field: doc_id},
                    tenant_id=src["tenant_id"],
                    document=dict(src),
                    created_at=_parse_dt(src.get("created_at")) or datetime.utcnow(),
                    updated_at=_parse_dt(src.get("updated_at")) or datetime.utcnow(),
                    **typed,
                ))

    # --- Orders / jobs current-state (hybrid document tables) ---------------
    _cs_count_key = {
        _FUEL_ORDERS_INDEX: "fuel_orders_current",
        _JOBS_INDEX: "jobs_current",
        _SHIPMENTS_INDEX: "shipments_current",
        _TENANT_JOB_POLICIES_INDEX: "tenant_job_policies",
    }
    for model_name, index, pk_field, tenant_keyed, typed_cols in _CURRENT_STATE_BACKFILL:
        model = getattr(_models, model_name)
        ckey = _cs_count_key[index]
        async with session_scope() as session:
            for src in _scroll(es_client, index, tenant_id):
                doc_id = src["tenant_id"] if tenant_keyed else src.get(pk_field)
                if not doc_id or await session.get(model, doc_id):
                    continue
                counts[ckey] += 1
                if dry_run:
                    continue
                typed = {c: src.get(c) for c in typed_cols if c in src}
                session.add(model(
                    **{("policy_id" if tenant_keyed else pk_field): doc_id},
                    tenant_id=src.get("tenant_id") or "unknown",
                    document=dict(src),
                    created_at=_parse_dt(src.get("created_at")) or datetime.utcnow(),
                    updated_at=_parse_dt(src.get("updated_at")) or datetime.utcnow(),
                    **typed,
                ))

    # --- Master data (hybrid document tables) -------------------------------
    _md_count_key = {
        _DRIVERS_INDEX: "drivers",
        _DEPOTS_INDEX: "depots",
        _TERMINALS_INDEX: "terminals",
        _ASSET_CERTS_INDEX: "asset_certifications",
        _INTAKE_CHANNELS_INDEX: "intake_channels",
        _TRUCKS_INDEX: "trucks",
        _LOCATIONS_INDEX: "locations",
    }
    for model_name, index, pk_field, typed_cols in _MASTER_DATA_BACKFILL:
        model = getattr(_models, model_name)
        ckey = _md_count_key[index]
        async with session_scope() as session:
            for src in _scroll(es_client, index, tenant_id):
                # Legacy trucks index keys on truck_id or asset_id.
                doc_id = src.get(pk_field) or src.get("asset_id") or src.get("id")
                if not doc_id or await session.get(model, doc_id):
                    continue
                counts[ckey] += 1
                if dry_run:
                    continue
                typed = {c: src.get(c) for c in typed_cols if c in src}
                session.add(model(
                    **{pk_field: doc_id},
                    tenant_id=src.get("tenant_id") or "unknown",
                    document=dict(src),
                    created_at=_parse_dt(src.get("created_at")) or datetime.utcnow(),
                    updated_at=_parse_dt(src.get("updated_at")) or datetime.utcnow(),
                    **typed,
                ))

    verb = "Would backfill" if dry_run else "Backfilled"
    logger.info(
        "%s: %d customers, %d accounts, %d invoices, %d payments, "
        "%d price_books, %d pricing_rules, %d invoice_events, "
        "%d account_events, %d dunning_events, %d ar_aging_snapshots, "
        "%d tax_jurisdictions, %d tax_exemptions, %d price_protection_contracts, "
        "%d compliance_pricing_rules, %d supplier_contracts, "
        "%d fuel_orders_current, %d jobs_current, %d shipments_current, "
        "%d tenant_job_policies, %d drivers, %d depots, %d terminals, "
        "%d asset_certifications, %d intake_channels, %d trucks, %d locations%s",
        verb, counts["customers"], counts["accounts"], counts["invoices"],
        counts["payments"], counts["price_books"], counts["pricing_rules"],
        counts["invoice_events"], counts["account_events"], counts["dunning_events"],
        counts["ar_aging_snapshots"], counts["tax_jurisdictions"],
        counts["tax_exemptions"], counts["price_protection_contracts"],
        counts["compliance_pricing_rules"], counts["supplier_contracts"],
        counts["fuel_orders_current"], counts["jobs_current"],
        counts["shipments_current"], counts["tenant_job_policies"],
        counts["drivers"], counts["depots"], counts["terminals"],
        counts["asset_certifications"], counts["intake_channels"],
        counts["trucks"], counts["locations"],
        f" (tenant={tenant_id})" if tenant_id else "",
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill commerce ES rows into Postgres.")
    parser.add_argument("--tenant", default=None, help="Restrict to one tenant_id.")
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(backfill(args.tenant, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
