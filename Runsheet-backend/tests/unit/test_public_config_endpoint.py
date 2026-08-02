"""
Unit tests for ``GET /api/auth/public-config``.

The endpoint exists so a pre-auth client (the driver app's sign-in screen) can
learn the Runsheet web app origin from the one authoritative holder of it — the
backend, which is what ``InputAppInfo(website_domain=...)`` is built from and
therefore the origin SuperTokens mints password-reset links against.

Being unauthenticated, the thing worth pinning hardest is not the happy path but
the **shape**: the response must stay a single non-secret field. The key-set test
below is the guard rail — widening the body to carry, say, the SuperTokens
connection URI or API key turns an allowlisted public route into a config
disclosure, and that must not be possible without a test going red.

Covered here:
  * the response value and shape for a configured origin;
  * the exact response key set, and the explicit absence of every credential
    field a careless edit might reach for;
  * that the route is public via the *allowlist mechanism* — asserted both
    against ``is_public_route`` and end-to-end through the real
    ``AuthEnforcementMiddleware`` with a verifier that yields no session;
  * a blank ``supertokens_website_domain`` → ``null`` with HTTP **200**;
  * that the response is caller-independent (no request-derived echo).

Validates: SuperTokens Auth Migration Req 6.3, 10.1
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth.api.public_config_endpoints as public_config_endpoints
from middleware.auth_enforcement import (
    PUBLIC_CONFIG_ROUTES,
    AuthEnforcementMiddleware,
    is_public_route,
)
from ops.middleware.tenant_guard import configure_session_verifier

PUBLIC_CONFIG_PATH = "/api/auth/public-config"

#: Every field an edit might plausibly (and wrongly) add to a "config" endpoint.
#: None of these may ever appear in the body of a route reachable without a
#: session.
FORBIDDEN_KEYS = (
    "supertokens_connection_uri",
    "supertokens_api_key",
    "connection_uri",
    "api_key",
    "smtp_password",
    "smtp_username",
    "database_url",
    "elasticsearch_url",
    "elastic_api_key",
    "redis_url",
    "jwt_secret",
    "openai_api_key",
    "stripe_secret_key",
    "tenant_id",
)


def _client() -> TestClient:
    """A bare app with only the public-config router mounted."""
    app = FastAPI()
    app.include_router(public_config_endpoints.router)
    return TestClient(app)


def _settings(website_domain: str) -> MagicMock:
    return MagicMock(supertokens_website_domain=website_domain)


def _get(website_domain: str, **request_kwargs):
    """Issue the request with ``supertokens_website_domain`` set as given."""
    with patch.object(
        public_config_endpoints,
        "get_settings",
        return_value=_settings(website_domain),
    ):
        return _client().get(PUBLIC_CONFIG_PATH, **request_kwargs)


class TestPublicConfigResponseShape:
    """The body is the web app origin and nothing else."""

    def test_returns_the_configured_website_domain(self):
        response = _get("https://app.runsheet.example")

        assert response.status_code == 200
        assert response.json() == {"website_domain": "https://app.runsheet.example"}

    def test_normalizes_whitespace_and_trailing_slashes(self):
        """A client can concatenate a path onto the value as-is."""
        response = _get("  https://app.runsheet.example//  ")

        assert response.status_code == 200
        assert response.json()["website_domain"] == "https://app.runsheet.example"

    def test_response_key_set_is_exactly_the_website_domain(self):
        """Pin the key set so this public route cannot widen into a config dump.

        This is the security-relevant assertion in the file: the endpoint is
        reachable without a session, so every additional field would be
        disclosed to an unauthenticated caller. Adding one must fail here.
        """
        response = _get("https://app.runsheet.example")

        assert set(response.json().keys()) == {"website_domain"}

    @pytest.mark.parametrize("forbidden", FORBIDDEN_KEYS)
    def test_no_credential_or_connection_field_is_returned(self, forbidden: str):
        """Named guard against the specific fields that must never leak."""
        body = _get("https://app.runsheet.example").json()

        assert forbidden not in body

    def test_api_domain_and_base_path_are_deliberately_absent(self):
        """A caller already knows the API origin — it just called it.

        Excluded to keep the body as narrow as the driver app's actual need
        (``EXPO_PUBLIC_API_BASE_URL`` already carries the API origin).
        """
        body = _get("https://app.runsheet.example").json()

        assert "api_domain" not in body
        assert "api_base_path" not in body

    def test_response_is_identical_for_every_caller(self):
        """No tenant data and no request-derived echo.

        Two requests differing in every header a caller controls return the same
        body, so the endpoint cannot be used as an oracle for anything about the
        requester.
        """
        first = _get(
            "https://app.runsheet.example",
            headers={"X-Tenant-Id": "tenant-A", "Authorization": "Bearer nonsense"},
        )
        second = _get(
            "https://app.runsheet.example",
            headers={"X-Tenant-Id": "tenant-B"},
        )

        assert first.json() == second.json()


class TestBlankWebsiteDomain:
    """A deployment with no configured origin answers 200 with ``null``."""

    @pytest.mark.parametrize("blank", ["", "   ", "/"])
    def test_blank_setting_returns_null_with_http_200(self, blank: str):
        """Null, not a 5xx.

        The client contract for an unknown origin is already "render no
        affordance", which ``null`` expresses directly; and a public endpoint
        whose status code varies with deployment configuration would let an
        unauthenticated caller distinguish deployment states.
        """
        response = _get(blank)

        assert response.status_code == 200
        assert response.json() == {"website_domain": None}
        assert set(response.json().keys()) == {"website_domain"}


class _NoSessionVerifier:
    """A ``SessionVerifier`` that never yields a verified session."""

    async def verify(self, request):  # noqa: ANN001 - mirrors the protocol seam
        return None


class TestReachableWithoutASession:
    """The route is public through the allowlist mechanism, not by luck."""

    def teardown_method(self, method):
        configure_session_verifier(None)

    def test_path_is_on_the_public_route_allowlist(self):
        assert PUBLIC_CONFIG_ROUTES == frozenset({PUBLIC_CONFIG_PATH})
        assert is_public_route(PUBLIC_CONFIG_PATH) is True

    def test_sibling_api_auth_routes_stay_protected(self):
        """The entry is one exact path, not an ``/api/auth`` prefix."""
        assert is_public_route("/api/auth") is False
        assert is_public_route("/api/auth/account/me") is False
        assert is_public_route("/api/auth/admin/password-reset-link") is False
        assert is_public_route("/api/auth/public-config/extra") is False

    def test_enforcement_middleware_lets_a_sessionless_request_through(self):
        """End-to-end through the real gate with no session on the request.

        Asserting against the middleware — not just the handler — is the point:
        it proves the allowlist entry, and not some incidental absence of a
        dependency, is what makes the route reachable pre-auth.
        """
        app = FastAPI()
        app.add_middleware(AuthEnforcementMiddleware)
        app.include_router(public_config_endpoints.router)
        configure_session_verifier(_NoSessionVerifier())

        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "config.settings.get_settings",
            return_value=MagicMock(auth_provider="supertokens"),
        ), patch.object(
            public_config_endpoints,
            "get_settings",
            return_value=_settings("https://app.runsheet.example"),
        ):
            response = client.get(PUBLIC_CONFIG_PATH)

        assert response.status_code == 200, response.text
        assert response.json() == {"website_domain": "https://app.runsheet.example"}

    def test_the_same_gate_still_rejects_a_sibling_auth_route(self):
        """Guard that the middleware in the test above is genuinely enforcing."""
        app = FastAPI()
        app.add_middleware(AuthEnforcementMiddleware)

        @app.get("/api/auth/account/me")
        def _me():  # pragma: no cover - must never execute
            return {"reached": True}

        configure_session_verifier(_NoSessionVerifier())
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "config.settings.get_settings",
            return_value=MagicMock(auth_provider="supertokens"),
        ):
            response = client.get("/api/auth/account/me")

        assert response.status_code == 401, response.text
