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

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5
"""
import logging
from unittest.mock import MagicMock, patch

import pytest

import bootstrap.websockets as ws


def _make_settings(auth_provider="supertokens"):
    settings = MagicMock()
    settings.auth_provider = auth_provider
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
    def test_authorization_header_wins_over_cookie_and_query(self):
        """Mobile transport: header → cookie → ?token=. Driver-mobile Req 14.1."""
        websocket = _make_ws(
            query_params={"token": "query-token"},
            headers={
                "authorization": "Bearer header-token",
                "cookie": "sAccessToken=cookie-token",
                "anti-csrf": "csrf-1",
            },
        )
        token, anti_csrf = ws._extract_session_credential(websocket)
        assert token == "header-token"
        assert anti_csrf == "csrf-1"

    def test_authorization_header_only(self):
        websocket = _make_ws(headers={"authorization": "Bearer header-token"})
        token, anti_csrf = ws._extract_session_credential(websocket)
        assert token == "header-token"
        assert anti_csrf is None

    def test_authorization_scheme_is_case_insensitive(self):
        websocket = _make_ws(headers={"authorization": "bearer header-token"})
        token, _ = ws._extract_session_credential(websocket)
        assert token == "header-token"

    def test_non_bearer_authorization_falls_through_to_cookie(self):
        """A non-Bearer scheme must not shadow the existing cookie transport."""
        websocket = _make_ws(
            headers={"authorization": "Basic dXNlcjpwYXNz",
                     "cookie": "sAccessToken=cookie-token"},
        )
        token, _ = ws._extract_session_credential(websocket)
        assert token == "cookie-token"

    def test_empty_bearer_credential_falls_through_to_query(self):
        websocket = _make_ws(
            query_params={"token": "query-token"},
            headers={"authorization": "Bearer "},
        )
        token, _ = ws._extract_session_credential(websocket)
        assert token == "query-token"

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
    async def test_bearer_header_credential_authenticates_driver(self):
        """A mobile handshake carrying the credential in the Authorization
        header authenticates ``/ws/driver``. Driver-mobile Req 14.1, 14.3."""
        seen = {}

        async def fake_verify(access_token, anti_csrf):
            seen["token"] = access_token
            return {"tenant_id": "t-1", "driver_id": "d-1"}

        ws.configure_ws_session_verifier(fake_verify)
        websocket = _make_ws(headers={"authorization": "Bearer mobile-access-token"})

        with patch("config.settings.get_settings", return_value=_make_settings()):
            result = await ws._authenticate_driver(websocket)

        assert result == ("t-1", "d-1")
        assert seen["token"] == "mobile-access-token"

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
# Handshake Origin check — Cross-Site WebSocket Hijacking (staging finding F2)
# ---------------------------------------------------------------------------

_APP_ORIGIN = "https://app.example.test"
_EVIL_ORIGIN = "https://evil.example.com"


class TestHandshakeOrigin:
    """A valid credential from a disallowed Origin is rejected before verify."""

    @pytest.fixture(autouse=True)
    def _cors(self, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", f'["{_APP_ORIGIN}"]')

    @pytest.fixture
    def calls(self):
        seen = []

        async def fake_verify(access_token, anti_csrf):
            seen.append(access_token)
            return {"tenant_id": "t-1", "driver_id": "d-1"}

        ws.configure_ws_session_verifier(fake_verify)
        return seen

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "credential",
        [{"cookie": "sAccessToken=tok"}, {"authorization": "Bearer tok"}],
        ids=["cookie", "bearer"],
    )
    async def test_disallowed_origin_rejected_without_verifying(
        self, calls, credential
    ):
        websocket = _make_ws(headers={**credential, "origin": _EVIL_ORIGIN})

        assert await ws._authenticate_tenant(websocket) is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_null_origin_rejected(self, calls):
        websocket = _make_ws(
            headers={"cookie": "sAccessToken=tok", "origin": "null"}
        )

        assert await ws._authenticate_tenant(websocket) is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_driver_channel_disallowed_origin_rejected(self, calls):
        websocket = _make_ws(
            headers={"authorization": "Bearer tok", "origin": _EVIL_ORIGIN}
        )

        assert await ws._authenticate_driver(websocket) is None
        assert calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin", [_APP_ORIGIN, _APP_ORIGIN + "/"])
    async def test_allowed_origin_with_cookie_accepted(self, calls, origin):
        websocket = _make_ws(
            headers={"cookie": "sAccessToken=tok", "origin": origin}
        )

        assert await ws._authenticate_tenant(websocket) == "t-1"
        assert calls == ["tok"]

    @pytest.mark.asyncio
    async def test_no_origin_with_bearer_accepted(self, calls):
        """Native clients that send no Origin still need a credential."""
        websocket = _make_ws(headers={"authorization": "Bearer tok"})

        assert await ws._authenticate_tenant(websocket) == "t-1"

    @pytest.mark.asyncio
    async def test_same_origin_with_host_accepted(self, calls):
        """React Native sends the target URL as Origin (driver app)."""
        websocket = _make_ws(
            headers={
                "authorization": "Bearer tok",
                "origin": "https://api.example.test",
                "host": "api.example.test",
            }
        )

        assert await ws._authenticate_driver(websocket) == ("t-1", "d-1")

    @pytest.mark.asyncio
    async def test_suffix_lookalike_origin_rejected(self, calls):
        websocket = _make_ws(
            headers={
                "cookie": "sAccessToken=tok",
                "origin": _APP_ORIGIN + ".evil.com",
                "host": "api.example.test",
            }
        )

        assert await ws._authenticate_tenant(websocket) is None

    @pytest.mark.asyncio
    async def test_wildcard_entry_is_not_honoured(self, calls, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", '["*"]')
        websocket = _make_ws(
            headers={"cookie": "sAccessToken=tok", "origin": _EVIL_ORIGIN}
        )

        assert await ws._authenticate_tenant(websocket) is None


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
