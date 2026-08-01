"""Strip the retired ``ops_manager`` role from every ``auth_users`` row.

Revision ID: 0006_retire_ops_manager_role
Revises: 0005_invoice_unit_price_micros
Create Date: 2026-07-30

``ops_manager`` was declared in ``auth.supertokens_init.CANONICAL_ROLES`` from
the start of the SuperTokens migration but never gated anything: no
``require_role`` call site named it, no inline role check consulted it, and the
frontend never referenced it. It has been removed from ``CANONICAL_ROLES`` and
``CUSTOMER_ASSIGNABLE_ROLES``.

Nothing validates ``auth_users.roles`` against ``CANONICAL_ROLES``, so a
residual value is inert rather than dangerous — ``require_role`` is exact-match
and no site requires it. This revision removes it anyway, because a role name
sitting in the provisioning source of truth reads as a real permission tier to
whoever next writes an ``auth_users`` row by hand.

The dev-only seed in ``0002_auth_users`` granted ``admin@runsheet.com``
``ARRAY['admin','ops_manager']``. That revision is left alone: it has already
been applied, and editing an applied revision makes the recorded history
disagree with what actually ran. This is the forward fix instead.

``array_remove`` is exact-match on the element, so it cannot touch a role that
merely contains ``ops_manager`` as a substring. The ``WHERE`` clause keeps the
statement a no-op on databases that never held the value.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_retire_ops_manager_role"
down_revision: Union[str, None] = "0005_invoice_unit_price_micros"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RETIRED_ROLE = "ops_manager"


def upgrade() -> None:
    op.execute(
        "UPDATE auth_users "
        f"SET roles = array_remove(roles, '{_RETIRED_ROLE}'), updated_at = now() "
        f"WHERE '{_RETIRED_ROLE}' = ANY(roles)"
    )


def downgrade() -> None:
    """Deliberate no-op.

    A downgrade that re-added ``ops_manager`` would have to guess *which* rows
    held it — the information is gone by then — and would reintroduce a role the
    codebase no longer declares, so the restored value would grant nothing while
    once again looking like a permission tier.

    Rolling back past this revision therefore leaves role arrays as the upgrade
    left them. That is safe in the direction that matters: no row gains a role
    it did not have, and no row loses one it still needs. If a specific account
    genuinely needs a role restored, it is a deliberate ``UPDATE`` against that
    row, not a schema rollback.
    """
