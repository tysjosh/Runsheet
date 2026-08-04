"""Project ORM rows into the Elasticsearch ``*_current`` document shapes.

The outbox relay calls these to build the exact document the existing ES
mappings expect, so the search/read projection stays byte-compatible with what
the commerce services wrote directly before the migration. Keeping this in one
place means the ES contract has a single, testable source of truth.

Datetimes are serialised to ISO-8601 strings (ES ``date`` fields); dates to
``YYYY-MM-DD``; ``None`` values are preserved where the mapping allows them.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional

from commerce.services.commerce_es_mappings import (
    ACCOUNTS_CURRENT_INDEX,
    ACCOUNT_EVENTS_INDEX,
    AR_AGING_SNAPSHOTS_INDEX,
    CUSTOMERS_CURRENT_INDEX,
    DUNNING_EVENTS_INDEX,
    INVOICES_CURRENT_INDEX,
    INVOICE_EVENTS_INDEX,
    PAYMENTS_CURRENT_INDEX,
    PRICE_BOOKS_CURRENT_INDEX,
    PRICING_RULES_CURRENT_INDEX,
)
from compliance.services.compliance_es_mappings import (
    PRICE_PROTECTION_CONTRACTS_INDEX,
    PRICING_RULES_INDEX as COMPLIANCE_PRICING_RULES_INDEX,
    TAX_EXEMPTIONS_INDEX,
    TAX_JURISDICTIONS_INDEX,
)
from fuel.services.fuel_ops_es_mappings import (
    DEPOTS_INDEX,
    SUPPLIER_CONTRACTS_INDEX,
    TERMINALS_INDEX,
)
from fuel.services.order_es_mappings import (
    FUEL_ORDERS_CURRENT_INDEX,
    INTAKE_CHANNELS_INDEX,
)
from compliance.services.compliance_es_mappings import (
    ASSET_CERTIFICATIONS_INDEX,
    DRIVERS_INDEX,
)
from scheduling.services.scheduling_es_mappings import (
    JOBS_CURRENT_INDEX,
    TENANT_JOB_POLICIES_INDEX,
)
from ops.services.ops_es_service import OpsElasticsearchService
from persistence.models import (
    AccountEventORM,
    AccountORM,
    ArAgingSnapshotORM,
    AssetCertificationORM,
    CompliancePricingRuleORM,
    CustomerORM,
    DepotORM,
    DriverMasterORM,
    DunningEventORM,
    FuelOrderCurrentORM,
    IntakeChannelORM,
    InvoiceEventORM,
    InvoiceORM,
    JobCurrentORM,
    LocationORM,
    PaymentORM,
    PriceBookORM,
    PriceProtectionContractORM,
    PricingRuleORM,
    ShipmentCurrentORM,
    SupplierContractORM,
    TaxExemptionORM,
    TaxJurisdictionORM,
    TenantJobPolicyORM,
    TerminalORM,
    TruckORM,
)


def _iso(value: Optional[datetime | date]) -> Optional[str]:
    """ISO-8601 serialise a datetime/date, passing ``None`` through."""
    if value is None:
        return None
    return value.isoformat()


def customer_to_doc(row: CustomerORM) -> Dict[str, Any]:
    """Build the ``customers_current`` document for a Customer row."""
    from commerce.services.customer_service import (
        _ACCOUNT_SOURCE_KEYS,
        _PHONE_SOURCE_KEYS,
        _project_lookup_field,
    )

    external_refs = row.external_refs or {}
    metadata = row.customer_metadata or {}
    doc: Dict[str, Any] = {
        "customer_id": row.customer_id,
        "tenant_id": row.tenant_id,
        "display_name": row.display_name,
        "legal_name": row.legal_name,
        "primary_email": row.primary_email,
        "tax_id": row.tax_id,
        "status": row.status,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "external_refs": external_refs,
        "metadata": metadata,
    }

    # Project the optional phone/account_id lookup fields (Req 13) so the
    # Postgres read-cutover projection matches the ES write path.
    phone = _project_lookup_field(external_refs, metadata, _PHONE_SOURCE_KEYS)
    if phone is not None:
        doc["phone"] = phone
    account_id = _project_lookup_field(external_refs, metadata, _ACCOUNT_SOURCE_KEYS)
    if account_id is not None:
        doc["account_id"] = account_id

    return doc


def account_to_doc(row: AccountORM) -> Dict[str, Any]:
    """Build the ``accounts_current`` document for an Account row."""
    return {
        "account_id": row.account_id,
        "tenant_id": row.tenant_id,
        "customer_id": row.customer_id,
        "display_name": row.display_name,
        "status": row.status,
        "credit_limit_cents": row.credit_limit_cents,
        "open_balance_cents": row.open_balance_cents,
        "available_credit_cents": row.available_credit_cents,
        "credit_balance_cents": row.credit_balance_cents,
        "credit_state": row.credit_state,
        "credit_override_expires_at": _iso(row.credit_override_expires_at),
        "net_terms_days": row.net_terms_days,
        "tier": row.tier,
        "billing_address": row.billing_address,
        "payment_method_preference": row.payment_method_preference,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "external_refs": row.external_refs or {},
    }


def invoice_to_doc(row: InvoiceORM) -> Dict[str, Any]:
    """Build the ``invoices_current`` document for an Invoice row.

    Line items are nested (matching the ``nested`` mapping field) and ordered
    by their stored ``position``.
    """
    return {
        "invoice_id": row.invoice_id,
        "tenant_id": row.tenant_id,
        "customer_id": row.customer_id,
        "account_id": row.account_id,
        "order_id": row.order_id,
        "pod_id": row.pod_id,
        "delivered_at": _iso(row.delivered_at),
        "delivery_result": row.delivery_result,
        "invoice_number": row.invoice_number,
        "status": row.status,
        "total_cents": row.total_cents,
        "amount_paid_cents": row.amount_paid_cents,
        "remaining_cents": row.remaining_cents,
        "tax_cents": row.tax_cents,
        "subtotal_cents": row.subtotal_cents,
        "line_items": [
            {
                "line_id": li.line_id,
                "product_code": li.product_code,
                "quantity_gallons": li.quantity_gallons,
                "unit_price_cents": li.unit_price_cents,
                "unit_price_micros": li.unit_price_micros,
                "subtotal_cents": li.subtotal_cents,
            }
            for li in sorted(row.line_items, key=lambda li: li.position)
        ],
        "tax_breakdown": row.tax_breakdown,
        "exemptions_applied": row.exemptions_applied or [],
        "issued_at": _iso(row.issued_at),
        "due_date": _iso(row.due_date),
        "finalized_at": _iso(row.finalized_at),
        "voided_at": _iso(row.voided_at),
        "void_reason": row.void_reason,
        "qbo_push_state": row.qbo_push_state,
        "qbo_push_attempts": row.qbo_push_attempts,
        "qbo_push_last_error": row.qbo_push_last_error,
        "external_refs": row.external_refs or {},
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def payment_to_doc(row: PaymentORM) -> Dict[str, Any]:
    """Build the ``payments_current`` document for a Payment row."""
    return {
        "payment_id": row.payment_id,
        "tenant_id": row.tenant_id,
        "invoice_id": row.invoice_id,
        "account_id": row.account_id,
        "amount_cents": row.amount_cents,
        "source": row.source,
        "method": row.method,
        "external_id": row.external_id,
        "reference": row.reference,
        "status": row.status,
        "received_at": _iso(row.received_at),
        "applied_at": _iso(row.applied_at),
        "reversed_at": _iso(row.reversed_at),
    }


# Maps aggregate_type -> (target ES index, projector). Used by the relay and by
# repositories when enqueuing outbox events.
PROJECTORS = {
    "customer": (CUSTOMERS_CURRENT_INDEX, customer_to_doc),
    "account": (ACCOUNTS_CURRENT_INDEX, account_to_doc),
    "invoice": (INVOICES_CURRENT_INDEX, invoice_to_doc),
    "payment": (PAYMENTS_CURRENT_INDEX, payment_to_doc),
}


# ---------------------------------------------------------------------------
# Pricing config
# ---------------------------------------------------------------------------


def price_book_to_doc(row) -> Dict[str, Any]:
    """Build the ``price_books_current`` document for a PriceBook row.

    Note: the service's create response embeds ``rules``, but the ES
    ``price_books_current`` doc itself stores only the book metadata (rules
    live in ``pricing_rules_current``), so the projection matches the stored
    book doc, not the API response envelope.
    """
    return {
        "price_book_id": row.price_book_id,
        "tenant_id": row.tenant_id,
        "name": row.name,
        "description": row.description,
        "status": row.status,
        "rule_count": row.rule_count,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def pricing_rule_to_doc(row) -> Dict[str, Any]:
    """Build the ``pricing_rules_current`` document for a PricingRule row."""
    return {
        "rule_id": row.rule_id,
        "price_book_id": row.price_book_id,
        "tenant_id": row.tenant_id,
        "product_code": row.product_code,
        "scope_type": row.scope_type,
        "scope_value": row.scope_value,
        "effective_from": _iso(row.effective_from),
        "effective_to": _iso(row.effective_to),
        "min_quantity_gallons": row.min_quantity_gallons,
        "unit_price_cents": row.unit_price_cents,
        "unit_price_micros": row.unit_price_micros,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


# ---------------------------------------------------------------------------
# Event ledgers
# ---------------------------------------------------------------------------


def invoice_event_to_doc(row) -> Dict[str, Any]:
    """Build the ``invoice_events`` document for an InvoiceEvent row."""
    return {
        "event_id": row.event_id,
        "invoice_id": row.invoice_id,
        "tenant_id": row.tenant_id,
        "event_type": row.event_type,
        "payload": row.payload or {},
        "occurred_at": _iso(row.occurred_at),
        "actor": row.actor,
        "sequence_number": row.sequence_number,
    }


def account_event_to_doc(row) -> Dict[str, Any]:
    """Build the ``account_events`` document for an AccountEvent row."""
    return {
        "event_id": row.event_id,
        "account_id": row.account_id,
        "tenant_id": row.tenant_id,
        "event_type": row.event_type,
        "payload": row.payload or {},
        "occurred_at": _iso(row.occurred_at),
        "actor": row.actor,
        "sequence_number": row.sequence_number,
    }


def dunning_event_to_doc(row) -> Dict[str, Any]:
    """Build the ``dunning_events`` document for a DunningEvent row."""
    return {
        "event_id": row.event_id,
        "invoice_id": row.invoice_id,
        "account_id": row.account_id,
        "tenant_id": row.tenant_id,
        "threshold_days": row.threshold_days,
        "template_key": row.template_key,
        "queued_at": _iso(row.queued_at),
        "cancelled_at": _iso(row.cancelled_at),
        "cancellation_reason": row.cancellation_reason,
    }


def ar_aging_snapshot_to_doc(row) -> Dict[str, Any]:
    """Build the ``ar_aging_snapshots`` document for an ArAgingSnapshot row."""
    return {
        "snapshot_id": row.snapshot_id,
        "tenant_id": row.tenant_id,
        "snapshot_date": _iso(row.snapshot_date),
        "total_open_cents": row.total_open_cents,
        "bucket_0_30_cents": row.bucket_0_30_cents,
        "bucket_31_60_cents": row.bucket_31_60_cents,
        "bucket_61_90_cents": row.bucket_61_90_cents,
        "bucket_90_plus_cents": row.bucket_90_plus_cents,
        "account_count_with_balance": row.account_count_with_balance,
    }


PROJECTORS.update({
    "price_book": (PRICE_BOOKS_CURRENT_INDEX, price_book_to_doc),
    "pricing_rule": (PRICING_RULES_CURRENT_INDEX, pricing_rule_to_doc),
    "invoice_event": (INVOICE_EVENTS_INDEX, invoice_event_to_doc),
    "account_event": (ACCOUNT_EVENTS_INDEX, account_event_to_doc),
    "dunning_event": (DUNNING_EVENTS_INDEX, dunning_event_to_doc),
    "ar_aging_snapshot": (AR_AGING_SNAPSHOTS_INDEX, ar_aging_snapshot_to_doc),
})


# ---------------------------------------------------------------------------
# Compliance config (hybrid: the stored ``document`` IS the ES projection)
# ---------------------------------------------------------------------------


def _document_passthrough(row) -> Dict[str, Any]:
    """Return the verbatim stored ES document for a hybrid compliance row."""
    return dict(row.document or {})


PROJECTORS.update({
    "tax_jurisdiction": (TAX_JURISDICTIONS_INDEX, _document_passthrough),
    "tax_exemption": (TAX_EXEMPTIONS_INDEX, _document_passthrough),
    "price_protection_contract": (PRICE_PROTECTION_CONTRACTS_INDEX, _document_passthrough),
    "compliance_pricing_rule": (COMPLIANCE_PRICING_RULES_INDEX, _document_passthrough),
    "supplier_contract": (SUPPLIER_CONTRACTS_INDEX, _document_passthrough),
})


# ---------------------------------------------------------------------------
# Orders / jobs current-state (hybrid: stored ``document`` is the projection)
# ---------------------------------------------------------------------------

PROJECTORS.update({
    "fuel_order": (FUEL_ORDERS_CURRENT_INDEX, _document_passthrough),
    "job": (JOBS_CURRENT_INDEX, _document_passthrough),
    "shipment": (OpsElasticsearchService.SHIPMENTS_CURRENT, _document_passthrough),
    "tenant_job_policy": (TENANT_JOB_POLICIES_INDEX, _document_passthrough),
})


# ---------------------------------------------------------------------------
# Master data (hybrid: stored ``document`` is the projection)
# ---------------------------------------------------------------------------

PROJECTORS.update({
    "driver": (DRIVERS_INDEX, _document_passthrough),
    "depot": (DEPOTS_INDEX, _document_passthrough),
    "terminal": (TERMINALS_INDEX, _document_passthrough),
    "asset_certification": (ASSET_CERTIFICATIONS_INDEX, _document_passthrough),
    "intake_channel": (INTAKE_CHANNELS_INDEX, _document_passthrough),
    "truck": ("trucks", _document_passthrough),
    "location": ("locations", _document_passthrough),
})
