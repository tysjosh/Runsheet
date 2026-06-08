"""
Tenant Guard / Session_Verifier for the Runsheet backend.

This module is the central **Session_Verifier** seam: ``get_tenant_context``
derives tenant identity exclusively from a verified SuperTokens session's
signed access-token payload. The homegrown HS256 JWT scheme (and the
``legacy`` / ``dual`` ``auth_provider`` paths that used it) was removed once
the SuperTokens hard cutover completed.

The critical architectural property is that **the returned ``TenantContext``
shape and the ``get_tenant_context`` signature are unchanged**, so the dozens of
``Depends(get_tenant_context)`` handlers across the app do not change.

``get_tenant_context`` always verifies a SuperTokens session only (Req 3.1,
3.4). A request with no/invalid session, or a session lacking a ``tenant_id``
claim, is rejected with **401** and no context is produced (Req 2.6, 3.2, 5.3).

Identity (``user_id`` / ``tenant_id`` / ``roles`` / ``has_pii_access``) comes
only from the verified token; any ``tenant_id`` from a query param, header,
path, or body is ignored for scoping (Req 5.1, 5.2). The context continues to
hydrate ``region`` / ``measurement_units`` from ``TenantSettingsService``
exactly as before.

Validates: Requirements 2.6, 3.1, 3.2, 3.3, 3.5, 5.1, 5.3, 5.4, 9.1, 9.6
"""

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, Tuple, runtime_checkable

from fastapi import Request

from errors.exceptions import unauthorized
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
    ``roles``) are derived exclusively from the verified SuperTokens
    access-token payload. The remaining fields (``region`` and
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
      * Return ``None`` when the request carries **no** SuperTokens session.
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
    seam tests use to exercise SuperTokens session verification without
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

    Always verifies a SuperTokens session only (Req 3.1, 3.4) and builds the
    context from its signed claims. A request with no/invalid session, or a
    session lacking a ``tenant_id`` claim, is rejected with **401** and no
    context is produced. The context then hydrates ``region`` /
    ``measurement_units`` from ``TenantSettingsService`` (Req 5.4). Identity is
    derived exclusively from the verified token; any ``tenant_id`` supplied via
    query/header/path/body is ignored for scoping (Req 5.1, 5.2).

    Validates: Requirements 2.6, 3.1, 3.2, 3.3, 3.5, 5.1, 5.3, 5.4
    """
    user_id, claims = await _verify_supertokens_session(request, required=True)
    return await _context_from_session_claims(user_id, claims)


async def _verify_supertokens_session(
    request: Request, *, required: bool
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Verify the request's SuperTokens session via the Session_Verifier seam.

    Returns ``(user_id, claims)`` for a valid session. When no session is
    present: raises 401 if ``required`` is True (the normal call path), or
    returns ``None`` if ``required`` is False (Req 2.6, 3.1, 3.2). A
    present-but-invalid session always raises (handled inside the verifier).
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


def inject_tenant_filter(query: dict, tenant_id: str) -> dict:
    """
    Wrap any Elasticsearch query with a bool filter on ``tenant_id``.

    Applied to all read endpoints (shipments, riders, events, metrics)
    to enforce tenant-scoped data isolation.

    Only the ``query`` clause is rewritten to add the tenant filter; every
    other top-level search-body key the caller supplied (``sort``, ``size``,
    ``search_after``, ``aggs``, ``_source``, ...) is preserved. Dropping
    those siblings silently breaks ordering and ``search_after`` pagination,
    so they are carried through unchanged.

    Validates: Requirements 9.2, 9.4
    """
    wrapped = {
        key: value for key, value in query.items() if key != "query"
    }
    wrapped["query"] = {
        "bool": {
            "must": [query.get("query", {"match_all": {}})],
            "filter": [{"term": {"tenant_id": tenant_id}}],
        }
    }
    return wrapped
