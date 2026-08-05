"""Give the three Elasticsearch-only fuel-asset indices a Postgres home.

Revision ID: 0008_fuel_asset_tables
Revises: 0007_drop_shipments_current
Create Date: 2026-08-05

``customer_tanks``, ``truck_compartments`` and ``fuel_stations`` were the whole
contents of ``persistence.rebuild_from_postgres.ES_ONLY_INDICES``: authoritative
operational state with no Postgres table, no ORM model and no projector, so
recreating the Elasticsearch cluster destroyed them permanently. That is not a
hypothetical — it happened during an end-to-end test of the MVP pipeline, and
the A1 tank-forecasting and A3 compartment-loading stages then ran with no input
at all while the endpoint still reported ``status: "complete"``.

They are not seed data. ``KFactorCalibrationService`` writes calibrated
``k_factor`` values back into ``customer_tanks``; the Veeder-Root ATG connector
updates tank levels in ``customer_tanks`` and ``fuel_stations``; and
``CompartmentLoadingAgent._persist_loading_plan`` writes
``last_loaded_product`` into ``truck_compartments``, which is the history the
cross-contamination guard reads before allowing a product into a compartment.

Shape follows the established hybrid pattern: typed identity / tenant / filter
columns for indexing, plus the full Elasticsearch document verbatim, so reads can
return byte-identical documents and no call site changes.

Two deliberate departures from the older hybrid tables:

``jsonb``, not ``json``
    Every earlier hybrid table used the portable ``sa.JSON()``, which resolves to
    PostgreSQL ``json`` — verified with ``\\d fuel_orders_current``. A ``json``
    column cannot carry a GIN index and has no operator support, so every
    document-field predicate is a sequential scan plus a per-row parse. The
    Postgres query adapter that replaces ``search_documents`` needs both, and
    these tables are being created fresh, so there is no existing column to
    change type on.

``truck_compartments`` keeps the composite key verbatim
    Its Elasticsearch ``_id`` is ``f"{truck_id}_{compartment_id}"`` (e.g.
    ``TNK-002_C1``) and the application fetches compartments by that id rather
    than by query, so it is stored as ``compartment_key`` instead of being
    recomputed on read. 160 chars: 64 (truck) + 1 + 32 (compartment) with room
    to spare.

``fuel_stations`` likewise keeps its Elasticsearch ``_id`` as ``station_key``
    ``FuelService.create_station`` writes ``f"{station_id}::{fuel_type}"`` while
    every seeded document and the ATG connector's update path use the bare
    ``station_id``. Both conventions live in the same index, so ``station_id``
    cannot be the primary key without one product's document silently
    overwriting another's. It stays as an indexed, non-unique column. (The
    disagreement between the two write paths is a real pre-existing bug — an ATG
    reading for an API-created station updates nothing — and is deliberately not
    fixed by this revision.)

``customer_tanks`` is keyed on ``customer_tank_id``, which is what
``CustomerTankRepository.upsert`` passes to ``index_document``. The live
Elasticsearch documents are keyed by ``customer_id`` instead — a seeder bug
(``_resolve_json_doc_id`` preferred the foreign key) that stayed latent only
because no fixture gave one customer two tanks; the second would have silently
overwritten the first. The backfill remaps as it copies, and this primary key
makes the collision impossible to reintroduce.

``downgrade()`` drops all three. **That is data loss** once dual-write is live
and Elasticsearch has been retired, because at that point Postgres is the only
copy. Take a backup first (``python -m scripts.es_only_backup export --all``
covers the Elasticsearch side while it still exists).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008_fuel_asset_tables"
down_revision: Union[str, None] = "0007_drop_shipments_current"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())


def _timestamps() -> Sequence[sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "customer_tanks",
        sa.Column("customer_tank_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("fuel_type", sa.String(length=32), nullable=True),
        sa.Column("customer_type", sa.String(length=32), nullable=True),
        sa.Column("zip_code", sa.String(length=16), nullable=True),
        sa.Column("external_tank_id", sa.String(length=128), nullable=True),
        sa.Column("source_system", sa.String(length=64), nullable=True),
        sa.Column("document", _JSONB, nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("customer_tank_id"),
    )
    op.create_index("ix_customer_tank_tenant", "customer_tanks", ["tenant_id"])
    op.create_index(
        "ix_customer_tank_tenant_customer", "customer_tanks",
        ["tenant_id", "customer_id"],
    )
    op.create_index(
        "ix_customer_tank_tenant_status", "customer_tanks",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_customer_tank_tenant_zip", "customer_tanks", ["tenant_id", "zip_code"],
    )
    # The ATG connector resolves a tank by the vendor's own id.
    op.create_index(
        "ix_customer_tank_tenant_external", "customer_tanks",
        ["tenant_id", "external_tank_id"],
    )

    op.create_table(
        "truck_compartments",
        sa.Column("compartment_key", sa.String(length=160), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("truck_id", sa.String(length=64), nullable=True),
        sa.Column("compartment_id", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=True),
        sa.Column("last_loaded_product", sa.String(length=32), nullable=True),
        sa.Column("document", _JSONB, nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("compartment_key"),
    )
    op.create_index("ix_truck_compartment_tenant", "truck_compartments", ["tenant_id"])
    op.create_index(
        "ix_truck_compartment_tenant_truck", "truck_compartments",
        ["tenant_id", "truck_id"],
    )
    op.create_index(
        "ix_truck_compartment_tenant_state", "truck_compartments",
        ["tenant_id", "state"],
    )

    op.create_table(
        "fuel_stations",
        sa.Column("station_key", sa.String(length=160), nullable=False),
        sa.Column("station_id", sa.String(length=64), nullable=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("fuel_type", sa.String(length=32), nullable=True),
        sa.Column("fuel_grade", sa.String(length=32), nullable=True),
        sa.Column("document", _JSONB, nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("station_key"),
    )
    op.create_index("ix_fuel_station_tenant", "fuel_stations", ["tenant_id"])
    op.create_index(
        "ix_fuel_station_tenant_station", "fuel_stations",
        ["tenant_id", "station_id"],
    )
    op.create_index(
        "ix_fuel_station_tenant_status", "fuel_stations", ["tenant_id", "status"],
    )
    op.create_index(
        "ix_fuel_station_tenant_fuel_type", "fuel_stations",
        ["tenant_id", "fuel_type"],
    )


def downgrade() -> None:
    """Drop all three tables. See the module docstring: this loses data."""
    op.drop_table("fuel_stations", if_exists=True)
    op.drop_table("truck_compartments", if_exists=True)
    op.drop_table("customer_tanks", if_exists=True)
