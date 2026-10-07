"""SQLAlchemy ORM models for the PostgreSQL source-of-truth.

These tables are the authoritative store for the financial / commerce
entities and for the idempotency-key concurrency primitive. They mirror the
existing Pydantic models (``commerce/models/*``) and ES mappings
(``commerce/services/commerce_es_mappings.py``) field-for-field so the ES
``*_current`` indices can be projected from these rows by the outbox relay.

Money is stored as integer cents (``BigInteger``) to match Constraint C1 in
the commerce design — never floats. Timestamps are timezone-aware. Every
tenant-scoped table carries a ``tenant_id`` column and composite indexes that
lead with ``tenant_id`` so tenant isolation is cheap at the query layer.

Foreign keys encode the real relationships the ES layer could only imply:
    customer 1──* account 1──* invoice 1──* payment
    invoice 1──* invoice_line_item
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from persistence.database import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now (mirrors services.time_utils.utcnow)."""
    return datetime.now(timezone.utc)


# Portable JSON type: works on both PostgreSQL and SQLite (TEXT) so the same
# models run in tests and production.
#
# NB: this resolves to PostgreSQL ``json``, NOT ``jsonb`` — the generic
# SQLAlchemy ``JSON`` type does not upgrade itself, and ``\d
# fuel_orders_current`` confirms ``document | json``. The comment here used to
# claim "JSONB under the hood via the dialect", which is wrong and matters: a
# ``json`` column cannot carry a GIN index, so containment and key lookups
# against it are sequential scans.
_JSON = JSON().with_variant(JSON(), "sqlite")

# Real ``jsonb`` on PostgreSQL, plain JSON on SQLite. Used by tables whose
# document column is *queried* rather than merely stored, because jsonb is what
# supports GIN indexing and the ``@>`` / ``->>`` operators the Elasticsearch
# query adapter needs. New hybrid tables should prefer this.
_JSONB = JSON().with_variant(_PG_JSONB(), "postgresql")


class TimestampMixin:
    """created_at / updated_at columns shared by current-state tables."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------


class CustomerORM(TimestampMixin, Base):
    """Authoritative Customer record (projects to ``customers_current``)."""

    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[Optional[str]] = mapped_column(String(255))
    primary_email: Mapped[Optional[str]] = mapped_column(String(320))
    tax_id: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    external_refs: Mapped[Dict[str, Any]] = mapped_column(_JSON, default=dict, nullable=False)
    customer_metadata: Mapped[Dict[str, Any]] = mapped_column(
        "metadata", _JSON, default=dict, nullable=False
    )

    accounts: Mapped[List["AccountORM"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # tax_id is unique per tenant when present (DB-enforced dedupe).
        UniqueConstraint("tenant_id", "tax_id", name="uq_customer_tenant_tax_id"),
        Index("ix_customer_tenant_status", "tenant_id", "status"),
        Index("ix_customer_tenant_created", "tenant_id", "created_at"),
    )


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


class AccountORM(TimestampMixin, Base):
    """Authoritative Account record (projects to ``accounts_current``).

    ``credit_limit_cents`` / ``open_balance_cents`` are the numbers that must
    never drift, which is exactly why this row lives in Postgres: credit
    checks can take a row lock (``SELECT ... FOR UPDATE``) instead of racing
    against eventually-consistent ES reads.
    """

    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.customer_id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    credit_limit_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    open_balance_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    available_credit_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    credit_balance_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    credit_state: Mapped[str] = mapped_column(String(32), default="ok", nullable=False)
    credit_override_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Who approved the active override and why (OI-42, migration 0010).
    credit_override_reason: Mapped[Optional[str]] = mapped_column(Text)
    credit_override_authorized_by: Mapped[Optional[str]] = mapped_column(String(255))
    net_terms_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    tier: Mapped[str] = mapped_column(String(32), default="default", nullable=False)
    billing_address: Mapped[Optional[Dict[str, Any]]] = mapped_column(_JSON)
    payment_method_preference: Mapped[str] = mapped_column(
        String(32), default="invoice", nullable=False
    )
    external_refs: Mapped[Dict[str, Any]] = mapped_column(_JSON, default=dict, nullable=False)

    customer: Mapped["CustomerORM"] = relationship(back_populates="accounts")
    invoices: Mapped[List["InvoiceORM"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_account_tenant_customer", "tenant_id", "customer_id"),
        Index("ix_account_tenant_status", "tenant_id", "status"),
    )


# ---------------------------------------------------------------------------
# Invoice + line items
# ---------------------------------------------------------------------------


class InvoiceORM(TimestampMixin, Base):
    """Authoritative Invoice record (projects to ``invoices_current``)."""

    __tablename__ = "invoices"

    invoice_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.customer_id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[Optional[str]] = mapped_column(String(64))
    pod_id: Mapped[Optional[str]] = mapped_column(String(64))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    delivery_result: Mapped[Optional[Dict[str, Any]]] = mapped_column(_JSON)
    invoice_number: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    total_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    amount_paid_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    remaining_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tax_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    subtotal_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tax_breakdown: Mapped[Optional[Dict[str, Any]]] = mapped_column(_JSON)
    exemptions_applied: Mapped[Optional[List[str]]] = mapped_column(_JSON)
    issued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[Optional[str]] = mapped_column(Text)
    qbo_push_state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    qbo_push_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qbo_push_last_error: Mapped[Optional[str]] = mapped_column(Text)
    external_refs: Mapped[Dict[str, Any]] = mapped_column(_JSON, default=dict, nullable=False)

    account: Mapped["AccountORM"] = relationship(back_populates="invoices")
    line_items: Mapped[List["InvoiceLineItemORM"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceLineItemORM.position",
    )
    payments: Mapped[List["PaymentORM"]] = relationship(back_populates="invoice")

    __table_args__ = (
        # Human-readable invoice numbers are unique per tenant (the integrity
        # guarantee ES could not enforce). NULL numbers (drafts) are allowed
        # to repeat because SQL treats NULLs as distinct.
        UniqueConstraint("tenant_id", "invoice_number", name="uq_invoice_tenant_number"),
        Index("ix_invoice_tenant_status", "tenant_id", "status"),
        Index("ix_invoice_tenant_customer", "tenant_id", "customer_id"),
        Index("ix_invoice_tenant_account", "tenant_id", "account_id"),
        Index("ix_invoice_order", "order_id"),
        Index("ix_invoice_pod", "pod_id"),
    )


class InvoiceLineItemORM(Base):
    """A single Invoice line item (flattened into ``invoices_current.line_items``)."""

    __tablename__ = "invoice_line_items"

    line_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("invoices.invoice_id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    product_code: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity_gallons: Mapped[float] = mapped_column(nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_price_micros: Mapped[Optional[int]] = mapped_column(BigInteger)
    subtotal_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)

    invoice: Mapped["InvoiceORM"] = relationship(back_populates="line_items")

    __table_args__ = (Index("ix_line_item_invoice", "invoice_id"),)


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------


class PaymentORM(Base):
    """Authoritative Payment record (projects to ``payments_current``).

    ``external_id`` is unique per (tenant, source) so a webhook/sync that
    delivers the same Stripe charge or QBO payment twice can never create two
    payment rows — the DB rejects the duplicate. This is the second place
    (besides idempotency keys) where ES could not enforce correctness.
    """

    __tablename__ = "payments"

    payment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    invoice_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("invoices.invoice_id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(128))
    reference: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="applied", nullable=False)
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    reversed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    invoice: Mapped["InvoiceORM"] = relationship(back_populates="payments")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source", "external_id", name="uq_payment_tenant_source_external"
        ),
        Index("ix_payment_tenant_invoice", "tenant_id", "invoice_id"),
        Index("ix_payment_tenant_account", "tenant_id", "account_id"),
    )


# ---------------------------------------------------------------------------
# Idempotency keys (concurrency primitive)
# ---------------------------------------------------------------------------


class IdempotencyKeyORM(Base):
    """Request-dedupe store with a REAL unique constraint.

    The ES ``idempotency_keys`` index could not actually prevent two
    concurrent requests with the same key from both being processed (no
    cross-document uniqueness under concurrency). The composite primary key
    here makes a duplicate insert fail atomically, which is the whole point of
    moving this primitive to Postgres.
    """

    __tablename__ = "idempotency_keys"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_fingerprint: Mapped[Optional[str]] = mapped_column(String(128))
    response_status: Mapped[Optional[int]] = mapped_column(Integer)
    response_body: Mapped[Optional[Dict[str, Any]]] = mapped_column(_JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_idempotency_expires", "expires_at"),)


# ---------------------------------------------------------------------------
# Per-tenant invoice number counter
# ---------------------------------------------------------------------------


class InvoiceCounterORM(Base):
    """Per-tenant monotonic invoice-number counter.

    Replaces the former Redis-INCR + ES-checkpoint + reseed machinery
    (the removed ``commerce/services/invoice_numbering.py``) with a single row
    per tenant. The next number is allocated by incrementing ``next_seq`` under
    a row lock
    (``SELECT ... FOR UPDATE``) inside the SAME transaction as the invoice
    finalize, so a number is never skipped on rollback and never issued twice
    under concurrency. This is the guarantee ES/Redis could only approximate.
    """

    __tablename__ = "invoice_counters"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    next_seq: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Transactional outbox
# ---------------------------------------------------------------------------


class OutboxEventORM(Base):

    """Transactional outbox row.

    Written in the SAME database transaction as the business write. A relay
    process (``persistence.outbox_relay.OutboxRelay``) later drains unpublished
    rows and projects them into Elasticsearch. Because the outbox row and the
    business row commit atomically, the ES projection can never silently miss
    a write: either both the row and its outbox event exist, or neither does.
    """

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Target ES index + full projected document for the relay to index.
    target_index: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[Dict[str, Any]] = mapped_column(_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        # The relay polls "unpublished, oldest first": published_at IS NULL
        # ordered by id. A partial index on published_at keeps that scan cheap.
        Index("ix_outbox_unpublished", "published_at", "id"),
        Index("ix_outbox_aggregate", "aggregate_type", "aggregate_id"),
    )


# ---------------------------------------------------------------------------
# Commerce — pricing config (price books + rules)
# ---------------------------------------------------------------------------


class PriceBookORM(TimestampMixin, Base):
    """Authoritative PriceBook (projects to ``price_books_current``)."""

    __tablename__ = "price_books"

    price_book_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    rule_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("ix_price_book_tenant_status", "tenant_id", "status"),
        Index("ix_price_book_tenant_created", "tenant_id", "created_at"),
    )


class PricingRuleORM(Base):
    """Authoritative PricingRule (projects to ``pricing_rules_current``).

    Mirrors the commerce ``price_book`` fan-out rule shape (scope_type /
    scope_value / unit_price_cents), not the compliance sell-side rule.
    """

    __tablename__ = "pricing_rules"

    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    price_book_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("price_books.price_book_id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    product_code: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_value: Mapped[str] = mapped_column(String(128), nullable=False)
    effective_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    min_quantity_gallons: Mapped[Optional[float]] = mapped_column()
    unit_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_price_micros: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_pricing_rule_book", "price_book_id"),
        Index("ix_pricing_rule_tenant_product", "tenant_id", "product_code"),
    )


# ---------------------------------------------------------------------------
# Commerce — append-only ledgers / event logs
# ---------------------------------------------------------------------------


class InvoiceEventORM(Base):
    """Append-only invoice ledger entry (projects to ``invoice_events``).

    Financial audit trail; lives in Postgres with the invoice so a state
    transition and its event commit together. ``(invoice_id, sequence_number)``
    is unique so a replayed event can never double-insert.
    """

    __tablename__ = "invoice_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[Dict[str, Any]] = mapped_column(_JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("invoice_id", "sequence_number", name="uq_invoice_event_seq"),
        Index("ix_invoice_event_invoice", "tenant_id", "invoice_id"),
    )


class AccountEventORM(Base):
    """Append-only account audit entry (projects to ``account_events``)."""

    __tablename__ = "account_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[Dict[str, Any]] = mapped_column(_JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "sequence_number", name="uq_account_event_seq"),
        Index("ix_account_event_account", "tenant_id", "account_id"),
    )


class DunningEventORM(Base):
    """Dunning (collections) event (projects to ``dunning_events``)."""

    __tablename__ = "dunning_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    threshold_days: Mapped[Optional[int]] = mapped_column(Integer)
    template_key: Mapped[Optional[str]] = mapped_column(String(64))
    queued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_dunning_tenant_invoice", "tenant_id", "invoice_id"),
        Index("ix_dunning_tenant_account", "tenant_id", "account_id"),
    )


# ---------------------------------------------------------------------------
# Commerce — AR aging snapshots (derived, but financial)
# ---------------------------------------------------------------------------


class ArAgingSnapshotORM(Base):
    """Daily AR aging snapshot (projects to ``ar_aging_snapshots``)."""

    __tablename__ = "ar_aging_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_date: Mapped[Optional[date]] = mapped_column(Date)
    total_open_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    bucket_0_30_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    bucket_31_60_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    bucket_61_90_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    bucket_90_plus_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    account_count_with_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("ix_ar_aging_tenant_date", "tenant_id", "snapshot_date"),
    )


# ---------------------------------------------------------------------------
# Compliance config (reference data: tax, contracts, sell-side pricing rules)
# ---------------------------------------------------------------------------
#
# These are config / reference tables that pricing and tax computation depend
# on. They use a hybrid shape: typed identity / tenant / status / date columns
# for indexing and constraints, plus a ``document`` JSON column holding the
# full ES document. The projector returns ``document`` verbatim, so the ES
# projection is byte-identical and robust to the nested/variable structures
# these records carry (tier_thresholds, product_codes arrays, etc.).


class _ComplianceConfigBase:
    """Shared columns for hybrid compliance-config tables."""

    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[Optional[str]] = mapped_column(String(32))
    document: Mapped[Dict[str, Any]] = mapped_column(_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class TaxJurisdictionORM(_ComplianceConfigBase, Base):
    """Authoritative tax jurisdiction rate (projects to ``tax_jurisdictions``)."""

    __tablename__ = "tax_jurisdictions"

    jurisdiction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fips_code: Mapped[Optional[str]] = mapped_column(String(16))
    tax_type: Mapped[Optional[str]] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_tax_juris_tenant_fips", "tenant_id", "fips_code"),
        Index("ix_tax_juris_tenant_type", "tenant_id", "tax_type"),
    )


class TaxExemptionORM(_ComplianceConfigBase, Base):
    """Authoritative customer tax exemption (projects to ``tax_exemptions``)."""

    __tablename__ = "tax_exemptions"

    exemption_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64))
    certificate_number: Mapped[Optional[str]] = mapped_column(String(128))

    __table_args__ = (
        Index("ix_tax_exempt_tenant_customer", "tenant_id", "customer_id"),
        Index("ix_tax_exempt_tenant_cert", "tenant_id", "certificate_number"),
    )


class PriceProtectionContractORM(_ComplianceConfigBase, Base):
    """Authoritative price-protection contract (projects to ``price_protection_contracts``).

    Carries the optimistic-concurrency ``version`` so the gallons-decrement
    path can compare-and-set in Postgres rather than racing on ES.
    """

    __tablename__ = "price_protection_contracts"

    contract_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64))
    product_code: Mapped[Optional[str]] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    __table_args__ = (
        Index("ix_ppc_tenant_customer", "tenant_id", "customer_id"),
        Index("ix_ppc_tenant_status", "tenant_id", "status"),
    )


class CompliancePricingRuleORM(_ComplianceConfigBase, Base):
    """Authoritative sell-side pricing rule (projects to the compliance
    ``pricing_rules`` index — distinct from the commerce ``pricing_rules_current``).
    """

    __tablename__ = "compliance_pricing_rules"

    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64))
    product_code: Mapped[Optional[str]] = mapped_column(String(64))
    strategy: Mapped[Optional[str]] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_cpr_tenant_product", "tenant_id", "product_code"),
        Index("ix_cpr_tenant_customer", "tenant_id", "customer_id"),
    )


class SupplierContractORM(_ComplianceConfigBase, Base):
    """Authoritative supplier contract (projects to ``supplier_contracts``)."""

    __tablename__ = "supplier_contracts"

    contract_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    supplier_name: Mapped[Optional[str]] = mapped_column(String(255))
    product_code: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_supplier_contract_tenant_status", "tenant_id", "status"),
        Index("ix_supplier_contract_tenant_supplier", "tenant_id", "supplier_name"),
    )


# ---------------------------------------------------------------------------
# Orders / jobs current-state (hybrid document tables)
# ---------------------------------------------------------------------------
#
# The mutable current-state row of each order/job. Status transitions need to
# be atomic, so the authoritative row lives in Postgres; the matching *_events
# streams stay in Elasticsearch. These carry rich, strict schemas (geo_point,
# nested intake_metadata), so they use the same hybrid shape as compliance
# config: typed identity / tenant / status columns + a verbatim ``document``
# JSON column the projector returns unchanged.


class FuelOrderCurrentORM(_ComplianceConfigBase, Base):
    """Authoritative fuel order current-state (projects to ``fuel_orders_current``)."""

    __tablename__ = "fuel_orders_current"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64))
    assigned_driver_id: Mapped[Optional[str]] = mapped_column(String(64))
    # Optional fleet asset/truck reference (cross-module-entity-linkage Req 2.1).
    # Nullable; indexed per-tenant for "orders on this truck" reads.
    assigned_asset_id: Mapped[Optional[str]] = mapped_column(String(64))
    # Stale-event guard: the last applied event timestamp (ISO string), used to
    # reject out-of-order upserts the way the ES scripted upsert does.
    last_event_timestamp: Mapped[Optional[str]] = mapped_column(String(40))

    __table_args__ = (
        Index("ix_fuel_order_tenant_status", "tenant_id", "status"),
        Index("ix_fuel_order_tenant_customer", "tenant_id", "customer_id"),
        Index("ix_fuel_order_tenant_driver", "tenant_id", "assigned_driver_id"),
        Index("ix_fuel_order_tenant_asset", "tenant_id", "assigned_asset_id"),
    )


class JobCurrentORM(_ComplianceConfigBase, Base):
    """Authoritative scheduling job current-state (projects to ``jobs_current``)."""

    __tablename__ = "jobs_current"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[Optional[str]] = mapped_column(String(64))
    last_event_timestamp: Mapped[Optional[str]] = mapped_column(String(40))

    __table_args__ = (
        Index("ix_job_tenant_status", "tenant_id", "status"),
        Index("ix_job_tenant_asset", "tenant_id", "asset_id"),
    )


# ``ShipmentCurrentORM`` (table ``shipments_current``) stood here. It was the
# authoritative current-state row for the pre-pivot Nigerian last-mile model,
# written only by the ``LegacyDualWriter`` mirror. The mirror is retired, the
# ``POST /webhooks/dinee`` route that originated the data is deleted, and the
# table is dropped by revision ``0007_drop_shipments_current``. There was never
# a ``RiderCurrentORM``: ``riders_current`` only ever existed as an
# Elasticsearch index, which is why only one table needed dropping.


class TenantJobPolicyORM(_ComplianceConfigBase, Base):
    """Authoritative per-tenant job policy (projects to ``tenant_job_policies``)."""

    __tablename__ = "tenant_job_policies"

    policy_id: Mapped[str] = mapped_column(String(128), primary_key=True)

    __table_args__ = (
        Index("ix_tenant_job_policy_tenant", "tenant_id"),
    )


# ---------------------------------------------------------------------------
# Master data (hybrid document tables)
# ---------------------------------------------------------------------------
#
# Low-volume reference / master entities. Like compliance config + current
# state, these use the hybrid shape (typed identity / tenant / status columns
# + verbatim ``document`` JSON the projector returns unchanged). They are the
# FK targets the rest of the model graph points at, so they belong in the
# relational store even though they are not high-churn.


class DriverMasterORM(_ComplianceConfigBase, Base):
    """Authoritative driver qualification record (projects to ``drivers``).

    Distinct from the order-intake ``drivers_current`` index — this is the
    compliance CDL/medical qualification file (a legal record).
    """

    __tablename__ = "drivers"

    driver_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cdl_number: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_driver_tenant_status", "tenant_id", "status"),
        Index("ix_driver_tenant_cdl", "tenant_id", "cdl_number"),
    )


class DepotORM(_ComplianceConfigBase, Base):
    """Authoritative depot (projects to ``depots``)."""

    __tablename__ = "depots"

    depot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    is_default: Mapped[Optional[bool]] = mapped_column(Boolean)

    __table_args__ = (
        Index("ix_depot_tenant", "tenant_id"),
        Index("ix_depot_tenant_default", "tenant_id", "is_default"),
    )


class TerminalORM(_ComplianceConfigBase, Base):
    """Authoritative terminal (projects to ``terminals``)."""

    __tablename__ = "terminals"

    terminal_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (
        Index("ix_terminal_tenant_status", "tenant_id", "status"),
    )


class AssetCertificationORM(_ComplianceConfigBase, Base):
    """Authoritative asset certification (projects to ``asset_certifications``)."""

    __tablename__ = "asset_certifications"

    cert_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_asset_cert_tenant_asset", "tenant_id", "asset_id"),
        Index("ix_asset_cert_tenant_status", "tenant_id", "status"),
    )


class IntakeChannelORM(_ComplianceConfigBase, Base):
    """Authoritative intake channel config (projects to ``intake_channels``)."""

    __tablename__ = "intake_channels"

    channel_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (
        Index("ix_intake_channel_tenant", "tenant_id"),
    )


class TruckORM(_ComplianceConfigBase, Base):
    """Authoritative truck/asset (projects to legacy ``trucks`` index).

    The legacy ``trucks`` index is not strictly tenant-scoped in every write
    path; ``tenant_id`` may be absent, so it is nullable here.
    """

    __tablename__ = "trucks"

    truck_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (
        Index("ix_truck_tenant", "tenant_id"),
    )


class LocationORM(_ComplianceConfigBase, Base):
    """Authoritative location (projects to legacy ``locations`` index)."""

    __tablename__ = "locations"

    location_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (
        Index("ix_location_tenant", "tenant_id"),
    )


# ---------------------------------------------------------------------------
# Fuel assets: the three indices Elasticsearch was the ONLY home for
# ---------------------------------------------------------------------------
#
# ``customer_tanks``, ``truck_compartments`` and ``fuel_stations`` were listed in
# the rebuild tool's ``ES_ONLY_INDICES`` registry (deleted with the cluster in
# Phase 6): authoritative state with no Postgres table, no projector and no
# rebuild spec, so recreating the
# Elasticsearch cluster destroyed them permanently. They are not seed data —
# live code paths write them:
#
#   * ``customer_tanks.k_factor`` is written back by ``KFactorCalibrationService``
#     after each calibration, and level/reading fields by the Veeder-Root ATG
#     connector.
#   * ``truck_compartments.last_loaded_product`` is written by
#     ``CompartmentLoadingAgent._persist_loading_plan`` and is what the
#     cross-contamination guard reads before assigning a product to a
#     compartment. Losing it does not merely lose data: it silently removes the
#     evidence a product-compatibility block depends on.
#   * ``fuel_stations`` holds tank inventory for the legacy retail path, also
#     updated by the ATG connector.
#
# Shape follows the established hybrid pattern — typed identity / tenant /
# filter columns for indexing, plus the full ES document verbatim — so the read
# path can return byte-identical documents and no caller changes. The filter
# columns are not guesswork: they are the fields the codebase actually issues
# ``term`` clauses on (status 91, customer_id 21, truck_id 19, station_id 13,
# fuel_type 8, customer_tank_id 8, zip_code 3).
#
# Unlike the older hybrid tables these use ``_JSONB``, because the Elasticsearch
# query adapter that replaces ``search_documents`` needs GIN indexing and the
# jsonb operators. A ``json`` column would force a sequential scan per query.


class _FuelAssetBase:
    """Shared columns for the hybrid fuel-asset tables."""

    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    document: Mapped[Dict[str, Any]] = mapped_column(_JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class CustomerTankORM(_FuelAssetBase, Base):
    """Authoritative customer tank (projects to ``customer_tanks``).

    Keyed on ``customer_tank_id``, which is what
    ``CustomerTankRepository.upsert`` passes to ``index_document``. The live
    Elasticsearch documents are keyed by ``customer_id`` instead — a seeder bug
    (``_resolve_json_doc_id`` preferred the foreign key) that was latent only
    because no fixture gave one customer two tanks. Keying correctly here fixes
    it rather than carrying it into Postgres, and the primary key makes the
    collision impossible to reintroduce.
    """

    __tablename__ = "customer_tanks"

    customer_tank_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[Optional[str]] = mapped_column(String(32))
    fuel_type: Mapped[Optional[str]] = mapped_column(String(32))
    customer_type: Mapped[Optional[str]] = mapped_column(String(32))
    zip_code: Mapped[Optional[str]] = mapped_column(String(16))
    external_tank_id: Mapped[Optional[str]] = mapped_column(String(128))
    source_system: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_customer_tank_tenant", "tenant_id"),
        Index("ix_customer_tank_tenant_customer", "tenant_id", "customer_id"),
        Index("ix_customer_tank_tenant_status", "tenant_id", "status"),
        Index("ix_customer_tank_tenant_zip", "tenant_id", "zip_code"),
        # The ATG connector resolves a tank by the vendor's own id.
        Index("ix_customer_tank_tenant_external", "tenant_id", "external_tank_id"),
    )


class TruckCompartmentORM(_FuelAssetBase, Base):
    """Authoritative truck compartment (projects to ``truck_compartments``).

    The Elasticsearch ``_id`` is ``f"{truck_id}_{compartment_id}"`` (e.g.
    ``TNK-002_C1``) and the application looks compartments up by that id rather
    than by query, so it is preserved verbatim as ``compartment_key`` instead of
    being recomputed on read.

    ``last_loaded_product`` is promoted to a column despite being queried rarely:
    it is the input to the cross-contamination guard, and a column makes it
    visible to a plain SQL audit rather than buried in a JSON blob.
    """

    __tablename__ = "truck_compartments"

    compartment_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    truck_id: Mapped[Optional[str]] = mapped_column(String(64))
    compartment_id: Mapped[Optional[str]] = mapped_column(String(32))
    state: Mapped[Optional[str]] = mapped_column(String(32))
    last_loaded_product: Mapped[Optional[str]] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_truck_compartment_tenant", "tenant_id"),
        Index("ix_truck_compartment_tenant_truck", "tenant_id", "truck_id"),
        Index("ix_truck_compartment_tenant_state", "tenant_id", "state"),
    )


class FuelStationORM(_FuelAssetBase, Base):
    """Authoritative fuel station (projects to ``fuel_stations``).

    Keyed on ``station_key``, the verbatim Elasticsearch ``_id``, NOT on
    ``station_id`` — because the index carries two id conventions at once:

      * ``FuelService.create_station`` writes ``f"{station_id}::{fuel_type}"``
        (``_make_doc_id``), so one station with two products is two documents;
      * every seeded document, and the Veeder-Root ATG connector's
        ``_apply_to_fuel_station`` update, uses the bare ``station_id``.

    Those two disagree in the live cluster today (all 14 documents are bare ids,
    so an ATG reading for an API-created station updates nothing). That is a
    pre-existing bug in the Elasticsearch write path and is not fixed here.
    What matters for this table is that ``station_id`` cannot be the primary key:
    a second product for the same station would collide and one document would
    silently vanish. Preserving the ``_id`` keeps both conventions round-tripping
    byte-identically, and ``station_id`` stays an indexed non-unique column so
    "every document for this station" is still one index scan.
    """

    __tablename__ = "fuel_stations"

    station_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    station_id: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[Optional[str]] = mapped_column(String(32))
    fuel_type: Mapped[Optional[str]] = mapped_column(String(32))
    fuel_grade: Mapped[Optional[str]] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_fuel_station_tenant", "tenant_id"),
        Index("ix_fuel_station_tenant_station", "tenant_id", "station_id"),
        Index("ix_fuel_station_tenant_status", "tenant_id", "status"),
        Index("ix_fuel_station_tenant_fuel_type", "tenant_id", "fuel_type"),
    )


# ---------------------------------------------------------------------------
# Generic document store: the Postgres replacement for the Elasticsearch cluster
# ---------------------------------------------------------------------------
#
# Phase 2 of the Elasticsearch → Postgres migration. Every index that is NOT
# already a hybrid/relational aggregate lands here, one row per document, keyed
# exactly as Elasticsearch keyed it. ``persistence.document_store`` serves the
# ``ElasticsearchService`` async surface (``index_document``, ``search_documents``,
# ``get_document`` …) from this table, so the 684 call sites do not change.
#
# One generic table rather than ~75 per-index tables, because:
#
#   * the whole cluster is 7,623 documents / 6.1 MB, and the largest single index
#     holds 988 — so per-table partitioning buys nothing measurable;
#   * a per-index table would need a schema decision and a migration for each of
#     the ~75, and the point of this phase is that call sites keep working
#     unchanged while their storage moves;
#   * documents in these indices have no agreed schema. Several are written by
#     more than one producer with different field sets, which is exactly what a
#     ``jsonb`` column is for.
#
# ``document`` is ``jsonb``, not ``json``: the query translator needs the
# containment operator (``@>``) and key-existence (``?``) to be index-backed, and
# it needs jsonb's total ordering for ``sort`` — jsonb orders numbers numerically
# and strings lexicographically, so one expression sorts both correctly, where
# ``->>`` would compare every number as text and put "10" before "9".
#
# ``tenant_id`` is lifted out of the document into a typed column because
# essentially every read is tenant-scoped (813 ``term`` clauses across the
# codebase, ``tenant_id`` the most common field), and a NULL-tolerant column with
# a composite index answers that far faster than a jsonb extraction. It is
# nullable: the legacy ``trucks`` / ``locations`` indices were created with
# dynamic mappings and some documents genuinely carry no tenant.
#
# The GIN index on ``document`` is created by the migration and deliberately NOT
# declared here: ``postgresql_using="gin"`` would break ``Base.metadata.create_all``
# against the SQLite database the test suite uses.


class EsDocumentORM(Base):
    """One Elasticsearch document, stored in Postgres.

    Primary key is ``(index_name, doc_id)`` — the same pair Elasticsearch uses,
    so a document keyed ``TNK-002_C1`` in ``truck_compartments`` is keyed
    identically here and every existing reader finds it.
    """

    __tablename__ = "es_documents"

    index_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(128))
    document: Mapped[Dict[str, Any]] = mapped_column(_JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_es_documents_index_tenant", "index_name", "tenant_id"),
        # Supports the ``sort: created_at desc`` default that ``get_all_documents``
        # and most list endpoints use.
        Index("ix_es_documents_index_updated", "index_name", "updated_at"),
    )


# ---------------------------------------------------------------------------
# Margin feed (cost / margin / COGS)
# ---------------------------------------------------------------------------
#
# Seven typed tables, kept out of ``es_documents`` on purpose: margin data is
# tenant-admin only (margin-feed D1/D3), and nothing that reads the document
# store (search tools, rebuild, outbox projections) can reach these rows. The
# only module that reads or writes them is
# ``commerce/services/margin_repository.py``.
#
# Units are in the column suffix: ``_cents`` (1/100 USD), ``_micros`` (1/10^6
# USD per gallon), ``_milli`` (1/1000 gallon, cost entries) and ``_ugal``
# (1/10^6 gallon, margin records). All integers, so SQLite and Postgres agree.
#
# Partial indexes declare both ``postgresql_where`` and ``sqlite_where`` so the
# SQLite test schema built by ``create_all`` enforces the same uniqueness. The
# migration ``0011_margin_feed`` declares them for Postgres.

_MCE_KIND_FIELDS_CHECK = (
    "(kind <> 'purchase' OR (gallons_milli IS NOT NULL AND terminal_id IS NOT NULL "
    "AND effective_to IS NULL)) "
    "AND (kind = 'purchase' OR (gallons_milli IS NULL AND bol_id IS NULL)) "
    "AND ((kind = 'adder') = (adder_type IS NOT NULL))"
)

# Missing cost is never stored as 0 (AC-8): every cost-derived column is NULL
# exactly when method = 'none'. adders_micros is excluded (reported for none).
_MR_COST_NULL_IFF_NONE_CHECK = (
    "(method = 'none') = (landed_cost_micros IS NULL) "
    "AND (method = 'none') = (product_cost_micros IS NULL) "
    "AND (method = 'none') = (cost_cents IS NULL) "
    "AND (method = 'none') = (margin_cents IS NULL) "
    "AND (method = 'none') = (margin_per_gallon_micros IS NULL) "
    "AND (method <> 'none' OR margin_bp IS NULL)"
)

_ACTIVE = text("status = 'active'")
_ACTIVE_WITH_BOL = text("status = 'active' AND bol_id IS NOT NULL")
_ALERT_PENDING = text("alert_state = 'pending'")
_RUN_RUNNING = text("status = 'running'")
_DIGEST_PENDING = text("digest_state = 'pending'")


class MarginCostEntryORM(Base):
    """Admin cost input: a purchase lot, a cost override or an adder (FR1).

    Immutable: edits supersede and deletes void, so the only mutations are
    status transitions.
    """

    __tablename__ = "margin_cost_entries"

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    product_code: Mapped[str] = mapped_column(String(64), nullable=False)
    terminal_id: Mapped[Optional[str]] = mapped_column(String(128))
    supplier_name: Mapped[Optional[str]] = mapped_column(String(128))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    unit_cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gallons_milli: Mapped[Optional[int]] = mapped_column(BigInteger)
    adder_type: Mapped[Optional[str]] = mapped_column(String(16))
    bol_id: Mapped[Optional[str]] = mapped_column(String(128))
    reference: Mapped[Optional[str]] = mapped_column(String(128))
    notes: Mapped[Optional[str]] = mapped_column(String(500))
    natural_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    supersedes_id: Mapped[Optional[str]] = mapped_column(String(64))
    superseded_by_id: Mapped[Optional[str]] = mapped_column(String(64))
    status_reason: Mapped[Optional[str]] = mapped_column(String(500))
    status_changed_by: Mapped[Optional[str]] = mapped_column(String(255))
    status_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    import_batch_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(_MCE_KIND_FIELDS_CHECK, name="ck_mce_kind_fields"),
        Index(
            "uq_mce_active_natural_key",
            "tenant_id",
            "natural_key",
            unique=True,
            postgresql_where=_ACTIVE,
            sqlite_where=_ACTIVE,
        ),
        Index(
            "uq_mce_active_bol",
            "tenant_id",
            "bol_id",
            unique=True,
            postgresql_where=_ACTIVE_WITH_BOL,
            sqlite_where=_ACTIVE_WITH_BOL,
        ),
        Index(
            "ix_mce_lookup", "tenant_id", "kind", "product_code", "terminal_id", "effective_at"
        ),
    )


class MarginSettingsORM(Base):
    """Per-tenant margin settings plus the activation watermark (D6, D7, D14).

    A tenant with no row uses the defaults. ``feed_activated_at`` is set once by
    ``MarginRepository.ensure_activated`` and never cleared.
    """

    __tablename__ = "margin_settings"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    wac_window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    rack_staleness_days: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    floor_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=100_000)
    product_floors: Mapped[Dict[str, Any]] = mapped_column(
        _JSONB, nullable=False, default=dict
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="America/Chicago"
    )
    feed_activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[Optional[str]] = mapped_column(String(255))
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class MarginRecordORM(Base):
    """One versioned margin record per (stage, source key) (FR3).

    ``alert_state`` is the RevenueGuard work queue: set to ``pending`` in the
    same insert as a flagged live delivery/invoice record.
    """

    __tablename__ = "margin_records"

    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    source_key: Mapped[str] = mapped_column(String(256), nullable=False)
    order_id: Mapped[Optional[str]] = mapped_column(String(128))
    invoice_id: Mapped[Optional[str]] = mapped_column(String(128))
    line_index: Mapped[Optional[int]] = mapped_column(Integer)
    line_id: Mapped[Optional[str]] = mapped_column(String(128))
    customer_id: Mapped[Optional[str]] = mapped_column(String(128))
    account_id: Mapped[Optional[str]] = mapped_column(String(128))
    product_code: Mapped[str] = mapped_column(String(64), nullable=False)
    terminal_id: Mapped[Optional[str]] = mapped_column(String(128))
    gallons_ugal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_price_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revenue_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    product_cost_micros: Mapped[Optional[int]] = mapped_column(BigInteger)
    adders_micros: Mapped[Optional[int]] = mapped_column(BigInteger)
    landed_cost_micros: Mapped[Optional[int]] = mapped_column(BigInteger)
    cost_cents: Mapped[Optional[int]] = mapped_column(BigInteger)
    margin_cents: Mapped[Optional[int]] = mapped_column(BigInteger)
    margin_per_gallon_micros: Mapped[Optional[int]] = mapped_column(BigInteger)
    margin_bp: Mapped[Optional[int]] = mapped_column(BigInteger)
    no_cost_reason: Mapped[Optional[str]] = mapped_column(String(40))
    flag_missing_cost: Mapped[bool] = mapped_column(Boolean, nullable=False)
    flag_negative_margin: Mapped[bool] = mapped_column(Boolean, nullable=False)
    flag_below_floor: Mapped[bool] = mapped_column(Boolean, nullable=False)
    flag_terminal_unattributed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    floor_micros_used: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_snapshot: Mapped[Dict[str, Any]] = mapped_column(_JSONB, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    frozen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    recompute_run_id: Mapped[Optional[str]] = mapped_column(String(64))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    alert_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none"
    )

    __table_args__ = (
        CheckConstraint(_MR_COST_NULL_IFF_NONE_CHECK, name="ck_mr_cost_null_iff_none"),
        CheckConstraint(
            "alert_state IN ('none', 'pending', 'done', 'expired')",
            name="ck_mr_alert_state",
        ),
        CheckConstraint("flag_missing_cost = (method = 'none')", name="ck_mr_missing_flag"),
        UniqueConstraint("tenant_id", "stage", "source_key", "version", name="uq_mr_version"),
        Index(
            "uq_mr_active",
            "tenant_id",
            "stage",
            "source_key",
            unique=True,
            postgresql_where=_ACTIVE,
            sqlite_where=_ACTIVE,
        ),
        Index("ix_mr_tenant_asof", "tenant_id", "as_of", "record_id"),
        Index(
            "ix_mr_tenant_cust_prod", "tenant_id", "customer_id", "product_code", "stage", "as_of"
        ),
        Index("ix_mr_tenant_order", "tenant_id", "order_id"),
        Index(
            "ix_mr_alert_pending",
            "tenant_id",
            "computed_at",
            postgresql_where=_ALERT_PENDING,
            sqlite_where=_ALERT_PENDING,
        ),
    )


class MarginAlertORM(Base):
    """Admin-only margin alert or leakage proposal (FR5.3, FR5.4)."""

    __tablename__ = "margin_alerts"

    alert_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False)
    record_id: Mapped[Optional[str]] = mapped_column(String(64))
    order_id: Mapped[Optional[str]] = mapped_column(String(128))
    run_id: Mapped[Optional[str]] = mapped_column(String(64))
    proposal_id: Mapped[Optional[str]] = mapped_column(String(128))
    customer_id: Mapped[Optional[str]] = mapped_column(String(128))
    product_code: Mapped[Optional[str]] = mapped_column(String(64))
    details: Mapped[Dict[str, Any]] = mapped_column(_JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    resolved_by: Mapped[Optional[str]] = mapped_column(String(255))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[Optional[str]] = mapped_column(String(500))

    __table_args__ = (
        UniqueConstraint("tenant_id", "alert_type", "dedupe_key", name="uq_malert_dedupe"),
        Index("ix_malert_tenant_status", "tenant_id", "status", "created_at"),
    )


class MarginRecomputeRunORM(Base):
    """One admin recompute run (FR3.5). At most one ``running`` per tenant."""

    __tablename__ = "margin_recompute_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    stages: Mapped[List[str]] = mapped_column(_JSONB, nullable=False)
    only_missing: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    counts: Mapped[Dict[str, Any]] = mapped_column(_JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    digest_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none"
    )

    __table_args__ = (
        Index(
            "uq_mrun_running",
            "tenant_id",
            unique=True,
            postgresql_where=_RUN_RUNNING,
            sqlite_where=_RUN_RUNNING,
        ),
        Index(
            "ix_mrun_digest_pending",
            "tenant_id",
            postgresql_where=_DIGEST_PENDING,
            sqlite_where=_DIGEST_PENDING,
        ),
    )


class MarginWeeklyReportORM(Base):
    """Per-tenant weekly margin report (FR5.7). The PK makes reruns no-ops."""

    __tablename__ = "margin_weekly_reports"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    iso_week: Mapped[str] = mapped_column(String(8), primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revenue_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    margin_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revenue_cents_missing_cost: Mapped[int] = mapped_column(BigInteger, nullable=False)
    records_total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gallons_ugal_total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    flag_counts: Mapped[Dict[str, Any]] = mapped_column(_JSONB, nullable=False, default=dict)
    missing_cost_share_bp: Mapped[Optional[int]] = mapped_column(BigInteger)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MarginSkippedSourceORM(Base):
    """Durable record of a phase-1 ``invalid_inputs`` skip (no values stored)."""

    __tablename__ = "margin_skipped_sources"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    stage: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    order_id: Mapped[Optional[str]] = mapped_column(String(128))
    invoice_id: Mapped[Optional[str]] = mapped_column(String(128))
    line_index: Mapped[Optional[int]] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    error_type: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False)
