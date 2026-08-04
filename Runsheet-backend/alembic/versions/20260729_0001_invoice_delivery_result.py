"""Persist POD delivery facts on authoritative invoice rows.

Revision ID: 0004_invoice_delivery_result
Revises: 0003_order_assigned_asset
Create Date: 2026-07-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_invoice_delivery_result"
down_revision: Union[str, None] = "0003_order_assigned_asset"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("pod_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "invoices",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "invoices",
        sa.Column("delivery_result", sa.JSON(), nullable=True),
    )
    op.create_index("ix_invoice_pod", "invoices", ["pod_id"])


def downgrade() -> None:
    op.drop_index("ix_invoice_pod", table_name="invoices")
    op.drop_column("invoices", "delivery_result")
    op.drop_column("invoices", "delivered_at")
    op.drop_column("invoices", "pod_id")
