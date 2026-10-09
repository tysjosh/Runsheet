"""
Regression tests for staging finding F3: an access token kept working for ~57
minutes after ``POST /auth/signout``.

Session verification now passes ``check_database`` (``settings.
session_check_database``, default True) to the SuperTokens SDK on REST
(``ops.middleware.tenant_guard``) and WebSocket (``bootstrap.websockets``)
paths, so the core rejects a revoked session immediately. The REST result is
memoized on ``request.state`` so the auth gate and ``get_tenant_context``
share one core call per request.

The SDK is mocked throughout: with ``check_database=True`` the real SDK asks
the core and raises ``UnauthorisedError`` for a revoked session.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest
from supertokens_python.recipe.session.exceptions import UnauthorisedError

import bootstrap.websockets as ws
from errors.exceptions import AppException
from ops.middleware import tenant_guard
from ops.middleware.tenant_guard import (
    TenantContext,
    _SuperTokensSessionVerifier,
    configure_session_verifier,
    get_tenant_context,
)

_GET_SESSION = "supertokens_python.recipe.session.asyncio.get_session"
_GET_SESSION_NO_REQ = (
    "supertokens_python.recipe.session.asyncio.get_session_without_request_response"
)


def _fake_session(tenant_id: str = "t-1"):
    session = MagicMock()
    session.get_user_id.return_value = "st-1"
    session.get_access_token_payload.return_value = {
        "tenant_id": tenant_id,
        "roles": ["admin"],
    }
    return session


def _request() -> StarletteRequest:
    return StarletteRequest(
        {"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b""}
    )


def _settings(check_database: bool):
    return SimpleNamespace(session_check_database=check_database)


@pytest.fixture(autouse=True)
def _reset_seams():
    configure_session_verifier(None)
    ws.configure_ws_session_verifier(None)
    yield
    configure_session_verifier(None)
    ws.configure_ws_session_verifier(None)


# ---------------------------------------------------------------------------
# REST verifier
# ---------------------------------------------------------------------------


async def test_rest_verify_asks_core_by_default():
    get_session = AsyncMock(return_value=_fake_session())
    with patch(_GET_SESSION, get_session):
        verified = await _SuperTokensSessionVerifier().verify(_request())

    assert verified.claims["tenant_id"] == "t-1"
    assert get_session.await_args.kwargs.get("check_database") is True


async def test_rest_verify_honours_setting_off():
    get_session = AsyncMock(return_value=_fake_session())
    with patch(_GET_SESSION, get_session), patch(
        "config.settings.get_settings", return_value=_settings(False)
    ):
        await _SuperTokensSessionVerifier().verify(_request())

    assert get_session.await_args.kwargs.get("check_database") is False


async def test_revoked_session_yields_401_from_get_tenant_context():
    get_session = AsyncMock(side_effect=UnauthorisedError("session revoked"))
    with patch(_GET_SESSION, get_session):
        with pytest.raises(AppException) as excinfo:
            await get_tenant_context(_request())

    assert excinfo.value.status_code == 401
    assert get_session.await_args.kwargs.get("check_database") is True


async def test_two_verifications_on_one_request_hit_the_core_once():
    """The auth gate and get_tenant_context share one verification."""
    get_session = AsyncMock(return_value=_fake_session())
    request = _request()
    verifier = _SuperTokensSessionVerifier()
    with patch(_GET_SESSION, get_session):
        first = await verifier.verify(request)
        second = await verifier.verify(request)

    assert first is second
    assert get_session.await_count == 1


def test_signed_out_token_gets_401_end_to_end():
    app = FastAPI()

    @app.get("/protected")
    async def protected(tenant: TenantContext = Depends(get_tenant_context)):
        return {"tenant_id": tenant.tenant_id}

    @app.exception_handler(AppException)
    async def _handler(request: Request, exc: AppException):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    client = TestClient(app)
    alive = AsyncMock(return_value=_fake_session())
    with patch(_GET_SESSION, alive):
        assert client.get("/protected").status_code == 200

    # After sign-out the core reports the session gone.
    revoked = AsyncMock(side_effect=UnauthorisedError("session revoked"))
    with patch(_GET_SESSION, revoked):
        resp = client.get("/protected")

    assert resp.status_code == 401
    assert revoked.await_args.kwargs.get("check_database") is True


def test_settings_failure_fails_safe_to_checking_the_core():
    with patch("config.settings.get_settings", side_effect=RuntimeError("boom")):
        assert tenant_guard.session_check_database_enabled() is True


# ---------------------------------------------------------------------------
# WebSocket verifier
# ---------------------------------------------------------------------------


async def test_ws_verify_asks_core():
    verify = AsyncMock(return_value=_fake_session())
    with patch(_GET_SESSION_NO_REQ, verify):
        claims = await ws._default_ws_verify("tok", None)

    assert claims["tenant_id"] == "t-1"
    assert verify.await_args.kwargs.get("check_database") is True


async def test_ws_revoked_session_is_rejected():
    verify = AsyncMock(side_effect=UnauthorisedError("session revoked"))
    with patch(_GET_SESSION_NO_REQ, verify):
        assert await ws._default_ws_verify("tok", None) is None

    assert verify.await_args.kwargs.get("check_database") is True
