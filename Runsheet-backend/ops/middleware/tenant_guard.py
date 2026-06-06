"""
Tenant Guard / Session_Verifier for the Runsheet backend.

Historically this module extracted tenant identity from a homegrown HS256 JWT
(`Authorization: Bearer <token>`) signed with ``settings.jwt_secret``. As part
of the SuperTokens Auth Migration it becomes the central **Session_Verifier**
seam: ``get_tenant_context`` now branches on the ``auth_provider``
Migration_Controller flag and, for the SuperTokens paths, derives identity
exclusively from a verified SuperTokens session's signed access-token payload.

The critical architectural property is that **the returned ``TenantContext``
shape and the ``get_tenant_context`` signature are unchanged**, so the dozens of
``Depends(get_tenant_context)`` handlers across the app do not change.

``auth_provider`` behavior (Req 9.1, 9.2, 9.5):

* ``"supertokens"`` — verify a SuperTokens session only (Req 3.1, 3.4). A
  request with no/invalid session, or a session lacking a ``tenant_id`` claim,
  is rejected with **401** and no context is produced (Req 2.6, 3.2, 5.3).
* ``"dual"`` — prefer a SuperTokens session; when no SuperTokens session is
  present on the request, fall back to verifying a legacy JWT. A *present but
  invalid* SuperTokens session is rejected rather than silently downgraded.
* ``"legacy"`` (and any unrecognized value, e.g. a test ``MagicMock``) — the
  pre-migration legacy JWT path, unchanged (rejects with 403).

In every branch identity (``user_id`` / ``tenant_id`` / ``roles`` /
``has_pii_access``) comes only from the verified token; any ``tenant_id`` from
a query param, header, path, or body is ignored for scoping (Req 5.1, 5.2). The
context continues to hydrate ``region`` / ``measurement_units`` from
``TenantSettingsService`` exactly as before.

Validates: Requirements 2.6, 3.1, 3.2, 3.3, 3.5, 5.1, 5.3, 5.4, 9.1, 9.2, 9.6
"""

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, Tuple, runtime_checkable

from fastapi import Request
from jose import JWTError, jwt

from config.settings import get_settings
from errors.exceptions import forbidden, unauthorized
from services.tenant_settings import (
    TenantSettings,
    TenantSettingsService,
    default_measurement_units_for_region,
    default_tenant_settings,
)

logger = logging.getLogger(__name__)

request_tenant_id_var: ContextVar[str] = ContextVar(
    "request_tenant_id", default=""
)

# Module-level tenant settings service — configured once at bootstrap via
# ``configure_tenant_guard``. Remains None in tests that never wire it, in
# which case the middleware falls back to in-process US/imperial defaults so
# no request path breaks due to missing infrastructure.
_tenant_settings_service: Optional[TenantSettingsService] = None


def configure_tenant_guard(
    tenant_settings_service: Optional[TenantSettingsService],
) -> None:
    """Register the tenant settings service used to hydrate TenantContext.

    Called from the bootstrap layer once Redis is available. Passing ``None``
    resets the wiring (useful for tests that need an isolated baseline).

    Validates: Requirement 6.1.5, 6.3.1.
    """
    global _tenant_settings_service
    _tenant_settings_service = tenant_settings_service


def get_tenant_settings_service() -> Optional[TenantSettingsService]:
    """Return the currently wired :class:`TenantSettingsService`, or ``None``."""
    return _tenant_settings_service


@dataclass
class TenantContext:
    """Verified tenant identity for a request (the requirements' ``Auth_Context``).

    The identity fields (``tenant_id``, ``user_id``, ``has_pii_access``,
    ``roles``) are derived exclusively from the verified session — a SuperTokens
    access-token payload under the SuperTokens paths, or the legacy JWT claims
    under the legacy path. The remaining fields (``region`` and
    ``measurement_units``) come from the tenant settings service and default to
    the platform's US/imperial values when a tenant has no explicit record. The
    tenant guard populates them on every request so downstream handlers can
    render volumes and distances in the tenant's preferred units without
    re-querying the settings store.

    This shape is deliberately **unchanged** by the SuperTokens migration: it is
    the integration seam that lets ``get_tenant_context`` be re-implemented
    without touching the handlers that depend on it.
    """

    tenant_id: str
    user_id: str
    has_pii_access: bool
    roles: list[str] = field(default_factory=list)
    region: str = "US"
    measurement_units: Dict[str, str] = field(
        default_factory=lambda: default_measurement_units_for_region("US").to_dict()
    )


# ---------------------------------------------------------------------------
# Session_Verifier seam
# ---------------------------------------------------------------------------


@dataclass
class VerifiedSession:
    """The verified identity produced by a :class:`SessionVerifier`.

    ``user_id`` is the SuperTokens user id (``session.get_user_id()``) and
    ``claims`` is the verified, server-signed access-token payload carrying
    ``tenant_id`` / ``roles`` / ``has_pii_access`` (and ``driver_id`` for driver
    users). The client cannot assert or mutate these — they are signed by the
    managed core (Req 3.3).
    """

    user_id: str
    claims: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SessionVerifier(Protocol):
    """Protocol seam for verifying a SuperTokens session on a request.

    The default implementation (:class:`_SuperTokensSessionVerifier`) delegates
    to the SuperTokens SDK. Tests inject a fake via
    :func:`configure_session_verifier` so the Session_Verifier branching can be
    exercised without a live managed core.

    Contract:
      * Return a :class:`VerifiedSession` when the request carries a valid
        SuperTokens session.
      * Return ``None`` when the request carries **no** SuperTokens session
        (so the ``dual`` path can fall back to a legacy JWT).
      * Raise an authentication error (``AppException`` / HTTP 401) when a
        session is present but invalid, expired, or revoked (Req 2.4, 2.6) —
        such a session must never be silently downgraded.
    """

    async def verify(self, request: Request) -> Optional[VerifiedSession]:
        ...


class _SuperTokensSessionVerifier:
    """Default :class:`SessionVerifier` backed by the SuperTokens SDK.

    Imports the SDK lazily so simply importing this module never forces the
    SuperTokens dependency to load or a managed-core call to occur.
    """

    async def verify(self, request: Request) -> Optional[VerifiedSession]:
        # Lazy import: keep module import side-effect free and avoid pulling in
        # the SDK in environments/tests that never use the SuperTokens paths.
        from supertokens_python.recipe.session.asyncio import get_session
        from supertokens_python.recipe.session.exceptions import (
            SuperTokensSessionError,
        )

        try:
            # session_required=False: returns None when no session token is
            # present, raises when a token is present but unverifiable.
            session = await get_session(request, session_required=False)
        except SuperTokensSessionError as exc:
            # A session was presented but failed verification (expired, revoked,
            # token theft, ...). Reject — do not fall back (Req 2.4, 2.6).
            logger.debug("SuperTokens session verification failed: %s", exc)
            raise unauthorized(
                message="Invalid or expired session",
                details={"reason": "SuperTokens session verification failed"},
            ) from exc

        if session is None:
            return None

        return VerifiedSession(
            user_id=session.get_user_id(),
            claims=dict(session.get_access_token_payload() or {}),
        )


# Module-level verifier seam. ``None`` means "use the default SDK-backed
# verifier"; tests install a fake via ``configure_session_verifier``.
_session_verifier: Optional[SessionVerifier] = None


def configure_session_verifier(verifier: Optional[SessionVerifier]) -> None:
    """Install the :class:`SessionVerifier` used by the SuperTokens paths.

    Passing ``None`` resets to the default SDK-backed verifier. This is the
    seam tests use to exercise the ``supertokens`` / ``dual`` branching without
    a live managed core.
    """
    global _session_verifier
    _session_verifier = verifier


def _get_session_verifier() -> SessionVerifier:
    """Return the configured verifier, constructing the default if unset."""
    if _session_verifier is not None:
        return _session_verifier
    return _SuperTokensSessionVerifier()


# ---------------------------------------------------------------------------
# Tenant settings hydration (unchanged)
# ---------------------------------------------------------------------------


async def _load_tenant_settings(tenant_id: str) -> TenantSettings:
    """Fetch tenant settings via the configured service, with safe defaults.

    Returns the default ``TenantSettings`` whenever the service is missing or
    a lookup fails, so a flaky settings backend never locks users out.
    """
    service = _tenant_settings_service
    if service is None:
        return default_tenant_settings()
    try:
        return await service.get(tenant_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Tenant guard: settings lookup failed for tenant=%s: %s — using defaults",
            tenant_id,
            exc,
        )
        return default_tenant_settings()


def _build_context(
    *,
    tenant_id: str,
    user_id: str,
    has_pii_access: bool,
    roles: list[str],
    settings: TenantSettings,
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        has_pii_access=has_pii_access,
        roles=roles,
        region=settings.region,
        measurement_units=settings.measurement_units.to_dict(),
    )


# ---------------------------------------------------------------------------
# get_tenant_context — the Session_Verifier dependency
# ---------------------------------------------------------------------------


async def get_tenant_context(request: Request) -> TenantContext:
    """FastAPI dependency producing a verified :class:`TenantContext`.

    Branches on the ``auth_provider`` Migration_Controller flag:

      * ``"supertokens"`` — verify a SuperTokens session only (Req 3.1, 3.4).
      * ``"dual"`` — prefer a SuperTokens session; fall back to the legacy JWT
        only when no SuperTokens session is present on the request.
      * ``"legacy"`` / anything else — the legacy JWT path (unchanged).

    All branches yield the **same** ``TenantContext`` shape and then hydrate
    ``region`` / ``measurement_units`` from ``TenantSettingsService`` (Req 5.4).
    Identity is derived exclusively from the verified token; any ``tenant_id``
    supplied via query/header/path/body is ignored for scoping (Req 5.1, 5.2).

    Validates: Requirements 2.6, 3.1, 3.2, 3.3, 3.5, 5.1, 5.3, 5.4
    """
    settings_obj = get_settings()
    # Default to the legacy path for any value that is not explicitly one of the
    # SuperTokens modes. This keeps backward compatibility with code/tests that
    # provide settings without a real ``auth_provider`` string.
    provider = getattr(settings_obj, "auth_provider", "legacy")

    if provider == "supertokens":
        # Hard-cutover path (Req 3.4): verify a SuperTokens session ONLY and
        # build the context from its signed claims. This branch must NEVER reach
        # ``_legacy_context`` / the ``jwt_secret`` shared-secret decode — a
        # request that carries only a legacy HS256 token (and no valid
        # SuperTokens session) is rejected with 401 by the ``required=True``
        # verification below, not silently accepted via the legacy path.
        user_id, claims = await _verify_supertokens_session(request, required=True)
        return await _context_from_session_claims(user_id, claims)

    if provider == "dual":
        verified = await _verify_supertokens_session(request, required=False)
        if verified is not None:
            user_id, claims = verified
            return await _context_from_session_claims(user_id, claims)
        # No SuperTokens session present — fall back to the legacy JWT path so
        # existing clients (and the test suite minting dev JWTs) keep working
        # through the transition.
        return await _legacy_context(request, settings_obj)

    # "legacy" (and any unrecognized value): pre-migration behavior.
    return await _legacy_context(request, settings_obj)


async def _verify_supertokens_session(
    request: Request, *, required: bool
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Verify the request's SuperTokens session via the Session_Verifier seam.

    Returns ``(user_id, claims)`` for a valid session. When no session is
    present: returns ``None`` if ``required`` is False (lets ``dual`` fall back
    to the legacy path), or raises 401 if ``required`` is True (the
    ``supertokens`` hard-cutover path) (Req 2.6, 3.1, 3.2). A present-but-invalid
    session always raises (handled inside the verifier).
    """
    verifier = _get_session_verifier()
    verified = await verifier.verify(request)
    if verified is None:
        if required:
            raise unauthorized(
                message="Authentication required",
                details={"reason": "No valid SuperTokens session on the request"},
            )
        return None
    return verified.user_id, dict(verified.claims or {})


async def _context_from_session_claims(
    user_id: Optional[str], claims: Dict[str, Any]
) -> TenantContext:
    """Build a ``TenantContext`` from verified SuperTokens session claims.

    Derives ``tenant_id`` / ``roles`` / ``has_pii_access`` exclusively from the
    signed access-token payload (Req 3.3, 3.5). A session lacking a ``tenant_id``
    claim is rejected with 401 and no context is produced (Req 5.3).
    """
    tenant_id = claims.get("tenant_id")
    if not tenant_id or not isinstance(tenant_id, str):
        logger.debug("SuperTokens session rejected: missing tenant_id claim")
        raise unauthorized(
            message="Missing tenant_id in session",
            details={"reason": "Verified session must carry a tenant_id claim"},
        )

    request_tenant_id_var.set(tenant_id)
    resolved_user_id = user_id if user_id else "unknown"
    has_pii_access = bool(claims.get("has_pii_access", False))
    roles = [r for r in (claims.get("roles") or []) if isinstance(r, str)]

    tenant_settings = await _load_tenant_settings(tenant_id)

    logger.debug(
        "Tenant scope enforced (supertokens): tenant_id=%s user_id=%s region=%s",
        tenant_id,
        resolved_user_id,
        tenant_settings.region,
    )

    return _build_context(
        tenant_id=tenant_id,
        user_id=resolved_user_id,
        has_pii_access=has_pii_access,
        roles=roles,
        settings=tenant_settings,
    )


async def _legacy_context(request: Request, settings_obj: Any) -> TenantContext:
    """Legacy homegrown-JWT verification path (pre-migration behavior).

    Extracts ``tenant_id`` exclusively from the signed legacy JWT ``tenant_id``
    claim, rejecting (403) when the token is missing/invalid or the claim is
    absent, and ignoring any ``tenant_id`` supplied via query/header. Retained
    intact for the ``legacy`` and ``dual`` (fallback) branches.

    Validates: Requirements 9.1, 9.6, 9.8, 6.1.5, 6.3.1
    """
    # Extract the Authorization header
    auth_header: str | None = request.headers.get("Authorization")

    if not auth_header or not auth_header.startswith("Bearer "):
        logger.debug(
            "Tenant guard rejected request: missing or malformed Authorization header "
            "for %s %s",
            request.method,
            request.url.path,
        )
        raise forbidden(
            message="Missing or invalid authentication token",
            details={"reason": "Authorization header with Bearer token is required"},
        )

    token = auth_header.removeprefix("Bearer ").strip()
    if not token:
        raise forbidden(
            message="Missing or invalid authentication token",
            details={"reason": "Bearer token is empty"},
        )

    # Decode and verify the JWT
    try:
        payload = jwt.decode(
            token,
            settings_obj.jwt_secret,
            algorithms=[settings_obj.jwt_algorithm],
        )
    except JWTError as exc:
        logger.debug("Tenant guard JWT verification failed: %s", exc)
        raise forbidden(
            message="Invalid authentication token",
            details={"reason": "JWT verification failed"},
        )

    # Extract tenant_id — must be present in the signed claims
    tenant_id: str | None = payload.get("tenant_id")
    if not tenant_id:
        logger.debug(
            "Tenant guard rejected request: JWT missing tenant_id claim for %s %s",
            request.method,
            request.url.path,
        )
        raise forbidden(
            message="Missing tenant_id in authentication token",
            details={"reason": "JWT must contain a tenant_id claim"},
        )

    request_tenant_id_var.set(tenant_id)
    user_id: str = payload.get("sub", payload.get("user_id", "unknown"))
    has_pii_access: bool = payload.get("has_pii_access", False)
    roles: list[str] = payload.get("roles", [])

    tenant_settings = await _load_tenant_settings(tenant_id)

    logger.debug(
        "Tenant scope enforced: tenant_id=%s user_id=%s region=%s endpoint=%s %s",
        tenant_id,
        user_id,
        tenant_settings.region,
        request.method,
        request.url.path,
    )

    return _build_context(
        tenant_id=tenant_id,
        user_id=user_id,
        has_pii_access=has_pii_access,
        roles=roles,
        settings=tenant_settings,
    )


def inject_tenant_filter(query: dict, tenant_id: str) -> dict:
    """
    Wrap any Elasticsearch query with a bool filter on ``tenant_id``.

    Applied to all read endpoints (shipments, riders, events, metrics)
    to enforce tenant-scoped data isolation.

    Validates: Requirements 9.2, 9.4
    """
    return {
        "query": {
            "bool": {
                "must": [query.get("query", {"match_all": {}})],
                "filter": [{"term": {"tenant_id": tenant_id}}],
            }
        }
    }
