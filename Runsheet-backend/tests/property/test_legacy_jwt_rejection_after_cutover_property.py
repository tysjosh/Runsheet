"""
Property-based test for legacy-JWT rejection after the hard cutover.

# Feature: supertokens-auth-migration, Property 9: Legacy JWTs are rejected after hard cutover

**Validates: Requirements 3.4**

Property 9: Legacy JWTs are rejected after hard cutover — with
``auth_provider="supertokens"``, a request presenting only a legacy HS256
token (signed with ``jwt_secret``) and **no** SuperTokens session is rejected
with HTTP 401, and no ``TenantContext`` is produced.

This exercises ``ops.middleware.tenant_guard.get_tenant_context`` on the hard-
cutover (``supertokens``) branch. A fake :class:`SessionVerifier` is injected
via ``configure_session_verifier`` that returns ``None`` — i.e. the request
carries **no** SuperTokens session, exactly the situation of a stale client
still presenting a legacy Bearer JWT. The real
``_SuperTokensSessionVerifier`` returns ``None`` in the same way when no
SuperTokens session token is on the request.

Every generated request carries a **valid** legacy HS256 Bearer token (signed
with the fake settings' ``jwt_secret``). That token is the trap: were the
``supertokens`` branch to fall through to the legacy shared-secret decode, it
would mint a perfectly good ``TenantContext``. The property asserts it never
does — the legacy path must never be reached once cut over (Req 3.4), so the
request is rejected with 401 instead.
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
from errors.exceptions import AppException  # noqa: E402
from ops.middleware.tenant_guard import (  # noqa: E402
    TenantContext,
    configure_session_verifier,
    get_tenant_context,
)

# Fixed secret/algorithm for the fake settings. Generated legacy tokens are
# signed with these so that, were the supertokens branch to fall back to the
# legacy decode, it would succeed and return a TenantContext (which the
# property forbids).
_JWT_SECRET = "test-legacy-secret-for-cutover-rejection-property"
_JWT_ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _NoSuperTokensSessionVerifier:
    """A :class:`SessionVerifier` that reports NO SuperTokens session.

    Returning ``None`` mirrors the real verifier's behavior when the request
    carries no SuperTokens session token — exactly the case for a client that
    still presents only a legacy HS256 Bearer JWT. On the ``supertokens`` hard-
    cutover branch (``required=True``) this must produce a 401, never a fall-
    back to the legacy shared-secret decode.
    """

    async def verify(self, request):  # noqa: ANN001 - mirrors the protocol seam
        return None


class _FakeURL:
    def __init__(self, path: str):
        self.path = path


class _FakeRequest:
    """Minimal stand-in for a Starlette ``Request``.

    Exposes only what ``get_tenant_context`` (and the legacy fallback it must
    NOT take) touches: ``headers`` (with ``.get``), ``method``, and
    ``url.path``.
    """

    def __init__(self, headers: dict, method: str, path: str):
        self.headers = headers
        self.method = method
        self.url = _FakeURL(path)


def _make_legacy_token(tenant_id: str, user_id: str, roles, has_pii: bool) -> str:
    """Mint a VALID legacy HS256 JWT signed with the fake settings' secret."""
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
# Strategies — generated request shapes, each carrying a valid legacy token
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


@st.composite
def _requests(draw):
    """Build a varied request that carries a VALID legacy Bearer token.

    The valid legacy token is the trap: if the ``supertokens`` branch ever fell
    back to the legacy scheme, it would return a TenantContext. The property
    requires rejection instead.
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
# Property 9 — legacy JWTs are rejected after hard cutover
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 9: Legacy JWTs are rejected after hard cutover
class TestLegacyJwtRejectionAfterCutover:
    """**Validates: Requirements 3.4**"""

    @given(request=_requests())
    @settings(max_examples=100)
    def test_legacy_only_request_is_rejected_under_supertokens(
        self, request: _FakeRequest
    ):
        """Under ``auth_provider="supertokens"``, a request carrying only a
        valid legacy HS256 token (and no SuperTokens session) is rejected with
        401 and never produces a TenantContext via the legacy path."""
        configure_session_verifier(_NoSuperTokensSessionVerifier())

        with pytest.raises(AppException) as exc_info:
            result = asyncio.run(get_tenant_context(request))
            # If we ever get here, the contract was violated — surface the
            # offending return value for the counterexample report.
            assert not isinstance(result, TenantContext), (
                "get_tenant_context returned a TenantContext for a legacy-"
                f"only request under auth_provider='supertokens': {result!r}"
            )

        # The rejection must be an authentication error (HTTP 401 / UNAUTHORIZED).
        # A 403 (the legacy path's `forbidden`) would mean the legacy decode was
        # reached — exactly what Req 3.4 forbids after cutover.
        exc = exc_info.value
        assert exc.error_code == ErrorCode.UNAUTHORIZED, (
            f"expected UNAUTHORIZED (401), got {exc.error_code!r} — the legacy "
            f"shared-secret path must never be reached under 'supertokens'"
        )
        assert exc.status_code == 401, f"expected HTTP 401, got {exc.status_code}"
