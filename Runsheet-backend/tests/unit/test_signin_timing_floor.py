"""Failed sign-ins take a fixed minimum time (OI-12).

A wrong password for a real account took ~450 ms longer than an unknown
email, so response time revealed which emails exist. Failures on
``/auth/signin`` and ``POST /auth/driver/session`` are now padded to
``SIGNIN_FAILURE_FLOOR_MS``. Successes and throttled 429s are not padded.

The clock and the sleep are injected, so nothing here really waits.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response
from starlette.testclient import TestClient

import auth.signin_timing as signin_timing

FLOOR_MS = 1000


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def timing(monkeypatch):
    """Fake clock plus a sleep that records what it was asked to wait."""
    clock = _Clock()
    slept = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(signin_timing, "_now", clock)
    monkeypatch.setattr(signin_timing, "_sleep", record_sleep)
    monkeypatch.setattr(signin_timing, "failure_floor_ms", lambda: FLOOR_MS)
    return SimpleNamespace(clock=clock, slept=slept)


def _fake_throttle(retry=None):
    throttle = MagicMock()
    throttle.check_sign_in = AsyncMock(return_value=retry)
    throttle.record_sign_in_failure = AsyncMock()
    throttle.clear_sign_in_failures = AsyncMock()
    return throttle


# ---------------------------------------------------------------------------
# pad_to_floor
# ---------------------------------------------------------------------------


async def test_pad_sleeps_the_remainder(timing):
    started = signin_timing.now()
    timing.clock.now += 0.3
    slept = await signin_timing.pad_to_floor(started)
    assert slept == pytest.approx(0.7)
    assert timing.clock.now - started == pytest.approx(1.0)


async def test_pad_is_zero_when_floor_already_met(timing):
    started = signin_timing.now()
    timing.clock.now += 1.5
    assert await signin_timing.pad_to_floor(started) == 0.0
    assert timing.slept == []


async def test_zero_floor_disables_padding(timing):
    assert await signin_timing.pad_to_floor(signin_timing.now(), floor_ms=0) == 0.0
    assert timing.slept == []


def test_floor_setting_defaults_to_1000_ms():
    from config.settings import Settings

    assert Settings.model_fields["signin_failure_floor_ms"].default == 1000


# ---------------------------------------------------------------------------
# Web: the /auth/signin APIs override
# ---------------------------------------------------------------------------


def _web_api(monkeypatch, result, *, delay=0.0, clock=None, retry=None):
    from supertokens_python.recipe.emailpassword.api.implementation import (
        APIImplementation,
    )

    import auth.supertokens_init as st_init

    throttle = _fake_throttle(retry)
    monkeypatch.setattr(st_init, "get_signin_throttle", lambda: throttle)

    async def original(*_args, **_kwargs):
        if clock is not None:
            clock.now += delay
        return result

    impl = APIImplementation()
    impl.sign_in_post = original
    return st_init._override_emailpassword_apis(impl)


async def _call_web(api):
    from supertokens_python.framework.fastapi.fastapi_request import FastApiRequest
    from supertokens_python.framework.fastapi.fastapi_response import FastApiResponse
    from supertokens_python.recipe.emailpassword.types import FormField

    request = Request(
        {"type": "http", "method": "POST", "path": "/auth/signin", "headers": [],
         "client": ("203.0.113.7", 1)}
    )
    opts = SimpleNamespace(
        request=FastApiRequest(request), response=FastApiResponse(Response())
    )
    return await api.sign_in_post(
        [FormField("email", "user@x.test"), FormField("password", "p")],
        "public", None, False, opts, {},
    )


@pytest.mark.parametrize("delay", [0.0, 0.3], ids=["unknown-email", "wrong-password"])
async def test_web_wrong_credentials_padded_to_floor(monkeypatch, timing, delay):
    from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError

    api = _web_api(monkeypatch, WrongCredentialsError(), delay=delay, clock=timing.clock)
    started = timing.clock.now

    result = await _call_web(api)

    assert isinstance(result, WrongCredentialsError)
    assert delay + sum(timing.slept) >= FLOOR_MS / 1000 - 1e-9
    assert timing.clock.now - started == pytest.approx(FLOOR_MS / 1000)


async def test_web_success_is_not_padded(monkeypatch, timing):
    from supertokens_python.recipe.emailpassword.interfaces import SignInPostOkResult

    ok = MagicMock(spec=SignInPostOkResult)
    api = _web_api(monkeypatch, ok, clock=timing.clock)

    assert await _call_web(api) is ok
    assert timing.slept == []


async def test_web_throttled_is_not_padded(monkeypatch, timing):
    from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError

    api = _web_api(monkeypatch, WrongCredentialsError(), clock=timing.clock, retry=30)

    result = await _call_web(api)

    assert getattr(result, "message", None) == "RATE_LIMITED"
    assert timing.slept == []


# ---------------------------------------------------------------------------
# Driver: POST /auth/driver/session
# ---------------------------------------------------------------------------


def _driver_client(monkeypatch, result, *, retry=None):
    from driver.api import session_endpoints
    from errors.handlers import register_exception_handlers

    monkeypatch.setattr(session_endpoints, "emailpassword_sign_in", AsyncMock(return_value=result))
    monkeypatch.setattr(session_endpoints, "get_signin_throttle", lambda: _fake_throttle(retry))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(session_endpoints.router)
    return TestClient(app)


def _driver_signin(client):
    return client.post(
        "/auth/driver/session", json={"email": "driver@x.test", "password": "Wrong-pass-1"}
    )


def test_driver_bad_credential_padded_to_floor(monkeypatch, timing):
    from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError

    client = _driver_client(monkeypatch, WrongCredentialsError())

    resp = _driver_signin(client)

    assert resp.status_code == 401, resp.text
    assert sum(timing.slept) == pytest.approx(FLOOR_MS / 1000)


def test_driver_throttled_is_not_padded(monkeypatch, timing):
    from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError

    client = _driver_client(monkeypatch, WrongCredentialsError(), retry=30)

    resp = _driver_signin(client)

    assert resp.status_code == 429, resp.text
    assert timing.slept == []
