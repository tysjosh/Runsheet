"""
Auth_Backend — SuperTokens SDK initialization (``init_supertokens``).

This module initializes the SuperTokens Python SDK once at startup, before the
FastAPI app is created, wiring the three recipes the migration uses:

* **EmailPassword** — email/password sign-up + sign-in served by the SDK-owned
  auth routes under ``/auth`` (Req 1.1–1.6). Sign-up enforces a configurable
  minimum password length through a form-field validator (Req 1.7).
* **Session** — SuperTokens-issued session tokens delivered as ``HttpOnly`` /
  ``Secure`` cookies with anti-CSRF protection (Req 2.1, 2.2, 2.3, 2.5, 2.7).
  A ``create_new_session`` override reads the signing user's ``auth_users`` row
  and writes ``tenant_id`` / ``roles`` / ``has_pii_access`` into the
  access-token payload so those claims are signed by the managed core and can
  never be asserted by the client (Req 3.3).
* **UserRoles** — represents the canonical roles ``admin`` / ``dispatcher`` /
  ``ops_manager`` / ``driver`` (Req 4.4).

Deployment is the SuperTokens **managed SaaS core**: the SDK reaches a remote
core over HTTPS via ``connection_uri`` + ``api_key`` loaded from environment
configuration — never hardcoded (Req 10.1, 10.2).

Design reference: ``.kiro/specs/supertokens-auth-migration/design.md`` §Auth_Backend.

Note on session lifetime (Req 2.7)
-----------------------------------
The access-token / session validity in SuperTokens is a property of the
**core**, not an SDK ``session.init`` argument (the managed core owns token
issuance). ``settings.session_lifetime_seconds`` is therefore the source of
truth that an operator configures on the managed core; this module surfaces it
(logs it at init and exposes it via :func:`configured_session_lifetime_seconds`)
so the value is single-sourced from settings and verifiable, rather than
duplicated as a literal.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from supertokens_python import InputAppInfo, SupertokensConfig, init
from supertokens_python.recipe import emailpassword, session, userroles
from supertokens_python.recipe.emailpassword import (
    InputFormField,
    InputSignUpFeature,
)
from supertokens_python.recipe.session.interfaces import (
    RecipeInterface as SessionRecipeInterface,
)

from config.settings import Settings

logger = logging.getLogger(__name__)

#: Canonical SuperTokens UserRoles for the platform (Req 4.4). The
#: Role_Authorizer matches these by exact name; the provisioning script
#: (task 2.3) creates them in the core.
#:
#: ``admin`` is a **tenant-scoped** role: the customer's own administrator. It
#: confers no rights over any other tenant. That distinction was previously
#: implicit and cost us a cross-tenant hole in the feature-flag endpoints, where
#: an ``admin`` in one tenant could flip another tenant's flags.
#:
#: ``platform_admin`` is the Runsheet-staff role. Because staff sign in through
#: the same app as customers, it is the only way to express "may act outside my
#: own tenant". Nothing grants it by default — it is provisioned deliberately,
#: and :data:`CUSTOMER_ASSIGNABLE_ROLES` excludes it so a customer administrator
#: cannot grant it to themselves.
CANONICAL_ROLES: tuple[str, ...] = (
    "admin",
    "dispatcher",
    "ops_manager",
    "driver",
    "platform_admin",
)

#: The Runsheet-staff role. Callers holding it may target a tenant other than
#: their own on endpoints that take a ``tenant_id`` parameter.
PLATFORM_ADMIN_ROLE: str = "platform_admin"

#: Roles a tenant's own administrator may assign. Deliberately excludes
#: ``platform_admin`` so tenant-scoped admin cannot escalate to cross-tenant.
CUSTOMER_ASSIGNABLE_ROLES: tuple[str, ...] = (
    "admin",
    "dispatcher",
    "ops_manager",
    "driver",
)

#: The form-field id EmailPassword uses for the password field.
_PASSWORD_FIELD_ID = "password"

# Process-wide init guard. The SuperTokens SDK ``init`` is itself idempotent
# per process, but tracking it here lets the global Auth_Middleware (task 8.1)
# fail closed at startup if it was never initialized while the provider
# requires it.
_initialized: bool = False
_session_lifetime_seconds: int = 0


class SuperTokensConfigError(RuntimeError):
    """Raised when SuperTokens cannot be initialized due to missing config.

    Surfacing this at startup keeps the platform fail-closed: the app refuses
    to boot with auth enforcement that cannot work, rather than starting in a
    degraded state (Req 6.7, 10.3).
    """


def is_supertokens_initialized() -> bool:
    """Return whether :func:`init_supertokens` has run in this process.

    Consumed by the global Auth_Middleware wiring (task 8.1) to fail closed if
    the provider requires SuperTokens but the SDK was never initialized.
    """
    return _initialized


def configured_session_lifetime_seconds() -> int:
    """Return the configured session lifetime (0 until init runs) (Req 2.7)."""
    return _session_lifetime_seconds


def _build_email_delivery(settings: Settings):
    """Build the EmailPassword email-delivery config for password-reset email.

    When a custom SMTP relay is configured (``smtp_host`` + ``smtp_from_email``),
    password-reset email is sent through it using credentials loaded from the
    environment (never hardcoded — mirrors the Req 10.x posture for the core
    connection secrets). When SMTP is not configured, returns ``None`` so the
    EmailPassword recipe falls back to SuperTokens' built-in email service — the
    forgot-password flow therefore works in development without an operator-run
    relay, and an operator enables their own relay purely via env config.

    Returns:
        An ``EmailDeliveryConfig`` wrapping an SMTP service, or ``None`` to use
        the SDK default.
    """
    if not settings.smtp_configured:
        logger.info(
            "SuperTokens email delivery: SMTP not configured — using the "
            "SuperTokens built-in email service for password-reset email. "
            "Set SMTP_HOST + SMTP_FROM_EMAIL to route through your own relay."
        )
        return None

    from supertokens_python.ingredients.emaildelivery.types import (
        EmailDeliveryConfig,
        SMTPSettings,
        SMTPSettingsFrom,
    )
    from supertokens_python.recipe.emailpassword.emaildelivery.services.smtp import (
        SMTPService,
    )

    smtp_service = SMTPService(
        smtp_settings=SMTPSettings(
            host=settings.smtp_host.strip(),
            port=settings.smtp_port,
            from_=SMTPSettingsFrom(
                name=settings.smtp_from_name,
                email=settings.smtp_from_email.strip(),
            ),
            password=(settings.smtp_password or None),
            username=(settings.smtp_username.strip() or None),
            secure=settings.smtp_secure,
        )
    )
    logger.info(
        "SuperTokens email delivery: routing password-reset email via SMTP "
        "host=%s port=%d from=%s secure=%s",
        settings.smtp_host,
        settings.smtp_port,
        settings.smtp_from_email,
        settings.smtp_secure,
    )
    return EmailDeliveryConfig(service=smtp_service)


def _make_password_validator(
    min_length: int,
) -> Callable[[str, str], Any]:
    """Build an EmailPassword form-field validator enforcing a minimum length.

    The validator returns an error message string when the candidate password
    is shorter than ``min_length`` and ``None`` when it is acceptable, matching
    the SuperTokens form-field contract. The minimum is captured from settings
    at init time so the policy is configurable (Req 1.7).
    """

    async def validate_password(value: str, _tenant_id: str) -> Optional[str]:
        if not isinstance(value, str) or len(value) < min_length:
            return f"Password must be at least {min_length} characters long"
        return None

    return validate_password


async def _claims_for_user(user_id: str) -> Dict[str, Any]:
    """Resolve the server-controlled session claims for a SuperTokens user.

    Looks the user up by email (the provisioning idempotency key) in the
    PostgreSQL ``auth_users`` source-of-truth and returns the ``tenant_id`` /
    ``roles`` / ``has_pii_access`` (and ``driver_id`` when present) to embed in
    the access-token payload (Req 3.3, 9.6, 7.3).

    Returns an empty mapping when the user's email or ``auth_users`` row cannot
    be found, or when the persistence layer is dormant. An empty mapping means
    the session carries no ``tenant_id`` claim, so the Session_Verifier rejects
    it on protected routes (Req 5.3) — fail-closed by construction.
    """
    email = await _lookup_user_email(user_id)
    if not email:
        logger.warning(
            "SuperTokens session: no email for user_id=%s; session will lack "
            "tenant claims and be rejected on protected routes",
            user_id,
        )
        return {}
    return await _lookup_auth_user_claims(email)


async def _lookup_user_email(user_id: str) -> Optional[str]:
    """Return the primary email for a SuperTokens user id, or ``None``."""
    # Imported lazily so importing this module never forces a core call.
    from supertokens_python.asyncio import get_user

    try:
        user = await get_user(user_id)
    except Exception as exc:  # pragma: no cover - defensive (network/core)
        logger.warning("SuperTokens get_user failed for %s: %s", user_id, exc)
        return None
    if user is None or not user.emails:
        return None
    return user.emails[0]


async def _lookup_auth_user_claims(email: str) -> Dict[str, Any]:
    """Read ``tenant_id`` / ``roles`` / ``has_pii_access`` from ``auth_users``.

    The ``email`` column is CITEXT (case-insensitive) and is the provisioning
    idempotency key (Req 9.4), so it is the natural lookup key here.
    """
    from persistence.database import is_persistence_enabled, session_scope

    if not is_persistence_enabled():
        logger.warning(
            "auth_users lookup skipped for %s: persistence layer is dormant "
            "(database_url unset)",
            email,
        )
        return {}

    from sqlalchemy import text

    query = text(
        "SELECT tenant_id, roles, has_pii_access, driver_id "
        "FROM auth_users WHERE email = :email"
    )
    try:
        async with session_scope() as db:
            row = (await db.execute(query, {"email": email})).first()
    except Exception as exc:  # pragma: no cover - defensive (DB unavailable)
        logger.warning("auth_users lookup failed for %s: %s", email, exc)
        return {}

    if row is None:
        logger.warning(
            "No auth_users row for email=%s; session will lack tenant claims",
            email,
        )
        return {}

    tenant_id, roles, has_pii_access, driver_id = row
    claims: Dict[str, Any] = {
        "tenant_id": tenant_id,
        # Only the canonical role names are stored; surface them verbatim for
        # the Role_Authorizer's exact-match (Req 4.4, 9.6).
        "roles": [r for r in (roles or []) if isinstance(r, str)],
        "has_pii_access": bool(has_pii_access),
    }
    # driver_id is present only for driver users; the WebSocket_Authenticator
    # reads it from the verified session (Req 7.3).
    if driver_id:
        claims["driver_id"] = driver_id
    return claims


def _override_session_functions(
    original_implementation: SessionRecipeInterface,
) -> SessionRecipeInterface:
    """Override ``create_new_session`` to embed server-set tenant claims.

    On every session creation (sign-up and sign-in), the signing user's
    ``auth_users`` row is read and ``tenant_id`` / ``roles`` / ``has_pii_access``
    (and ``driver_id`` when present) are written into the access-token payload.
    Because the managed core signs the payload, these claims are verified on
    every request and cannot be asserted or mutated by the client (Req 3.3).
    """
    original_create_new_session = original_implementation.create_new_session

    async def create_new_session(  # type: ignore[override]
        user_id: str,
        recipe_user_id,
        access_token_payload: Optional[Dict[str, Any]],
        session_data_in_database: Optional[Dict[str, Any]],
        disable_anti_csrf: Optional[bool],
        tenant_id: str,
        user_context: Dict[str, Any],
    ):
        payload: Dict[str, Any] = dict(access_token_payload or {})
        payload.update(await _claims_for_user(user_id))
        return await original_create_new_session(
            user_id,
            recipe_user_id,
            payload,
            session_data_in_database,
            disable_anti_csrf,
            tenant_id,
            user_context,
        )

    original_implementation.create_new_session = create_new_session
    return original_implementation


def init_supertokens(settings: Settings) -> None:
    """Initialize the SuperTokens SDK with the EmailPassword/Session/UserRoles recipes.

    Called once at startup, before app creation. Reaches the managed SaaS core
    over HTTPS using ``connection_uri`` + ``api_key`` from environment config
    (Req 10.1, 10.2). Raises :class:`SuperTokensConfigError` when the required
    connection settings are missing, so the app fails closed rather than
    starting with a non-functional auth provider (Req 6.7, 10.3).

    Args:
        settings: The loaded application settings (provides the SuperTokens
            connection config, app domains, password policy, and session
            lifetime).

    Raises:
        SuperTokensConfigError: when ``supertokens_connection_uri`` is missing.
    """
    global _initialized, _session_lifetime_seconds

    connection_uri = (settings.supertokens_connection_uri or "").strip()
    if not connection_uri:
        # Without a core endpoint the SDK cannot verify sessions — refuse to
        # initialize rather than boot a broken auth path (Req 6.7, 10.3).
        raise SuperTokensConfigError(
            "Cannot initialize SuperTokens: supertokens_connection_uri is not "
            "set. Provide SUPERTOKENS_CONNECTION_URI (and SUPERTOKENS_API_KEY) "
            "for the managed SuperTokens core."
        )

    api_key = (settings.supertokens_api_key or "").strip() or None

    init(
        app_info=InputAppInfo(
            app_name="Runsheet",
            api_domain=settings.supertokens_api_domain,
            website_domain=settings.supertokens_website_domain,
            api_base_path="/auth",
        ),
        supertokens_config=SupertokensConfig(
            connection_uri=connection_uri,
            api_key=api_key,
        ),
        framework="fastapi",
        mode="asgi",
        recipe_list=[
            # EmailPassword: server-side credential verification only; no
            # hardcoded credential pair (Req 1.1–1.5). Sign-up enforces the
            # configurable minimum password length (Req 1.7).
            emailpassword.init(
                sign_up_feature=InputSignUpFeature(
                    form_fields=[
                        InputFormField(
                            id=_PASSWORD_FIELD_ID,
                            validate=_make_password_validator(
                                settings.password_min_length
                            ),
                        )
                    ]
                ),
                email_delivery=_build_email_delivery(settings),
            ),
            # Session: SuperTokens-issued tokens in HttpOnly/Secure cookies with
            # anti-CSRF (Req 2.1, 2.2, 2.5). The create_new_session override
            # embeds the server-set tenant claims (Req 3.3).
            session.init(
                cookie_secure=True,
                cookie_domain=settings.supertokens_cookie_domain,
                anti_csrf="VIA_TOKEN",
                override=session.InputOverrideConfig(
                    functions=_override_session_functions,
                ),
            ),
            # UserRoles: represents the canonical roles (Req 4.4).
            userroles.init(),
        ],
    )

    _session_lifetime_seconds = settings.session_lifetime_seconds
    _initialized = True

    logger.info(
        "SuperTokens initialized (api_domain=%s, website_domain=%s, "
        "password_min_length=%d, session_lifetime_seconds=%d [enforced on the "
        "managed core])",
        settings.supertokens_api_domain,
        settings.supertokens_website_domain,
        settings.password_min_length,
        settings.session_lifetime_seconds,
    )


__all__ = [
    "CANONICAL_ROLES",
    "CUSTOMER_ASSIGNABLE_ROLES",
    "PLATFORM_ADMIN_ROLE",
    "SuperTokensConfigError",
    "init_supertokens",
    "is_supertokens_initialized",
    "configured_session_lifetime_seconds",
]
