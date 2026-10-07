"""
Auth_Backend — SuperTokens SDK initialization (``init_supertokens``).

This module initializes the SuperTokens Python SDK once at startup, before the
FastAPI app is created, wiring the three recipes the migration uses:

* **EmailPassword** — email/password sign-in and password reset served by the
  SDK-owned auth routes under ``/auth`` (Req 1.1–1.6). The public sign-up and
  email-exists HTTP APIs are **disabled** (staging finding F1): accounts are
  created only by :mod:`auth.provisioner`, which calls the recipe-level
  ``sign_up`` function and is unaffected. The sign-up form-field validator
  still enforces the configurable minimum password length (Req 1.7), because
  the password-reset form validates against it. Sign-in and password-reset
  are throttled per IP and per email (staging finding F5,
  :mod:`auth.signin_throttle`).
* **Session** — SuperTokens-issued session tokens delivered as ``HttpOnly`` /
  ``Secure`` cookies with anti-CSRF protection (Req 2.1, 2.2, 2.3, 2.5, 2.7).
  A ``create_new_session`` override reads the ``auth_users`` row bound to the
  signing SuperTokens user (``auth_users.st_user_id``) and writes
  ``tenant_id`` / ``roles`` / ``has_pii_access`` into the access-token payload
  so those claims are signed by the managed core and can never be asserted by
  the client (Req 3.3).
* **UserRoles** — represents the canonical roles listed in
  :data:`CANONICAL_ROLES`: ``admin`` / ``dispatcher`` / ``driver`` /
  ``platform_admin`` / ``customer`` (Req 4.4; ``customer`` is the exclusive
  portal identity, OI-06). That constant is the single source of truth;
  enumerate from it rather than restating the list.

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
from typing import Any, Callable, Dict, List, Optional

from supertokens_python import InputAppInfo, SupertokensConfig, init
from supertokens_python.recipe import emailpassword, session, userroles
from supertokens_python.recipe.emailpassword import (
    InputFormField,
    InputSignUpFeature,
)
from supertokens_python.recipe.emailpassword.interfaces import (
    APIInterface as EmailPasswordAPIInterface,
    SignInPostOkResult,
    WrongCredentialsError,
)
from supertokens_python.recipe.emailpassword.types import FormField
from supertokens_python.recipe.session.interfaces import (
    RecipeInterface as SessionRecipeInterface,
)
from supertokens_python.types.response import GeneralErrorResponse

from auth import signin_timing
from auth.signin_throttle import (
    configure_signin_throttle,
    get_signin_throttle,
    throttled_envelope,
)
from config.settings import Settings
from middleware.rate_limiter import get_client_ip

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
#:
#: No role in this tuple implies another. In particular ``platform_admin`` does
#: not imply ``admin``: :func:`auth.authorization.require_role` is exact-match
#: with no implication graph, so a caller holding only ``platform_admin`` is
#: correctly refused everywhere ``admin`` is required. That refusal is the
#: intended semantics of an exact-match check, not a shortcoming of it. Staff
#: accounts therefore carry both roles — see :data:`PLATFORM_STAFF_ROLES`. The
#: rule is pinned by ``tests/unit/test_tenant_scope_authz.py::
#: test_platform_admin_alone_does_not_satisfy_require_role``, which fails if
#: anyone adds the implication.
#:
#: ``ops_manager`` was retired. It was declared here from the beginning but
#: never gated anything: no ``require_role`` call site named it, no inline role
#: check consulted it, and the frontend never referenced it. A role that grants
#: nothing is worse than absent — it reads as a real permission tier to anyone
#: provisioning a user, so an operator could hand it out believing it conferred
#: access it did not. ``tests/unit/test_tenant_scope_authz.py::
#: test_ops_manager_is_retired`` fails if it comes back.
#:
#: ``customer`` is the customer-portal identity (OI-06). It is **exclusive**: a
#: customer row holds no other role, no ``driver_id`` and no PII flag, and is
#: bound to exactly one commerce ``customer_id`` (the ``auth_users`` CHECK
#: ``ck_auth_users_customer_binding`` owns that invariant). A customer session
#: is refused on every route outside the portal allowlist (``portal.scope``).
#: :data:`CUSTOMER_ASSIGNABLE_ROLES` excludes it: only the portal grant flow
#: writes it.
CANONICAL_ROLES: tuple[str, ...] = (
    "admin",
    "dispatcher",
    "driver",
    "platform_admin",
    "customer",
)

#: The customer-portal role (see :data:`CANONICAL_ROLES`).
CUSTOMER_PORTAL_ROLE: str = "customer"

#: Every staff (non-portal) role. The portal staff-deny tests iterate it.
STAFF_ROLES: tuple[str, ...] = ("admin", "dispatcher", "driver", "platform_admin")

#: The Runsheet-staff role. Callers holding it may target a tenant other than
#: their own on endpoints that take a ``tenant_id`` parameter.
PLATFORM_ADMIN_ROLE: str = "platform_admin"

#: Roles a tenant's own administrator may assign. Deliberately excludes
#: ``platform_admin`` so tenant-scoped admin cannot escalate to cross-tenant.
#: Also excludes ``customer``: portal users are created only by the portal grant
#: flow, never by assigning a role. Every other canonical role is assignable,
#: so this is :data:`CANONICAL_ROLES` minus those two — but it stays an explicit
#: tuple rather than a derived
#: one, so adding a future privileged role does not silently make it
#: customer-assignable by omission.
CUSTOMER_ASSIGNABLE_ROLES: tuple[str, ...] = (
    "admin",
    "dispatcher",
    "driver",
)

#: The role set a Runsheet staff account is provisioned with.
#:
#: It is **two roles, not one**, and that is deliberate. ``platform_admin`` is a
#: narrow *additive* capability: it answers "may I point this at a tenant that
#: is not mine". It does not answer "may I do this at all".
#: :func:`auth.authorization.require_role` is exact-match and has no
#: role-implication graph — ``platform_admin`` no more satisfies a requirement
#: for ``admin`` than ``admin_ops`` does. Teaching it an implication graph was
#: considered and rejected: it would silently widen every existing and future
#: ``require_role(tenant, "admin")`` call site, and an explicit role list in
#: ``auth_users.roles`` is readable in a way an implication graph is not.
#:
#: So staff hold ``admin`` to reach the ordinary admin surface, and
#: ``platform_admin`` to aim it at another tenant. A staff account granted only
#: ``platform_admin`` gets 403 ``INSUFFICIENT_ROLE`` on every admin endpoint,
#: which is the correct outcome: it holds the cross-tenant capability but not the
#: admin role the endpoint asks for. This constant does not change that
#: behaviour — it records which pair a staff account is meant to hold, a
#: decision that had not been written down anywhere before.
#:
#: Nothing grants this bundle automatically. There is no staff-provisioning
#: path: an operator inserts the roles into the ``auth_users`` row deliberately,
#: and :func:`auth.provisioner.provision_all` pushes them to the core.
#: :data:`CUSTOMER_ASSIGNABLE_ROLES` excludes ``platform_admin``, so a customer
#: administrator cannot assemble this bundle for themselves.
PLATFORM_STAFF_ROLES: tuple[str, ...] = ("admin", PLATFORM_ADMIN_ROLE)

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

    Reads the PostgreSQL ``auth_users`` row **bound** to this SuperTokens user
    (``auth_users.st_user_id = user_id``) and returns the ``tenant_id`` /
    ``roles`` / ``has_pii_access`` (and ``driver_id`` when present) to embed in
    the access-token payload (Req 3.3, 9.6, 7.3).

    The binding is ``st_user_id``, which only :mod:`auth.provisioner` writes
    (``mark_provisioned``). Email is deliberately NOT the key (staging finding
    F1): a SuperTokens user registered by someone else under a provisioned
    email address would otherwise inherit that row's tenant and roles.

    Returns an empty mapping when no row (or more than one) is bound to the
    user, or when the persistence layer is dormant. An empty mapping means the
    session carries no ``tenant_id`` claim, so the Session_Verifier rejects it
    on protected routes (Req 5.3) — fail-closed by construction.
    """
    return await _lookup_auth_user_claims(user_id)


async def _lookup_auth_user_claims(st_user_id: str) -> Dict[str, Any]:
    """Read ``tenant_id`` / ``roles`` / ``has_pii_access`` from ``auth_users``.

    Keyed on ``st_user_id`` (the provisioner's write-back), not ``email`` —
    see :func:`_claims_for_user` (F1). Exactly one bound row yields claims;
    zero or several yield ``{}`` (fail closed).
    """
    from persistence.database import is_persistence_enabled, session_scope

    if not is_persistence_enabled():
        logger.warning(
            "auth_users lookup skipped for st_user_id=%s: persistence layer is "
            "dormant (database_url unset)",
            st_user_id,
        )
        return {}

    from sqlalchemy import text

    query = text(
        "SELECT tenant_id, roles, has_pii_access, driver_id, customer_id "
        "FROM auth_users WHERE st_user_id = :user_id"
    )
    try:
        async with session_scope() as db:
            rows = (await db.execute(query, {"user_id": st_user_id})).all()
    except Exception as exc:  # pragma: no cover - defensive (DB unavailable)
        logger.warning(
            "auth_users lookup failed for st_user_id=%s: %s", st_user_id, exc
        )
        return {}

    if not rows:
        logger.warning(
            "No auth_users row bound to st_user_id=%s; session will carry no "
            "tenant claims",
            st_user_id,
        )
        return {}
    if len(rows) > 1:
        logger.warning(
            "%d auth_users rows bound to st_user_id=%s; refusing to pick one, "
            "session will carry no tenant claims",
            len(rows),
            st_user_id,
        )
        return {}

    row = tuple(rows[0])
    tenant_id, roles, has_pii_access, driver_id = row[:4]
    customer_id = row[4] if len(row) > 4 else None
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
    # customer_id is present only for portal (``customer``) users; the
    # central deny and the portal guard read it from the verified session.
    if customer_id:
        claims["customer_id"] = customer_id
    return claims


def _form_field_value(form_fields: List[FormField], field_id: str) -> Any:
    for field in form_fields or []:
        if getattr(field, "id", None) == field_id:
            return field.value
    return None


def _send_throttled(api_options: Any, retry_after: int) -> GeneralErrorResponse:
    """Write the F5 429 onto the SDK response and return a placeholder result.

    In supertokens-python 0.31.3 ``FastApiResponse.set_status_code`` and
    ``set_json_content`` are first-write-wins, so the ``send_200_response`` the
    SDK handler runs after the override returns is a no-op and the client gets
    this 429. Raising instead would not work: the SDK middleware sits outside
    FastAPI's exception handlers, so an exception would surface as a 500.
    """
    response = api_options.response
    response.set_status_code(429)
    response.set_header("Retry-After", str(retry_after))
    response.set_json_content(throttled_envelope(retry_after))
    return GeneralErrorResponse("RATE_LIMITED")


def _override_emailpassword_apis(
    original_implementation: EmailPasswordAPIInterface,
) -> EmailPasswordAPIInterface:
    """Disable sign-up / email-exists (F1) and throttle sign-in / reset (F5).

    ``POST /auth/signup`` let anyone create users in the core, and
    ``GET /auth/signup/email/exists`` was an account-enumeration oracle (F4).
    With the SDK flags set, the middleware no longer matches those routes, so
    requests fall through to FastAPI and get a 404. Accounts are created only by
    :mod:`auth.provisioner` via the recipe-level ``sign_up`` function, which
    these flags do not affect.

    Sign-in and password-reset-token stay served but are throttled per client
    IP and per email (staging finding F5, :mod:`auth.signin_throttle`). A
    throttled request gets a 429 error envelope with ``Retry-After``, never
    ``WRONG_CREDENTIALS_ERROR``. The throttle is looked up per request so tests
    can inject one.
    """
    original_implementation.disable_sign_up_post = True
    original_implementation.disable_email_exists_get = True

    original_sign_in_post = original_implementation.sign_in_post
    original_reset_token_post = (
        original_implementation.generate_password_reset_token_post
    )

    async def sign_in_post(  # type: ignore[override]
        form_fields: List[FormField],
        tenant_id: str,
        session: Any,
        should_try_linking_with_session_user: Optional[bool],
        api_options: Any,
        user_context: Dict[str, Any],
    ):
        throttle = get_signin_throttle()
        email = _form_field_value(form_fields, "email")
        retry = await throttle.check_sign_in(
            get_client_ip(api_options.request.request), email
        )
        if retry is not None:
            # Not padded: a 429 doesn't depend on whether the account exists.
            return _send_throttled(api_options, retry)

        started = signin_timing.now()
        result = await original_sign_in_post(
            form_fields,
            tenant_id,
            session,
            should_try_linking_with_session_user,
            api_options,
            user_context,
        )
        if isinstance(result, WrongCredentialsError):
            await throttle.record_sign_in_failure(email)
        elif isinstance(result, SignInPostOkResult):
            await throttle.clear_sign_in_failures(email)
        if not isinstance(result, SignInPostOkResult):
            # Fixed minimum time for every failure, so "unknown email" and
            # "wrong password" take the same time (OI-12).
            await signin_timing.pad_to_floor(started)
        return result

    async def generate_password_reset_token_post(  # type: ignore[override]
        form_fields: List[FormField],
        tenant_id: str,
        api_options: Any,
        user_context: Dict[str, Any],
    ):
        retry = await get_signin_throttle().check_password_reset(
            get_client_ip(api_options.request.request),
            _form_field_value(form_fields, "email"),
        )
        if retry is not None:
            return _send_throttled(api_options, retry)
        return await original_reset_token_post(
            form_fields, tenant_id, api_options, user_context
        )

    original_implementation.sign_in_post = sign_in_post
    original_implementation.generate_password_reset_token_post = (
        generate_password_reset_token_post
    )
    return original_implementation


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
            # hardcoded credential pair (Req 1.1–1.5). The sign-up feature's
            # password validator stays: the reset-password form validates
            # against it (Req 1.7). The HTTP sign-up route itself is disabled
            # by the APIs override (F1).
            emailpassword.init(
                override=emailpassword.EmailPasswordOverrideConfig(
                    apis=_override_emailpassword_apis,
                ),
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

    # Shared by the SDK override above and /auth/driver/session (F5).
    configure_signin_throttle(settings)

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
    "CUSTOMER_PORTAL_ROLE",
    "PLATFORM_ADMIN_ROLE",
    "PLATFORM_STAFF_ROLES",
    "STAFF_ROLES",
    "SuperTokensConfigError",
    "init_supertokens",
    "is_supertokens_initialized",
    "configured_session_lifetime_seconds",
]
