"""
Mobile_Session router — sign-in, refresh, and sign-out for ``Driver_App``.

Mounted at prefix ``/auth/driver``, which ``is_public_route``
(``middleware/auth_enforcement.py``) already covers through its ``/auth``
prefix, so these three paths verify a credential **in-handler** rather than
presupposing a verified session — the same category as ``/auth/signin``. No
edit to ``auth_enforcement.py`` is required and no ``/api/driver/*`` path
becomes public.

Why this router exists at all, when the SuperTokens SDK already serves
``/auth/signin``:

* **R1.1** requires the access token *and* the refresh token in the response
  **body** as well as in the response headers. The SDK returns them only as
  ``st-access-token`` / ``st-refresh-token`` / ``front-token`` headers, which an
  Expo *web* build cannot read unless every one is in CORS ``expose_headers``.
  Both are emitted here so the contract is explicit.
* **R1.15** requires sign-in itself to fail with ``DRIVER_RECORD_NOT_PROVISIONED``
  when a ``driver``-role user has no ``drivers_current`` record. ``/auth/signin``
  has no knowledge of ``drivers_current``.

The transport is SuperTokens' own header-based session, not a parallel token
scheme: ``session.init`` does not override ``get_token_transfer_method``, so the
SDK default accepts an ``Authorization: Bearer`` access token on verification
and the web app keeps its cookies untouched.

Every rejection on this surface is an ``AppException`` from
``errors/exceptions.py`` — this module raises **zero** raw ``HTTPException``
(R15.10), and no handler or log statement here ever emits a token value
(R15.1).

Validates: Requirements 1.1, 1.8, 1.9, 1.10, 1.15, 15.10
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from supertokens_python.recipe.emailpassword.asyncio import (
    sign_in as emailpassword_sign_in,
)
from supertokens_python.recipe.emailpassword.interfaces import SignInOkResult
from supertokens_python.recipe.session.asyncio import (
    create_new_session_without_request_response,
    get_session_without_request_response,
    refresh_session_without_request_response,
)
from supertokens_python.recipe.session.exceptions import (
    SuperTokensSessionError,
    TryRefreshTokenError,
)

from auth.supertokens_init import (
    _lookup_auth_user_claims,
    configured_session_lifetime_seconds,
)
from errors.exceptions import (
    driver_record_not_provisioned,
    insufficient_role,
    internal_error,
    session_expired,
    unauthorized,
)

logger = logging.getLogger(__name__)

#: The SuperTokens tenant the platform signs in against. Multi-tenancy in this
#: deployment is a Runsheet ``tenant_id`` claim, not a SuperTokens tenant.
_SUPERTOKENS_TENANT_ID = "public"

#: The role a Mobile_Session holder must carry (R1.5).
_DRIVER_ROLE = "driver"

router = APIRouter(prefix="/auth/driver", tags=["driver-session"])

# Module-level collaborators, wired via configure_session_endpoints().
_driver_repository: Optional[Any] = None
_es_service: Optional[Any] = None


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_session_endpoints(
    *,
    driver_repository: Any = None,
    es_service: Any = None,
) -> None:
    """Wire the ``drivers_current`` read used by the R1.15 provisioning check.

    ``driver_repository`` is preferred; when only ``es_service`` is supplied a
    :class:`fuel.driver_repository.DriverRepository` is constructed over it so
    the sign-in gate never silently degrades to "no check".
    """
    global _driver_repository, _es_service
    _es_service = es_service
    if driver_repository is not None:
        _driver_repository = driver_repository
        return
    if es_service is not None:
        from fuel.driver_repository import DriverRepository

        _driver_repository = DriverRepository(es_service)
        return
    _driver_repository = None


async def lookup_auth_user_claims(email: str) -> Dict[str, Any]:
    """Read the server-set session claims for ``email`` from ``auth_users``.

    Thin wrapper over the same read the ``create_new_session`` override
    performs (``auth/supertokens_init.py``), so sign-in and session creation
    cannot disagree about a user's ``tenant_id`` / ``roles`` / ``driver_id``.
    """
    return await _lookup_auth_user_claims(email)


def _get_driver_repository() -> Any:
    """Return the wired driver repository, failing closed when absent."""
    if _driver_repository is None:
        logger.error(
            "Driver session endpoints not configured — refusing to issue a "
            "Mobile_Session without the drivers_current provisioning check."
        )
        raise internal_error(
            message="Driver sign-in is temporarily unavailable",
            details={"reason": "session_endpoints_not_configured"},
        )
    return _driver_repository


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DriverSignInRequest(BaseModel):
    """Body for ``POST /auth/driver/session``."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=256)
    device_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description=(
            "Optional device identifier. Accepted so the app can send one "
            "payload on sign-in; the device record itself is written by the "
            "Device_Registry endpoint."
        ),
    )

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, value: str) -> str:
        """Lightweight shape check; credential verification is the real gate."""
        candidate = value.strip()
        if "@" not in candidate or candidate.startswith("@") or candidate.endswith("@"):
            raise ValueError("A valid email address is required")
        return candidate


class DriverRefreshRequest(BaseModel):
    """Body for ``POST /auth/driver/session/refresh``."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(..., min_length=1)


class DriverSessionResponse(BaseModel):
    """Both Mobile_Session tokens plus the identity they are scoped to (R1.1)."""

    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    driver_id: str
    tenant_id: str


class DriverSignOutResponse(BaseModel):
    """Result of ``DELETE /auth/driver/session``."""

    revoked: bool


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


def _bearer_access_token(request: Request) -> Optional[str]:
    """Extract the access token from ``Authorization`` or ``st-access-token``.

    Header mode is what ``Driver_App`` uses; the ``st-access-token`` fallback
    matches the header the SDK itself sets on a response. The value is never
    logged (R15.1).
    """
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        candidate = header[7:].strip()
        if candidate:
            return candidate
    candidate = (request.headers.get("st-access-token") or "").strip()
    return candidate or None


async def verified_driver_session(request: Request):
    """Exception-translating dependency producing a verified session handle.

    The SuperTokens SDK signals an **expired** access token with
    ``TryRefreshTokenError`` and every other verification failure (revoked,
    malformed, unknown) with another ``SuperTokensSessionError``. This
    dependency re-maps the first to 401 ``SESSION_EXPIRED`` and the second to
    401 ``UNAUTHORIZED``, so ``Driver_App`` can tell "refresh me" from "sign in
    again". The decision is taken from the access token alone — the validity of
    any accompanying refresh token is irrelevant to it (R1.8).
    """
    access_token = _bearer_access_token(request)
    if not access_token:
        raise unauthorized(
            message="Authentication required",
            details={"reason": "missing_access_token"},
        )

    try:
        session = await get_session_without_request_response(
            access_token,
            anti_csrf_check=False,
            session_required=True,
        )
    except TryRefreshTokenError as exc:
        # Structurally valid but no longer usable → the app should refresh.
        raise session_expired(details={"reason": "access_token_expired"}) from exc
    except SuperTokensSessionError as exc:
        # Revoked, stolen, malformed, or not a SuperTokens token at all.
        raise unauthorized(
            message="Invalid or expired session",
            details={"reason": "session_verification_failed"},
        ) from exc

    if session is None:  # pragma: no cover - session_required=True guarantees one
        raise unauthorized(
            message="Authentication required",
            details={"reason": "no_session_on_request"},
        )
    return session


def _mirror_session_headers(response: Response, tokens: Dict[str, Any]) -> None:
    """Mirror the SDK's session headers so a header-mode client works either way.

    ``get_all_session_tokens_dangerously()`` returns ``accessToken``,
    ``refreshToken``, and ``frontToken``; the refresh token is absent for a
    session created in a transfer method that does not carry one.
    """
    access_token = tokens.get("accessToken") or ""
    refresh_token = tokens.get("refreshToken") or ""
    front_token = tokens.get("frontToken") or ""
    if access_token:
        response.headers["st-access-token"] = access_token
    if refresh_token:
        response.headers["st-refresh-token"] = refresh_token
    if front_token:
        response.headers["front-token"] = front_token


def _session_response(
    response: Response, tokens: Dict[str, Any], claims: Dict[str, Any]
) -> DriverSessionResponse:
    """Build the body + headers pair that satisfies R1.1."""
    _mirror_session_headers(response, tokens)
    return DriverSessionResponse(
        access_token=tokens.get("accessToken") or "",
        refresh_token=tokens.get("refreshToken") or "",
        expires_in=configured_session_lifetime_seconds(),
        driver_id=str(claims.get("driver_id") or ""),
        tenant_id=str(claims.get("tenant_id") or ""),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/session", response_model=DriverSessionResponse)
async def create_driver_session(
    body: DriverSignInRequest,
    response: Response,
) -> DriverSessionResponse:
    """Sign a driver in and return both Mobile_Session tokens.

    Four outcomes, in order: bad credential → 401 ``UNAUTHORIZED``; no
    ``driver`` role → 403 ``INSUFFICIENT_ROLE``; no ``drivers_current`` record
    → 403 ``DRIVER_RECORD_NOT_PROVISIONED``; success → both tokens in the body
    **and** in the ``st-*`` headers.

    Validates: Requirements 1.1, 1.15, 15.10
    """
    # 1. Verify the credential through the EmailPassword recipe.
    result = await emailpassword_sign_in(
        _SUPERTOKENS_TENANT_ID, body.email, body.password
    )
    if not isinstance(result, SignInOkResult):
        # Uniform rejection: never distinguish "unknown email" from "wrong
        # password", and never echo the submitted credential.
        raise unauthorized(
            message="Invalid credentials",
            details={"reason": "credential_verification_failed"},
        )

    # 2. Resolve the server-set claims from auth_users — the same read the
    #    create_new_session override performs.
    claims = await lookup_auth_user_claims(body.email)
    roles = [r for r in (claims.get("roles") or []) if isinstance(r, str)]
    if _DRIVER_ROLE not in roles:
        # R15.14: echo only the required role, never the caller's held roles.
        raise insufficient_role(
            message="Caller lacks a required role for this operation",
            details={"required_roles": [_DRIVER_ROLE]},
        )

    tenant_id = claims.get("tenant_id")
    driver_id = claims.get("driver_id")

    # 3. R1.15 — a driver-role user with no drivers_current record cannot sign
    #    in. A missing tenant claim is the same failure: without it there is no
    #    tenant to scope the drivers_current read to.
    if not tenant_id or not driver_id:
        raise driver_record_not_provisioned(
            details={"reason": "no_drivers_current_record"}
        )

    repository = _get_driver_repository()
    if await repository.get(tenant_id, driver_id) is None:
        raise driver_record_not_provisioned(
            details={"reason": "no_drivers_current_record"}
        )

    # 4. Mint the session out-of-band so the token values are in hand and can
    #    be placed in the body as well as the headers.
    session = await create_new_session_without_request_response(
        _SUPERTOKENS_TENANT_ID,
        result.recipe_user_id,
        access_token_payload=dict(claims),
    )
    tokens = dict(session.get_all_session_tokens_dangerously())

    logger.info(
        "Mobile_Session issued for tenant=%s driver=%s", tenant_id, driver_id
    )
    return _session_response(response, tokens, claims)


@router.post("/session/refresh", response_model=DriverSessionResponse)
async def refresh_driver_session(
    body: DriverRefreshRequest,
    response: Response,
) -> DriverSessionResponse:
    """Rotate a Mobile_Session, returning a replacement access **and** refresh token.

    SuperTokens rotates the refresh token on every refresh, so both halves of
    the pair in the response are new (R1.9).

    Validates: Requirements 1.9, 15.10
    """
    try:
        session = await refresh_session_without_request_response(
            body.refresh_token,
            disable_anti_csrf=True,
        )
    except SuperTokensSessionError as exc:
        # Expired, revoked, or stolen refresh token — the app must sign in
        # again. The value itself is never logged (R15.1).
        logger.info("Mobile_Session refresh rejected: %s", type(exc).__name__)
        raise unauthorized(
            message="Invalid or expired refresh token",
            details={"reason": "refresh_token_verification_failed"},
        ) from exc

    tokens = dict(session.get_all_session_tokens_dangerously())
    claims = dict(session.get_access_token_payload() or {})
    return _session_response(response, tokens, claims)


@router.delete("/session", response_model=DriverSignOutResponse)
async def revoke_driver_session(
    session=Depends(verified_driver_session),
) -> DriverSignOutResponse:
    """Sign out: revoke the Mobile_Session at the session store.

    Revocation happens at the managed core, so every subsequent request bearing
    the revoked token fails verification with 401 (R1.10).

    Validates: Requirements 1.10, 15.10
    """
    await session.revoke_session()
    logger.info("Mobile_Session revoked for session handle (value not logged)")
    return DriverSignOutResponse(revoked=True)


__all__ = [
    "router",
    "configure_session_endpoints",
    "lookup_auth_user_claims",
    "verified_driver_session",
    "DriverSignInRequest",
    "DriverRefreshRequest",
    "DriverSessionResponse",
    "DriverSignOutResponse",
]
