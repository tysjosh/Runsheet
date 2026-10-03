"""
Regression tests for staging finding F5/L3: the rate-limit client IP must not
be spoofable through X-Forwarded-For.

Behind the AWS ALB the leftmost X-Forwarded-For entries are client-supplied;
the ALB APPENDS the address it saw as the rightmost entry. ``get_client_ip``
used to take the leftmost entry, so rotating it per request handed the caller a
fresh rate-limit bucket every time. It now takes the Nth entry from the right,
N = ``trusted_proxy_hops`` (0 = ignore the header, use the socket peer).

Module attributes are read inside each test body (``rl.get_client_ip``) so on
old code every test fails on its own rather than as one collection error.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from config.settings import Environment, Settings
from middleware import rate_limiter as rl

_PEER = "10.0.1.5"


def _request(*headers: tuple[str, str], client=(_PEER, 12345)) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "client": client,
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _reset_hops():
    # getattr so the fixture itself works on pre-F5 code and each test fails
    # (or passes) on its own assertion.
    reset = getattr(rl, "configure_trusted_proxy_hops", lambda _n: None)
    reset(0)
    yield
    reset(0)


# ---------------------------------------------------------------------------
# Regression: fail on the leftmost-entry implementation
# ---------------------------------------------------------------------------


def test_hops1_ignores_spoofed_leftmost_entry():
    req = _request(("X-Forwarded-For", "6.6.6.6, 203.0.113.7"))
    assert rl.get_client_ip(req, trusted_hops=1) == "203.0.113.7"


def test_hops0_ignores_xff_and_uses_peer():
    req = _request(("X-Forwarded-For", "6.6.6.6"))
    assert rl.get_client_ip(req, trusted_hops=0) == _PEER


def test_x_real_ip_is_ignored():
    req = _request(("X-Real-IP", "6.6.6.6"))
    assert rl.get_client_ip(req, trusted_hops=1) == _PEER


def test_hops2_takes_second_from_right():
    req = _request(("X-Forwarded-For", "6.6.6.6, 198.51.100.9, 10.0.0.2"))
    assert rl.get_client_ip(req, trusted_hops=2) == "198.51.100.9"


def test_configured_hops_used_by_default():
    rl.configure_trusted_proxy_hops(1)
    req = _request(("X-Forwarded-For", "6.6.6.6, 203.0.113.7"))
    assert rl.get_client_ip(req) == "203.0.113.7"


def test_driver_rate_key_fallback_uses_rightmost():
    rl.configure_trusted_proxy_hops(1)
    req = _request(("X-Forwarded-For", "6.6.6.6, 203.0.113.7"))
    assert rl.driver_rate_key(req) == "203.0.113.7"


def test_rotating_leftmost_xff_cannot_bypass_slowapi_limit():
    """The L3 repro: a rotating spoofed leftmost entry must hit the same bucket."""
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded

    rl.configure_trusted_proxy_hops(1)
    limiter = Limiter(key_func=rl.get_client_ip)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rl._custom_rate_limit_handler)

    @app.get("/limited")
    @limiter.limit("2/minute")
    async def limited(request: Request):
        return {"ok": True}

    client = TestClient(app)
    codes = [
        client.get(
            "/limited",
            headers={"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.7"},
        ).status_code
        for i in range(3)
    ]
    assert codes == [200, 200, 429]


def test_setup_rate_limiting_configures_hops():
    rl.setup_rate_limiting(FastAPI(), trusted_proxy_hops=1)
    assert rl._trusted_proxy_hops == 1


@pytest.mark.parametrize(
    "environment,expected",
    [
        (Environment.DEVELOPMENT, 0),
        (Environment.TEST, 0),
        (Environment.STAGING, 1),
        (Environment.PRODUCTION, 1),
    ],
)
def test_effective_hops_defaults_per_environment(environment, expected):
    settings = Settings.model_construct(
        environment=environment, trusted_proxy_hops=None
    )
    assert settings.effective_trusted_proxy_hops == expected


def test_explicit_hops_overrides_environment():
    settings = Settings.model_construct(
        environment=Environment.STAGING, trusted_proxy_hops=2
    )
    assert settings.effective_trusted_proxy_hops == 2


def test_fewer_entries_than_hops_falls_back_to_peer():
    req = _request(("X-Forwarded-For", "203.0.113.7"))
    assert rl.get_client_ip(req, trusted_hops=2) == _PEER


def test_invalid_entry_falls_back_to_peer():
    req = _request(("X-Forwarded-For", "not-an-ip"))
    assert rl.get_client_ip(req, trusted_hops=1) == _PEER


def test_multiple_xff_headers_joined_in_order():
    req = _request(
        ("X-Forwarded-For", "6.6.6.6"),
        ("X-Forwarded-For", "203.0.113.7"),
    )
    assert rl.get_client_ip(req, trusted_hops=1) == "203.0.113.7"


# ---------------------------------------------------------------------------
# Preservation: same result on old and new code
# ---------------------------------------------------------------------------


def test_no_client_falls_back_to_default():
    req = _request(client=None)
    assert rl.get_client_ip(req) == "127.0.0.1"
