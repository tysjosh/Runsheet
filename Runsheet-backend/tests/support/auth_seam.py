"""
Shared Test_Auth_Path helpers for endpoint tests.

Historically the endpoint test suite reached protected routes by minting a
homegrown ``Bearer <dev JWT>`` signed with the legacy ``jwt_secret`` and
patching ``ops.middleware.tenant_guard.get_settings`` so the legacy verifier
would accept it. The SuperTokens Auth Migration retires that scheme: once
``auth_provider`` is ``supertokens`` those forged tokens are rejected and the
legacy ``dual``-accept window can be removed.

This module is the supported replacement. Instead of minting a token, tests
install a FastAPI ``dependency_overrides[get_tenant_context]`` entry (the
Test_Auth_Path seam from ``auth.test_auth``) that yields a verified
``TenantContext`` for a requested ``tenant_id`` / role set / ``has_pii_access``
value (Req 11.1, 11.2).

To keep the conversion's blast radius small, the override is **header-driven**:
each test still passes ``headers=auth_headers(tenant_id, roles=..., sub=...)``
exactly as before, but the headers now carry the desired scope in plain
``X-Test-*`` headers rather than a signed JWT. ``install_test_auth`` reads those
headers per-request and returns the matching context, so a single app-level
override faithfully reproduces the old per-call token semantics (different
tenants/roles/users across calls on the same app).

The headers are only ever honored through this test seam — there is no
verification of a real credential here — so this code, like ``auth.test_auth``,
is usable only in the ``test``/``development`` environments.
"""

from __future__ import annotations

import json
from typing import List, Optional, Sequence

from fastapi import Request

from auth.test_auth import issue_test_context
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

# Header names carrying the desired Test_Auth_Path scope. These replace the
# claims that used to live inside the signed dev JWT.
TENANT_HEADER = "X-Test-Tenant-Id"
USER_HEADER = "X-Test-User-Id"
ROLES_HEADER = "X-Test-Roles"
PII_HEADER = "X-Test-Pii-Access"
#: Canonical ``drivers_current.driver_id`` for driver-surface tests. Emitted
#: only when the caller asks for one, so omitting it reproduces a session with
#: no ``driver_id`` claim and keeps the 403 ``DRIVER_IDENTITY_MISSING`` path
#: exercised (Req 1.6).
DRIVER_HEADER = "X-Test-Driver-Id"

#: Default scope mirrors the old demo token: an admin user. ``has_pii_access``
#: defaults to ``False`` to match the legacy ``_make_token`` payloads, which
#: omitted the claim (the verifier then defaulted it to ``False``).
_DEFAULT_ROLES: tuple[str, ...] = ("admin",)


def auth_headers(
    tenant_id: str = "t1",
    *,
    sub: str = "test-user",
    roles: Optional[Sequence[str]] = None,
    has_pii_access: bool = False,
    driver_id: Optional[str] = None,
) -> dict:
    """Build request headers carrying a Test_Auth_Path scope.

    Drop-in replacement for the old ``_auth_headers`` helpers: the returned
    headers make ``install_test_auth``'s overridden ``get_tenant_context``
    yield a :class:`TenantContext` scoped to ``tenant_id`` with the given user
    id, roles, and PII flag.

    ``driver_id`` is the canonical ``drivers_current.driver_id`` a driver
    surface authorizes against. A driver-scoped request is therefore
    ``auth_headers("t1", roles=["driver"], driver_id="drv_1")``. When it is
    omitted no :data:`DRIVER_HEADER` is emitted at all, so the resulting
    context carries ``driver_id=None`` and ``/api/driver`` handlers reject the
    request with 403 ``DRIVER_IDENTITY_MISSING`` (Req 1.6) — exactly what a
    session with no ``driver_id`` claim does in production.
    """
    headers = {
        TENANT_HEADER: tenant_id,
        USER_HEADER: sub,
        PII_HEADER: "true" if has_pii_access else "false",
    }
    role_list = list(roles) if roles is not None else list(_DEFAULT_ROLES)
    headers[ROLES_HEADER] = json.dumps(role_list)
    if driver_id:
        headers[DRIVER_HEADER] = driver_id
    return headers


def _parse_roles(raw: Optional[str]) -> List[str]:
    if not raw:
        return list(_DEFAULT_ROLES)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        # Tolerate a bare comma-separated string for convenience.
        return [r.strip() for r in raw.split(",") if r.strip()]
    if isinstance(parsed, str):
        return [parsed]
    return [str(r) for r in parsed]


def install_test_auth(app) -> None:
    """Install a header-driven Test_Auth_Path override on ``app``.

    Overrides ``get_tenant_context`` so each request is authenticated with a
    :class:`TenantContext` derived from the request's ``X-Test-*`` headers
    (see :func:`auth_headers`). A request that carries no tenant header is
    rejected exactly like the real verifier rejects a request with no verified
    session, so negative tests (missing/blank auth) keep working.

    Idempotent: calling it more than once on the same app is harmless.
    """
    from errors.exceptions import unauthorized

    async def _override(request: Request) -> TenantContext:
        tenant_id = request.headers.get(TENANT_HEADER)
        if not tenant_id:
            # No Test_Auth_Path scope on the request → unauthenticated, just as
            # the real Session_Verifier rejects a request with no session.
            raise unauthorized(
                message="Authentication required",
                details={"reason": "No Test_Auth_Path scope on the request"},
            )
        roles = _parse_roles(request.headers.get(ROLES_HEADER))
        has_pii = (request.headers.get(PII_HEADER) or "").strip().lower() == "true"
        user_id = request.headers.get(USER_HEADER) or None
        driver_id = request.headers.get(DRIVER_HEADER) or None
        ctx = issue_test_context(
            tenant_id,
            roles=roles,
            has_pii_access=has_pii,
            user_id=user_id,
            driver_id=driver_id,
        )
        # Middleware that cannot depend on get_tenant_context (idempotency
        # replay, per-driver rate limiting) reads these off request.state, so
        # the seam stamps them exactly as the real verifier does.
        request.state.tenant_id = ctx.tenant_id
        request.state.driver_id = ctx.driver_id
        return ctx

    # Installing this app-level override also activates the Test_Auth_Path
    # bypass for the global AuthEnforcementMiddleware (active under
    # auth_provider=supertokens): ``is_test_auth_bypass_active`` detects the
    # presence of this override and lets the request through to the override
    # instead of rejecting it for lacking a real SuperTokens session (Req 11.2).
    # Because the bypass is keyed off the override's presence, clearing
    # ``app.dependency_overrides`` automatically releases it (Req 11.3).
    app.dependency_overrides[get_tenant_context] = _override
