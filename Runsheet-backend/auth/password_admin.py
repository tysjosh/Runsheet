"""
Password administration for provisioned SuperTokens users.

The SuperTokens Auth Migration provisions users from the ``auth_users`` source
of truth (see ``auth/provisioner.py``) but, per design Open Question #6, deferred
password-reset / email delivery. Provisioning therefore creates each user with a
*random* password nobody knows, leaving no first-class way for a provisioned
user to establish a credential they can actually sign in with.

This module closes that gap WITHOUT introducing an email transport:

* :func:`create_password_set_link` mints a SuperTokens password-reset link for a
  provisioned user. Because there is no email delivery, the link is returned to
  the caller (an admin) to hand off out-of-band; the user then opens it and sets
  their own password on the self-serve reset page (the "first-class" path).
* :func:`set_password_for_email` sets a password directly for a provisioned
  user. This is the break-glass / local-development path used by the
  ``scripts/set_user_password.py`` CLI.

Both functions are gated on the ``auth_users`` source of truth: a password can
only ever be set for an email that has been provisioned, so this admin surface
can never mint credentials for an arbitrary, un-provisioned address.

The SuperTokens SDK is imported lazily inside each function so importing this
module never forces the ``supertokens-python`` dependency to load (mirroring the
provisioner). The SDK must already be initialized (``auth/supertokens_init.py``)
before these functions are called.

Design reference: ``.kiro/specs/supertokens-auth-migration/design.md``
§User_Provisioner (OQ6 follow-up).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

#: The SuperTokens tenant these operations run against. The migration does NOT
#: use the MultiTenancy recipe, so every SuperTokens user lives in the single
#: default ("public") tenant — matching ``provisioner.DEFAULT_ST_TENANT_ID``.
DEFAULT_ST_TENANT_ID = "public"


class PasswordAdminError(RuntimeError):
    """Raised when a password-admin operation cannot complete.

    Carries a stable :attr:`reason` code so callers (the admin endpoint, the
    CLI) can map it to the right HTTP status / exit code without string
    matching.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class PasswordSetLink:
    """A password-set (reset) link for a provisioned user."""

    email: str
    st_user_id: str
    link: str


async def _require_provisioned_email(email: str) -> str:
    """Return the normalized email iff it exists in ``auth_users``.

    Provisioning is the gate: a password can only be set for a user that the
    User_Provisioner created from the source of truth. Raises
    :class:`PasswordAdminError` (``reason='not_provisioned'``) otherwise.
    """
    normalized = (email or "").strip()
    if not normalized:
        raise PasswordAdminError("invalid_email", "An email is required")

    from persistence.database import is_persistence_enabled, session_scope

    if not is_persistence_enabled():
        raise PasswordAdminError(
            "persistence_dormant",
            "Cannot administer passwords: the persistence layer is dormant "
            "(database_url is not set).",
        )

    from sqlalchemy import text

    query = text("SELECT email FROM auth_users WHERE email = :email")
    async with session_scope() as db:
        row = (await db.execute(query, {"email": normalized})).first()

    if row is None:
        raise PasswordAdminError(
            "not_provisioned",
            f"No provisioned auth_users record for {normalized!r}. Provision "
            "the user before setting a password.",
        )
    # Return the stored email (CITEXT is case-insensitive; keep the caller's
    # value, which already matched).
    return normalized


async def _st_user_id_for_email(email: str) -> str:
    """Resolve the SuperTokens user id for ``email`` (must already exist)."""
    from supertokens_python.asyncio import (
        AccountInfoInput,
        list_users_by_account_info,
    )

    users = await list_users_by_account_info(
        DEFAULT_ST_TENANT_ID, AccountInfoInput(email=email)
    )
    for user in users or []:
        user_id = getattr(user, "user_id", None) or getattr(user, "id", None)
        if user_id:
            return user_id
    raise PasswordAdminError(
        "no_supertokens_user",
        f"{email!r} exists in auth_users but has no SuperTokens user yet. Run "
        "the provisioning script first.",
    )


async def _recipe_user_id_for_email(email: str):
    """Resolve the EmailPassword ``RecipeUserId`` for ``email``.

    ``update_email_or_password`` operates on a recipe user id, not the primary
    user id, so we read the user and pick its EmailPassword login method.
    """
    from supertokens_python.asyncio import (
        AccountInfoInput,
        list_users_by_account_info,
    )

    users = await list_users_by_account_info(
        DEFAULT_ST_TENANT_ID, AccountInfoInput(email=email)
    )
    for user in users or []:
        for method in getattr(user, "login_methods", []) or []:
            if getattr(method, "recipe_id", None) == "emailpassword":
                return method.recipe_user_id
    raise PasswordAdminError(
        "no_supertokens_user",
        f"{email!r} has no EmailPassword login method in SuperTokens. Run the "
        "provisioning script first.",
    )


async def create_password_set_link(email: str) -> PasswordSetLink:
    """Mint a SuperTokens password-reset link for a provisioned user.

    The user must already exist in ``auth_users`` (the provisioning gate) and in
    SuperTokens. Because the migration ships no email transport (design OQ6),
    the link is returned to the caller to deliver out-of-band; the user opens it
    on the self-serve reset page and sets their own password.

    Raises:
        PasswordAdminError: when the email is not provisioned, has no SuperTokens
            user, or the SDK reports an unknown user.
    """
    normalized = await _require_provisioned_email(email)
    st_user_id = await _st_user_id_for_email(normalized)

    from supertokens_python.recipe.emailpassword.asyncio import (
        create_reset_password_link,
    )
    from supertokens_python.recipe.emailpassword.interfaces import (
        UnknownUserIdError,
    )

    result = await create_reset_password_link(
        DEFAULT_ST_TENANT_ID, st_user_id, normalized
    )
    if isinstance(result, UnknownUserIdError):
        raise PasswordAdminError(
            "no_supertokens_user",
            f"SuperTokens does not recognize a user id for {normalized!r}.",
        )

    # SDK 0.31.3 returns the reset link directly as a ``str`` (older docs return
    # an object with a ``.link`` attribute). Support both shapes.
    link = result if isinstance(result, str) else getattr(result, "link", None)
    if not link:
        raise PasswordAdminError(
            "link_unavailable",
            f"SuperTokens did not return a reset link for {normalized!r}.",
        )

    logger.info("Issued password-set link for %s (user_id=%s)", normalized, st_user_id)
    return PasswordSetLink(email=normalized, st_user_id=st_user_id, link=link)


async def set_password_for_email(
    email: str, password: str, *, apply_password_policy: bool = True
) -> str:
    """Set ``password`` directly for a provisioned user (break-glass / dev).

    The user must already exist in ``auth_users`` and SuperTokens. Returns the
    SuperTokens user id on success.

    Raises:
        PasswordAdminError: when the email is not provisioned / has no
            SuperTokens user, or the password violates the configured policy.
    """
    normalized = await _require_provisioned_email(email)
    recipe_user_id = await _recipe_user_id_for_email(normalized)

    from supertokens_python.recipe.emailpassword.asyncio import (
        update_email_or_password,
    )
    from supertokens_python.recipe.emailpassword.interfaces import (
        PasswordPolicyViolationError,
        UnknownUserIdError,
        UpdateEmailOrPasswordOkResult,
    )

    result = await update_email_or_password(
        recipe_user_id,
        password=password,
        apply_password_policy=apply_password_policy,
        tenant_id_for_password_policy=DEFAULT_ST_TENANT_ID,
    )

    if isinstance(result, UpdateEmailOrPasswordOkResult):
        logger.info("Set password for %s", normalized)
        return recipe_user_id.get_as_string()
    if isinstance(result, PasswordPolicyViolationError):
        raise PasswordAdminError(
            "password_policy",
            getattr(result, "failure_reason", None)
            or "The password does not meet the configured policy.",
        )
    if isinstance(result, UnknownUserIdError):
        raise PasswordAdminError(
            "no_supertokens_user",
            f"SuperTokens does not recognize a user for {normalized!r}.",
        )
    raise PasswordAdminError(
        "update_failed",
        f"Failed to set password for {normalized!r}: {type(result).__name__}",
    )


async def _email_for_st_user_id(st_user_id: str) -> str:
    """Resolve the primary email for a SuperTokens user id (must exist)."""
    from supertokens_python.asyncio import get_user

    user = await get_user(st_user_id)
    emails = getattr(user, "emails", None) if user is not None else None
    if not emails:
        raise PasswordAdminError(
            "no_supertokens_user",
            f"No SuperTokens user / email for user id {st_user_id!r}.",
        )
    return emails[0]


async def change_password(
    st_user_id: str, current_password: str, new_password: str
) -> str:
    """Change a signed-in user's own password after verifying the current one.

    Unlike :func:`set_password_for_email` (an admin/break-glass operation), this
    is the self-service path: the caller proves they know the *current*
    password before the new one is accepted. The ``st_user_id`` is taken from
    the caller's verified session — never from the request body — so a user can
    only ever change their own credential.

    Steps:
      1. Resolve the user's email from the verified SuperTokens user id.
      2. Re-authenticate with ``current_password`` via the EmailPassword recipe;
         a wrong/missing current password is rejected (``reason='wrong_password'``).
      3. Update the credential to ``new_password`` under the configured policy.

    Returns the SuperTokens recipe user id on success.

    Raises:
        PasswordAdminError: ``wrong_password`` when the current password does
            not match, ``password_policy`` when the new password is too weak,
            or ``no_supertokens_user`` / ``update_failed`` for SDK errors.
    """
    if not isinstance(current_password, str) or not current_password:
        raise PasswordAdminError(
            "wrong_password", "The current password is required"
        )
    if not isinstance(new_password, str) or not new_password:
        raise PasswordAdminError(
            "password_policy", "A new password is required"
        )

    email = await _email_for_st_user_id(st_user_id)

    from supertokens_python.recipe.emailpassword.asyncio import sign_in
    from supertokens_python.recipe.emailpassword.interfaces import (
        SignInOkResult,
    )

    # 1. Re-authenticate: prove the caller knows the CURRENT password.
    signin_result = await sign_in(DEFAULT_ST_TENANT_ID, email, current_password)
    if not isinstance(signin_result, SignInOkResult):
        raise PasswordAdminError(
            "wrong_password", "The current password is incorrect"
        )

    # 2. Apply the new password (reuses the policy-aware setter path).
    recipe_user_id = await _recipe_user_id_for_email(email)

    from supertokens_python.recipe.emailpassword.asyncio import (
        update_email_or_password,
    )
    from supertokens_python.recipe.emailpassword.interfaces import (
        PasswordPolicyViolationError,
        UnknownUserIdError,
        UpdateEmailOrPasswordOkResult,
    )

    result = await update_email_or_password(
        recipe_user_id,
        password=new_password,
        apply_password_policy=True,
        tenant_id_for_password_policy=DEFAULT_ST_TENANT_ID,
    )

    if isinstance(result, UpdateEmailOrPasswordOkResult):
        logger.info("User %s changed their own password", email)
        return recipe_user_id.get_as_string()
    if isinstance(result, PasswordPolicyViolationError):
        raise PasswordAdminError(
            "password_policy",
            getattr(result, "failure_reason", None)
            or "The new password does not meet the configured policy.",
        )
    if isinstance(result, UnknownUserIdError):
        raise PasswordAdminError(
            "no_supertokens_user",
            f"SuperTokens does not recognize a user for {email!r}.",
        )
    raise PasswordAdminError(
        "update_failed",
        f"Failed to change password for {email!r}: {type(result).__name__}",
    )


__all__ = [
    "DEFAULT_ST_TENANT_ID",
    "PasswordAdminError",
    "PasswordSetLink",
    "create_password_set_link",
    "set_password_for_email",
    "change_password",
]
