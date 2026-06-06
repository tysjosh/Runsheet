"""
Test_Auth_Path — a supported, test/development-only way to obtain a valid
authenticated ``TenantContext`` without driving the production sign-in UI.

This is the migration's answer to the large existing test suite that mints
``Bearer <dev JWT>`` tokens to reach protected endpoints. Once SuperTokens is
the session authority those forged tokens stop working, so tests instead use
this module to install a verified ``TenantContext`` for the duration of a test.

What it provides (Requirement 11):

* :func:`issue_test_context` — build a valid ``TenantContext`` (the
  ``Auth_Context`` of the requirements glossary) for a given ``tenant_id`` /
  role set / ``has_pii_access`` value (Req 11.1).
* :func:`override_auth` — a context manager that, for the lifetime of the
  ``with`` block, installs ``app.dependency_overrides[get_tenant_context]`` so
  every ``Depends(get_tenant_context)`` handler receives the issued context
  (Req 11.2), AND registers the app in a module-level *bypass registry* so the
  global ``AuthEnforcementMiddleware`` (task 8.1) lets the request through
  instead of rejecting it for lacking a real SuperTokens session.

Middleware integration seam (task 8.1)
--------------------------------------
The global ``AuthEnforcementMiddleware`` does not exist yet. When it is built it
MUST honor this module's bypass by calling :func:`is_test_auth_bypass_active`
before rejecting an unauthenticated request, e.g.::

    from auth.test_auth import is_test_auth_bypass_active
    ...
    if is_test_auth_bypass_active(request.app):
        return await call_next(request)   # overridden dependency supplies ctx

The predicate is *itself* keyed off ``settings.environment``: it returns
``False`` in production no matter what, so a stray bypass registration can never
weaken enforcement in production.

Fail-closed in production (Req 11.3)
------------------------------------
Every public entry point raises :class:`TestAuthPathDisabledError` when
``settings.environment == production``. The path is usable only in the
``test`` and ``development`` environments.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterable, Iterator, Optional, Sequence

from config.settings import Environment, get_settings
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.tenant_settings import default_tenant_settings

logger = logging.getLogger(__name__)

#: Environments in which the Test_Auth_Path is permitted to operate. Anything
#: outside this set (notably ``production``, but also ``staging``) cannot use
#: it (Req 11.3).
_TEST_AUTH_ENVIRONMENTS: frozenset[Environment] = frozenset(
    {Environment.TEST, Environment.DEVELOPMENT}
)

#: Default values used when a caller does not specify them. ``admin`` with PII
#: access mirrors the demo user so most tests "just work".
_DEFAULT_ROLES: tuple[str, ...] = ("admin",)
_DEFAULT_TENANT_ID: str = "test-tenant"

#: Identity of apps (``id(app)``) that currently have an ``override_auth``
#: bypass installed. Reference-counted via :data:`_active_app_refcounts` so
#: nested/overlapping ``override_auth`` blocks on the same app compose safely.
_active_app_refcounts: dict[int, int] = {}

#: Sentinel marking "no previous dependency override was present" so that
#: :func:`override_auth` can faithfully restore the prior state on exit.
_UNSET = object()


class TestAuthPathDisabledError(RuntimeError):
    """Raised when the Test_Auth_Path is used outside test/development.

    Guards the module's entry points so the test authentication shortcut can
    never be exercised in production (Req 11.3).
    """


def _assert_test_auth_allowed() -> None:
    """Raise unless the current environment permits the Test_Auth_Path.

    Fails closed: any environment other than ``test``/``development`` — most
    importantly ``production`` — is rejected (Req 11.3).
    """
    environment = get_settings().environment
    if environment not in _TEST_AUTH_ENVIRONMENTS:
        raise TestAuthPathDisabledError(
            "Test_Auth_Path is disabled in the "
            f"'{environment.value}' environment; it is available only in "
            "test and development."
        )


def _normalize_roles(roles: Optional[Iterable[str]]) -> list[str]:
    """Coerce a roles iterable into a clean ``list[str]`` of non-empty names."""
    if roles is None:
        return list(_DEFAULT_ROLES)
    if isinstance(roles, str):
        # A bare string is almost certainly a single role passed by mistake;
        # treat it as one role rather than iterating its characters.
        roles = (roles,)
    normalized: list[str] = []
    for role in roles:
        if not isinstance(role, str):
            raise TypeError(f"roles must be strings, got {type(role).__name__}")
        stripped = role.strip()
        if stripped:
            normalized.append(stripped)
    return normalized


def issue_test_context(
    tenant_id: str,
    roles: Optional[Sequence[str]] = _DEFAULT_ROLES,
    has_pii_access: bool = True,
    *,
    user_id: Optional[str] = None,
) -> TenantContext:
    """Build a valid ``TenantContext`` for tests (Req 11.1).

    Produces the same ``TenantContext`` shape the Session_Verifier yields on a
    real request, so handlers depending on ``get_tenant_context`` cannot tell
    the difference. ``region`` / ``measurement_units`` use the platform
    US/imperial defaults (``default_tenant_settings``), matching the tenant
    guard's fall-open behavior when no tenant settings record exists.

    Args:
        tenant_id: The tenant the issued context is scoped to. Must be a
            non-empty string — tenant scope is meaningless without it, and a
            blank tenant is exactly what the real verifier rejects (Req 5.3).
        roles: Role names carried in the context (exact names; the
            Role_Authorizer matches these exactly). Defaults to ``("admin",)``.
        has_pii_access: Whether the context grants PII access (Req 4.5).
        user_id: Optional explicit user id for audit-attribution assertions
            (Req 5.5). Defaults to a deterministic ``test-user::<tenant_id>``
            so two contexts for the same tenant share an identity while
            different tenants stay distinct.

    Returns:
        A populated :class:`TenantContext`.

    Raises:
        TestAuthPathDisabledError: If used in production (Req 11.3).
        ValueError: If ``tenant_id`` is empty/blank.
    """
    _assert_test_auth_allowed()

    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    tenant_id = tenant_id.strip()

    resolved_user_id = user_id if user_id else f"test-user::{tenant_id}"

    settings = default_tenant_settings()
    return TenantContext(
        tenant_id=tenant_id,
        user_id=resolved_user_id,
        has_pii_access=bool(has_pii_access),
        roles=_normalize_roles(roles),
        region=settings.region,
        measurement_units=settings.measurement_units.to_dict(),
    )


def is_test_auth_bypass_active(app: Optional[object] = None) -> bool:
    """Return whether an ``override_auth`` bypass is active (for ``app``).

    This is the contract the global ``AuthEnforcementMiddleware`` (task 8.1)
    MUST consult before rejecting an unauthenticated request: when this returns
    ``True`` the middleware should pass the request through and let the
    overridden ``get_tenant_context`` dependency supply the context (Req 11.2).

    Keyed off ``settings.environment`` so it is **always** ``False`` outside
    test/development — a stray registration can never weaken production
    enforcement (Req 11.3).

    Args:
        app: The FastAPI app to check. When ``None``, returns whether *any*
            app currently has a bypass active (useful for middleware that does
            not have a handle on the app instance).
    """
    if get_settings().environment not in _TEST_AUTH_ENVIRONMENTS:
        return False
    if not _active_app_refcounts:
        return False
    if app is None:
        return True
    return id(app) in _active_app_refcounts


@contextmanager
def override_auth(
    app: object,
    tenant_id: str = _DEFAULT_TENANT_ID,
    roles: Sequence[str] = _DEFAULT_ROLES,
    has_pii_access: bool = True,
    *,
    user_id: Optional[str] = None,
) -> Iterator[TenantContext]:
    """Install a test ``TenantContext`` for the duration of the ``with`` block.

    For the lifetime of the block this:

    1. issues a :class:`TenantContext` via :func:`issue_test_context`,
    2. installs ``app.dependency_overrides[get_tenant_context]`` so every
       ``Depends(get_tenant_context)`` handler receives it (Req 11.2), and
    3. registers ``app`` in the bypass registry so the global
       ``AuthEnforcementMiddleware`` (task 8.1) lets the request through
       (see :func:`is_test_auth_bypass_active`).

    On exit the prior dependency override (if any) is restored and the bypass
    registration is released. Nested/overlapping blocks on the same app are
    reference-counted, so the bypass stays active until the outermost block
    exits.

    Args:
        app: The FastAPI application under test (anything exposing a
            ``dependency_overrides`` mapping).
        tenant_id: Tenant scope for the issued context. Defaults to
            ``"test-tenant"``.
        roles: Roles for the issued context. Defaults to ``("admin",)``.
        has_pii_access: PII access flag. Defaults to ``True``.
        user_id: Optional explicit user id (see :func:`issue_test_context`).

    Yields:
        The issued :class:`TenantContext`, so tenant-isolation tests can scope
        a context to tenant A and assert tenant B's data is inaccessible
        (Req 11.4).

    Raises:
        TestAuthPathDisabledError: If used in production (Req 11.3).
    """
    _assert_test_auth_allowed()

    ctx = issue_test_context(
        tenant_id, roles, has_pii_access, user_id=user_id
    )

    overrides = app.dependency_overrides
    previous = overrides.get(get_tenant_context, _UNSET)
    app_key = id(app)

    overrides[get_tenant_context] = lambda: ctx
    _active_app_refcounts[app_key] = _active_app_refcounts.get(app_key, 0) + 1
    logger.debug(
        "Test_Auth_Path: installed override for tenant=%s roles=%s pii=%s",
        ctx.tenant_id,
        ctx.roles,
        ctx.has_pii_access,
    )

    try:
        yield ctx
    finally:
        # Restore this block's captured prior override. Context managers exit
        # in LIFO order, so restoring the value captured at entry is correct
        # under nesting (inner block restores the outer block's lambda, then
        # the outer block restores the original state).
        if previous is _UNSET:
            overrides.pop(get_tenant_context, None)
        else:
            overrides[get_tenant_context] = previous
        # Release the bypass registration. Reference-counted so an overlapping
        # block on the same app keeps the middleware bypass active until the
        # last block exits.
        remaining = _active_app_refcounts.get(app_key, 0) - 1
        if remaining > 0:
            _active_app_refcounts[app_key] = remaining
        else:
            _active_app_refcounts.pop(app_key, None)


__all__ = [
    "TestAuthPathDisabledError",
    "issue_test_context",
    "override_auth",
    "is_test_auth_bypass_active",
]
