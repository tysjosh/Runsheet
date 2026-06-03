"""Initial commerce source-of-truth: customers, accounts, invoices, line items,
payments, idempotency_keys, outbox_events.

Revision ID: 0001_commerce_sot
Revises:
Create Date: 2026-06-02

First migration slice of the Postgres source-of-truth. Creates the
transactional commerce tables, the idempotency-key concurrency primitive
(real composite PK), and the transactional outbox the relay drains into ES.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001_commerce_sot"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("customer_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("legal_name", sa.String(255)),
        sa.Column("primary_email", sa.String(320)),
        sa.Column("tax_id", sa.String(64)),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("external_refs", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "tax_id", name="uq_customer_tenant_tax_id"),
    )
    op.create_index("ix_customer_tenant_status", "customers", ["tenant_id", "status"])
    op.create_index("ix_customer_tenant_created", "customers", ["tenant_id", "created_at"])

    op.create_table(
        "accounts",
        sa.Column("account_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("credit_limit_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("open_balance_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("available_credit_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("credit_balance_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("credit_state", sa.String(32), nullable=False, server_default="ok"),
        sa.Column("credit_override_expires_at", sa.DateTime(timezone=True)),
        sa.Column("net_terms_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("tier", sa.String(32), nullable=False, server_default="default"),
        sa.Column("billing_address", sa.JSON()),
        sa.Column("payment_method_preference", sa.String(32), nullable=False, server_default="invoice"),
        sa.Column("external_refs", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.customer_id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_account_tenant_customer", "accounts", ["tenant_id", "customer_id"])
    op.create_index("ix_account_tenant_status", "accounts", ["tenant_id", "status"])

    op.create_table(
        "invoices",
        sa.Column("invoice_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(64)),
        sa.Column("invoice_number", sa.String(64)),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("total_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("amount_paid_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("remaining_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tax_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("subtotal_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tax_breakdown", sa.JSON()),
        sa.Column("exemptions_applied", sa.JSON()),
        sa.Column("issued_at", sa.DateTime(timezone=True)),
        sa.Column("due_date", sa.Date()),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        sa.Column("void_reason", sa.Text()),
        sa.Column("qbo_push_state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("qbo_push_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("qbo_push_last_error", sa.Text()),
        sa.Column("external_refs", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.customer_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("tenant_id", "invoice_number", name="uq_invoice_tenant_number"),
    )
    op.create_index("ix_invoice_tenant_status", "invoices", ["tenant_id", "status"])
    op.create_index("ix_invoice_tenant_customer", "invoices", ["tenant_id", "customer_id"])
    op.create_index("ix_invoice_tenant_account", "invoices", ["tenant_id", "account_id"])
    op.create_index("ix_invoice_order", "invoices", ["order_id"])

    op.create_table(
        "invoice_line_items",
        sa.Column("line_id", sa.String(64), primary_key=True),
        sa.Column("invoice_id", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("product_code", sa.String(64), nullable=False),
        sa.Column("quantity_gallons", sa.Float(), nullable=False),
        sa.Column("unit_price_cents", sa.BigInteger(), nullable=False),
        sa.Column("subtotal_cents", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.invoice_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_line_item_invoice", "invoice_line_items", ["invoice_id"])

    op.create_table(
        "payments",
        sa.Column("payment_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("invoice_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128)),
        sa.Column("reference", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False, server_default="applied"),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.invoice_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("tenant_id", "source", "external_id", name="uq_payment_tenant_source_external"),
    )
    op.create_index("ix_payment_tenant_invoice", "payments", ["tenant_id", "invoice_id"])
    op.create_index("ix_payment_tenant_account", "payments", ["tenant_id", "account_id"])

    op.create_table(
        "idempotency_keys",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(255), primary_key=True),
        sa.Column("request_fingerprint", sa.String(128)),
        sa.Column("response_status", sa.Integer()),
        sa.Column("response_body", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_idempotency_expires", "idempotency_keys", ["expires_at"])

    op.create_table(
        "invoice_counters",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("next_seq", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("target_index", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
    )
    op.create_index("ix_outbox_unpublished", "outbox_events", ["published_at", "id"])
    op.create_index("ix_outbox_aggregate", "outbox_events", ["aggregate_type", "aggregate_id"])

    # --- Pricing config -----------------------------------------------------
    op.create_table(
        "price_books",
        sa.Column("price_book_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("rule_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_price_book_tenant_status", "price_books", ["tenant_id", "status"])
    op.create_index("ix_price_book_tenant_created", "price_books", ["tenant_id", "created_at"])

    op.create_table(
        "pricing_rules",
        sa.Column("rule_id", sa.String(64), primary_key=True),
        sa.Column("price_book_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("product_code", sa.String(64), nullable=False),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("scope_value", sa.String(128), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True)),
        sa.Column("effective_to", sa.DateTime(timezone=True)),
        sa.Column("min_quantity_gallons", sa.Float()),
        sa.Column("unit_price_cents", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["price_book_id"], ["price_books.price_book_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_pricing_rule_book", "pricing_rules", ["price_book_id"])
    op.create_index("ix_pricing_rule_tenant_product", "pricing_rules", ["tenant_id", "product_code"])

    # --- Event ledgers ------------------------------------------------------
    op.create_table(
        "invoice_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("invoice_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.UniqueConstraint("invoice_id", "sequence_number", name="uq_invoice_event_seq"),
    )
    op.create_index("ix_invoice_event_invoice", "invoice_events", ["tenant_id", "invoice_id"])

    op.create_table(
        "account_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.UniqueConstraint("account_id", "sequence_number", name="uq_account_event_seq"),
    )
    op.create_index("ix_account_event_account", "account_events", ["tenant_id", "account_id"])

    op.create_table(
        "dunning_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("invoice_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("threshold_days", sa.Integer()),
        sa.Column("template_key", sa.String(64)),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_reason", sa.String(64)),
    )
    op.create_index("ix_dunning_tenant_invoice", "dunning_events", ["tenant_id", "invoice_id"])
    op.create_index("ix_dunning_tenant_account", "dunning_events", ["tenant_id", "account_id"])

    # --- AR aging snapshots -------------------------------------------------
    op.create_table(
        "ar_aging_snapshots",
        sa.Column("snapshot_id", sa.String(160), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("snapshot_date", sa.Date()),
        sa.Column("total_open_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bucket_0_30_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bucket_31_60_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bucket_61_90_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bucket_90_plus_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("account_count_with_balance", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_ar_aging_tenant_date", "ar_aging_snapshots", ["tenant_id", "snapshot_date"])

    # --- Compliance config (hybrid document tables) -------------------------
    op.create_table(
        "tax_jurisdictions",
        sa.Column("jurisdiction_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("fips_code", sa.String(16)),
        sa.Column("tax_type", sa.String(32)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tax_juris_tenant_fips", "tax_jurisdictions", ["tenant_id", "fips_code"])
    op.create_index("ix_tax_juris_tenant_type", "tax_jurisdictions", ["tenant_id", "tax_type"])

    op.create_table(
        "tax_exemptions",
        sa.Column("exemption_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("customer_id", sa.String(64)),
        sa.Column("certificate_number", sa.String(128)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tax_exempt_tenant_customer", "tax_exemptions", ["tenant_id", "customer_id"])
    op.create_index("ix_tax_exempt_tenant_cert", "tax_exemptions", ["tenant_id", "certificate_number"])

    op.create_table(
        "price_protection_contracts",
        sa.Column("contract_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("customer_id", sa.String(64)),
        sa.Column("product_code", sa.String(64)),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ppc_tenant_customer", "price_protection_contracts", ["tenant_id", "customer_id"])
    op.create_index("ix_ppc_tenant_status", "price_protection_contracts", ["tenant_id", "status"])

    op.create_table(
        "compliance_pricing_rules",
        sa.Column("rule_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("customer_id", sa.String(64)),
        sa.Column("product_code", sa.String(64)),
        sa.Column("strategy", sa.String(32)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_cpr_tenant_product", "compliance_pricing_rules", ["tenant_id", "product_code"])
    op.create_index("ix_cpr_tenant_customer", "compliance_pricing_rules", ["tenant_id", "customer_id"])

    op.create_table(
        "supplier_contracts",
        sa.Column("contract_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("supplier_name", sa.String(255)),
        sa.Column("product_code", sa.String(64)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_supplier_contract_tenant_status", "supplier_contracts", ["tenant_id", "status"])
    op.create_index("ix_supplier_contract_tenant_supplier", "supplier_contracts", ["tenant_id", "supplier_name"])

    # --- Orders / jobs current-state (hybrid document tables) ---------------
    op.create_table(
        "fuel_orders_current",
        sa.Column("order_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("customer_id", sa.String(64)),
        sa.Column("assigned_driver_id", sa.String(64)),
        sa.Column("last_event_timestamp", sa.String(40)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_fuel_order_tenant_status", "fuel_orders_current", ["tenant_id", "status"])
    op.create_index("ix_fuel_order_tenant_customer", "fuel_orders_current", ["tenant_id", "customer_id"])
    op.create_index("ix_fuel_order_tenant_driver", "fuel_orders_current", ["tenant_id", "assigned_driver_id"])

    op.create_table(
        "jobs_current",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("asset_id", sa.String(64)),
        sa.Column("last_event_timestamp", sa.String(40)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_job_tenant_status", "jobs_current", ["tenant_id", "status"])
    op.create_index("ix_job_tenant_asset", "jobs_current", ["tenant_id", "asset_id"])

    op.create_table(
        "shipments_current",
        sa.Column("shipment_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("last_event_timestamp", sa.String(40)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_shipment_tenant_status", "shipments_current", ["tenant_id", "status"])

    op.create_table(
        "tenant_job_policies",
        sa.Column("policy_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tenant_job_policy_tenant", "tenant_job_policies", ["tenant_id"])

    # --- Master data (hybrid document tables) -------------------------------
    op.create_table(
        "drivers",
        sa.Column("driver_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("cdl_number", sa.String(64)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_driver_tenant_status", "drivers", ["tenant_id", "status"])
    op.create_index("ix_driver_tenant_cdl", "drivers", ["tenant_id", "cdl_number"])

    op.create_table(
        "depots",
        sa.Column("depot_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("is_default", sa.Boolean()),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_depot_tenant", "depots", ["tenant_id"])
    op.create_index("ix_depot_tenant_default", "depots", ["tenant_id", "is_default"])

    op.create_table(
        "terminals",
        sa.Column("terminal_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_terminal_tenant_status", "terminals", ["tenant_id", "status"])

    op.create_table(
        "asset_certifications",
        sa.Column("cert_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("asset_id", sa.String(64)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_asset_cert_tenant_asset", "asset_certifications", ["tenant_id", "asset_id"])
    op.create_index("ix_asset_cert_tenant_status", "asset_certifications", ["tenant_id", "status"])

    op.create_table(
        "intake_channels",
        sa.Column("channel_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_intake_channel_tenant", "intake_channels", ["tenant_id"])

    op.create_table(
        "trucks",
        sa.Column("truck_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_truck_tenant", "trucks", ["tenant_id"])

    op.create_table(
        "locations",
        sa.Column("location_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_location_tenant", "locations", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("locations")
    op.drop_table("trucks")
    op.drop_table("intake_channels")
    op.drop_table("asset_certifications")
    op.drop_table("terminals")
    op.drop_table("depots")
    op.drop_table("drivers")
    op.drop_table("tenant_job_policies")
    op.drop_table("shipments_current")
    op.drop_table("jobs_current")
    op.drop_table("fuel_orders_current")
    op.drop_table("supplier_contracts")
    op.drop_table("compliance_pricing_rules")
    op.drop_table("price_protection_contracts")
    op.drop_table("tax_exemptions")
    op.drop_table("tax_jurisdictions")
    op.drop_table("ar_aging_snapshots")
    op.drop_table("dunning_events")
    op.drop_table("account_events")
    op.drop_table("invoice_events")
    op.drop_table("pricing_rules")
    op.drop_table("price_books")
    op.drop_table("outbox_events")
    op.drop_table("invoice_counters")
    op.drop_table("idempotency_keys")
    op.drop_table("payments")
    op.drop_table("invoice_line_items")
    op.drop_table("invoices")
    op.drop_table("accounts")
    op.drop_table("customers")
