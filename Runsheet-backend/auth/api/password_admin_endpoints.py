"""
Admin REST endpoints for provisioned-user password administration.

Closes the SuperTokens Auth Migration gap (design Open Question #6): provisioning
creates users with a random password, and no email transport ships with the
migration, so there was no first-class way for a provisioned user to establish a
credential. These admin-gated routes let an operator mint a password-set link to
hand off out-of-band; the user then sets their own password on the self-serve
reset page (``/auth/reset-password`` in the frontend).

Routes mounted on :data:`router` (prefix ``/api/auth/admin``):

* ``POST /api/auth/admin/password-reset-link``
  — mint a SuperTokens password-reset link for a provisioned user. Returns the
  link in the response body (there is no email delivery) so the admin can hand
  it to the user. Admin-gated.

Security posture
----------------
* Every handler depends on :func:`get_tenant_context` (a verified session) and
  is gated to the ``admin`` role via the shared :func:`require_role`
  (Role_Authorizer, Req 4.x). The global Auth_Middleware also protects this
  prefix — it is NOT on the Public_Route_Allowlist — so it fails closed.
* The underlying service (:mod:`auth.password_admin`) only operates on emails
  present in the ``auth_users`` source of truth, so this surface can never mint
  a credential for an un-provisioned address.

Validates: SuperTokens Auth Migration Req 1.1/1.7 follow-up (OQ6).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth.authorization import require_role
from auth.password_admin import (
    PasswordAdminError,
    create_password_set_link,
)
from errors.exceptions import internal_error, resource_not_found, validation_error
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/admin", tags=["auth-admin"])


class PasswordResetLinkRequest(BaseModel):
    """Body for ``POST /api/auth/admin/password-reset-link``."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(
        ...,
        min_length=3,
        max_length=320,
        description=(
            "Email of a provisioned user (must exist in auth_users). A "
            "password-reset link is minted for this user."
        ),
    )

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, value: str) -> str:
        """Lightweight shape check; the auth_users lookup is the real gate."""
        candidate = value.strip()
        if "@" not in candidate or candidate.startswith("@") or candidate.endswith("@"):
            raise ValueError("A valid email address is required")
        return candidate


class PasswordResetLinkResponse(BaseModel):
    """Response carrying the out-of-band password-set link."""

    email: str
    link: str
    note: str = Field(
        default=(
            "No email is sent. Deliver this link to the user out-of-band; it "
            "lets them set their own password on the reset page."
        ),
    )


def _raise_for_password_admin_error(exc: PasswordAdminError):
    """Map a :class:`PasswordAdminError` to the right structured HTTP error."""
    if exc.reason in ("not_provisioned", "no_supertokens_user"):
        raise resource_not_found(message=exc.message, details={"reason": exc.reason})
    if exc.reason in ("invalid_email", "password_policy"):
        raise validation_error(message=exc.message, details={"reason": exc.reason})
    # persistence_dormant / link_unavailable / update_failed → server-side.
    raise internal_error(message=exc.message, details={"reason": exc.reason})


@router.post("/password-reset-link", response_model=PasswordResetLinkResponse)
async def issue_password_reset_link(
    body: PasswordResetLinkRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> PasswordResetLinkResponse:
    """Mint a password-set (reset) link for a provisioned user (admin only)."""
    require_role(tenant, "admin")

    try:
        result = await create_password_set_link(str(body.email))
    except PasswordAdminError as exc:
        logger.info(
            "Password-reset-link request for %s rejected: %s",
            body.email,
            exc.reason,
        )
        _raise_for_password_admin_error(exc)

    return PasswordResetLinkResponse(email=result.email, link=result.link)


__all__ = ["router"]
