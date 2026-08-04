"""auth_users provisioning source-of-truth for the SuperTokens migration.

Revision ID: 0002_auth_users
Revises: 0001_commerce_sot
Create Date: 2026-06-03

Creates the ``auth_users`` table — the PostgreSQL source of truth the
User_Provisioner reads to create/update SuperTokens users, assign UserRoles,
and set the ``tenant_id`` / ``has_pii_access`` session claims. ``email`` is a
CITEXT UNIQUE column so it doubles as the idempotency key for provisioning
(Req 9.4); ``roles`` holds only canonical role names — the set is owned by
``auth.supertokens_init.CANONICAL_ROLES`` — so the backend Role_Authorizer can
use exact matching (Req 4.4, 9.6); ``st_user_id`` is
backfilled on first successful provision and ``provision_error`` records a
per-row failure (Req 9.7).

In ``development`` only, a single demo row is seeded
(``admin@runsheet.com`` / ``demo-tenant`` / ``{admin, ops_manager}`` /
``has_pii_access=true``) so the local sign-in loop has a user to provision.
The seed is skipped in every non-development environment and is idempotent
(``ON CONFLICT (email) DO NOTHING``).

``ops_manager`` was later retired from ``CANONICAL_ROLES``. The seed below is
left exactly as it was applied — rewriting an applied revision would make the
migration history disagree with what already ran against a developer's
database. Revision ``0006_retire_ops_manager_role`` strips the value forward
instead.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_auth_users"
down_revision: Union[str, None] = "0001_commerce_sot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_development() -> bool:
    """True when running against the development environment (dev-only seed)."""
    from config.settings import Environment, get_settings

    return get_settings().environment == Environment.DEVELOPMENT


def upgrade() -> None:
    # CITEXT (case-insensitive email) requires the citext extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "auth_users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # email is the idempotency key for provisioning (Req 9.4); CITEXT makes
        # the UNIQUE constraint case-insensitive.
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        # maps to the session tenant_id claim (Req 9.6); sole source of scope.
        sa.Column("tenant_id", sa.Text(), nullable=False),
        # canonical roles only — exact-match by the Role_Authorizer (Req 4.4, 9.6).
        sa.Column(
            "roles",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "has_pii_access",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # present only for driver users (Req 7.3).
        sa.Column("driver_id", sa.Text(), nullable=True),
        # backfilled after a successful provision; its presence keeps re-runs idempotent.
        sa.Column("st_user_id", sa.Text(), nullable=True),
        sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=True),
        # per-user failure record so a batch can continue past one bad row (Req 9.7).
        sa.Column("provision_error", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("email", name="uq_auth_users_email"),
    )
    # Tenant-scoped lookups (consistent with the tenant_id-leading indexes
    # elsewhere in the schema).
    op.create_index("ix_auth_users_tenant", "auth_users", ["tenant_id"])

    # --- Demo seed (development only) --------------------------------------
    # Seeds the single demo user so the local sign-in / provisioning loop has a
    # source row. Idempotent and skipped in every non-development environment.
    if _is_development():
        op.execute(
            sa.text(
                """
                INSERT INTO auth_users (email, tenant_id, roles, has_pii_access)
                VALUES (
                    'admin@runsheet.com',
                    'demo-tenant',
                    ARRAY['admin', 'ops_manager']::text[],
                    TRUE
                )
                ON CONFLICT (email) DO NOTHING
                """
            )
        )


def downgrade() -> None:
    op.drop_index("ix_auth_users_tenant", table_name="auth_users")
    op.drop_table("auth_users")
    # The citext extension is intentionally left installed: other objects may
    # depend on it and dropping a shared extension on downgrade is unsafe.
