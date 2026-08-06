"""Add the generic document store that replaces the Elasticsearch cluster.

Revision ID: 0009_es_documents
Revises: 0008_fuel_asset_tables
Create Date: 2026-08-06

Phase 2 of the Elasticsearch → Postgres migration. ``persistence.document_store``
serves the ``ElasticsearchService`` async surface from this table, so the 684
Elasticsearch call sites keep working unchanged while their storage moves.

One generic table, not ~75 per-index tables. The whole cluster is 7,623 documents
/ 6.1 MB and the largest single index holds 988, so per-index partitioning buys
nothing measurable — and the documents in these indices have no agreed schema
(several are written by more than one producer with different field sets), which
is what ``jsonb`` is for.

``(index_name, doc_id)`` is the primary key, matching Elasticsearch exactly, so a
document keyed ``TNK-002_C1`` is keyed identically here.

Three indexes, each answering a measured access pattern rather than a guess:

``ix_es_documents_index_tenant``
    Essentially every read is tenant-scoped — ``tenant_id`` is the most common
    field across the 813 ``term`` clauses in the codebase. Lifted to a typed
    column so this is a btree lookup, not a jsonb extraction. Nullable, because
    the legacy dynamically-mapped ``trucks`` / ``locations`` documents genuinely
    carry no tenant.

``ix_es_documents_index_updated``
    ``get_all_documents`` and most list endpoints sort by ``created_at``/
    ``updated_at`` descending.

``ix_es_documents_document`` (GIN, ``jsonb_ops``)
    Backs the translated ``term`` / ``terms`` filters, which compile to the
    containment operator ``document @> '{"field": value}'``, and ``exists``,
    which compiles to key-existence ``document ? 'field'``. ``jsonb_ops`` rather
    than ``jsonb_path_ops`` specifically because the latter supports only ``@>``
    and would leave ``exists`` unindexed.

    Declared only here, never on the ORM model: ``postgresql_using="gin"`` would
    break ``Base.metadata.create_all`` against the SQLite database the test suite
    uses.

``downgrade()`` drops the table. Once Elasticsearch is retired that is the only
copy of every index without a relational or hybrid home, so this is data loss —
take a dump first.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009_es_documents"
down_revision: Union[str, None] = "0008_fuel_asset_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "es_documents"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("index_name", sa.String(length=128), nullable=False),
        sa.Column("doc_id", sa.String(length=512), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
        sa.Column(
            "document", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("index_name", "doc_id"),
    )
    op.create_index(
        "ix_es_documents_index_tenant", _TABLE, ["index_name", "tenant_id"]
    )
    op.create_index(
        "ix_es_documents_index_updated", _TABLE, ["index_name", "updated_at"]
    )
    op.create_index(
        "ix_es_documents_document",
        _TABLE,
        ["document"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    """Drop the document store. See the module docstring: this loses data."""
    op.drop_index("ix_es_documents_document", table_name=_TABLE, if_exists=True)
    op.drop_index("ix_es_documents_index_updated", table_name=_TABLE, if_exists=True)
    op.drop_index("ix_es_documents_index_tenant", table_name=_TABLE, if_exists=True)
    op.drop_table(_TABLE, if_exists=True)
