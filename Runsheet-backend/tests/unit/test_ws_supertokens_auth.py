"""
Unit tests for the SuperTokens-backed WebSocket_Authenticator
(``bootstrap.websockets._authenticate_tenant`` / ``_authenticate_driver``).

These cover the re-implemented behavior from the SuperTokens Auth Migration:

* Cookie-first session extraction on the handshake, with a redacted
  short-lived ``token`` query-param fallback (Req 7.5).
* ``tenant_id`` (and ``driver_id``) derived exclusively from the verified
  session claims (Req 7.1, 7.3).
* Unverifiable connections return ``None`` so the endpoint closes with 4001
  (Req 7.2).
* The session-token value is never written to logs (Req 7.4).
* ``auth_provider`` branching: ``supertokens`` verifies the session only;
  ``dual`` falls back to the legacy JWT only when no session is present.

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5
"""
import logging
from unittest.mock import MagicMock, patch

import pytest

import bootstrap.websockets as ws


def _make_settings(auth_provider="supertokens", jwt_secret="test-secret",
                   jwt_algorithm="HS256"):
    settings = MagicMock()
    settings.auth_provider = auth_provider
    settings.jwt_secret = jwt_secret
    settings.jwt_algorithm = jwt_algorithm
    return settings


def _make_ws(query_params=None, headers=None):
    websocket = MagicMock()
    websocket.query_params = query_params or {}
    websocket.headers = headers or {}
    return websocket


@pytest.fixture(autouse=True)
def _reset_verifier():
    """Ensure each test starts and ends with the default verifier seam."""
    ws.configure_ws_session_verifier(None)
    yield
    ws.configure_ws_session_verifier(None)


# ---------------------------------------------------------------------------
# Credential extraction (cookie-first, query-param fallback)
# ---------------------------------------------------------------------------


class TestExtractSessionCredential:
    def test_cookie_first(self):
        websocket = _make_ws(
            query_params={"token": "query-token"},
            headers={"cookie": "foo=bar; sAccessToken=cookie-token; baz=qux",
                     "anti-csrf": "csrf-1"},
        )
        token, anti_csrf = ws._extract_session_credential(websocket)
        assert token == "cookie-token"
        assert anti_csrf == "csrf-1"

    def test_query_param_fallback(self):
        websocket = _make_ws(query_params={"token": "query-token"}, headers={})
        token, anti_csrf = ws._extract_session_credential(websocket)
        assert token == "query-token"
        assert anti_csrf is None

    def test_no_credential(self):
        websocket = _make_ws(query_params={}, headers={})
        token, anti_csrf = ws._extract_session_credential(websocket)
        assert token == ""
        assert anti_csrf is None


# ---------------------------------------------------------------------------
# _authenticate_tenant — SuperTokens path
# ---------------------------------------------------------------------------


class TestAuthenticateTenantSupertokens:
    @pytest.mark.asyncio
    async def test_valid_session_returns_tenant(self):
        async def fake_verify(access_token, anti_csrf):
            assert access_token == "cookie-token"
            return {"tenant_id": "t-1", "roles": ["admin"]}

        ws.configure_ws_session_verifier(fake_verify)
        websocket = _make_ws(headers={"cookie": "sAccessToken=cookie-token"})

        with patch("config.settings.get_settings", return_value=_make_settings()):
            result = await ws._authenticate_tenant(websocket)

        assert result == "t-1"

    @pytest.mark.asyncio
    async def test_no_session_returns_none(self):
        async def fake_verify(access_token, anti_csrf):
            return None

        ws.configure_ws_session_verifier(fake_verify)
        websocket = _make_ws(query_params={"token": "x"})

        with patch("config.settings.get_settings", return_value=_make_settings()):
            result = await ws._authenticate_tenant(websocket)

        assert result is None

    @pytest.mark.asyncio
    async def test_session_without_tenant_returns_none(self):
        async def fake_verify(access_token, anti_csrf):
            return {"roles": ["admin"]}  # no tenant_id

        ws.configure_ws_session_verifier(fake_verify)
        websocket = _make_ws(query_params={"token": "x"})

        with patch("config.settings.get_settings", return_value=_make_settings()):
            result = await ws._authenticate_tenant(websocket)

        assert result is None

    @pytest.mark.asyncio
    async def test_supertokens_does_not_fall_back_to_legacy(self):
        """In ``supertokens`` mode a legacy JWT in ?token is not accepted."""
        from jose import jwt as jose_jwt

        legacy_token = jose_jwt.encode(
            {"tenant_id": "t-legacy"}, "test-secret", algorithm="HS256"
        )

        async def fake_verify(access_token, anti_csrf):
            return None  # no SuperTokens session

        ws.configure_ws_session_verifier(fake_verify)
        websocket = _make_ws(query_params={"token": legacy_token})

        with patch("config.settings.get_settings",
                   return_value=_make_settings(auth_provider="supertokens")):
            result = await ws._authenticate_tenant(websocket)

        assert result is None


# ---------------------------------------------------------------------------
# _authenticate_driver — SuperTokens path
# ---------------------------------------------------------------------------


class TestAuthenticateDriverSupertokens:
    @pytest.mark.asyncio
    async def test_valid_session_returns_tenant_and_driver(self):
        async def fake_verify(access_token, anti_csrf):
            return {"tenant_id": "t-1", "driver_id": "d-1"}

        ws.configure_ws_session_verifier(fake_verify)
        websocket = _make_ws(headers={"cookie": "sAccessToken=tok"})

        with patch("config.settings.get_settings", return_value=_make_settings()):
            result = await ws._authenticate_driver(websocket)

        assert result == ("t-1", "d-1")

    @pytest.mark.asyncio
    async def test_session_missing_driver_returns_none(self):
        async def fake_verify(access_token, anti_csrf):
            return {"tenant_id": "t-1"}

        ws.configure_ws_session_verifier(fake_verify)
        websocket = _make_ws(headers={"cookie": "sAccessToken=tok"})

        with patch("config.settings.get_settings", return_value=_make_settings()):
            result = await ws._authenticate_driver(websocket)

        assert result is None


# ---------------------------------------------------------------------------
# dual-mode fallback to legacy JWT
# ---------------------------------------------------------------------------


class TestDualModeFallback:
    @pytest.mark.asyncio
    async def test_prefers_supertokens_session(self):
        async def fake_verify(access_token, anti_csrf):
            return {"tenant_id": "t-st"}

        ws.configure_ws_session_verifier(fake_verify)
        websocket = _make_ws(headers={"cookie": "sAccessToken=tok"})

        with patch("config.settings.get_settings",
                   return_value=_make_settings(auth_provider="dual")):
            result = await ws._authenticate_tenant(websocket)

        assert result == "t-st"

    @pytest.mark.asyncio
    async def test_falls_back_to_legacy_when_no_session(self):
        from jose import jwt as jose_jwt

        legacy_token = jose_jwt.encode(
            {"tenant_id": "t-legacy"}, "test-secret", algorithm="HS256"
        )

        async def fake_verify(access_token, anti_csrf):
            return None  # no SuperTokens session present

        ws.configure_ws_session_verifier(fake_verify)
        websocket = _make_ws(query_params={"token": legacy_token})

        with patch("config.settings.get_settings",
                   return_value=_make_settings(auth_provider="dual")):
            result = await ws._authenticate_tenant(websocket)

        assert result == "t-legacy"


# ---------------------------------------------------------------------------
# Token redaction — the credential value is never logged (Req 7.4, 7.5)
# ---------------------------------------------------------------------------


class TestTokenNeverLogged:
    @pytest.mark.asyncio
    async def test_credential_absent_from_logs_on_failure(self, caplog):
        secret_token = "super-secret-session-token-value"

        async def fake_verify(access_token, anti_csrf):
            raise RuntimeError("verification boom")

        # Use the default verifier path so the SDK-failure log line is exercised.
        ws.configure_ws_session_verifier(None)
        websocket = _make_ws(query_params={"token": secret_token})

        with patch("config.settings.get_settings", return_value=_make_settings()):
            with patch(
                "supertokens_python.recipe.session.asyncio."
                "get_session_without_request_response",
                side_effect=RuntimeError("verification boom"),
            ):
                with caplog.at_level(logging.DEBUG):
                    result = await ws._authenticate_tenant(websocket)

        assert result is None
        assert secret_token not in caplog.text
