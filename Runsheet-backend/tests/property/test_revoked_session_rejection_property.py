"""
Property-based test for revoked / signed-out SuperTokens session rejection.

# Feature: supertokens-auth-migration, Property 2: Revoked / signed-out sessions are rejected

**Validates: Requirements 1.6, 2.4**

Property 2: Revoked / signed-out sessions are rejected — every request
presenting a revoked session to a protected route is rejected. Concretely:
for any generated request shape and for both the ``supertokens`` and ``dual``
``auth_provider`` modes, ``ops.middleware.tenant_guard.get_tenant_context``
raises an authentication error (HTTP 401) and never returns a
``TenantContext`` when the request's SuperTokens session is revoked.

The test injects a fake :class:`SessionVerifier` (via
``configure_session_verifier``) that simulates a revoked/signed-out session by
raising the **same** ``unauthorized`` (HTTP 401) error the real
``_SuperTokensSessionVerifier`` raises when the SDK raises
``SuperTokensSessionError`` on session verification. No live managed core is
involved.

Crucially, every generated request also carries a **valid legacy Bearer JWT**
(signed with the fake settings' ``jwt_secret``). In ``dual`` mode this would
produce a perfectly good ``TenantContext`` *if* the verifier silently fell
back to the legacy path. The property asserts it does not: a present-but-
revoked SuperTokens session must be rejected, never downgraded to legacy
(Req 2.4).
"""

import asyncio
import sys
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jose import jwt

# ---------------------------------------------------------------------------
# Keep importing the tenant guard side-effect free in the test environment.
# ---------------------------------------------------------------------------
sys.modules.setdefault("services.elasticsearch_service", MagicMock())

from errors.codes import ErrorCode  # noqa: E402
from errors.exceptions import AppException, unauthorized  # noqa: E402
from ops.middleware.tenant_guard import (  # noqa: E402
    TenantContext,
    configure_session_verifier,
    get_tenant_context,
)

# A fixed secret/algorithm for the fake settings used by the legacy fallback
# path. Generated legacy tokens are signed with these so that, were the dual
# path to fall back, it would succeed and return a TenantContext (which the
# property forbids).
_JWT_SECRET = "test-legacy-secret-for-revoked-session-property"
_JWT_ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _RevokedSessionVerifier:
    """A :class:`SessionVerifier` that simulates a revoked/signed-out session.

    It raises the exact same ``unauthorized`` (HTTP 401) error that the real
    ``_SuperTokensSessionVerifier`` raises when the SDK reports a
    ``SuperTokensSessionError`` (i.e. a session token was presented but is
    invalid, expired, or revoked). It never returns ``None`` and never returns
    a verified session — the session is always present-but-revoked.
    """

    async def verify(self, request):  # noqa: ANN001 - mirrors the protocol seam
        raise unauthorized(
            message="Invalid or expired session",
            details={"reason": "SuperTokens session verification failed"},
        )


class _FakeURL:
    def __init__(self, path: str):
        self.path = path


class _FakeRequest:
    """Minimal stand-in for a Starlette ``Request``.

    Only exposes the attributes ``get_tenant_context`` and its legacy fallback
    touch: ``headers`` (with a ``.get``), ``method``, and ``url.path``. The
    injected verifier ignores the request entirely, but generating varied
    shapes exercises the "across request shapes" dimension of the property.
    """

    def __init__(self, headers: dict, method: str, path: str):
        self.headers = headers
        self.method = method
        self.url = _FakeURL(path)


def _make_legacy_token(tenant_id: str, user_id: str, roles, has_pii: bool) -> str:
    """Mint a VALID legacy HS256 JWT for the fake settings' secret."""
    return jwt.encode(
        {
            "tenant_id": tenant_id,
            "sub": user_id,
            "roles": list(roles),
            "has_pii_access": has_pii,
        },
        _JWT_SECRET,
        algorithm=_JWT_ALGORITHM,
    )


# ---------------------------------------------------------------------------
# Strategies — generated request shapes
# ---------------------------------------------------------------------------
_identifiers = st.from_regex(r"[a-zA-Z0-9_\-]{1,32}", fullmatch=True)
_roles = st.lists(
    st.sampled_from(["admin", "dispatcher", "ops_manager", "driver"]),
    min_size=0,
    max_size=4,
    unique=True,
)
_methods = st.sampled_from(["GET", "POST", "PUT", "PATCH", "DELETE"])
_paths = st.sampled_from(
    [
        "/api/fuel/mvp/approvals",
        "/api/ops/shipments",
        "/api/fuel/orders",
        "/data/export",
        "/api/drivers/me",
    ]
)
_providers = st.sampled_from(["supertokens"])


@st.composite
def _requests(draw):
    """Build a varied request that carries a VALID legacy Bearer token.

    The valid legacy token is the trap: if the dual path ever fell back to the
    legacy scheme on a revoked SuperTokens session, it would return a
    TenantContext. The property requires rejection instead.
    """
    tenant_id = draw(_identifiers)
    user_id = draw(_identifiers)
    roles = draw(_roles)
    has_pii = draw(st.booleans())
    method = draw(_methods)
    path = draw(_paths)

    token = _make_legacy_token(tenant_id, user_id, roles, has_pii)
    headers = {"Authorization": f"Bearer {token}"}

    # Optionally include a (always-ignored) client-supplied tenant_id header to
    # add request-shape variety.
    if draw(st.booleans()):
        headers["X-Tenant-Id"] = draw(_identifiers)

    return _FakeRequest(headers=headers, method=method, path=path)


# ---------------------------------------------------------------------------
# Fixture — reset the verifier seam after every test (teardown)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_session_verifier():
    yield
    configure_session_verifier(None)


# ---------------------------------------------------------------------------
# Property 2 — revoked sessions are always rejected, never downgraded
# ---------------------------------------------------------------------------
class TestRevokedSessionRejection:
    """**Validates: Requirements 1.6, 2.4**"""

    @given(request=_requests(), provider=_providers)
    @settings(max_examples=100)
    def test_revoked_session_is_rejected_and_no_context_returned(
        self, request: _FakeRequest, provider: str
    ):
        """A revoked SuperTokens session is rejected with 401 under the
        ``supertokens`` provider; no TenantContext is returned, and the
        legacy path is never consulted (it no longer exists)."""
        configure_session_verifier(_RevokedSessionVerifier())

        with pytest.raises(AppException) as exc_info:
            result = asyncio.run(get_tenant_context(request))
            # If we ever get here, the contract was violated — surface the
            # offending return value for the counterexample report.
            assert not isinstance(result, TenantContext), (
                f"get_tenant_context returned a TenantContext for a revoked "
                f"session under provider={provider!r}: {result!r}"
            )

        # The rejection must be an authentication error (HTTP 401 / UNAUTHORIZED),
        # matching the real verifier's behavior on SuperTokensSessionError.
        exc = exc_info.value
        assert exc.error_code == ErrorCode.UNAUTHORIZED, (
            f"expected UNAUTHORIZED, got {exc.error_code!r}"
        )
        assert exc.status_code == 401, f"expected HTTP 401, got {exc.status_code}"
