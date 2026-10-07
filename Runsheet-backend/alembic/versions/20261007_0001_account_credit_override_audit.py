"""Add nullable credit-override audit columns to ``accounts``.

Revision ID: 0010_acct_override_audit
Revises: 0009_es_documents
Create Date: 2026-10-07

OI-42. ``CreditService.apply_override`` recorded ``reason`` and
``authorized_by`` only in the ``override_applied`` event payload, so an account
GET under ``COMMERCE_READ_FROM_POSTGRES`` couldn't say why an account was on
override or who approved it. Both columns are nullable: existing rows stay
valid without backfill, and expiry/clear sets them back to NULL.

The revision id is kept under 32 characters, the width of Alembic's default
``alembic_version.version_num`` column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0010_acct_override_audit"
down_revision: Union[str, None] = "0009_es_documents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("credit_override_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("credit_override_authorized_by", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "credit_override_authorized_by")
    op.drop_column("accounts", "credit_override_reason")
