"""Customer portal: ``auth_users.customer_id``, portal grants, payment attempts.

Revision ID: 0012_customer_portal
Revises: 0011_margin_feed
Create Date: 2026-10-08

OI-06 (design §1.3, §1.4, §6.1, §9).

* ``auth_users.customer_id`` binds a ``customer`` identity to exactly one
  commerce customer. The CHECK ``ck_auth_users_customer_binding`` makes the
  role exclusive at the database: a row either holds no ``customer`` role and
  no binding, or holds exactly ``{customer}`` with a binding, no driver id and
  no PII flag. Every existing row satisfies the first branch because no row
  holds ``customer`` today. No FK to ``customers``: that table is not
  authoritative in every deployment, so the binding is checked through
  ``CustomerService`` instead.
* ``portal_user_grants`` keeps invite/revoke history and backs the per-customer
  user cap (``auth_users`` holds only the current binding).
* ``portal_payment_attempts`` records customer-initiated ACH attempts. No bank
  details are stored. ``uq_ppa_inflight`` is the DB backstop for "one attempt
  in flight per invoice".

Single-head rule: re-parented from ``0010_acct_override_audit`` onto
``0011_margin_feed`` when margin-feed landed first. If another revision lands
on the same parent, re-parent again and bump the id; never add a merge
revision.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012_customer_portal"
down_revision: Union[str, None] = "0011_margin_feed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CUSTOMER_BINDING_CHECK = (
    "(customer_id IS NULL AND NOT ('customer' = ANY(roles))) "
    "OR "
    "(customer_id IS NOT NULL AND roles = ARRAY['customer']::text[] "
    "AND driver_id IS NULL AND has_pii_access = false)"
)


def upgrade() -> None:
    # --- auth_users.customer_id + exclusivity invariant -------------------
    op.add_column(
        "auth_users",
        sa.Column("customer_id", sa.String(64), nullable=True),
    )
    op.create_check_constraint(
        "ck_auth_users_customer_binding", "auth_users", _CUSTOMER_BINDING_CHECK
    )
    op.create_index(
        "ix_auth_users_tenant_customer",
        "auth_users",
        ["tenant_id", "customer_id"],
        postgresql_where=sa.text("customer_id IS NOT NULL"),
    )

    # --- portal_user_grants -----------------------------------------------
    # CITEXT email: already installed by 0001 auth_users, kept idempotent.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.create_table(
        "portal_user_grants",
        sa.Column("grant_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')", name="ck_portal_grant_status"
        ),
    )
    op.create_index(
        "uq_portal_grant_active_email",
        "portal_user_grants",
        ["tenant_id", "email"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_portal_grant_customer",
        "portal_user_grants",
        ["tenant_id", "customer_id", "status"],
    )

    # --- portal_payment_attempts ------------------------------------------
    op.create_table(
        "portal_payment_attempts",
        sa.Column("payment_attempt_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("invoice_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("actor_user_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stripe_payment_intent_id", sa.String(128), nullable=True),
        sa.Column("payment_id", sa.String(64), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_cents > 0", name="ck_ppa_amount_positive"),
        sa.CheckConstraint(
            "status IN ('creating', 'created', 'pending', 'succeeded', "
            "'failed', 'canceled')",
            name="ck_ppa_status",
        ),
        sa.UniqueConstraint(
            "stripe_payment_intent_id", name="uq_ppa_stripe_payment_intent"
        ),
        sa.UniqueConstraint(
            "tenant_id", "actor_user_id", "idempotency_key", name="uq_ppa_idem"
        ),
    )
    op.create_index(
        "uq_ppa_inflight",
        "portal_payment_attempts",
        ["tenant_id", "invoice_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('creating', 'created', 'pending')"),
    )
    op.create_index(
        "ix_ppa_customer",
        "portal_payment_attempts",
        ["tenant_id", "customer_id", "invoice_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_ppa_customer", table_name="portal_payment_attempts")
    op.drop_index("uq_ppa_inflight", table_name="portal_payment_attempts")
    op.drop_table("portal_payment_attempts")

    op.drop_index("ix_portal_grant_customer", table_name="portal_user_grants")
    op.drop_index("uq_portal_grant_active_email", table_name="portal_user_grants")
    op.drop_table("portal_user_grants")

    op.drop_index("ix_auth_users_tenant_customer", table_name="auth_users")
    op.drop_constraint(
        "ck_auth_users_customer_binding", "auth_users", type_="check"
    )
    op.drop_column("auth_users", "customer_id")
    # citext stays installed (auth_users.email depends on it).
