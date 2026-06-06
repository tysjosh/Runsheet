"""Add nullable ``assigned_asset_id`` reference + index to ``fuel_orders_current``.

Revision ID: 0003_order_assigned_asset
Revises: 0002_auth_users
Create Date: 2026-06-04

Cross-module-entity-linkage spec, task 2 (Req 2.1, 6.1, 6.3). Adds the optional
fleet asset/truck reference to the fuel order current-state table so an order
can record which asset is carrying it, alongside the existing
``assigned_driver_id``. The column is nullable so existing records remain valid
without backfill, and a tenant-scoped ``(tenant_id, assigned_asset_id)`` index
mirrors the existing driver index for "orders on this truck" reads.

This change is purely additive: no existing column is removed or repurposed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003_order_assigned_asset"
down_revision: Union[str, None] = "0002_auth_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "fuel_orders_current",
        sa.Column("assigned_asset_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_fuel_order_tenant_asset",
        "fuel_orders_current",
        ["tenant_id", "assigned_asset_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_fuel_order_tenant_asset", table_name="fuel_orders_current")
    op.drop_column("fuel_orders_current", "assigned_asset_id")
