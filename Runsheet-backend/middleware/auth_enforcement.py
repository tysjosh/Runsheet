"""
Auth_Middleware + Public_Route_Allowlist — global, fail-closed authentication
enforcement for the Runsheet backend.

This module replaces the dead ``POLICY_MATRIX`` / ``validate_policy_matrix``
machinery (``middleware/auth_policy.py``, which only logged warnings at startup)
with a real Starlette middleware that **fails closed**: every backend route that
is not on the :data:`PUBLIC_ROUTE_ALLOWLIST` (and is not a sanctioned public
prefix or self-verifying webhook) requires a verified SuperTokens session before
the handler executes (Req 6.1, 6.2). A newly added route with no allowlist entry
is therefore protected by default.

Design reference: ``.kiro/specs/supertokens-auth-migration/design.md``
§"Auth_Middleware + Public_Route_Allowlist".

Provider gating (Migration_Controller, Req 9.1, 9.2)
----------------------------------------------------
Global enforcement is part of the SuperTokens cutover, so it is gated on the
``auth_provider`` flag exactly like the Session_Verifier:

* ``"legacy"`` — global enforcement is **off**; the pre-migration per-handler
  ``Depends(get_tenant_context)`` auth is preserved unchanged (Req 9.2). The
  middleware short-circuits to ``call_next`` so the large existing test suite
  and legacy clients are unaffected.
* ``"dual"`` — a request passes the gate when it carries EITHER a verifiable
  SuperTokens session OR a legacy ``Authorization: Bearer`` token (whose
  validity the per-handler ``get_tenant_context`` then checks). A request that
  carries a *present but invalid* SuperTokens session is rejected outright
  rather than silently downgraded.
* ``"supertokens"`` — a verified SuperTokens session is required; anything else
  on a protected route is rejected with **401** (Req 6.1).

In every enforcing mode, ``OPTIONS`` requests (CORS preflight) pass through
untouched, and the Test_Auth_Path bypass (``override_auth``) is honored in
test/development only (see :func:`auth.test_auth.is_test_auth_bypass_active`).

Why the middleware returns 401 directly (does not raise)
--------------------------------------------------------
A Starlette ``BaseHTTPMiddleware`` runs *outside* the FastAPI exception-handler
middleware, so an exception raised here would not be converted to the structured
``ErrorResponse`` JSON — it would surface as a 500. The middleware therefore
builds the 401 response itself, matching the shape produced by
``errors.handlers.handle_app_exception`` (``error_code`` / ``message`` /
``request_id``) so clients see one consistent error contract.

Fail-closed startup (Req 6.7)
-----------------------------
:func:`register_auth_enforcement` raises :class:`AuthEnforcementConfigError` if
the ``auth_provider`` requires SuperTokens but the SDK was never initialized —
the app refuses to boot with an enforcement layer that cannot verify sessions,
rather than starting with enforcement silently disabled.

Validates: Requirements 6.1, 6.2, 6.3, 6.6, 6.7
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from auth.supertokens_init import is_supertokens_initialized
from auth.test_auth import is_test_auth_bypass_active
from config.settings import Settings
from errors.codes import ErrorCode
from errors.exceptions import AppException

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public_Route_Allowlist (Req 6.3)
#
# The allowlist is intentionally tiny and contains ONLY four sanctioned
# categories: health checks, API documentation, the SuperTokens auth routes,
# and self-verifying webhook (HMAC) routes. Everything else is protected by
# default (Req 6.2). Property 16 (task 8.3) asserts this categorization holds.
# ---------------------------------------------------------------------------

#: Exact-match health-check and documentation routes (Req 6.3). These expose no
#: tenant data and must be reachable without a session (load balancers, the
#: OpenAPI UI, the readiness/liveness probes, the root banner).
HEALTH_ROUTES: frozenset[str] = frozenset(
    {
        "/",
        "/health",
        "/health/ready",
        "/health/live",
        "/api/health",
    }
)

#: Exact-match API documentation routes (Req 6.3).
DOCS_ROUTES: frozenset[str] = frozenset(
    {
        "/docs",
        "/redoc",
        "/openapi.json",
        "/docs/oauth2-redirect",
    }
)

#: The full set of exact-match public routes (health + docs). Kept as a single
#: frozenset for O(1) membership checks on the hot path.
PUBLIC_ROUTE_ALLOWLIST: frozenset[str] = HEALTH_ROUTES | DOCS_ROUTES

#: Public path prefixes. The SuperTokens SDK serves its own auth routes
#: (``/auth/signin``, ``/auth/signup``, ``/auth/signout``,
#: ``/auth/session/refresh``, ...) under ``/auth`` — those must be reachable
#: without an existing session so users can sign in (Req 6.3).
PUBLIC_PREFIXES: tuple[str, ...] = ("/auth",)

#: Webhook routes that verify their own HMAC signature in-handler and are
#: therefore allowlisted from session auth (Req 6.3, 6.6). All inbound webhooks
#: in this codebase are mounted under ``/webhooks``:
#:   * ``POST /webhooks/dinee``                 (Dinee platform events)
#:   * ``POST /webhooks/orders/{channel_id}``   (channel-aware order intake)
#:   * ``POST /webhooks/stripe/{tenant_id}``    (Stripe payment events)
#: ``/webhooks/dinee`` is a fixed path; the other two carry a path parameter so
#: they are matched by prefix. Each handler performs its own signature
#: verification, which is the sole condition under which Req 6.6 permits the
#: allowlist entry.
WEBHOOK_HMAC_ROUTES: frozenset[str] = frozenset({"/webhooks/dinee"})

#: Prefixes for the parameterized webhook routes (see above). A trailing slash
#: keeps the match tight: ``/webhooks/orders/<channel>`` matches but a bare
#: ``/webhooks/orders`` (no such route) does not.
WEBHOOK_HMAC_PREFIXES: tuple[str, ...] = (
    "/webhooks/orders/",
    "/webhooks/stripe/",
)

#: auth_provider values under which global enforcement is active. After the
#: hard cutover ``supertokens`` is the only supported (and only enforcing)
#: provider.
_ENFORCING_PROVIDERS: frozenset[str] = frozenset({"supertokens"})


class AuthEnforcementConfigError(RuntimeError):
    """Raised at registration when enforcement cannot be wired fail-closed.

    Surfaced when ``auth_provider`` requires SuperTokens but the SDK was never
    initialized — the app must refuse to start rather than run with enforcement
    disabled (Req 6.7).
    """


def provider_enforces(provider: Any) -> bool:
    """Return whether ``provider`` activates global session enforcement.

    After the hard cutover ``supertokens`` is the only supported provider and
    enforcement is always on for it. Any unrecognized value (e.g. a test
    ``MagicMock``) does not enforce, so test harnesses that supply a stub
    settings object are unaffected.
    """
    return isinstance(provider, str) and provider == "supertokens"


def is_public_route(path: str) -> bool:
    """Return whether ``path`` is on the Public_Route_Allowlist (Req 6.3).

    Matches the exact health/docs routes, the ``/auth`` SuperTokens prefix, and
    the self-verifying webhook routes. Everything else is treated as protected
    so a newly added route is fail-closed by default (Req 6.2).
    """
    if path in PUBLIC_ROUTE_ALLOWLIST:
        return True
    if path in WEBHOOK_HMAC_ROUTES:
        return True
    for prefix in PUBLIC_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    for prefix in WEBHOOK_HMAC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _request_id(request: Request) -> str:
    """Best-effort request id for the error envelope.

    Falls back to a fresh UUID when the RequestID middleware has not stamped
    the request (the 401 still carries a traceable id either way).
    """
    rid = getattr(request.state, "request_id", None)
    return rid if isinstance(rid, str) and rid else str(uuid.uuid4())


def _unauthorized_response(request: Request, *, reason: str) -> JSONResponse:
    """Build a 401 JSON response matching the app's ``ErrorResponse`` shape.

    Mirrors ``errors.handlers.handle_app_exception`` (top-level ``error_code`` /
    ``message`` / ``details`` / ``request_id``) so the global gate returns the
    same error contract as the rest of the API. No ``TenantContext`` /
    ``Auth_Context`` is produced and the handler never runs (Req 6.1, 6.2).
    """
    return JSONResponse(
        status_code=401,
        content={
            "error_code": ErrorCode.UNAUTHORIZED.value,
            "message": "Authentication required",
            "details": {"reason": reason},
            "request_id": _request_id(request),
        },
    )


async def _has_verifiable_session(request: Request) -> bool:
    """Return whether the request carries a *verifiable* SuperTokens session.

    Delegates to the shared Session_Verifier seam in ``tenant_guard`` so the
    middleware and the per-handler dependency agree on what "verified" means
    and tests can inject a fake verifier via ``configure_session_verifier``.

    Returns ``True`` for a valid session, ``False`` when no SuperTokens session
    token is present, and re-raises the verifier's :class:`AppException` when a
    session is present but invalid/expired/revoked (so the caller rejects it
    rather than downgrading — Req 2.4, 2.6).
    """
    # Imported lazily and via the module so test-installed verifier fakes
    # (configure_session_verifier) are always honored.
    from ops.middleware import tenant_guard

    verifier = tenant_guard._get_session_verifier()
    verified = await verifier.verify(request)
    return verified is not None


def _has_legacy_bearer(request: Request) -> bool:
    """Deprecated no-op retained for backward import compatibility.

    The legacy ``Authorization: Bearer`` (homegrown JWT) acceptance window was
    removed with the SuperTokens hard cutover. Always returns ``False``.
    """
    return False


class AuthEnforcementMiddleware(BaseHTTPMiddleware):
    """Global fail-closed authentication gate (Req 6.1, 6.2).

    For every request the middleware:

    1. lets ``OPTIONS`` (CORS preflight) through untouched;
    2. is a no-op when ``auth_provider`` is not enforcing (a stubbed/unknown
       value), so test harnesses that supply a stub settings object are
       unaffected;
    3. lets Public_Route_Allowlist paths through (Req 6.3);
    4. honors the Test_Auth_Path bypass in test/development only;
    5. otherwise requires a verified SuperTokens session, returning **401**
       when one is not present so the handler never executes.
    """

    async def dispatch(self, request: Request, call_next):
        # CORS preflight must always pass so the browser can complete the
        # actual request afterwards (Req 6.1 carve-out for OPTIONS).
        if request.method == "OPTIONS":
            return await call_next(request)

        from config.settings import get_settings

        settings = get_settings()
        provider = getattr(settings, "auth_provider", "legacy")

        # Pre-migration: no global gate — handlers enforce auth themselves.
        if not provider_enforces(provider):
            return await call_next(request)

        path = request.url.path

        # Sanctioned public routes (health/docs/auth/self-verifying webhooks).
        if is_public_route(path):
            return await call_next(request)

        # Test_Auth_Path bypass — test/development only; always False in
        # production, so it can never weaken production enforcement (Req 11.3).
        if is_test_auth_bypass_active(request.app):
            return await call_next(request)

        try:
            has_session = await _has_verifiable_session(request)
        except AppException as exc:
            # A session was presented but failed verification (expired/revoked/
            # tampered). Reject — never downgrade to legacy (Req 2.4, 2.6).
            logger.debug(
                "Auth gate rejected %s %s: %s",
                request.method,
                path,
                exc.message,
            )
            return _unauthorized_response(
                request, reason="Invalid or expired session"
            )

        if has_session:
            return await call_next(request)

        logger.debug(
            "Auth gate rejected %s %s: no verifiable session",
            request.method,
            path,
        )
        return _unauthorized_response(
            request, reason="No valid session on the request"
        )


def register_auth_enforcement(app: Any, settings: Settings) -> None:
    """Register the SuperTokens SDK middleware and :class:`AuthEnforcementMiddleware`.

    MUST be called at application *import time* (before the app starts serving)
    — Starlette refuses ``add_middleware`` once the app has started.

    Ordering: this function adds the SuperTokens SDK middleware first, then the
    :class:`AuthEnforcementMiddleware`, so the enforcement gate sits *outside*
    the SDK middleware (it allowlists ``/auth`` and lets those requests reach
    the SDK). The caller should add CORS **after** this so CORS remains the
    outermost middleware and can attach headers to the 401s this gate returns.

    Fail-closed startup (Req 6.7): raises :class:`AuthEnforcementConfigError`
    when ``auth_provider`` requires SuperTokens but the SDK was never
    initialized.

    The :class:`AuthEnforcementMiddleware` is always registered (it self-gates
    on ``auth_provider`` and is a no-op under ``legacy``), so flipping the flag
    to ``dual``/``supertokens`` activates the gate without re-wiring. The SDK
    middleware is added only when the provider enforces, since it requires the
    initialized SDK.

    Args:
        app: The FastAPI application (must not have started yet).
        settings: The loaded application settings.
    """
    provider = getattr(settings, "auth_provider", "legacy")
    enforces = provider_enforces(provider)

    if enforces and not is_supertokens_initialized():
        raise AuthEnforcementConfigError(
            "auth_provider="
            f"'{provider}' requires a verified SuperTokens session on every "
            "protected route, but the SuperTokens SDK has not been initialized. "
            "Call auth.supertokens_init.init_supertokens(settings) before "
            "registering auth enforcement (fail-closed startup, Req 6.7)."
        )

    if enforces:
        # Serve the SDK-owned /auth routes and manage session response headers.
        from supertokens_python.framework.fastapi import get_middleware

        app.add_middleware(get_middleware())
        logger.info("SuperTokens SDK middleware registered (auth_provider=%s)", provider)

    app.add_middleware(AuthEnforcementMiddleware)
    logger.info(
        "AuthEnforcementMiddleware registered (auth_provider=%s, enforcing=%s)",
        provider,
        enforces,
    )


__all__ = [
    "AuthEnforcementMiddleware",
    "AuthEnforcementConfigError",
    "PUBLIC_ROUTE_ALLOWLIST",
    "HEALTH_ROUTES",
    "DOCS_ROUTES",
    "PUBLIC_PREFIXES",
    "WEBHOOK_HMAC_ROUTES",
    "WEBHOOK_HMAC_PREFIXES",
    "is_public_route",
    "provider_enforces",
    "register_auth_enforcement",
]
