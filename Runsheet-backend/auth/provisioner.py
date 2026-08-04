"""
User_Provisioner — maps the PostgreSQL ``auth_users`` source-of-truth into
SuperTokens (the managed SaaS identity provider).

The ``auth_users`` table (created by the ``0002_auth_users`` migration) is the
authoritative list of who may sign in, which tenant they belong to, which
canonical roles they hold, and whether they have PII access. This module reads
those rows and reflects them into SuperTokens_Core:

* creates (or finds, idempotently) the SuperTokens user keyed by ``email``,
* assigns the UserRoles recipe roles so they match the source row exactly,
* writes ``tenant_id`` / ``has_pii_access`` / ``driver_id`` into the user's
  SuperTokens metadata, and
* backfills ``st_user_id`` / ``provisioned_at`` (or records ``provision_error``)
  back onto the source row.

Design: see ``.kiro/specs/supertokens-auth-migration/design.md`` §User_Provisioner.

Validates: Requirements 9.3, 9.4, 9.6, 9.7
- 9.3: create a SuperTokens user, assign roles, set ``tenant_id`` /
  ``has_pii_access`` for each migrated user.
- 9.4: idempotent — re-running for the same source email never creates a
  duplicate SuperTokens user (looked up by email before creating).
- 9.6: map each row's ``tenant_id`` / role set / ``has_pii_access`` to the
  equivalent SuperTokens identity attributes.
- 9.7: per-row failure is recorded and the batch continues with the rest.

Testability
-----------
The two side-effecting collaborators are expressed as Protocols so tests can
inject an in-memory fake "managed core" and a fake source store without the
SuperTokens SDK installed or a live database:

* :class:`SuperTokensAdmin` — the SuperTokens admin operations the provisioner
  needs (look up by email, create user, set roles, set metadata).
* :class:`AuthUserStore` — write-back of ``st_user_id`` / ``provision_error``
  onto the ``auth_users`` row.

The production implementations (:class:`SDKSuperTokensAdmin`,
:class:`PostgresAuthUserStore`) import their heavy dependencies lazily, so
importing this module never pulls in the SuperTokens SDK or forces the
persistence engine to initialise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from services.time_utils import utcnow

logger = logging.getLogger(__name__)

#: The SuperTokens tenant the provisioner operates against. This migration does
#: NOT use the SuperTokens MultiTenancy recipe — tenant isolation is enforced in
#: the app layer via the ``tenant_id`` access-token claim — so every SuperTokens
#: user lives in the single default ("public") SuperTokens tenant.
DEFAULT_ST_TENANT_ID = "public"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthUserRow:
    """One row of the ``auth_users`` provisioning source-of-truth.

    Mirrors the columns the provisioner cares about. ``email`` is the
    idempotency key (Req 9.4); ``roles`` carries only canonical role names —
    see :data:`auth.supertokens_init.CANONICAL_ROLES`, currently ``admin`` /
    ``dispatcher`` / ``driver`` / ``platform_admin``;
    ``st_user_id`` is the SuperTokens user id once backfilled.
    """

    email: str
    tenant_id: str
    roles: tuple[str, ...] = ()
    has_pii_access: bool = False
    driver_id: Optional[str] = None
    st_user_id: Optional[str] = None
    id: Optional[str] = None


class ProvisionStatus(str, Enum):
    """Outcome of provisioning a single source row."""

    CREATED = "created"  # a new SuperTokens user was created
    UPDATED = "updated"  # an existing SuperTokens user was reconciled
    FAILED = "failed"  # provisioning raised; recorded, batch continued


@dataclass
class ProvisionResult:
    """The result of provisioning one :class:`AuthUserRow`."""

    email: str
    status: ProvisionStatus
    st_user_id: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True when the row provisioned successfully (created or updated)."""
        return self.status is not ProvisionStatus.FAILED


@dataclass
class ProvisionReport:
    """Aggregate outcome of a :func:`provision_all` batch."""

    results: list[ProvisionResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[ProvisionResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[ProvisionResult]:
        return [r for r in self.results if not r.ok]

    @property
    def all_succeeded(self) -> bool:
        """True when every provisioned row succeeded (vacuously true if empty)."""
        return all(r.ok for r in self.results)

    def summary(self) -> str:
        """Human-readable one-line summary for the provisioning script."""
        created = sum(1 for r in self.results if r.status is ProvisionStatus.CREATED)
        updated = sum(1 for r in self.results if r.status is ProvisionStatus.UPDATED)
        failed = len(self.failed)
        return (
            f"{len(self.results)} row(s): "
            f"{created} created, {updated} updated, {failed} failed"
        )


# ---------------------------------------------------------------------------
# Collaborator seams (Protocols)
# ---------------------------------------------------------------------------


@runtime_checkable
class SuperTokensAdmin(Protocol):
    """The SuperTokens admin operations the provisioner depends on.

    A real implementation talks to the managed SuperTokens core via the SDK
    (:class:`SDKSuperTokensAdmin`); tests inject an in-memory fake.
    """

    async def get_user_id_by_email(self, email: str) -> Optional[str]:
        """Return the SuperTokens user id for ``email``, or ``None`` if absent."""
        ...

    async def create_user(self, email: str) -> str:
        """Create a SuperTokens user for ``email`` and return its user id."""
        ...

    async def set_user_roles(self, user_id: str, roles: Sequence[str]) -> None:
        """Make the user's assigned roles exactly equal ``roles`` (idempotent)."""
        ...

    async def set_user_metadata(
        self, user_id: str, metadata: Mapping[str, Any]
    ) -> None:
        """Set the user's metadata keys to the supplied values (idempotent)."""
        ...


@runtime_checkable
class AuthUserStore(Protocol):
    """Write-back onto the ``auth_users`` source row after provisioning."""

    async def mark_provisioned(self, *, email: str, st_user_id: str) -> None:
        """Backfill ``st_user_id`` / ``provisioned_at`` and clear any error."""
        ...

    async def mark_failed(self, *, email: str, error: str) -> None:
        """Record ``provision_error`` for a row whose provisioning failed."""
        ...


# ---------------------------------------------------------------------------
# Core provisioning logic
# ---------------------------------------------------------------------------


def _normalize_roles(roles: Optional[Sequence[str]]) -> list[str]:
    """Return a clean, de-duplicated, order-preserving list of role names."""
    if not roles:
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for role in roles:
        if not isinstance(role, str):
            continue
        stripped = role.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            cleaned.append(stripped)
    return cleaned


def _build_metadata(row: AuthUserRow) -> dict[str, Any]:
    """Build the SuperTokens user-metadata payload for a source row (Req 9.6).

    ``driver_id`` is always included (``None`` when the user is not a driver) so
    re-provisioning a user who lost their driver role clears the stale value
    rather than leaving it behind — keeping the metadata an exact image of the
    source row.
    """
    return {
        "tenant_id": row.tenant_id,
        "has_pii_access": bool(row.has_pii_access),
        "driver_id": row.driver_id,
    }


async def provision_user(
    row: AuthUserRow,
    *,
    admin: Optional[SuperTokensAdmin] = None,
    store: Optional[AuthUserStore] = None,
) -> ProvisionResult:
    """Provision a single ``auth_users`` row into SuperTokens (idempotently).

    Steps:

    1. Look up the SuperTokens user by ``email``. If one already exists it is
       reused (the ``UPDATED`` path); otherwise a new user is created (the
       ``CREATED`` path). This lookup-before-create is what makes provisioning
       idempotent — running it once or N times yields exactly one SuperTokens
       user for the source email (Req 9.4).
    2. Reconcile the user's UserRoles to exactly the row's role set (Req 9.3).
    3. Write ``tenant_id`` / ``has_pii_access`` / ``driver_id`` into the user's
       metadata so they map to the equivalent SuperTokens identity attributes
       (Req 9.3, 9.6).
    4. Backfill ``st_user_id`` / ``provisioned_at`` onto the source row.

    Exceptions are NOT swallowed here — they propagate so the caller can decide
    how to react. :func:`provision_all` wraps this call to record per-row
    failures and continue (Req 9.7).

    Args:
        row: The source row to provision.
        admin: SuperTokens admin seam; defaults to the SDK-backed implementation.
        store: ``auth_users`` write-back seam; defaults to the Postgres-backed
            implementation.

    Returns:
        A :class:`ProvisionResult` with status ``CREATED`` or ``UPDATED``.

    Raises:
        ValueError: if the row is missing an ``email`` or ``tenant_id``.
    """
    if not isinstance(row.email, str) or not row.email.strip():
        raise ValueError("auth_users row is missing a non-empty email")
    if not isinstance(row.tenant_id, str) or not row.tenant_id.strip():
        raise ValueError(
            f"auth_users row {row.email!r} is missing a non-empty tenant_id"
        )

    admin = admin if admin is not None else _default_admin()
    store = store if store is not None else _default_store()

    email = row.email.strip()
    roles = _normalize_roles(row.roles)

    # 1. Idempotent create-or-find keyed by email (Req 9.4).
    existing_user_id = await admin.get_user_id_by_email(email)
    if existing_user_id is None:
        st_user_id = await admin.create_user(email)
        status = ProvisionStatus.CREATED
        logger.info("Provisioned new SuperTokens user for %s", email)
    else:
        st_user_id = existing_user_id
        status = ProvisionStatus.UPDATED
        logger.info(
            "SuperTokens user already exists for %s — reconciling roles/metadata",
            email,
        )

    # 2. Roles must match the source row exactly (Req 9.3, 9.6).
    await admin.set_user_roles(st_user_id, roles)

    # 3. tenant_id / has_pii_access / driver_id into metadata (Req 9.3, 9.6).
    await admin.set_user_metadata(st_user_id, _build_metadata(row))

    # 4. Backfill st_user_id so the next run is provably idempotent.
    await store.mark_provisioned(email=email, st_user_id=st_user_id)

    return ProvisionResult(email=email, status=status, st_user_id=st_user_id)


async def provision_all(
    rows: Sequence[AuthUserRow],
    *,
    admin: Optional[SuperTokensAdmin] = None,
    store: Optional[AuthUserStore] = None,
) -> ProvisionReport:
    """Provision every row, isolating per-row failures (Req 9.7).

    Each row is provisioned independently. If provisioning a given row raises,
    the failure is recorded (both in the returned report and, best-effort, as
    ``provision_error`` on the source row) and the batch continues with the
    remaining rows. A single bad row therefore never aborts the migration.

    Args:
        rows: The source rows to provision.
        admin: SuperTokens admin seam; defaults to the SDK-backed implementation.
        store: ``auth_users`` write-back seam; defaults to the Postgres-backed
            implementation.

    Returns:
        A :class:`ProvisionReport` with one :class:`ProvisionResult` per row.
    """
    admin = admin if admin is not None else _default_admin()
    store = store if store is not None else _default_store()

    report = ProvisionReport()
    for row in rows:
        email = (row.email or "").strip() or "<missing-email>"
        try:
            result = await provision_user(row, admin=admin, store=store)
        except Exception as exc:  # noqa: BLE001 — isolate per-row failure (Req 9.7)
            error = f"{type(exc).__name__}: {exc}"
            logger.error("Failed to provision %s: %s", email, error)
            # Best-effort failure write-back; never let it mask the original
            # error or abort the batch.
            try:
                await store.mark_failed(email=email, error=error)
            except Exception as record_exc:  # noqa: BLE001 — defensive
                logger.error(
                    "Also failed to record provision_error for %s: %s",
                    email,
                    record_exc,
                )
            report.results.append(
                ProvisionResult(
                    email=email, status=ProvisionStatus.FAILED, error=error
                )
            )
        else:
            report.results.append(result)

    logger.info("Provisioning complete: %s", report.summary())
    return report


# ---------------------------------------------------------------------------
# Production implementations (lazy SDK / persistence imports)
# ---------------------------------------------------------------------------


class SDKSuperTokensAdmin:
    """:class:`SuperTokensAdmin` backed by the SuperTokens Python SDK.

    All SDK imports are deferred to call time so importing this module never
    requires the ``supertokens-python`` package (it is pinned in
    ``requirements.txt`` but only needed where provisioning actually runs). The
    SDK must already be initialised (see ``auth/supertokens_init.py``, task 4.1)
    before these methods are called.

    Note on credentials: the ``auth_users`` source has no password column and
    password reset/email flows are deferred (design OQ6). New users are created
    with a strong random password; establishing a user-known credential is a
    follow-up concern handled outside provisioning.
    """

    def __init__(self, tenant_id: str = DEFAULT_ST_TENANT_ID) -> None:
        self._tenant_id = tenant_id

    async def get_user_id_by_email(self, email: str) -> Optional[str]:
        from supertokens_python.asyncio import (
            AccountInfoInput,
            list_users_by_account_info,
        )

        # The SDK returns a List[User] directly (each User exposes ``user_id``).
        users = await list_users_by_account_info(
            self._tenant_id, AccountInfoInput(email=email)
        )
        for user in users or []:
            user_id = getattr(user, "user_id", None) or getattr(user, "id", None)
            if user_id:
                return user_id
        return None

    async def create_user(self, email: str) -> str:
        import secrets

        from supertokens_python.recipe.emailpassword.asyncio import sign_up
        from supertokens_python.recipe.emailpassword.interfaces import (
            SignUpOkResult,
        )

        # Strong random password — provisioning establishes identity, not a
        # user-known credential (see class docstring / design OQ6).
        password = secrets.token_urlsafe(32)
        result = await sign_up(self._tenant_id, email, password)
        if not isinstance(result, SignUpOkResult):
            raise RuntimeError(
                f"SuperTokens sign_up did not succeed for {email!r}: "
                f"{type(result).__name__}"
            )
        user = result.user
        return getattr(user, "user_id", None) or getattr(user, "id")

    async def set_user_roles(self, user_id: str, roles: Sequence[str]) -> None:
        from supertokens_python.recipe.userroles.asyncio import (
            add_role_to_user,
            get_roles_for_user,
            remove_user_role,
        )

        desired = set(_normalize_roles(roles))

        current_result = await get_roles_for_user(self._tenant_id, user_id)
        current = set(getattr(current_result, "roles", []) or [])

        for role in desired - current:
            await add_role_to_user(self._tenant_id, user_id, role)
        for role in current - desired:
            await remove_user_role(self._tenant_id, user_id, role)

    async def set_user_metadata(
        self, user_id: str, metadata: Mapping[str, Any]
    ) -> None:
        from supertokens_python.recipe.usermetadata.asyncio import (
            update_user_metadata,
        )

        await update_user_metadata(user_id, dict(metadata))


class PostgresAuthUserStore:
    """:class:`AuthUserStore` backed by the persistence layer's ``auth_users``.

    Uses :func:`persistence.database.session_scope` so each write commits in its
    own transaction. Rows are keyed by ``email`` (the table's CITEXT UNIQUE
    idempotency key). Imports are deferred so importing this module never forces
    the persistence engine to initialise.
    """

    async def mark_provisioned(self, *, email: str, st_user_id: str) -> None:
        from sqlalchemy import text

        from persistence.database import session_scope

        async with session_scope() as session:
            await session.execute(
                text(
                    """
                    UPDATE auth_users
                       SET st_user_id = :st_user_id,
                           provisioned_at = :now,
                           provision_error = NULL,
                           updated_at = :now
                     WHERE email = :email
                    """
                ),
                {"st_user_id": st_user_id, "now": utcnow(), "email": email},
            )

    async def mark_failed(self, *, email: str, error: str) -> None:
        from sqlalchemy import text

        from persistence.database import session_scope

        async with session_scope() as session:
            await session.execute(
                text(
                    """
                    UPDATE auth_users
                       SET provision_error = :error,
                           updated_at = :now
                     WHERE email = :email
                    """
                ),
                {"error": error, "now": utcnow(), "email": email},
            )


def _default_admin() -> SuperTokensAdmin:
    """The production SuperTokens admin (SDK-backed)."""
    return SDKSuperTokensAdmin()


def _default_store() -> AuthUserStore:
    """The production ``auth_users`` store (Postgres-backed)."""
    return PostgresAuthUserStore()


__all__ = [
    "DEFAULT_ST_TENANT_ID",
    "AuthUserRow",
    "ProvisionStatus",
    "ProvisionResult",
    "ProvisionReport",
    "SuperTokensAdmin",
    "AuthUserStore",
    "provision_user",
    "provision_all",
    "SDKSuperTokensAdmin",
    "PostgresAuthUserStore",
]
