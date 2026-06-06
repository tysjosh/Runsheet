"""
Property-based test for claim-sourced identity in the Session_Verifier.

# Feature: supertokens-auth-migration, Property 3: Identity is derived exclusively from verified session claims

**Validates: Requirements 3.3, 3.5, 5.1, 5.3, 7.3**

Property 3: When ``auth_provider`` is ``"supertokens"``, ``get_tenant_context``
derives the request's identity **exclusively** from the verified SuperTokens
session's signed access-token payload. The resulting ``TenantContext``'s
``tenant_id`` / ``user_id`` / ``roles`` / ``has_pii_access`` equal the verified
claims regardless of any conflicting ``tenant_id`` supplied via the request's
query string, headers, or body (Req 3.3, 3.5, 5.1). A verified session whose
claims lack a ``tenant_id`` is rejected with a 401 and produces no context
(Req 5.3).

The test injects a fake :class:`SessionVerifier` via
``configure_session_verifier`` so the verifier branch is exercised without a
live managed core, and patches the ``auth_provider`` flag to ``"supertokens"``.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from starlette.requests import Request

from errors.codes import ErrorCode
from errors.exceptions import AppException
import ops.middleware.tenant_guard as tenant_guard
from ops.middleware.tenant_guard import (
    VerifiedSession,
    configure_session_verifier,
    get_tenant_context,
)


# ---------------------------------------------------------------------------
# Fake SessionVerifier seam
# ---------------------------------------------------------------------------
class _FakeVerifier:
    """Returns a fixed :class:`VerifiedSession`, ignoring the request entirely.

    Mirrors the contract of the SuperTokens-backed verifier: a valid session
    yields ``VerifiedSession(user_id, claims)`` derived only from the signed
    payload, never from the request's query/header/body.
    """

    def __init__(self, user_id: str, claims: dict):
        self._user_id = user_id
        self._claims = claims

    async def verify(self, request: Request):
        return VerifiedSession(user_id=self._user_id, claims=dict(self._claims))


# ---------------------------------------------------------------------------
# Request builder carrying CONFLICTING tenant_id everywhere
# ---------------------------------------------------------------------------
def _make_request(query_tenant: str, header_tenant: str, body_tenant: str) -> Request:
    """Build a Starlette Request with conflicting tenant_id in query/header/body."""
    query_string = f"tenant_id={query_tenant}".encode()
    body = json.dumps({"tenant_id": body_tenant}).encode()
    headers = [
        (b"x-tenant-id", header_tenant.encode()),
        (b"authorization", b"Bearer conflicting-legacy-token"),
        (b"content-type", b"application/json"),
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/some/protected/route",
        "query_string": query_string,
        "headers": headers,
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
_identifiers = st.from_regex(r"[a-zA-Z0-9_\-]{1,48}", fullmatch=True)
_roles = st.lists(
    st.sampled_from(["admin", "dispatcher", "ops_manager", "driver", "viewer"]),
    max_size=5,
    unique=True,
)
# Conflicting tenant values that may appear on the request. Constrained to URL/
# header-safe characters so the Request builder stays well-formed.
_conflict_values = st.from_regex(r"[a-zA-Z0-9_\-]{0,48}", fullmatch=True)


# ---------------------------------------------------------------------------
# Fixture: pin auth_provider="supertokens" and reset the verifier seam
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _supertokens_provider(monkeypatch):
    fake_settings = SimpleNamespace(
        auth_provider="supertokens",
        jwt_secret="unused-in-supertokens-mode",
        jwt_algorithm="HS256",
    )
    monkeypatch.setattr(tenant_guard, "get_settings", lambda: fake_settings)
    # Ensure no tenant settings service is wired so hydration uses safe defaults.
    monkeypatch.setattr(tenant_guard, "_tenant_settings_service", None)
    yield
    # Reset the injected verifier so other tests get the default SDK-backed one.
    configure_session_verifier(None)


# ---------------------------------------------------------------------------
# Property 3a — identity equals verified claims regardless of request inputs
# ---------------------------------------------------------------------------
class TestIdentityIsClaimSourced:
    """**Validates: Requirements 3.3, 3.5, 5.1, 7.3**"""

    @given(
        claim_tenant=_identifiers,
        claim_user=_identifiers,
        claim_roles=_roles,
        claim_pii=st.booleans(),
        claim_driver=st.one_of(st.none(), _identifiers),
        q_tenant=_conflict_values,
        h_tenant=_conflict_values,
        b_tenant=_conflict_values,
    )
    @settings(max_examples=100)
    def test_context_fields_equal_claims(
        self,
        claim_tenant,
        claim_user,
        claim_roles,
        claim_pii,
        claim_driver,
        q_tenant,
        h_tenant,
        b_tenant,
    ):
        """TenantContext identity fields equal the verified claims, ignoring
        any tenant_id supplied via query/header/body."""
        claims = {
            "tenant_id": claim_tenant,
            "roles": list(claim_roles),
            "has_pii_access": claim_pii,
        }
        if claim_driver is not None:
            claims["driver_id"] = claim_driver

        configure_session_verifier(_FakeVerifier(claim_user, claims))
        request = _make_request(q_tenant, h_tenant, b_tenant)

        ctx = asyncio.run(get_tenant_context(request))

        # Identity comes only from the verified session claims (Req 3.3, 3.5).
        assert ctx.tenant_id == claim_tenant
        assert ctx.user_id == claim_user
        assert ctx.roles == list(claim_roles)
        assert ctx.has_pii_access is bool(claim_pii)

        # The verified tenant scope is never overridden by request inputs
        # (Req 5.1) — even when the request carries a different tenant_id.
        assert ctx.tenant_id != q_tenant or q_tenant == claim_tenant
        assert ctx.tenant_id != h_tenant or h_tenant == claim_tenant
        assert ctx.tenant_id != b_tenant or b_tenant == claim_tenant


# ---------------------------------------------------------------------------
# Property 3b — missing tenant_id claim is rejected with 401
# ---------------------------------------------------------------------------
class TestMissingTenantClaimRejected:
    """**Validates: Requirements 5.3**"""

    @given(
        claim_user=_identifiers,
        claim_roles=_roles,
        claim_pii=st.booleans(),
        # An absent, empty, or non-string tenant_id claim must all be rejected.
        bad_tenant=st.one_of(
            st.none(),
            st.just(""),
            st.integers(),
            st.booleans(),
            st.lists(st.text(), max_size=2),
        ),
        include_key=st.booleans(),
        q_tenant=_conflict_values,
        h_tenant=_conflict_values,
        b_tenant=_conflict_values,
    )
    @settings(max_examples=100)
    def test_missing_tenant_claim_raises_401(
        self,
        claim_user,
        claim_roles,
        claim_pii,
        bad_tenant,
        include_key,
        q_tenant,
        h_tenant,
        b_tenant,
    ):
        """A verified session lacking a usable tenant_id claim is rejected with
        a 401 and no context — a conflicting request tenant_id never rescues it."""
        claims = {
            "roles": list(claim_roles),
            "has_pii_access": claim_pii,
        }
        # Either omit the tenant_id key entirely, or include it with an unusable
        # value (None/empty/non-string). Both must be rejected (Req 5.3).
        if include_key:
            claims["tenant_id"] = bad_tenant

        configure_session_verifier(_FakeVerifier(claim_user, claims))
        request = _make_request(q_tenant, h_tenant, b_tenant)

        with pytest.raises(AppException) as exc_info:
            asyncio.run(get_tenant_context(request))

        # Unauthorized (HTTP 401), with no TenantContext produced.
        assert exc_info.value.error_code == ErrorCode.UNAUTHORIZED
        assert exc_info.value.status_code == 401
