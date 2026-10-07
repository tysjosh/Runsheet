"""Per-user portal rate limits (design §8.2; RL-1, T-RL-COVERAGE, AC17)."""
from __future__ import annotations

import inspect

from middleware.rate_limiter import limiter
from portal.api._authz import PORTAL_READ_LIMIT, PORTAL_READ_SCOPE
from tests.portal.conftest import T1, CUSTOMER_A, call, fill_path, portal_routes


def _limit_name(route) -> str:
    endpoint = route.endpoint
    return f"{endpoint.__module__}.{endpoint.__name__}"


def _limits_for(route):
    name = _limit_name(route)
    return list(limiter._route_limits.get(name, [])) + list(
        limiter._dynamic_route_limits.get(name, [])
    )


def test_every_portal_route_is_limited(portal_app):
    """T-RL-COVERAGE: no portal route ships without a per-user limit."""
    missing, no_request = [], []
    for _method, route in portal_routes(portal_app):
        limits = _limits_for(route)
        if not limits:
            missing.append(route.path)
        for lim in limits:
            key_func = getattr(lim, "key_func", None)
            assert getattr(key_func, "__name__", "") == "portal_rate_key", route.path
        if "request" not in inspect.signature(route.endpoint).parameters:
            no_request.append(route.path)
    assert missing == []
    assert no_request == []


def _read_routes(app):
    """GET routes in the shared ``portal_read`` bucket."""
    out = []
    for method, route in portal_routes(app):
        if method != "GET":
            continue
        scopes = {getattr(lim, "scope", None) for lim in _limits_for(route)}
        if PORTAL_READ_SCOPE in scopes:
            out.append(route)
    return out


def test_read_limit_429_with_retry_after_per_user(portal_app, client, portal_on, sessions, portal_fakes):
    """RL-1: the shared read bucket, its 429, and per-user isolation."""
    amount = int(PORTAL_READ_LIMIT.split("/")[0])
    user1 = sessions.customer(T1, CUSTOMER_A)
    user2 = sessions.customer(T1, CUSTOMER_A)
    portal_fakes.grants.grant(user1)
    portal_fakes.grants.grant(user2)

    for _ in range(amount):
        assert call(client, "GET", "/api/portal/me", user1).status_code == 200

    blocked = call(client, "GET", "/api/portal/me", user1)
    assert blocked.status_code == 429
    assert blocked.json()["error_code"] == "RATE_LIMITED"
    assert int(blocked.headers["Retry-After"]) > 0

    # Every read route shares the bucket, so it's exhausted for all of them.
    for route in _read_routes(portal_app):
        resp = call(client, "GET", fill_path(route.path), user1)
        assert resp.status_code == 429, route.path

    # User 2's bucket is untouched.
    assert call(client, "GET", "/api/portal/me", user2).status_code == 200


def test_rate_key_uses_guard_stamps():
    from types import SimpleNamespace

    from portal.api._authz import portal_rate_key

    request = SimpleNamespace(
        state=SimpleNamespace(portal_tenant_id="t", portal_user_id="u"),
        headers={},
        client=SimpleNamespace(host="203.0.113.9"),
    )
    assert portal_rate_key(request) == "portal:t:u"
