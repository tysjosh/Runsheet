#!/usr/bin/env python3
"""
Provision the ``auth_users`` source-of-truth into SuperTokens.

This is the operator entry point for the User_Provisioner (task 2.3). It:

1. Initializes the SuperTokens SDK against the managed SaaS core
   (``auth.supertokens_init.init_supertokens``), reaching the core over HTTPS
   via ``connection_uri`` + ``api_key`` from settings (Req 10.1, 10.2). Without
   a configured core the SDK refuses to initialize and the script fails closed.
2. Creates the four canonical SuperTokens UserRoles — ``admin``, ``dispatcher``,
   ``ops_manager``, ``driver`` — idempotently (Req 4.4). Re-running is safe:
   a role that already exists is left untouched.
3. Reads every ``auth_users`` row from PostgreSQL (the provisioning source of
   truth created by the ``0002_auth_users`` migration) and calls
   :func:`auth.provisioner.provision_all`, which creates/updates one SuperTokens
   user per row, assigns roles, and writes the ``tenant_id`` / ``has_pii_access``
   metadata (Req 9.3, 9.6) — isolating per-row failures so one bad row never
   aborts the batch (Req 9.7).
4. Prints a one-line summary and exits non-zero if any row failed.

Usage:
    python -m scripts.provision_auth_users          # from Runsheet-backend/
    python scripts/provision_auth_users.py           # standalone
    python -m scripts.provision_auth_users --roles-only
    python -m scripts.provision_auth_users --skip-role-creation

Design reference: ``.kiro/specs/supertokens-auth-migration/design.md``
§User_Provisioner.

Validates: Requirements 9.3, 9.4, 9.6, 9.7, 4.4
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Awaitable, Callable, Optional, Sequence

# Ensure the project root is on sys.path when running as a standalone script
# (mirrors the other scripts in this directory).
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from auth.provisioner import (
    AuthUserRow,
    AuthUserStore,
    ProvisionReport,
    SuperTokensAdmin,
    provision_all,
)
from auth.supertokens_init import CANONICAL_ROLES, init_supertokens
from config.settings import Settings, get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# A role-creator seam: (role_name) -> awaitable[created_new_role: bool].
# Production binds this to the SuperTokens UserRoles SDK; tests inject a fake.
RoleCreator = Callable[[str], Awaitable[bool]]

# A rows reader seam: () -> awaitable[list[AuthUserRow]]. Production reads the
# ``auth_users`` table; tests inject an in-memory list.
RowsReader = Callable[[], Awaitable[Sequence[AuthUserRow]]]

#: The SuperTokens tenant the canonical roles are created in. This migration
#: does not use the MultiTenancy recipe, so roles live in the single default
#: ("public") SuperTokens tenant — matching ``provisioner.DEFAULT_ST_TENANT_ID``.
DEFAULT_ST_TENANT_ID = "public"


# ---------------------------------------------------------------------------
# Canonical UserRoles (Req 4.4)
# ---------------------------------------------------------------------------


async def _sdk_create_role(role: str) -> bool:
    """Create a single SuperTokens role with no permissions (idempotent).

    Returns ``True`` when a new role was created, ``False`` when it already
    existed. The SuperTokens ``create_new_role_or_add_permissions`` call is
    itself idempotent — creating an already-existing role with an empty
    permission list is a no-op — so this is safe to run repeatedly (Req 4.4).
    """
    from supertokens_python.recipe.userroles.asyncio import (
        create_new_role_or_add_permissions,
    )

    result = await create_new_role_or_add_permissions(role, [])
    return bool(getattr(result, "created_new_role", False))


async def create_canonical_roles(
    *,
    role_creator: Optional[RoleCreator] = None,
    roles: Sequence[str] = CANONICAL_ROLES,
) -> dict[str, bool]:
    """Create the four canonical SuperTokens UserRoles, idempotently (Req 4.4).

    Args:
        role_creator: Seam that creates one role and returns whether it was
            newly created; defaults to the SuperTokens SDK-backed creator.
        roles: The role names to ensure exist; defaults to the four canonical
            roles (``admin`` / ``dispatcher`` / ``ops_manager`` / ``driver``).

    Returns:
        A mapping of ``role name -> created_new_role`` so the caller can report
        how many roles were newly created versus already present.
    """
    create = role_creator if role_creator is not None else _sdk_create_role

    outcomes: dict[str, bool] = {}
    for role in roles:
        created = await create(role)
        outcomes[role] = created
        logger.info(
            "Canonical role %r: %s",
            role,
            "created" if created else "already exists",
        )

    created_count = sum(1 for v in outcomes.values() if v)
    logger.info(
        "Canonical UserRoles ensured: %d role(s), %d newly created, %d already present",
        len(outcomes),
        created_count,
        len(outcomes) - created_count,
    )
    return outcomes


# ---------------------------------------------------------------------------
# Read the auth_users source-of-truth
# ---------------------------------------------------------------------------


async def read_auth_user_rows() -> list[AuthUserRow]:
    """Read every ``auth_users`` row into :class:`AuthUserRow` records.

    Uses the persistence layer's transactional session scope. Raises a
    descriptive :class:`RuntimeError` when the persistence layer is dormant
    (no ``database_url``), since provisioning has no source of truth to read.
    """
    from persistence.database import is_persistence_enabled, session_scope

    if not is_persistence_enabled():
        raise RuntimeError(
            "Cannot read auth_users: the persistence layer is dormant "
            "(settings.database_url is not set). Configure DATABASE_URL to the "
            "PostgreSQL source of truth before provisioning."
        )

    from sqlalchemy import text

    query = text(
        "SELECT id, email, tenant_id, roles, has_pii_access, driver_id, "
        "st_user_id FROM auth_users ORDER BY email"
    )
    async with session_scope() as db:
        result = await db.execute(query)
        records = result.all()

    rows: list[AuthUserRow] = []
    for rec in records:
        rows.append(
            AuthUserRow(
                id=str(rec.id) if rec.id is not None else None,
                email=rec.email,
                tenant_id=rec.tenant_id,
                roles=tuple(r for r in (rec.roles or []) if isinstance(r, str)),
                has_pii_access=bool(rec.has_pii_access),
                driver_id=rec.driver_id,
                st_user_id=rec.st_user_id,
            )
        )

    logger.info("Read %d auth_users row(s) from the source of truth", len(rows))
    return rows


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def provision_auth_users(
    *,
    settings: Optional[Settings] = None,
    skip_role_creation: bool = False,
    roles_only: bool = False,
    initialize_sdk: bool = True,
    role_creator: Optional[RoleCreator] = None,
    rows_reader: Optional[RowsReader] = None,
    admin: Optional[SuperTokensAdmin] = None,
    store: Optional[AuthUserStore] = None,
) -> Optional[ProvisionReport]:
    """Initialize the SDK, create canonical roles, and provision every row.

    The collaborator seams (``role_creator`` / ``rows_reader`` / ``admin`` /
    ``store``) default to their production implementations but can be injected
    for testing without the SuperTokens SDK or a live database.

    Args:
        settings: Application settings; defaults to :func:`get_settings`.
        skip_role_creation: When ``True``, do not (re)create the canonical roles.
        roles_only: When ``True``, create the canonical roles and stop (no user
            provisioning). Returns ``None``.
        initialize_sdk: When ``True``, initialize the SuperTokens SDK first.
            Tests that inject fakes set this to ``False``.
        role_creator: Seam for creating one role (see :func:`create_canonical_roles`).
        rows_reader: Seam returning the source rows; defaults to reading
            ``auth_users`` from PostgreSQL.
        admin: SuperTokens admin seam passed to :func:`provision_all`.
        store: ``auth_users`` write-back seam passed to :func:`provision_all`.

    Returns:
        The :class:`ProvisionReport` from :func:`provision_all`, or ``None`` when
        ``roles_only`` is set.
    """
    settings = settings if settings is not None else get_settings()

    if initialize_sdk:
        # Fails closed (SuperTokensConfigError) when the managed core is not
        # configured — we never provision against a non-functional auth path.
        init_supertokens(settings)

    if not skip_role_creation:
        await create_canonical_roles(role_creator=role_creator)
    else:
        logger.info("Skipping canonical role creation (--skip-role-creation)")

    if roles_only:
        logger.info("Roles-only run complete; skipping user provisioning")
        return None

    reader = rows_reader if rows_reader is not None else read_auth_user_rows
    rows = list(await reader())

    if not rows:
        logger.warning(
            "No auth_users rows found — nothing to provision. (Did the "
            "0002_auth_users migration run, and is the demo seed present?)"
        )

    report = await provision_all(rows, admin=admin, store=store)
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(report: Optional[ProvisionReport]) -> None:
    """Print a human-readable summary of the provisioning run."""
    print(f"\n{'=' * 72}")
    print("SuperTokens auth_users provisioning")
    print(f"{'=' * 72}")
    if report is None:
        print("Roles-only run: canonical UserRoles ensured; no users provisioned.")
        print(f"{'=' * 72}\n")
        return

    print(report.summary())
    if report.failed:
        print(f"\n{len(report.failed)} row(s) failed:")
        for result in report.failed:
            print(f"  - {result.email}: {result.error}")
    print(f"{'=' * 72}\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse CLI arguments, run provisioning, and return a process exit code.

    Returns ``0`` when every row provisioned successfully (or a roles-only run
    completed), and ``1`` when any row failed (Req 9.7) or the run could not
    start (e.g. missing SuperTokens config or a dormant database).
    """
    parser = argparse.ArgumentParser(
        description=(
            "Provision the auth_users source-of-truth into SuperTokens and "
            "ensure the four canonical UserRoles exist."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create canonical roles and provision every auth_users row:
  python -m scripts.provision_auth_users

  # Only ensure the canonical UserRoles exist (no user provisioning):
  python -m scripts.provision_auth_users --roles-only

  # Provision users but assume the canonical roles already exist:
  python -m scripts.provision_auth_users --skip-role-creation
""",
    )
    parser.add_argument(
        "--roles-only",
        action="store_true",
        default=False,
        help="Only create the canonical UserRoles; do not provision users.",
    )
    parser.add_argument(
        "--skip-role-creation",
        action="store_true",
        default=False,
        help="Provision users without (re)creating the canonical UserRoles.",
    )

    args = parser.parse_args(argv)

    try:
        report = asyncio.run(
            provision_auth_users(
                skip_role_creation=args.skip_role_creation,
                roles_only=args.roles_only,
            )
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI failure
        logger.error("Provisioning aborted: %s", exc)
        return 1

    _print_summary(report)

    # Non-zero exit when any row failed so CI / operators notice (Req 9.7).
    if report is not None and not report.all_succeeded:
        logger.error(
            "Provisioning finished with %d failure(s)", len(report.failed)
        )
        return 1

    logger.info("Provisioning completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
