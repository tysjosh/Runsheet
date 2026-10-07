"""Portal-user provisioning for tenant admins (design §1.4, §1.7, F1, D2, D10).

Mirrors :class:`fuel.api.driver_endpoints.AppAccessService` method for method:
one PostgreSQL transaction per operation (``default_portal_access_uow``), the
provisioner running inside it as the only SuperTokens write path for the user,
an ordered commit with compensation, and an audit event on every outcome.

* **Invite** (steps 1-7 of §1.7) runs under the per-customer advisory lock
  ``pg_advisory_xact_lock(hashtext('portal-grants:<tenant>:<customer>'))``
  (FREEZE F1), so the count-then-insert user cap is exact. The password-set
  link and the reset email come after the commit and are best effort.
* **Revoke** removes the identity rather than emptying it (D10): it clears
  the SuperTokens roles, revokes every session, deletes the SuperTokens user,
  deletes the portal-only ``auth_users`` row and marks the grant revoked. An
  "unknown user" answer from any SuperTokens call counts as success, so a
  retry after a failed DB commit converges.
* **Resend** re-mints the link and re-sends the email for an active grant.
* :meth:`PortalAccessService.revoke_sessions_for_customer` is the customer
  archive hook: it revokes sessions and leaves the grants active (PD22).

Every rejection on invite answers the same 409 ``PORTAL_EMAIL_IN_USE`` with one
fixed message, so the route can't enumerate accounts; the real reason is only
in the WARN log and the telemetry audit outcome (R1.4, PD21). The request's
``portal_audit`` line is written by ``PortalAuditMiddleware`` (D7), so this
module never logs to ``portal_audit`` (and never puts an email there).
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from auth.authorization import require_role
from config.settings import get_settings
from errors.exceptions import (
    AppException,
    internal_error,
    portal_customer_archived,
    portal_email_in_use,
    portal_user_limit_reached,
    resource_not_found,
)
from ops.middleware.tenant_guard import TenantContext
from portal.models import (
    PortalUserGrant,
    PortalUserInviteResponse,
    PortalUserLinkResponse,
    PortalUserListResponse,
    PortalUserRevokeResponse,
)
from portal.scope import PORTAL_ROLE
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

#: Process-wide memo: the ``customer`` role has been created in the core.
_customer_role_ready = False


# ---------------------------------------------------------------------------
# Unit of work
# ---------------------------------------------------------------------------


@runtime_checkable
class PortalAccessUnitOfWork(Protocol):
    """One PostgreSQL transaction over ``auth_users`` + ``portal_user_grants``.

    Also satisfies :class:`auth.provisioner.AuthUserStore`, so
    ``provision_user`` writes ``st_user_id`` back inside this transaction.
    """

    async def lock_customer(self, *, tenant_id: str, customer_id: str) -> None: ...

    async def read_auth_user(self, email: str) -> Optional[Dict[str, Any]]:
        """The ``auth_users`` row for ``email``, UNSCOPED on purpose."""
        ...

    async def find_active_grant(
        self, *, tenant_id: str, customer_id: str, email: str
    ) -> Optional[Dict[str, Any]]: ...

    async def count_active_grants(self, *, tenant_id: str, customer_id: str) -> int: ...

    async def upsert_customer_user(
        self, *, email: str, tenant_id: str, customer_id: str
    ) -> None: ...

    async def insert_grant(
        self,
        *,
        grant_id: str,
        tenant_id: str,
        customer_id: str,
        email: str,
        created_by: str,
    ) -> Dict[str, Any]: ...

    async def list_grants(
        self, *, tenant_id: str, customer_id: str
    ) -> List[Dict[str, Any]]: ...

    async def get_grant(
        self, *, tenant_id: str, customer_id: str, grant_id: str
    ) -> Optional[Dict[str, Any]]: ...

    async def read_customer_user(
        self, *, email: str, tenant_id: str, customer_id: str
    ) -> Optional[Dict[str, Any]]: ...

    async def delete_customer_user(
        self, *, email: str, tenant_id: str, customer_id: str
    ) -> int: ...

    async def mark_grant_revoked(self, *, grant_id: str, revoked_by: str) -> None: ...

    async def active_grant_user_ids(
        self, *, tenant_id: str, customer_id: str
    ) -> List[str]: ...

    async def mark_provisioned(self, *, email: str, st_user_id: str) -> None: ...

    async def mark_failed(self, *, email: str, error: str) -> None: ...


_GRANT_COLUMNS = (
    "grant_id, tenant_id, customer_id, email, status, first_seen_at, "
    "created_by, created_at, revoked_by, revoked_at"
)
_GRANT_KEYS = tuple(c.strip() for c in _GRANT_COLUMNS.split(","))


def _grant_row(row: Any) -> Dict[str, Any]:
    return dict(zip(_GRANT_KEYS, tuple(row)))


class PostgresPortalAccessUnitOfWork:
    """:class:`PortalAccessUnitOfWork` bound to one ``AsyncSession``.

    No statement commits; the surrounding ``session_scope()`` commits once.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    async def _exec(self, sql: str, params: Mapping[str, Any]) -> Any:
        from sqlalchemy import text

        return await self._session.execute(text(sql), dict(params))

    async def lock_customer(self, *, tenant_id: str, customer_id: str) -> None:
        # FREEZE F1: invites and revokes for one customer are serialized.
        await self._exec(
            "SELECT pg_advisory_xact_lock("
            "hashtext('portal-grants:' || :tenant_id || ':' || :customer_id))",
            {"tenant_id": tenant_id, "customer_id": customer_id},
        )

    async def read_auth_user(self, email: str) -> Optional[Dict[str, Any]]:
        row = (
            await self._exec(
                "SELECT email, tenant_id, roles, has_pii_access, driver_id, "
                "st_user_id, customer_id FROM auth_users WHERE email = :email",
                {"email": email},
            )
        ).first()
        if row is None:
            return None
        return {
            "email": row[0],
            "tenant_id": row[1],
            "roles": list(row[2] or []),
            "has_pii_access": bool(row[3]),
            "driver_id": row[4],
            "st_user_id": row[5],
            "customer_id": row[6],
        }

    async def find_active_grant(
        self, *, tenant_id: str, customer_id: str, email: str
    ) -> Optional[Dict[str, Any]]:
        row = (
            await self._exec(
                f"SELECT {_GRANT_COLUMNS} FROM portal_user_grants "
                "WHERE tenant_id = :tenant_id AND customer_id = :customer_id "
                "AND email = :email AND status = 'active' LIMIT 1",
                {"tenant_id": tenant_id, "customer_id": customer_id, "email": email},
            )
        ).first()
        return None if row is None else _grant_row(row)

    async def count_active_grants(self, *, tenant_id: str, customer_id: str) -> int:
        row = (
            await self._exec(
                "SELECT count(*) FROM portal_user_grants "
                "WHERE tenant_id = :tenant_id AND customer_id = :customer_id "
                "AND status = 'active'",
                {"tenant_id": tenant_id, "customer_id": customer_id},
            )
        ).first()
        return int(row[0] if row is not None else 0)

    async def upsert_customer_user(
        self, *, email: str, tenant_id: str, customer_id: str
    ) -> None:
        await self._exec(
            """
            INSERT INTO auth_users
                   (email, tenant_id, roles, has_pii_access, driver_id,
                    customer_id, updated_at)
            VALUES (:email, :tenant_id, ARRAY['customer']::text[], false, NULL,
                    :customer_id, :now)
            ON CONFLICT (email) DO UPDATE
               SET tenant_id      = EXCLUDED.tenant_id,
                   roles          = EXCLUDED.roles,
                   has_pii_access = false,
                   driver_id      = NULL,
                   customer_id    = EXCLUDED.customer_id,
                   updated_at     = EXCLUDED.updated_at
            """,
            {
                "email": email,
                "tenant_id": tenant_id,
                "customer_id": customer_id,
                "now": utcnow(),
            },
        )

    async def insert_grant(
        self,
        *,
        grant_id: str,
        tenant_id: str,
        customer_id: str,
        email: str,
        created_by: str,
    ) -> Dict[str, Any]:
        from sqlalchemy.exc import IntegrityError

        try:
            row = (
                await self._exec(
                    "INSERT INTO portal_user_grants "
                    "(grant_id, tenant_id, customer_id, email, status, created_by) "
                    "VALUES (:grant_id, :tenant_id, :customer_id, :email, 'active', "
                    f":created_by) RETURNING {_GRANT_COLUMNS}",
                    {
                        "grant_id": grant_id,
                        "tenant_id": tenant_id,
                        "customer_id": customer_id,
                        "email": email,
                        "created_by": created_by,
                    },
                )
            ).first()
        except IntegrityError as exc:
            # uq_portal_grant_active_email: a concurrent invite of the same
            # email for another customer of this tenant won the race.
            logger.warning(
                "Portal invite refused: active grant for this email already "
                "exists in tenant=%s (customer=%s)",
                tenant_id,
                customer_id,
            )
            raise portal_email_in_use() from exc
        return _grant_row(row)

    async def list_grants(
        self, *, tenant_id: str, customer_id: str
    ) -> List[Dict[str, Any]]:
        rows = (
            await self._exec(
                f"SELECT {_GRANT_COLUMNS} FROM portal_user_grants "
                "WHERE tenant_id = :tenant_id AND customer_id = :customer_id "
                "ORDER BY created_at DESC, grant_id DESC",
                {"tenant_id": tenant_id, "customer_id": customer_id},
            )
        ).all()
        return [_grant_row(r) for r in rows]

    async def get_grant(
        self, *, tenant_id: str, customer_id: str, grant_id: str
    ) -> Optional[Dict[str, Any]]:
        row = (
            await self._exec(
                f"SELECT {_GRANT_COLUMNS} FROM portal_user_grants "
                "WHERE tenant_id = :tenant_id AND customer_id = :customer_id "
                "AND grant_id = :grant_id",
                {"tenant_id": tenant_id, "customer_id": customer_id, "grant_id": grant_id},
            )
        ).first()
        return None if row is None else _grant_row(row)

    async def read_customer_user(
        self, *, email: str, tenant_id: str, customer_id: str
    ) -> Optional[Dict[str, Any]]:
        row = (
            await self._exec(
                "SELECT email, st_user_id FROM auth_users WHERE email = :email "
                "AND tenant_id = :tenant_id AND customer_id = :customer_id",
                {"email": email, "tenant_id": tenant_id, "customer_id": customer_id},
            )
        ).first()
        return None if row is None else {"email": row[0], "st_user_id": row[1]}

    async def delete_customer_user(
        self, *, email: str, tenant_id: str, customer_id: str
    ) -> int:
        # The predicate can only match a portal-only row (design §1.7 step 3).
        result = await self._exec(
            "DELETE FROM auth_users WHERE email = :email AND tenant_id = :tenant_id "
            "AND customer_id = :customer_id AND roles = ARRAY['customer']::text[]",
            {"email": email, "tenant_id": tenant_id, "customer_id": customer_id},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def mark_grant_revoked(self, *, grant_id: str, revoked_by: str) -> None:
        await self._exec(
            "UPDATE portal_user_grants SET status = 'revoked', "
            "revoked_by = :revoked_by, revoked_at = :now WHERE grant_id = :grant_id",
            {"grant_id": grant_id, "revoked_by": revoked_by, "now": utcnow()},
        )

    async def active_grant_user_ids(
        self, *, tenant_id: str, customer_id: str
    ) -> List[str]:
        rows = (
            await self._exec(
                "SELECT a.st_user_id FROM portal_user_grants g "
                "JOIN auth_users a ON a.email = g.email AND a.tenant_id = g.tenant_id "
                "WHERE g.tenant_id = :tenant_id AND g.customer_id = :customer_id "
                "AND g.status = 'active' AND a.customer_id = :customer_id "
                "AND a.st_user_id IS NOT NULL",
                {"tenant_id": tenant_id, "customer_id": customer_id},
            )
        ).all()
        return [r[0] for r in rows]

    async def mark_provisioned(self, *, email: str, st_user_id: str) -> None:
        now = utcnow()
        await self._exec(
            "UPDATE auth_users SET st_user_id = :st_user_id, provisioned_at = :now, "
            "provision_error = NULL, updated_at = :now WHERE email = :email",
            {"st_user_id": st_user_id, "now": now, "email": email},
        )

    async def mark_failed(self, *, email: str, error: str) -> None:
        await self._exec(
            "UPDATE auth_users SET provision_error = :error, updated_at = :now "
            "WHERE email = :email",
            {"error": error, "now": utcnow(), "email": email},
        )


@asynccontextmanager
async def default_portal_access_uow() -> AsyncIterator[PortalAccessUnitOfWork]:
    """One ``session_scope`` transaction: commit on clean exit, else roll back."""
    from persistence.database import is_persistence_enabled, session_scope

    if not is_persistence_enabled():
        raise internal_error(
            message=(
                "Portal users cannot be administered: the persistence layer is "
                "dormant (database_url is not set)."
            ),
            details={"reason": "persistence_dormant"},
        )
    async with session_scope() as session:
        yield PostgresPortalAccessUnitOfWork(session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_code_value(exc: AppException) -> str:
    return str(getattr(exc.error_code, "value", exc.error_code))


def _display_status(grant: Mapping[str, Any]) -> str:
    """``invited`` (active, never seen), ``active``, or ``revoked``."""
    if grant.get("status") != "active":
        return "revoked"
    return "invited" if grant.get("first_seen_at") is None else "active"


def _rejection_reason(
    row: Mapping[str, Any], *, tenant_id: str, customer_id: str
) -> Optional[str]:
    """``None`` when the existing ``auth_users`` row may be (re)bound here.

    Acceptable only when the row is in the caller's tenant, holds no role or
    only ``customer``, is bound to no customer or this one, and has no driver
    link (design §1.7 step 3).
    """
    if row.get("tenant_id") != tenant_id:
        return "cross_tenant"
    roles = [r for r in (row.get("roles") or []) if isinstance(r, str)]
    if roles not in ([], [PORTAL_ROLE]):
        return "staff_role"
    bound = row.get("customer_id")
    if bound and bound != customer_id:
        return "other_customer"
    if row.get("driver_id"):
        return "driver_link"
    return None


def _is_unknown_user(result: Any = None, exc: Optional[BaseException] = None) -> bool:
    """True for SuperTokens' "unknown user" answer (result or error)."""
    text = str(exc if exc is not None else result or "").upper().replace(" ", "_")
    return "UNKNOWN_USER" in text


def _grant_model(grant: Mapping[str, Any]) -> PortalUserGrant:
    return PortalUserGrant(
        grant_id=str(grant["grant_id"]),
        email=str(grant["email"]),
        status=_display_status(grant),
        created_at=grant.get("created_at"),
        revoked_at=grant.get("revoked_at"),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PortalAccessService:
    """Admin-gated invite / list / resend / revoke of portal users.

    Collaborators default to their production implementations and resolve
    lazily, so the SuperTokens SDK is never imported early:

    * ``uow_factory``: zero-arg callable returning an async CM yielding a
      :class:`PortalAccessUnitOfWork` (default :func:`default_portal_access_uow`).
    * ``supertokens_admin``: :class:`auth.provisioner.SuperTokensAdmin`.
    * ``session_revoker(st_user_id)``, ``user_deleter(st_user_id)``,
      ``role_creator(role, permissions)``,
      ``link_minter(email, *, tenant_id) -> PasswordSetLink``,
      ``reset_emailer(st_tenant_id, st_user_id, email)``: SDK seams.
    * ``telemetry_service``: audit sink exposing ``log_audit_event``.
    """

    def __init__(
        self,
        *,
        customer_service: Any,
        uow_factory: Optional[Callable[[], Any]] = None,
        supertokens_admin: Any = None,
        session_revoker: Any = None,
        user_deleter: Any = None,
        role_creator: Any = None,
        link_minter: Any = None,
        reset_emailer: Any = None,
        telemetry_service: Any = None,
        provision_user: Any = None,
    ) -> None:
        self._customers = customer_service
        self._uow_factory = uow_factory or default_portal_access_uow
        self._supertokens_admin = supertokens_admin
        self._session_revoker = session_revoker
        self._user_deleter = user_deleter
        self._role_creator = role_creator
        self._link_minter = link_minter
        self._reset_emailer = reset_emailer
        self._telemetry = telemetry_service
        self._provision_user = provision_user
        # Per-instance memo when a role_creator is injected (tests); the
        # module flag covers the SDK default once per process.
        self._role_ready = False

    # -- collaborator resolution -------------------------------------------

    def _admin(self) -> Any:
        if self._supertokens_admin is None:
            from auth.provisioner import SDKSuperTokensAdmin

            self._supertokens_admin = SDKSuperTokensAdmin()
        return self._supertokens_admin

    def _provisioner(self) -> Any:
        if self._provision_user is None:
            from auth.provisioner import provision_user

            self._provision_user = provision_user
        return self._provision_user

    async def _revoke_sessions(self, st_user_id: str) -> Any:
        if self._session_revoker is None:
            from supertokens_python.recipe.session.asyncio import (
                revoke_all_sessions_for_user,
            )

            self._session_revoker = revoke_all_sessions_for_user
        return await self._session_revoker(st_user_id)

    async def _delete_user(self, st_user_id: str) -> Any:
        if self._user_deleter is None:
            from supertokens_python.asyncio import delete_user

            self._user_deleter = delete_user
        return await self._user_deleter(st_user_id)

    async def _ensure_customer_role(self) -> None:
        """Create the ``customer`` role in the core once per process."""
        global _customer_role_ready
        if self._role_creator is None:
            if _customer_role_ready:
                return
            from supertokens_python.recipe.userroles.asyncio import (
                create_new_role_or_add_permissions,
            )

            await create_new_role_or_add_permissions(PORTAL_ROLE, [])
            _customer_role_ready = True
            return
        if not self._role_ready:
            await self._role_creator(PORTAL_ROLE, [])
            self._role_ready = True

    async def _mint_link(self, email: str, tenant_id: str) -> Optional[str]:
        minter = self._link_minter
        if minter is None:
            from auth.password_admin import create_password_set_link

            minter = create_password_set_link
        try:
            result = await minter(email, tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001 — grant stays; admin resends
            logger.warning(
                "Portal password-set link could not be minted (tenant=%s): %s",
                tenant_id,
                getattr(exc, "reason", type(exc).__name__),
            )
            return None
        return getattr(result, "link", None) or None

    async def _send_email(self, st_user_id: Optional[str], email: str) -> bool:
        if not st_user_id:
            return False
        emailer = self._reset_emailer
        if emailer is None:
            from supertokens_python.recipe.emailpassword.asyncio import (
                send_reset_password_email,
            )

            emailer = send_reset_password_email
        from auth.provisioner import DEFAULT_ST_TENANT_ID

        try:
            result = await emailer(DEFAULT_ST_TENANT_ID, st_user_id, email)
        except Exception as exc:  # noqa: BLE001 — best effort
            logger.warning(
                "Portal invite email failed for st_user_id=%s: %s",
                st_user_id,
                type(exc).__name__,
            )
            return False
        if _is_unknown_user(result):
            logger.warning("Portal invite email: unknown st_user_id=%s", st_user_id)
            return False
        return True

    async def _link_and_email(
        self, email: str, tenant_id: str, st_user_id: Optional[str]
    ) -> PortalUserLinkResponse:
        link = await self._mint_link(email, tenant_id)
        sent = await self._send_email(st_user_id, email)
        return PortalUserLinkResponse(
            password_set_link=link, link_error=link is None, email_sent=sent
        )

    # -- audit (telemetry, like the driver flow) ---------------------------

    def _audit(
        self,
        *,
        action: str,
        tenant: TenantContext,
        customer_id: str,
        grant_id: Optional[str],
        email: Optional[str],
        outcome: str,
    ) -> None:
        payload = {
            "acting_user_id": tenant.user_id,
            "tenant_id": tenant.tenant_id,
            "customer_id": customer_id,
            "grant_id": grant_id,
            "email": email,
            "outcome": outcome,
        }
        telemetry = self._telemetry
        if telemetry is not None and hasattr(telemetry, "log_audit_event"):
            try:
                telemetry.log_audit_event(
                    event_type=f"portal_user_{action}",
                    user_id=tenant.user_id,
                    resource_type="portal_user_grant",
                    resource_id=grant_id or customer_id,
                    action=action,
                    details=payload,
                )
            except Exception as exc:  # noqa: BLE001 — audit must never 500
                logger.warning("Portal-access audit sink failed: %s", exc)
        logger.info(
            "Audit: portal_user_%s %s for customer %s",
            action,
            outcome,
            customer_id,
            extra={"extra_data": {"audit_event": True, **payload}},
        )

    # -- customer resolution -------------------------------------------------

    async def get_customer(self, tenant_id: str, customer_id: str) -> Dict[str, Any]:
        """The tenant's customer, else 404 ``RESOURCE_NOT_FOUND`` (R1.3)."""
        return await self._customers.get(tenant_id, customer_id)

    async def _authorize(
        self,
        tenant: TenantContext,
        customer_id: str,
        customer: Optional[Mapping[str, Any]],
        action: str,
    ) -> Mapping[str, Any]:
        require_role(tenant, "admin")
        if customer is not None:
            return customer
        try:
            return await self.get_customer(tenant.tenant_id, customer_id)
        except AppException as exc:
            self._audit(
                action=action,
                tenant=tenant,
                customer_id=customer_id,
                grant_id=None,
                email=None,
                outcome=f"rejected:{_error_code_value(exc)}",
            )
            raise

    # -- operations ----------------------------------------------------------

    async def list(
        self,
        tenant: TenantContext,
        customer_id: str,
        *,
        customer: Optional[Mapping[str, Any]] = None,
    ) -> PortalUserListResponse:
        """Every grant of the customer, newest first (R1.8)."""
        await self._authorize(tenant, customer_id, customer, "list")
        async with self._uow_factory() as uow:
            grants = await uow.list_grants(
                tenant_id=tenant.tenant_id, customer_id=customer_id
            )
        return PortalUserListResponse(data=[_grant_model(g) for g in grants])

    async def invite(
        self,
        tenant: TenantContext,
        customer_id: str,
        email: str,
        *,
        customer: Optional[Mapping[str, Any]] = None,
    ) -> PortalUserInviteResponse:
        """Invite ``email`` as a portal user of ``customer_id`` (design §1.7)."""
        email = (email or "").strip()
        customer = await self._authorize(tenant, customer_id, customer, "invite")

        audit_outcome: Optional[str] = None
        grant: Dict[str, Any] = {}
        already_invited = False
        provisioned = False
        previous_roles: List[str] = []
        existing: Dict[str, Any] = {}
        result: Any = None

        try:
            # 1. Archived customers can't gain users.
            if (customer or {}).get("status") == "archived":
                raise portal_customer_archived()

            async with self._uow_factory() as uow:
                # 2. FREEZE F1.
                await uow.lock_customer(
                    tenant_id=tenant.tenant_id, customer_id=customer_id
                )

                # 3. Unscoped read: authorization must see the row the
                #    upsert (keyed on the unique email) would land on.
                existing = await uow.read_auth_user(email) or {}
                reason = (
                    _rejection_reason(
                        existing, tenant_id=tenant.tenant_id, customer_id=customer_id
                    )
                    if existing
                    else None
                )
                if reason is not None:
                    logger.warning(
                        "Portal invite refused (%s): user=%s tenant=%s customer=%s",
                        reason,
                        tenant.user_id,
                        tenant.tenant_id,
                        customer_id,
                    )
                    audit_outcome = f"rejected:{reason}"
                    raise portal_email_in_use()
                previous_roles = [
                    r for r in (existing.get("roles") or []) if isinstance(r, str)
                ]

                # 4. Already invited: re-provision (idempotent) and resend.
                active = await uow.find_active_grant(
                    tenant_id=tenant.tenant_id, customer_id=customer_id, email=email
                )
                if active is not None:
                    grant = active
                    already_invited = True
                else:
                    # 5. The cap, exact under the lock.
                    limit = int(get_settings().portal_max_users_per_customer)
                    count = await uow.count_active_grants(
                        tenant_id=tenant.tenant_id, customer_id=customer_id
                    )
                    if count >= limit:
                        raise portal_user_limit_reached()

                    # 6. The binding, then the grant.
                    await uow.upsert_customer_user(
                        email=email,
                        tenant_id=tenant.tenant_id,
                        customer_id=customer_id,
                    )
                    grant = await uow.insert_grant(
                        grant_id=f"pug_{uuid.uuid4()}",
                        tenant_id=tenant.tenant_id,
                        customer_id=customer_id,
                        email=email,
                        created_by=tenant.user_id,
                    )

                # 7. Provision inside the transaction (st_user_id written back
                #    through the unit of work).
                from auth.provisioner import AuthUserRow, ProvisioningConflictError

                row = AuthUserRow(
                    email=email,
                    tenant_id=tenant.tenant_id,
                    roles=(PORTAL_ROLE,),
                    has_pii_access=False,
                    driver_id=None,
                    st_user_id=existing.get("st_user_id"),
                    customer_id=customer_id,
                )
                # The role must exist in the core before set_user_roles.
                await self._ensure_customer_role()
                provisioned = True
                try:
                    result = await self._provisioner()(
                        row, admin=self._admin(), store=uow
                    )
                except ProvisioningConflictError as exc:
                    # Refused before any SuperTokens write: nothing to undo.
                    provisioned = False
                    logger.warning(
                        "Portal invite refused (unbound_supertokens_user): "
                        "user=%s tenant=%s customer=%s",
                        tenant.user_id,
                        tenant.tenant_id,
                        customer_id,
                    )
                    audit_outcome = "rejected:unbound_supertokens_user"
                    raise portal_email_in_use() from exc
            # The transaction committed here.
        except AppException as exc:
            if provisioned:
                await self._compensate(
                    email, previous_roles, existing.get("st_user_id"), result
                )
            self._audit(
                action="invite",
                tenant=tenant,
                customer_id=customer_id,
                grant_id=grant.get("grant_id"),
                email=email,
                outcome=audit_outcome or f"rejected:{_error_code_value(exc)}",
            )
            raise
        except Exception:
            if provisioned:
                await self._compensate(
                    email, previous_roles, existing.get("st_user_id"), result
                )
            self._audit(
                action="invite",
                tenant=tenant,
                customer_id=customer_id,
                grant_id=grant.get("grant_id"),
                email=email,
                outcome="failed",
            )
            raise

        delivery = await self._link_and_email(
            email, tenant.tenant_id, getattr(result, "st_user_id", None)
        )
        status = getattr(getattr(result, "status", None), "value", "updated")
        self._audit(
            action="invite",
            tenant=tenant,
            customer_id=customer_id,
            grant_id=grant.get("grant_id"),
            email=email,
            outcome="already_invited" if already_invited else f"invited:{status}",
        )
        return PortalUserInviteResponse(
            grant_id=str(grant["grant_id"]),
            email=str(grant.get("email") or email),
            status=_display_status(grant),
            password_set_link=delivery.password_set_link,
            link_error=delivery.link_error,
            email_sent=delivery.email_sent,
            already_invited=already_invited,
        )

    async def _compensate(
        self,
        email: str,
        previous_roles: Sequence[str],
        previous_st_user_id: Optional[str],
        result: Any,
    ) -> None:
        """Undo what the rolled-back provisioning did in SuperTokens.

        A user this attempt created (the row had no ``st_user_id``, so the
        provisioner could only have created one) is deleted: the rollback
        also drops the ``st_user_id`` write-back, and a retry must not meet
        an unbound SuperTokens user. A previously bound user gets its
        previous roles back unless it already held ``customer``. Never raises.
        """
        try:
            admin = self._admin()
            st_user_id = getattr(result, "st_user_id", None)
            if st_user_id is None:
                st_user_id = await admin.get_user_id_by_email(email)
            if st_user_id is None:
                return
            if previous_st_user_id is None:
                await self._delete_user(st_user_id)
            elif PORTAL_ROLE not in previous_roles:
                await admin.set_user_roles(st_user_id, list(previous_roles))
            else:
                return
            logger.warning(
                "Compensated a failed portal invite for st_user_id=%s", st_user_id
            )
        except Exception as exc:  # noqa: BLE001 — never mask the original error
            logger.error(
                "Portal invite compensation failed: %s. Re-running the invite "
                "converges.",
                type(exc).__name__,
            )

    async def resend(
        self,
        tenant: TenantContext,
        customer_id: str,
        grant_id: str,
        *,
        customer: Optional[Mapping[str, Any]] = None,
    ) -> PortalUserLinkResponse:
        """Re-mint the link and re-send the email for an active grant."""
        await self._authorize(tenant, customer_id, customer, "resend")
        async with self._uow_factory() as uow:
            grant = await uow.get_grant(
                tenant_id=tenant.tenant_id, customer_id=customer_id, grant_id=grant_id
            )
            user = (
                await uow.read_customer_user(
                    email=grant["email"],
                    tenant_id=tenant.tenant_id,
                    customer_id=customer_id,
                )
                if grant is not None and grant.get("status") == "active"
                else None
            )
        if grant is None or grant.get("status") != "active":
            self._audit(
                action="resend",
                tenant=tenant,
                customer_id=customer_id,
                grant_id=grant_id,
                email=None,
                outcome="rejected:RESOURCE_NOT_FOUND",
            )
            raise resource_not_found(
                "Portal user not found", details={"grant_id": grant_id}
            )
        email = str(grant["email"])
        delivery = await self._link_and_email(
            email, tenant.tenant_id, (user or {}).get("st_user_id")
        )
        self._audit(
            action="resend",
            tenant=tenant,
            customer_id=customer_id,
            grant_id=grant_id,
            email=email,
            outcome="link_error" if delivery.link_error else "resent",
        )
        return delivery

    async def _st_idempotent(self, step: str, call: Any) -> None:
        """Run one SuperTokens call; an unknown-user answer counts as success."""
        try:
            await call
        except Exception as exc:  # noqa: BLE001 — classified below
            if not _is_unknown_user(exc=exc):
                raise
            logger.info("Portal revoke: %s found no SuperTokens user", step)

    async def revoke(
        self,
        tenant: TenantContext,
        customer_id: str,
        grant_id: str,
        *,
        customer: Optional[Mapping[str, Any]] = None,
    ) -> PortalUserRevokeResponse:
        """Remove the portal identity behind ``grant_id`` (design §1.7, D10)."""
        await self._authorize(tenant, customer_id, customer, "revoke")
        email: Optional[str] = None
        st_user_id: Optional[str] = None
        try:
            async with self._uow_factory() as uow:
                await uow.lock_customer(
                    tenant_id=tenant.tenant_id, customer_id=customer_id
                )
                grant = await uow.get_grant(
                    tenant_id=tenant.tenant_id,
                    customer_id=customer_id,
                    grant_id=grant_id,
                )
                if grant is None or grant.get("status") != "active":
                    raise resource_not_found(
                        "Portal user not found", details={"grant_id": grant_id}
                    )
                email = str(grant["email"])
                user = await uow.read_customer_user(
                    email=email, tenant_id=tenant.tenant_id, customer_id=customer_id
                )
                st_user_id = (user or {}).get("st_user_id")
                if st_user_id:
                    admin = self._admin()
                    await self._st_idempotent(
                        "set_user_roles", admin.set_user_roles(st_user_id, [])
                    )
                    await self._st_idempotent(
                        "revoke_all_sessions_for_user", self._revoke_sessions(st_user_id)
                    )
                    await self._st_idempotent("delete_user", self._delete_user(st_user_id))
                await uow.delete_customer_user(
                    email=email, tenant_id=tenant.tenant_id, customer_id=customer_id
                )
                await uow.mark_grant_revoked(grant_id=grant_id, revoked_by=tenant.user_id)
        except AppException as exc:
            self._audit(
                action="revoke",
                tenant=tenant,
                customer_id=customer_id,
                grant_id=grant_id,
                email=email,
                outcome=f"rejected:{_error_code_value(exc)}",
            )
            raise
        except Exception as exc:
            logger.error(
                "Portal revoke failed for grant_id=%s: %s", grant_id, type(exc).__name__
            )
            self._audit(
                action="revoke",
                tenant=tenant,
                customer_id=customer_id,
                grant_id=grant_id,
                email=email,
                outcome="failed",
            )
            raise

        if st_user_id:
            self._invalidate(st_user_id)
        self._audit(
            action="revoke",
            tenant=tenant,
            customer_id=customer_id,
            grant_id=grant_id,
            email=email,
            outcome="revoked",
        )
        return PortalUserRevokeResponse(grant_id=grant_id, status="revoked")

    @staticmethod
    def _invalidate(st_user_id: str) -> None:
        from portal.services.principal import get_principal_checker

        checker = get_principal_checker()
        if checker is not None:
            checker.invalidate_user(st_user_id)

    async def revoke_sessions_for_customer(
        self, tenant_id: str, customer_id: str
    ) -> int:
        """Customer-archive hook: revoke the sessions of every active grant.

        Grants stay active, so un-archiving restores access (PD22). Best
        effort: failures are logged at ERROR and never raised; the
        per-request principal check is the guarantee (R2.12). Returns the
        number of users whose sessions were revoked.
        """
        try:
            async with self._uow_factory() as uow:
                user_ids = await uow.active_grant_user_ids(
                    tenant_id=tenant_id, customer_id=customer_id
                )
        except Exception as exc:  # noqa: BLE001 — best effort
            logger.error(
                "Portal session revoke on archive failed for tenant=%s "
                "customer=%s: %s",
                tenant_id,
                customer_id,
                type(exc).__name__,
            )
            return 0
        revoked = 0
        for st_user_id in user_ids:
            try:
                await self._revoke_sessions(st_user_id)
                revoked += 1
            except Exception as exc:  # noqa: BLE001 — best effort, keep going
                logger.error(
                    "Portal session revoke on archive failed for tenant=%s "
                    "customer=%s st_user_id=%s: %s",
                    tenant_id,
                    customer_id,
                    st_user_id,
                    type(exc).__name__,
                )
            self._invalidate(st_user_id)
        return revoked


# ---------------------------------------------------------------------------
# Module registry (configured from bootstrap/core.py)
# ---------------------------------------------------------------------------

_service: Optional[PortalAccessService] = None


def configure_portal_access(service: Optional[PortalAccessService]) -> None:
    """Install the service; ``None`` leaves the admin routes answering 503."""
    global _service
    _service = service


def get_portal_access_service() -> Optional[PortalAccessService]:
    return _service


__all__ = [
    "PortalAccessService",
    "PortalAccessUnitOfWork",
    "PostgresPortalAccessUnitOfWork",
    "configure_portal_access",
    "default_portal_access_uow",
    "get_portal_access_service",
]
