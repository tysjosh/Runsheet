"""
Regression tests for staging finding F5: sign-in and password-reset had no
rate limit.

``POST /auth/signin`` and ``POST /auth/user/password/reset/token`` are served by
the SuperTokens SDK middleware, outside every ``@limiter.limit`` decorator, and
``POST /auth/driver/session`` verifies a password with no throttle either.
``auth/signin_throttle.py`` now counts attempts in Redis per client IP and per
email, and the SDK APIs override plus the driver endpoint answer a throttled
request with a 429 error envelope and ``Retry-After``.

Nothing here dials a SuperTokens core or a real Redis: the SDK's API
implementation is mocked, and Redis is an in-file fake driven by a fake clock
so window expiry is deterministic.
"""
from __future__ import annotations

import logging
import math
import re
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response
from starlette.testclient import TestClient

from middleware import rate_limiter as rl

# Reuse the SDK reset helpers so these tests leave no SDK state behind.
from tests.integration.test_supertokens_auth_flow import (  # noqa: F401
    _PLACEHOLDER_CONNECTION_URI,
    _build_auth_app,
    _form_fields,
    _reset_supertokens,
)

_MESSAGE = re.compile(r"^Too many attempts, try again in \d+ seconds$")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0


class _ClockedFakeRedis:
    """The async Redis subset the throttle uses, with TTLs on a fake clock."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.store: Dict[str, Tuple[Any, Optional[float]]] = {}

    def _purge(self, key: str) -> None:
        entry = self.store.get(key)
        if entry and entry[1] is not None and entry[1] <= self.clock.now:
            del self.store[key]

    async def get(self, key):
        self._purge(key)
        entry = self.store.get(key)
        return None if entry is None else str(entry[0])

    async def set(self, key, value, ex=None, nx=False):
        self._purge(key)
        if nx and key in self.store:
            return None
        expires = self.clock.now + ex if ex else None
        self.store[key] = (value, expires)
        return True

    async def incr(self, key):
        self._purge(key)
        value, expires = self.store.get(key, (0, None))
        value = int(value) + 1
        self.store[key] = (value, expires)
        return value

    async def ttl(self, key):
        self._purge(key)
        entry = self.store.get(key)
        if entry is None:
            return -2
        if entry[1] is None:
            return -1
        return math.ceil(entry[1] - self.clock.now)

    async def delete(self, key):
        self._purge(key)
        return 1 if self.store.pop(key, None) is not None else 0

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: _ClockedFakeRedis) -> None:
        self._redis = redis
        self._ops = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self._ops.append((name, args, kwargs))
            return self

        return queue

    async def execute(self):
        ops, self._ops = self._ops, []
        return [await getattr(self._redis, n)(*a, **k) for n, a, k in ops]


class _BrokenRedis:
    """Every call raises, as an unreachable Redis would."""

    def pipeline(self, transaction=True):
        return _BrokenPipeline()

    async def delete(self, key):
        raise ConnectionError("redis down")


class _BrokenPipeline:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __getattr__(self, name):
        return lambda *a, **k: self

    async def execute(self):
        raise ConnectionError("redis down")


def _throttle_module():
    import auth.signin_throttle as module

    return module


@pytest.fixture(autouse=True)
def _isolate():
    rl.configure_trusted_proxy_hops(0)
    try:
        _throttle_module().reset_signin_throttle()
    except Exception:
        pass
    yield
    rl.configure_trusted_proxy_hops(0)
    try:
        _throttle_module().reset_signin_throttle()
    except Exception:
        pass


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def fake_redis(clock):
    return _ClockedFakeRedis(clock)


@pytest.fixture
def throttle(fake_redis):
    return _throttle_module().SignInThrottle(redis_client=fake_redis)


# ---------------------------------------------------------------------------
# Service level
# ---------------------------------------------------------------------------


async def test_ip_limit_counts_all_attempts(throttle):
    for i in range(20):
        assert await throttle.check_sign_in("203.0.113.7", f"u{i}@x.test") is None
    retry = await throttle.check_sign_in("203.0.113.7", "u99@x.test")
    assert retry is not None and 1 <= retry <= 60


async def test_email_failures_block_after_limit(throttle):
    for _ in range(10):
        await throttle.record_sign_in_failure("victim@x.test")
    retry = await throttle.check_sign_in("198.51.100.1", "victim@x.test")
    assert retry is not None and 1 <= retry <= 900


async def test_success_clears_email_failures(throttle):
    for _ in range(9):
        await throttle.record_sign_in_failure("user@x.test")
    await throttle.clear_sign_in_failures("user@x.test")
    await throttle.record_sign_in_failure("user@x.test")
    assert await throttle.check_sign_in("198.51.100.1", "user@x.test") is None


async def test_email_window_expiry_unblocks(throttle, clock):
    for _ in range(10):
        await throttle.record_sign_in_failure("victim@x.test")
    assert await throttle.check_sign_in("198.51.100.1", "victim@x.test")
    clock.now += 901
    assert await throttle.check_sign_in("198.51.100.2", "victim@x.test") is None


async def test_blocked_checks_do_not_extend_email_lock(throttle, clock):
    for _ in range(10):
        await throttle.record_sign_in_failure("victim@x.test")
    previous = None
    for i in range(5):
        retry = await throttle.check_sign_in(f"198.51.100.{i}", "victim@x.test")
        assert retry is not None
        if previous is not None:
            assert retry <= previous
        previous = retry
        clock.now += 10
    assert previous <= 900 - 40


async def test_every_key_has_ttl(throttle, fake_redis):
    await throttle.check_sign_in("203.0.113.7", "a@x.test")
    await throttle.record_sign_in_failure("a@x.test")
    await throttle.check_password_reset("203.0.113.7", "a@x.test")
    assert fake_redis.store
    assert all(expires is not None for _, expires in fake_redis.store.values())


async def test_email_normalization_and_no_plaintext_in_keys(throttle, fake_redis):
    for _ in range(5):
        await throttle.record_sign_in_failure(" Admin@Example.COM ")
    for _ in range(5):
        await throttle.record_sign_in_failure("admin@example.com")
    assert await throttle.check_sign_in("198.51.100.1", "ADMIN@example.com")
    assert not any("example.com" in key.lower() for key in fake_redis.store)


async def test_password_reset_email_limit(throttle):
    for i in range(5):
        assert await throttle.check_password_reset(f"198.51.100.{i}", "v@x.test") is None
    retry = await throttle.check_password_reset("198.51.100.9", "v@x.test")
    assert retry is not None and 1 <= retry <= 3600


async def test_password_reset_ip_limit(throttle):
    for i in range(10):
        assert await throttle.check_password_reset("203.0.113.7", f"u{i}@x.test") is None
    retry = await throttle.check_password_reset("203.0.113.7", "u99@x.test")
    assert retry is not None and 1 <= retry <= 900


async def test_redis_down_fails_open_and_warns(caplog):
    throttle = _throttle_module().SignInThrottle(redis_client=_BrokenRedis())
    with caplog.at_level(logging.WARNING, logger="auth.signin_throttle"):
        assert await throttle.check_sign_in("203.0.113.7", "a@x.test") is None
        await throttle.record_sign_in_failure("a@x.test")
        await throttle.clear_sign_in_failures("a@x.test")
        assert await throttle.check_password_reset("203.0.113.7", "a@x.test") is None
    assert any("fail open" in r.getMessage() for r in caplog.records)


async def test_unconfigured_throttle_is_disabled_noop():
    throttle = _throttle_module().get_signin_throttle()
    assert throttle.enabled is False
    for _ in range(50):
        assert await throttle.check_sign_in("203.0.113.7", "a@x.test") is None
        await throttle.record_sign_in_failure("a@x.test")
    assert await throttle.check_password_reset("203.0.113.7", "a@x.test") is None


def test_configure_without_injected_client_is_disabled_under_test_env():
    """CI and local runs share one Redis: the real client is never built in tests."""
    from config.settings import get_settings

    settings = get_settings().model_copy(update={"redis_url": "redis://localhost:6379"})
    throttle = _throttle_module().configure_signin_throttle(settings)
    assert throttle.enabled is False


# ---------------------------------------------------------------------------
# SDK HTTP level: /auth/signin and /auth/user/password/reset/token
# ---------------------------------------------------------------------------


@pytest.fixture
def sdk_mocks(monkeypatch, fake_redis):
    """Init the SDK with mocked API implementations and a fake-Redis throttle."""
    import os

    from supertokens_python.recipe.emailpassword.api.implementation import (
        APIImplementation,
    )
    from supertokens_python.recipe.emailpassword.interfaces import (
        GeneratePasswordResetTokenPostOkResult,
        WrongCredentialsError,
    )

    from auth.supertokens_init import init_supertokens
    from config.settings import get_settings

    sign_in = AsyncMock(return_value=WrongCredentialsError())
    reset = AsyncMock(return_value=GeneratePasswordResetTokenPostOkResult())
    # Patched on the class BEFORE init, because the override captures the
    # originals when the recipe is built.
    monkeypatch.setattr(APIImplementation, "sign_in_post", sign_in)
    monkeypatch.setattr(APIImplementation, "generate_password_reset_token_post", reset)

    os.environ.setdefault("SUPERTOKENS_ENV", "testing")
    settings = get_settings()
    settings = settings.model_copy(
        update={
            "supertokens_connection_uri": settings.supertokens_connection_uri
            or _PLACEHOLDER_CONNECTION_URI
        }
    )
    _reset_supertokens()
    init_supertokens(settings)
    # Default limits (20/60s IP, 10 failures/900s email, reset 10/900s, 5/3600s).
    module = _throttle_module()
    module._throttle = module.SignInThrottle(redis_client=fake_redis)
    try:
        yield SimpleNamespace(sign_in=sign_in, reset=reset, settings=settings)
    finally:
        _reset_supertokens()


@pytest.fixture
def auth_client(sdk_mocks):
    from middleware.request_id import RequestIDMiddleware

    app = _build_auth_app()
    # Outside the SDK middleware, as in main.py, so request_id is set.
    app.add_middleware(RequestIDMiddleware)
    return TestClient(app)


_H = {"rid": "emailpassword"}


def _signin(client, email, xff=None, password="Wrong-pass-1"):
    headers = dict(_H)
    if xff:
        headers["X-Forwarded-For"] = xff
    return client.post("/auth/signin", json=_form_fields(email, password), headers=headers)


def _reset_token(client, email, xff=None):
    headers = dict(_H)
    if xff:
        headers["X-Forwarded-For"] = xff
    return client.post(
        "/auth/user/password/reset/token",
        json={"formFields": [{"id": "email", "value": email}]},
        headers=headers,
    )


def _assert_throttled(resp, max_wait):
    assert resp.status_code == 429, resp.text
    retry = int(resp.headers["Retry-After"])
    assert 1 <= retry <= max_wait
    body = resp.json()
    assert body["error_code"] == "RATE_LIMITED"
    assert _MESSAGE.match(body["message"]), body["message"]
    assert body["details"]["retry_after_seconds"] == retry
    assert body["request_id"]
    assert "status" not in body


def test_signin_ip_limit_returns_429_with_retry_after_and_envelope(auth_client, sdk_mocks):
    for i in range(20):
        resp = _signin(auth_client, f"u{i}@x.test")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "WRONG_CREDENTIALS_ERROR"
    _assert_throttled(_signin(auth_client, "u99@x.test"), 60)
    assert sdk_mocks.sign_in.await_count == 20


def test_signin_ip_limit_not_bypassed_by_rotating_spoofed_xff(auth_client, sdk_mocks):
    rl.configure_trusted_proxy_hops(1)
    for i in range(20):
        resp = _signin(auth_client, f"u{i}@x.test", xff=f"198.51.100.{i}, 203.0.113.7")
        assert resp.status_code == 200, resp.text
    _assert_throttled(
        _signin(auth_client, "u99@x.test", xff="198.51.100.99, 203.0.113.7"), 60
    )


def test_signin_email_limit_trips_across_rotating_ips(auth_client, sdk_mocks):
    rl.configure_trusted_proxy_hops(1)
    for i in range(10):
        email = "Victim@X.test" if i % 2 else "victim@x.test"
        resp = _signin(auth_client, email, xff=f"198.51.100.{i}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "WRONG_CREDENTIALS_ERROR"
    _assert_throttled(_signin(auth_client, "victim@x.test", xff="198.51.100.200"), 900)
    assert sdk_mocks.sign_in.await_count == 10


def test_signin_window_expiry_resets(auth_client, sdk_mocks, clock):
    for i in range(20):
        _signin(auth_client, f"u{i}@x.test")
    assert _signin(auth_client, "u99@x.test").status_code == 429
    clock.now += 61
    resp = _signin(auth_client, "u100@x.test")
    assert resp.status_code == 200, resp.text


def test_password_reset_token_throttled_per_email(auth_client, sdk_mocks):
    rl.configure_trusted_proxy_hops(1)
    for i in range(5):
        resp = _reset_token(auth_client, "victim@x.test", xff=f"198.51.100.{i}")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "OK"}
    _assert_throttled(_reset_token(auth_client, "victim@x.test", xff="198.51.100.9"), 3600)
    assert sdk_mocks.reset.await_count == 5


def test_password_reset_token_throttled_per_ip(auth_client, sdk_mocks):
    for i in range(10):
        assert _reset_token(auth_client, f"u{i}@x.test").status_code == 200
    _assert_throttled(_reset_token(auth_client, "u99@x.test"), 900)
    assert sdk_mocks.reset.await_count == 10


def test_signin_fails_open_when_redis_down(auth_client, sdk_mocks, caplog):
    module = _throttle_module()
    module._throttle = module.SignInThrottle(redis_client=_BrokenRedis())
    with caplog.at_level(logging.WARNING, logger="auth.signin_throttle"):
        for i in range(25):
            resp = _signin(auth_client, "victim@x.test")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "WRONG_CREDENTIALS_ERROR"
    # Every attempt reached the SDK, i.e. nothing was blocked, and the outage
    # was logged.
    assert sdk_mocks.sign_in.await_count == 25
    assert any("fail open" in r.getMessage() for r in caplog.records)


def test_signup_and_email_exists_still_disabled(sdk_mocks):
    from supertokens_python.recipe.emailpassword.recipe import EmailPasswordRecipe

    api = EmailPasswordRecipe.get_instance().api_implementation
    assert api.disable_sign_up_post is True
    assert api.disable_email_exists_get is True
    assert api.disable_sign_in_post is False
    assert api.disable_generate_password_reset_token_post is False


# ---------------------------------------------------------------------------
# Override level: a correct credential passes through and clears failures
# ---------------------------------------------------------------------------


async def test_successful_signin_under_limit_passes_through_and_clears_failures(
    fake_redis,
):
    from supertokens_python.framework.fastapi.fastapi_request import FastApiRequest
    from supertokens_python.framework.fastapi.fastapi_response import FastApiResponse
    from supertokens_python.recipe.emailpassword.api.implementation import (
        APIImplementation,
    )
    from supertokens_python.recipe.emailpassword.interfaces import SignInPostOkResult
    from supertokens_python.recipe.emailpassword.types import FormField

    import auth.supertokens_init as st_init

    module = _throttle_module()
    module._throttle = module.SignInThrottle(redis_client=fake_redis)
    email = "user@x.test"
    for _ in range(3):
        await module.get_signin_throttle().record_sign_in_failure(email)

    sentinel = MagicMock(spec=SignInPostOkResult)
    impl = APIImplementation()
    impl.sign_in_post = AsyncMock(return_value=sentinel)
    api = st_init._override_emailpassword_apis(impl)

    starlette_request = Request(
        {"type": "http", "method": "POST", "path": "/auth/signin", "headers": [],
         "client": ("203.0.113.7", 1)}
    )
    opts = SimpleNamespace(
        request=FastApiRequest(starlette_request),
        response=FastApiResponse(Response()),
    )
    result = await api.sign_in_post(
        [FormField("email", email), FormField("password", "p")],
        "public", None, False, opts, {},
    )
    assert result is sentinel
    assert opts.response.status_set is False
    assert not any(":signin:email:" in key for key in fake_redis.store)


# ---------------------------------------------------------------------------
# Driver endpoint: POST /auth/driver/session
# ---------------------------------------------------------------------------


@pytest.fixture
def driver_app(monkeypatch, fake_redis):
    from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError

    from driver.api import session_endpoints
    from errors.handlers import register_exception_handlers
    from middleware.request_id import RequestIDMiddleware

    sign_in = AsyncMock(return_value=WrongCredentialsError())
    monkeypatch.setattr(session_endpoints, "emailpassword_sign_in", sign_in)
    module = _throttle_module()
    module._throttle = module.SignInThrottle(redis_client=fake_redis)

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(session_endpoints.router)
    return SimpleNamespace(client=TestClient(app), sign_in=sign_in)


def _driver_signin(client, email):
    return client.post(
        "/auth/driver/session", json={"email": email, "password": "Wrong-pass-1"}
    )


def test_driver_session_email_limit_returns_429_envelope(driver_app):
    for _ in range(10):
        resp = _driver_signin(driver_app.client, "driver@x.test")
        assert resp.status_code == 401, resp.text
        assert resp.json()["error_code"] == "UNAUTHORIZED"
    _assert_throttled(_driver_signin(driver_app.client, "driver@x.test"), 900)
    assert driver_app.sign_in.await_count == 10


def test_driver_session_ip_limit(driver_app):
    for i in range(20):
        assert _driver_signin(driver_app.client, f"d{i}@x.test").status_code == 401
    _assert_throttled(_driver_signin(driver_app.client, "d99@x.test"), 60)
    assert driver_app.sign_in.await_count == 20


async def test_driver_and_web_signin_share_email_bucket(driver_app):
    throttle = _throttle_module().get_signin_throttle()
    for _ in range(5):
        # What the SDK override records for a failed /auth/signin.
        await throttle.record_sign_in_failure("driver@x.test")
    for _ in range(5):
        assert _driver_signin(driver_app.client, "driver@x.test").status_code == 401
    _assert_throttled(_driver_signin(driver_app.client, "driver@x.test"), 900)


def test_driver_session_fails_open_when_redis_down(driver_app):
    module = _throttle_module()
    module._throttle = module.SignInThrottle(redis_client=_BrokenRedis())
    for _ in range(25):
        resp = _driver_signin(driver_app.client, "driver@x.test")
        assert resp.status_code == 401, resp.text
