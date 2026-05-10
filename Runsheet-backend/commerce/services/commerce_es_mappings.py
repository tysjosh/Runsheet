"""Elasticsearch index mappings for Commerce Backbone indices.

Defines strict mappings for the 11 commerce indices covering customers, accounts,
price books, pricing rules, invoices, invoice events, payments, AR aging snapshots,
account events, dunning events, and invoice counter checkpoints.

All mappings use ``dynamic: strict`` to reject unexpected fields and every index carries a
``tenant_id`` keyword field so tenant isolation can be enforced at the query layer.

Follows the same tenant-prefixed alias strategy as ``fuel/services/fuel_ops_es_mappings.py``.

Validates: Requirements 1.1, 2.1, 3.1, 5.1, 6.1, 7.1, 7.4, 9.4
"""
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Index name constants
# ---------------------------------------------------------------------------

# Customer + Account
CUSTOMERS_CURRENT_INDEX = "customers_current"
ACCOUNTS_CURRENT_INDEX = "accounts_current"

# Pricing
PRICE_BOOKS_CURRENT_INDEX = "price_books_current"
PRICING_RULES_CURRENT_INDEX = "pricing_rules_current"

# Invoicing
INVOICES_CURRENT_INDEX = "invoices_current"
INVOICE_EVENTS_INDEX = "invoice_events"

# Payments
PAYMENTS_CURRENT_INDEX = "payments_current"

# AR Aging
AR_AGING_SNAPSHOTS_INDEX = "ar_aging_snapshots"

# Account events (audit trail)
ACCOUNT_EVENTS_INDEX = "account_events"

# Dunning
DUNNING_EVENTS_INDEX = "dunning_events"

# Invoice numbering
INVOICE_COUNTER_CHECKPOINTS_INDEX = "invoice_counter_checkpoints"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DEFAULT_SETTINGS = {
    "number_of_shards": 1,
    "number_of_replicas": 1,
}


# ---------------------------------------------------------------------------
# Customer + Account mappings
# ---------------------------------------------------------------------------

CUSTOMERS_CURRENT_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "customer_id":    {"type": "keyword"},
            "tenant_id":      {"type": "keyword"},
            "display_name":   {"type": "text", "fields": {"kw": {"type": "keyword"}}},
            "legal_name":     {"type": "text"},
            "primary_email":  {"type": "keyword"},
            "tax_id":         {"type": "keyword"},
            "status":         {"type": "keyword"},
            "created_at":     {"type": "date"},
            "updated_at":     {"type": "date"},
            "external_refs":  {"type": "object"},
            "metadata":       {"type": "object", "enabled": False},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

ACCOUNTS_CURRENT_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "account_id":                 {"type": "keyword"},
            "tenant_id":                  {"type": "keyword"},
            "customer_id":                {"type": "keyword"},
            "display_name":               {"type": "text", "fields": {"kw": {"type": "keyword"}}},
            "status":                     {"type": "keyword"},
            "credit_limit_cents":         {"type": "long"},
            "open_balance_cents":         {"type": "long"},
            "available_credit_cents":     {"type": "long"},
            "credit_balance_cents":       {"type": "long"},
            "credit_state":               {"type": "keyword"},
            "credit_override_expires_at": {"type": "date"},
            "net_terms_days":             {"type": "integer"},
            "tier":                       {"type": "keyword"},
            "billing_address":            {"type": "object"},
            "payment_method_preference":  {"type": "keyword"},
            "created_at":                 {"type": "date"},
            "updated_at":                 {"type": "date"},
            "external_refs":              {"type": "object"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Pricing mappings
# ---------------------------------------------------------------------------

PRICE_BOOKS_CURRENT_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "price_book_id": {"type": "keyword"},
            "tenant_id":     {"type": "keyword"},
            "name":          {"type": "keyword"},
            "description":   {"type": "text"},
            "status":        {"type": "keyword"},
            "rule_count":    {"type": "integer"},
            "created_at":    {"type": "date"},
            "updated_at":    {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

PRICING_RULES_CURRENT_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "rule_id":              {"type": "keyword"},
            "price_book_id":       {"type": "keyword"},
            "tenant_id":           {"type": "keyword"},
            "product_code":        {"type": "keyword"},
            "scope_type":          {"type": "keyword"},
            "scope_value":         {"type": "keyword"},
            "effective_from":      {"type": "date"},
            "effective_to":        {"type": "date"},
            "min_quantity_gallons": {"type": "double"},
            "unit_price_cents":    {"type": "long"},
            "created_at":          {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Invoice mappings
# ---------------------------------------------------------------------------

INVOICES_CURRENT_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "invoice_id":          {"type": "keyword"},
            "tenant_id":           {"type": "keyword"},
            "customer_id":        {"type": "keyword"},
            "account_id":          {"type": "keyword"},
            "order_id":            {"type": "keyword"},
            "invoice_number":      {"type": "keyword"},
            "status":              {"type": "keyword"},
            "total_cents":         {"type": "long"},
            "amount_paid_cents":   {"type": "long"},
            "remaining_cents":     {"type": "long"},
            "tax_cents":           {"type": "long"},
            "subtotal_cents":      {"type": "long"},
            "line_items": {
                "type": "nested",
                "properties": {
                    "line_id":          {"type": "keyword"},
                    "product_code":     {"type": "keyword"},
                    "quantity_gallons": {"type": "double"},
                    "unit_price_cents": {"type": "long"},
                    "subtotal_cents":   {"type": "long"},
                },
            },
            # Tax breakdown appended by InvoiceService.generate_from_order
            # when a TaxEngine is wired in (fuel-compliance-backbone
            # task 3.10). Stores the per-component cents bucket rollup
            # from TaxEngine.compute_tax() so Form 720 reporting has
            # a stable projection to read from. ``enabled: False``
            # keeps the object payload (including nested line_items)
            # but skips indexing to avoid exploding the field set on
            # the strict mapping — we never query into tax_breakdown,
            # only render it on the invoice.
            "tax_breakdown":       {"type": "object", "enabled": False},
            # Exemption ids honored by the TaxEngine when computing
            # the breakdown for this invoice. Persisted for IRS /
            # operator audit (Req 6.7).
            "exemptions_applied":  {"type": "keyword"},
            "issued_at":           {"type": "date"},
            "due_date":            {"type": "date"},
            "finalized_at":        {"type": "date"},
            "voided_at":           {"type": "date"},
            "void_reason":         {"type": "text"},
            "qbo_push_state":      {"type": "keyword"},
            "qbo_push_attempts":   {"type": "integer"},
            "qbo_push_last_error": {"type": "text"},
            "external_refs":       {"type": "object"},
            "created_at":          {"type": "date"},
            "updated_at":          {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}

INVOICE_EVENTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "event_id":    {"type": "keyword"},
            "invoice_id":  {"type": "keyword"},
            "tenant_id":   {"type": "keyword"},
            "event_type":  {"type": "keyword"},
            "payload":     {"type": "object"},
            "occurred_at": {"type": "date"},
            "actor":       {"type": "keyword"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Payment mappings
# ---------------------------------------------------------------------------

PAYMENTS_CURRENT_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "payment_id":   {"type": "keyword"},
            "tenant_id":    {"type": "keyword"},
            "invoice_id":   {"type": "keyword"},
            "account_id":   {"type": "keyword"},
            "amount_cents": {"type": "long"},
            "source":       {"type": "keyword"},
            "method":       {"type": "keyword"},
            "external_id":  {"type": "keyword"},
            "reference":    {"type": "text"},
            "status":       {"type": "keyword"},
            "received_at":  {"type": "date"},
            "applied_at":   {"type": "date"},
            "reversed_at":  {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Account events mapping (append-only audit trail)
# ---------------------------------------------------------------------------

ACCOUNT_EVENTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "event_id":        {"type": "keyword"},
            "account_id":      {"type": "keyword"},
            "tenant_id":       {"type": "keyword"},
            "event_type":      {"type": "keyword"},
            "payload":         {"type": "object"},
            "occurred_at":     {"type": "date"},
            "actor":           {"type": "keyword"},
            "sequence_number": {"type": "long"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Dunning events mapping
# ---------------------------------------------------------------------------

DUNNING_EVENTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "event_id":            {"type": "keyword"},
            "invoice_id":          {"type": "keyword"},
            "account_id":          {"type": "keyword"},
            "tenant_id":           {"type": "keyword"},
            "threshold_days":      {"type": "integer"},
            "template_key":        {"type": "keyword"},
            "queued_at":           {"type": "date"},
            "cancelled_at":        {"type": "date"},
            "cancellation_reason": {"type": "keyword"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# AR aging snapshots mapping
# ---------------------------------------------------------------------------

AR_AGING_SNAPSHOTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "snapshot_id":                {"type": "keyword"},
            "tenant_id":                  {"type": "keyword"},
            "snapshot_date":              {"type": "date"},
            "total_open_cents":           {"type": "long"},
            "bucket_0_30_cents":          {"type": "long"},
            "bucket_31_60_cents":         {"type": "long"},
            "bucket_61_90_cents":         {"type": "long"},
            "bucket_90_plus_cents":       {"type": "long"},
            "account_count_with_balance": {"type": "integer"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Invoice counter checkpoints mapping
# ---------------------------------------------------------------------------

INVOICE_COUNTER_CHECKPOINTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "tenant_id":       {"type": "keyword"},
            "max_seq":         {"type": "long"},
            "checkpoint_date": {"type": "date"},
            "created_at":      {"type": "date"},
        },
    },
    "settings": _DEFAULT_SETTINGS,
}


# ---------------------------------------------------------------------------
# Index registry
# ---------------------------------------------------------------------------

COMMERCE_INDEX_MAPPINGS = {
    # Customer + Account
    CUSTOMERS_CURRENT_INDEX:           CUSTOMERS_CURRENT_MAPPING,
    ACCOUNTS_CURRENT_INDEX:            ACCOUNTS_CURRENT_MAPPING,
    # Pricing
    PRICE_BOOKS_CURRENT_INDEX:         PRICE_BOOKS_CURRENT_MAPPING,
    PRICING_RULES_CURRENT_INDEX:       PRICING_RULES_CURRENT_MAPPING,
    # Invoicing
    INVOICES_CURRENT_INDEX:            INVOICES_CURRENT_MAPPING,
    INVOICE_EVENTS_INDEX:              INVOICE_EVENTS_MAPPING,
    # Payments
    PAYMENTS_CURRENT_INDEX:            PAYMENTS_CURRENT_MAPPING,
    # AR Aging
    AR_AGING_SNAPSHOTS_INDEX:          AR_AGING_SNAPSHOTS_MAPPING,
    # Account events
    ACCOUNT_EVENTS_INDEX:              ACCOUNT_EVENTS_MAPPING,
    # Dunning
    DUNNING_EVENTS_INDEX:              DUNNING_EVENTS_MAPPING,
    # Invoice numbering
    INVOICE_COUNTER_CHECKPOINTS_INDEX: INVOICE_COUNTER_CHECKPOINTS_MAPPING,
}


# ---------------------------------------------------------------------------
# Index setup function
# ---------------------------------------------------------------------------


def setup_commerce_indices(es_service) -> None:
    """Create Commerce Backbone ES indices if they don't already exist.

    Follows the same tenant-prefixed alias strategy as setup_fuel_ops_indices
    in fuel_ops_es_mappings.py. On Elasticsearch Serverless deployments,
    shard/replica settings are stripped before creation via
    ``ElasticsearchService.strip_serverless_incompatible_settings``.

    Args:
        es_service: An ElasticsearchService instance with ``.client`` and
            ``.is_serverless`` attributes.
    """
    from services.elasticsearch_service import ElasticsearchService

    es_client = es_service.client
    is_serverless = es_service.is_serverless

    for index_name, mapping in COMMERCE_INDEX_MAPPINGS.items():
        try:
            if not es_client.indices.exists(index=index_name):
                body = mapping
                if is_serverless:
                    body = ElasticsearchService.strip_serverless_incompatible_settings(body)
                es_client.indices.create(index=index_name, body=body)
                logger.info(f"Created commerce index: {index_name}")
            else:
                logger.info(f"Commerce index already exists: {index_name}")
        except Exception as e:
            logger.error(f"Failed to create commerce index {index_name}: {e}")
