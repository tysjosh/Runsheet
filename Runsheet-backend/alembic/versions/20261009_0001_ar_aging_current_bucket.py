"""AR aging: ``ar_aging_snapshots.bucket_current_cents``.

Revision ID: 0013_ar_aging_current
Revises: 0012_customer_portal
Create Date: 2026-10-09

Analytics fix F12: AR aging now ages open invoices by days past ``due_date``
and adds a "Current" (not yet due) bucket. The other bucket columns keep their
names; ``bucket_0_30_cents`` now means 1-30 days past due.

The new column is nullable with no default on purpose: NULL marks a snapshot
written before this change, which was aged by ``issued_at`` and had no Current
bucket. The UI renders it as "—" rather than a misleading 0.

Single-head rule: parented on ``0012_customer_portal``. If another revision
lands on the same parent, re-parent this one and bump the id; never add a
merge revision.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0013_ar_aging_current"
down_revision: Union[str, None] = "0012_customer_portal"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ar_aging_snapshots",
        sa.Column("bucket_current_cents", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ar_aging_snapshots", "bucket_current_cents")
