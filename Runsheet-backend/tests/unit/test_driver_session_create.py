"""``POST /auth/driver/session`` (create_driver_session) end to end (OI-51).

The SuperTokens recipe calls, the auth_users claim read, the drivers_current
repository and the session mint are monkeypatched, so the route's own
decisions are what's under test:

- an unbound SuperTokens user (no auth_users claims) is refused 403
  ``INSUFFICIENT_ROLE`` and gets no session;
- a bound driver gets 200 with its server-set driver/tenant claims;
- the claims are read by the signed-in SuperTokens user id, never the email;
- a bad credential is 401 and padded to the sign-in timing floor (OI-12).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from supertokens_python.recipe.emailpassword.interfaces import (
    SignInOkResult,
    WrongCredentialsError,
)

import auth.signin_timing as signin_timing
from driver.api import session_endpoints
from errors.handlers import register_exception_handlers

ST_USER_ID = "st-1"
DRIVER_CLAIMS = {
    "tenant_id": "tenant-A",
    "roles": ["driver"],
    "driver_id": "DRV-7",
}


def _ok_result() -> MagicMock:
    result = MagicMock(spec=SignInOkResult)
    result.user = SimpleNamespace(id=ST_USER_ID)
    result.recipe_user_id = SimpleNamespace(get_as_string=lambda: ST_USER_ID)
    return result


def _throttle() -> MagicMock:
    throttle = MagicMock()
    throttle.check_sign_in = AsyncMock(return_value=None)
    throttle.record_sign_in_failure = AsyncMock()
    throttle.clear_sign_in_failures = AsyncMock()
    return throttle


@pytest.fixture
def wired(monkeypatch):
    """Patch every external call; tests set the sign-in result and claims."""
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(signin_timing, "_sleep", record_sleep)
    monkeypatch.setattr(signin_timing, "failure_floor_ms", lambda: 1000)

    sign_in = AsyncMock(return_value=_ok_result())
    lookup = AsyncMock(return_value=dict(DRIVER_CLAIMS))
    repository = MagicMock()
    repository.get = AsyncMock(return_value={"driver_id": "DRV-7"})
    session = MagicMock()
    session.get_all_session_tokens_dangerously.return_value = {
        "accessToken": "acc-token",
        "refreshToken": "ref-token",
        "frontToken": "front-token",
    }
    create_session = AsyncMock(return_value=session)

    monkeypatch.setattr(session_endpoints, "emailpassword_sign_in", sign_in)
    monkeypatch.setattr(session_endpoints, "lookup_auth_user_claims", lookup)
    monkeypatch.setattr(session_endpoints, "_get_driver_repository", lambda: repository)
    monkeypatch.setattr(
        session_endpoints, "create_new_session_without_request_response", create_session
    )
    monkeypatch.setattr(session_endpoints, "get_signin_throttle", _throttle)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(session_endpoints.router)
    return SimpleNamespace(
        client=TestClient(app),
        sign_in=sign_in,
        lookup=lookup,
        repository=repository,
        create_session=create_session,
        slept=slept,
    )


def _sign_in(client: TestClient):
    return client.post(
        "/auth/driver/session",
        json={"email": "driver@x.test", "password": "Right-pass-1"},
    )


def test_bound_driver_gets_a_session_with_its_claims(wired):
    resp = _sign_in(wired.client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["driver_id"] == "DRV-7"
    assert body["tenant_id"] == "tenant-A"
    assert body["access_token"] == "acc-token"
    assert body["refresh_token"] == "ref-token"
    assert resp.headers["st-access-token"] == "acc-token"
    wired.repository.get.assert_awaited_once_with("tenant-A", "DRV-7")
    # The minted session carries the server-set claims, not request data.
    _, kwargs = wired.create_session.call_args
    assert kwargs["access_token_payload"] == DRIVER_CLAIMS
    assert wired.slept == []


def test_claims_are_read_by_the_signed_in_supertokens_user_id(wired):
    _sign_in(wired.client)

    wired.lookup.assert_awaited_once_with(ST_USER_ID)


def test_unbound_user_is_refused_without_a_session(wired):
    wired.lookup.return_value = {}

    resp = _sign_in(wired.client)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"
    wired.create_session.assert_not_awaited()


def test_driver_without_a_drivers_current_record_is_refused(wired):
    wired.repository.get.return_value = None

    resp = _sign_in(wired.client)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "DRIVER_RECORD_NOT_PROVISIONED"
    wired.create_session.assert_not_awaited()


def test_bad_credential_is_401_and_padded(wired):
    wired.sign_in.return_value = WrongCredentialsError()

    resp = _sign_in(wired.client)

    assert resp.status_code == 401, resp.text
    assert resp.json()["error_code"] == "UNAUTHORIZED"
    assert "Right-pass-1" not in resp.text
    assert len(wired.slept) == 1 and wired.slept[0] > 0
    wired.lookup.assert_not_awaited()
    wired.create_session.assert_not_awaited()
