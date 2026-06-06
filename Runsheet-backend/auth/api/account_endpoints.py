"""
Self-service account endpoints for the signed-in user.

Unlike the admin password surface (``auth/api/password_admin_endpoints.py``),
these routes act on the *caller's own* account. The acting identity is taken
from the verified SuperTokens session (``get_tenant_context``) — never from the
request body — so a user can only change their own credential.

Routes mounted on :data:`router` (prefix ``/api/auth/account``):

* ``POST /api/auth/account/change-password``
  — change the signed-in user's password after re-verifying the current one.
  Requires a valid session (the global Auth_Middleware also protects this
  prefix — it is NOT on the Public_Route_Allowlist). No email transport is
  involved; this is the "I know my current password" path.

Validates: SuperTokens Auth Migration Req 1.7 follow-up (self-service credential
management).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from auth.password_admin import (
    PasswordAdminError,
    change_password,
    _email_for_st_user_id,
)
from config.settings import get_settings
from errors.exceptions import internal_error, validation_error
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/account", tags=["auth-account"])


class AccountProfileResponse(BaseModel):
    """The signed-in user's identity, derived from the verified session."""

    user_id: str
    email: str
    tenant_id: str
    roles: list[str]
    has_pii_access: bool


@router.get("/me", response_model=AccountProfileResponse)
async def get_my_profile(
    tenant: TenantContext = Depends(get_tenant_context),
) -> AccountProfileResponse:
    """Return the signed-in user's identity (session-derived, authoritative).

    Identity fields come from the verified SuperTokens session
    (``TenantContext``); the email is resolved from the SuperTokens user record.
    Nothing here is taken from client input, so a caller can only ever read
    their own profile.
    """
    try:
        email = await _email_for_st_user_id(tenant.user_id)
    except PasswordAdminError:
        # The session is valid but the SuperTokens user/email could not be
        # resolved — surface a blank email rather than failing the whole page.
        email = ""

    return AccountProfileResponse(
        user_id=tenant.user_id,
        email=email,
        tenant_id=tenant.tenant_id,
        roles=tenant.roles or [],
        has_pii_access=tenant.has_pii_access,
    )


class ChangePasswordRequest(BaseModel):
    """Body for ``POST /api/auth/account/change-password``."""

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="The caller's current password (re-verified before change).",
    )
    new_password: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="The new password. Must satisfy the configured policy.",
    )


class ChangePasswordResponse(BaseModel):
    """Confirmation that the caller's password was changed."""

    status: str = "OK"


def _raise_for_change_password_error(exc: PasswordAdminError):
    """Map a :class:`PasswordAdminError` to the right structured HTTP error."""
    if exc.reason in ("wrong_password", "password_policy", "invalid_email"):
        # 400: the caller can fix the request (wrong current pw / weak new pw).
        raise validation_error(message=exc.message, details={"reason": exc.reason})
    # no_supertokens_user / update_failed → server-side.
    raise internal_error(message=exc.message, details={"reason": exc.reason})


@router.post("/change-password", response_model=ChangePasswordResponse)
async def change_own_password(
    body: ChangePasswordRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> ChangePasswordResponse:
    """Change the signed-in user's password (re-verifies the current one)."""
    # Enforce the configured minimum length up front for a clear client message;
    # the SDK policy is still authoritative on the actual update.
    min_length = get_settings().password_min_length
    if len(body.new_password) < min_length:
        raise validation_error(
            message=f"Password must be at least {min_length} characters long",
            details={"reason": "password_policy"},
        )

    try:
        # Identity comes from the verified session, never the request body.
        await change_password(
            tenant.user_id, body.current_password, body.new_password
        )
    except PasswordAdminError as exc:
        logger.info(
            "Change-password for user_id=%s rejected: %s",
            tenant.user_id,
            exc.reason,
        )
        _raise_for_change_password_error(exc)

    return ChangePasswordResponse()


__all__ = ["router"]
