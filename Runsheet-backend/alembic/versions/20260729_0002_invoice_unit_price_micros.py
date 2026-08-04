"""Preserve fractional-cent per-gallon prices on rules and invoice lines.

Revision ID: 0005_invoice_unit_price_micros
Revises: 0004_invoice_delivery_result
Create Date: 2026-07-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_invoice_unit_price_micros"
down_revision: Union[str, None] = "0004_invoice_delivery_result"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "invoice_line_items",
        sa.Column("unit_price_micros", sa.BigInteger(), nullable=True),
    )
    # Existing prices were exact to a whole cent. Populate their equivalent
    # micro-dollar value so projections and retry workers immediately use the
    # precise canonical field after deployment.
    op.execute(
        "UPDATE invoice_line_items "
        "SET unit_price_micros = unit_price_cents * 10000 "
        "WHERE unit_price_micros IS NULL"
    )
    op.add_column(
        "pricing_rules",
        sa.Column("unit_price_micros", sa.BigInteger(), nullable=True),
    )
    op.execute(
        "UPDATE pricing_rules "
        "SET unit_price_micros = unit_price_cents * 10000 "
        "WHERE unit_price_micros IS NULL"
    )


def downgrade() -> None:
    op.drop_column("pricing_rules", "unit_price_micros")
    op.drop_column("invoice_line_items", "unit_price_micros")
