"""Drop the retired ``shipments_current`` table.

Revision ID: 0007_drop_shipments_current
Revises: 0006_retire_ops_manager_role
Create Date: 2026-08-03

``shipments_current`` held the current-state row for the pre-pivot Nigerian
last-mile delivery model (Dinee shipments and riders). Nothing writes to it and
nothing reads it any more:

* ``POST /webhooks/dinee``, the route that originated shipment data, has been
  deleted along with its module.
* ``LegacyDualWriter``, the only remaining writer, mirrored every
  ``fuel_orders_current`` write here during the deprecation window its own
  docstring described as "deprecate in 1 minor release". It has been retired.
* The read API (``/api/ops/shipments*``, ``/api/ops/riders*``) sits behind
  ``legacy_ng_delivery_enabled``, which defaults to ``False``.
* No foreign key in any schema references this table, verified against a live
  database before writing this revision.

**Data loss is intentional and total.** Every row in this table is a projection
of a ``fuel_orders_current`` row that remains authoritative, so nothing unique
is destroyed — but the projection itself is not recoverable from this revision.
``downgrade()`` recreates the table and its index, empty. If a deployment needs
the rows back, they must be re-derived from ``fuel_orders_current``; there is
deliberately no data copy here, because re-deriving them would resurrect the
mirror this revision exists to remove.

There is no companion drop for ``riders_current``: that name only ever existed
as an Elasticsearch index, never as a PostgreSQL table.

Column set mirrors the former ``ShipmentCurrentORM`` (``_ComplianceConfigBase``
plus ``shipment_id`` and ``last_event_timestamp``) so a downgrade produces the
same shape the ORM would have emitted.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_drop_shipments_current"
down_revision: Union[str, None] = "0006_retire_ops_manager_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "shipments_current"
_INDEX = "ix_shipment_tenant_status"


def upgrade() -> None:
    """Drop the table and its index.

    ``if_exists`` keeps this a no-op on a database that never had the table —
    for example one built from a newer baseline, or a test database created by
    ``Base.metadata.create_all`` after the ORM class was removed.
    """
    op.drop_index(_INDEX, table_name=_TABLE, if_exists=True)
    op.drop_table(_TABLE, if_exists=True)


def downgrade() -> None:
    """Recreate the table and index, empty.

    Restores structure only. See the module docstring: the rows were a
    projection of ``fuel_orders_current`` and are not reconstructed here.
    """
    op.create_table(
        _TABLE,
        sa.Column("shipment_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=True),
        # ``sa.JSON`` not ``JSONB``: the ORM used the portable
        # ``JSON().with_variant(JSON(), "sqlite")`` alias, and the live column
        # was verified as PostgreSQL ``json``. Recreating it as ``jsonb`` would
        # silently change the type on downgrade.
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("last_event_timestamp", sa.String(length=40), nullable=True),
        sa.PrimaryKeyConstraint("shipment_id"),
    )
    op.create_index(_INDEX, _TABLE, ["tenant_id", "status"], unique=False)
