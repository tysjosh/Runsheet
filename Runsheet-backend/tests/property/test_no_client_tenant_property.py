"""
Property-based test for no-client-tenant resolution in the Session_Verifier.

# Feature: supertokens-auth-migration, Property 4: No-client-tenant

**Validates: Requirements 5.2, 6.5**

Property 4: No-client-tenant — a session scoped to tenant A always resolves
scope A regardless of any supplied ``tenant_id`` (via query parameter, path
parameter, request body, or header).

This exercises ``ops.middleware.tenant_guard.get_tenant_context`` with
``auth_provider="supertokens"``. A fake :class:`SessionVerifier` is injected via
``configure_session_verifier`` whose verified claims fix
``tenant_id = "tenant-A"`` (signed, server-controlled). A fake ``Request`` is
built that supplies a generated — and possibly different — ``tenant_id`` via the
query string, path, body, and/or header. The resulting
``TenantContext.tenant_id`` must always be ``"tenant-A"``, never the
client-supplied value (even when the supplied value equals another real tenant
id such as ``"tenant-B"``).
"""

import asyncio
import sys
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Keep importing the ops modules side-effect free (no real ES connection).
# ---------------------------------------------------------------------------
sys.modules.setdefault("services.elasticsearch_service", MagicMock())

from starlette.requests import Request  # noqa: E402

from ops.middleware.tenant_guard import (  # noqa: E402
    VerifiedSession,
    configure_session_verifier,
    configure_tenant_guard,
    get_tenant_context,
)

# The tenant the verified session is scoped to. The property asserts the
# resolved scope is ALWAYS this value, irrespective of any client-supplied id.
SESSION_TENANT = "tenant-A"


# ---------------------------------------------------------------------------
# Fake SessionVerifier — fixes the verified claims to tenant-A and ignores the
# request entirely (the request is what carries the spoofed client tenant_id).
# ---------------------------------------------------------------------------
class _FixedTenantVerifier:
    """A :class:`SessionVerifier` that always verifies a session for tenant-A.

    It derives identity solely from server-controlled claims and never reads
    the request, mirroring how a signed SuperTokens access-token payload is the
    sole source of scope.
    """

    def __init__(self, *, roles, has_pii_access):
        self._roles = roles
        self._has_pii_access = has_pii_access

    async def verify(self, request: Request):  # noqa: ARG002 - request intentionally ignored
        return VerifiedSession(
            user_id="user-A",
            claims={
                "tenant_id": SESSION_TENANT,
                "roles": list(self._roles),
                "has_pii_access": self._has_pii_access,
            },
        )


# Settings stub forcing the SuperTokens hard-cutover verification path.
# (Removed: get_tenant_context now always verifies a SuperTokens session.)


def _build_request(
    *, method: str, supplied_tenant: str, channels: frozenset
) -> Request:
    """Build a fake ASGI ``Request`` that supplies ``supplied_tenant`` via the
    selected channels (any of query/path/body/header)."""
    query_string = b""
    path = "/api/fuel/mvp/orders"
    headers = []

    if "query" in channels:
        from urllib.parse import urlencode

        query_string = urlencode({"tenant_id": supplied_tenant}).encode()
    if "path" in channels:
        # Path param style: include the supplied tenant in the URL path.
        from urllib.parse import quote

        path = f"/api/tenants/{quote(supplied_tenant, safe='')}/orders"
    if "header" in channels:
        headers.append((b"x-tenant-id", supplied_tenant.encode("utf-8", "replace")))

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "server": ("testserver", 80),
        "path": path,
        "raw_path": path.encode("utf-8", "replace"),
        "query_string": query_string,
        "headers": headers,
    }

    body = b""
    if "body" in channels and method != "GET":
        import json

        body = json.dumps({"tenant_id": supplied_tenant}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive=receive)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# A client-supplied tenant_id: deliberately includes other real tenant ids
# (tenant-B), the session's own tenant (tenant-A), the empty string, and
# arbitrary text — so both "differs from session" and "equals another tenant"
# cases are covered.
_supplied_tenants = st.one_of(
    st.just("tenant-B"),
    st.just("tenant-A"),
    st.just(""),
    st.just("../tenant-B"),
    st.text(min_size=0, max_size=48),
    st.from_regex(r"tenant-[A-Za-z0-9_\-]{1,12}", fullmatch=True),
)

# At least one channel must carry the spoofed value.
_channel_sets = st.sets(
    st.sampled_from(["query", "path", "body", "header"]),
    min_size=1,
    max_size=4,
).map(frozenset)

_methods = st.sampled_from(["GET", "POST", "PUT", "PATCH", "DELETE"])

_roles = st.lists(
    st.sampled_from(["admin", "dispatcher", "ops_manager", "driver"]),
    max_size=4,
    unique=True,
)


# ---------------------------------------------------------------------------
# Property 4 — verified scope is always tenant-A, never the supplied tenant_id
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 4: No-client-tenant
class TestNoClientTenantResolution:
    """**Validates: Requirements 5.2, 6.5**"""

    def teardown_method(self, method):
        # Reset both seams so nothing leaks into other tests.
        configure_session_verifier(None)
        configure_tenant_guard(None)

    @given(
        supplied_tenant=_supplied_tenants,
        channels=_channel_sets,
        http_method=_methods,
        roles=_roles,
        has_pii_access=st.booleans(),
    )
    @settings(max_examples=100)
    def test_resolves_session_tenant_ignoring_supplied_value(
        self,
        supplied_tenant: str,
        channels: frozenset,
        http_method: str,
        roles: list,
        has_pii_access: bool,
    ):
        """The resolved TenantContext scope is always the session's tenant-A,
        regardless of any client-supplied tenant_id."""
        configure_tenant_guard(None)  # use in-process US/imperial defaults
        configure_session_verifier(
            _FixedTenantVerifier(roles=roles, has_pii_access=has_pii_access)
        )

        request = _build_request(
            method=http_method,
            supplied_tenant=supplied_tenant,
            channels=channels,
        )

        context = asyncio.run(get_tenant_context(request))

        assert context.tenant_id == SESSION_TENANT, (
            f"supplied tenant_id={supplied_tenant!r} via {sorted(channels)} "
            f"must be ignored; expected scope {SESSION_TENANT!r}, "
            f"got {context.tenant_id!r}"
        )
        # Identity must come from the verified session, not the request.
        assert context.user_id == "user-A"
