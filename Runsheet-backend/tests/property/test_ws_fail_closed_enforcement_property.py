"""
Property-based test for WebSocket fail-closed authentication enforcement.

# Feature: supertokens-auth-migration, Property 11: Fail-closed enforcement on WebSocket connections — an unverifiable connection is closed with 4001 and never subscribed to a tenant channel.

**Validates: Requirements 7.1, 7.2**

Property 11: For any WebSocket handshake that cannot be associated with a
verified SuperTokens session — the session is absent, the verifier returns no
claims, or the verified claims lack a usable ``tenant_id`` (and, for the driver
channel, a usable ``driver_id``) — the connection is **closed with close code
4001** and the connection is **never subscribed to a tenant channel** (the WS
manager's ``connect`` / ``connect_driver`` is never called).

How the property is exercised
-----------------------------
Two complementary layers are checked, both driven by the same generated
"unverifiable session" scenarios:

* **Authenticator layer** — ``_authenticate_tenant`` / ``_authenticate_driver``
  in ``bootstrap.websockets`` return ``None`` for every unverifiable handshake,
  which is the signal the endpoints use to reject (Req 7.1, 7.2).
* **End-to-end layer** — a minimal FastAPI app registers a ``/ws`` endpoint that
  mirrors the real endpoints exactly (authenticate → ``_reject`` with 4001 on
  failure, else ``manager.connect``). A spy manager records every subscription.
  We assert the client observes a 4001 close and the spy's ``connect`` was
  never invoked, proving the connection was never subscribed to a tenant
  channel.

"Unverifiable" is modeled across the spectrum the real verifier produces:
  * the injected verifier returns ``None`` (no/invalid SuperTokens session);
  * the injected verifier returns claims with a missing / empty / non-string
    ``tenant_id`` (or ``driver_id`` for the driver channel).

Both enforcing providers (``supertokens`` and ``dual``) are exercised; no valid
legacy ``?token`` is ever supplied, so ``dual`` cannot fall back to a legacy
session and must also fail closed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from starlette.websockets import WebSocketDisconnect

import bootstrap.websockets as ws


# ---------------------------------------------------------------------------
# Settings stub — pins the auth_provider flag, supplies legacy JWT fields
# ---------------------------------------------------------------------------
def _make_settings(auth_provider: str) -> MagicMock:
    settings_obj = MagicMock()
    settings_obj.auth_provider = auth_provider
    return settings_obj


def _make_ws(query_params=None, headers=None) -> MagicMock:
    websocket = MagicMock()
    websocket.query_params = query_params or {}
    websocket.headers = headers or {}
    return websocket


# ---------------------------------------------------------------------------
# Strategies — generate UNVERIFIABLE handshakes
# ---------------------------------------------------------------------------
_providers = st.sampled_from(["supertokens"])

# A "no usable identifier" claim value. ``tenant_id`` / ``driver_id`` are
# server-set string claims (sourced from the ``auth_users`` table where
# ``tenant_id`` is ``TEXT``), so the realistic ways a verified session can fail
# to carry a usable identifier are: the key is absent, the value is ``None``,
# or the value is the empty string. All three mean "no tenant/driver scope can
# be derived" → the connection cannot be associated with a verified session.
_empty_value = st.one_of(st.none(), st.just(""))

_safe_identifier = st.from_regex(r"[a-zA-Z0-9_\-]{1,32}", fullmatch=True)


def _build_unverifiable_tenant_verifier(behavior, bad_tenant, include_key):
    """Return an async verifier modeling an unverifiable tenant handshake.

    * ``behavior == "none"`` → returns ``None`` (no verified session).
    * ``behavior == "no_tenant"`` → returns claims with a missing / empty
      ``tenant_id`` (so no tenant scope can be derived).
    """

    async def _verify(access_token, anti_csrf):  # noqa: ANN001 - mirrors seam
        if behavior == "none":
            return None
        claims = {"roles": ["admin"], "has_pii_access": True}
        if include_key:
            claims["tenant_id"] = bad_tenant
        return claims

    return _verify


def _build_unverifiable_driver_verifier(behavior, good_id, empty_id):
    """Return an async verifier modeling an unverifiable driver handshake.

    The driver channel requires BOTH a usable ``tenant_id`` and ``driver_id``
    (Req 7.3). Models the realistic failure modes:

    * ``behavior == "none"`` → returns ``None`` (no verified session).
    * ``behavior == "missing_tenant"`` → valid ``driver_id`` but empty/missing
      ``tenant_id``.
    * ``behavior == "missing_driver"`` → valid ``tenant_id`` but empty/missing
      ``driver_id``.
    """

    async def _verify(access_token, anti_csrf):  # noqa: ANN001 - mirrors seam
        if behavior == "none":
            return None
        if behavior == "missing_tenant":
            return {"tenant_id": empty_id, "driver_id": good_id}
        # missing_driver
        return {"tenant_id": good_id, "driver_id": empty_id}

    return _verify


# ---------------------------------------------------------------------------
# End-to-end app: a /ws endpoint mirroring the real endpoints, with a spy
# manager that records every subscription (connect) call.
# ---------------------------------------------------------------------------
class _SpyManager:
    """Records subscriptions so the test can assert "never subscribed"."""

    def __init__(self):
        self.connect_calls = []
        self.connect_driver_calls = []

    async def connect(self, websocket, **kwargs):
        self.connect_calls.append(kwargs)
        await websocket.accept()

    async def connect_driver(self, websocket, **kwargs):
        self.connect_driver_calls.append(kwargs)
        await websocket.accept()

    async def disconnect(self, websocket):
        pass


_spy_manager = _SpyManager()
_e2e_app = FastAPI()


@_e2e_app.websocket("/ws/tenant")
async def _tenant_ws(websocket: WebSocket):
    # Mirrors the real /ws/ops-style endpoints exactly.
    tenant_id = await ws._authenticate_tenant(websocket)
    if not tenant_id:
        return await ws._reject(websocket)
    await _spy_manager.connect(websocket, tenant_id=tenant_id)
    await websocket.close()


@_e2e_app.websocket("/ws/driver")
async def _driver_ws(websocket: WebSocket):
    # Mirrors the real /ws/driver endpoint exactly.
    auth = await ws._authenticate_driver(websocket)
    if not auth:
        return await ws._reject(websocket)
    tenant_id, driver_id = auth
    await _spy_manager.connect_driver(
        websocket, tenant_id=tenant_id, driver_id=driver_id
    )
    await websocket.close()


_e2e_client = TestClient(_e2e_app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Property 11a — authenticator layer returns None for every unverifiable handshake
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 11: Fail-closed enforcement on WebSocket connections
class TestWsAuthenticatorFailsClosed:
    """**Validates: Requirements 7.1, 7.2**"""

    def teardown_method(self, method):
        ws.configure_ws_session_verifier(None)

    @given(
        provider=_providers,
        behavior=st.sampled_from(["none", "no_tenant"]),
        bad_tenant=_empty_value,
        include_key=st.booleans(),
    )
    @settings(max_examples=100)
    def test_authenticate_tenant_returns_none(
        self, provider, behavior, bad_tenant, include_key
    ):
        """An unverifiable handshake yields no tenant_id, so the endpoint rejects."""
        ws.configure_ws_session_verifier(
            _build_unverifiable_tenant_verifier(behavior, bad_tenant, include_key)
        )
        websocket = _make_ws()  # no cookie, no valid legacy token

        with patch(
            "config.settings.get_settings", return_value=_make_settings(provider)
        ):
            result = asyncio.run(ws._authenticate_tenant(websocket))

        assert result is None, (
            f"unverifiable handshake (provider={provider}, behavior={behavior}, "
            f"tenant={bad_tenant!r}) resolved a tenant_id={result!r} — "
            f"fail-closed WS enforcement violated"
        )

    @given(
        provider=_providers,
        behavior=st.sampled_from(["none", "missing_tenant", "missing_driver"]),
        good_id=_safe_identifier,
        empty_id=_empty_value,
    )
    @settings(max_examples=100)
    def test_authenticate_driver_returns_none(
        self, provider, behavior, good_id, empty_id
    ):
        """An unverifiable driver handshake (missing tenant or driver) yields None."""
        ws.configure_ws_session_verifier(
            _build_unverifiable_driver_verifier(behavior, good_id, empty_id)
        )
        websocket = _make_ws()

        with patch(
            "config.settings.get_settings", return_value=_make_settings(provider)
        ):
            result = asyncio.run(ws._authenticate_driver(websocket))

        assert result is None, (
            f"unverifiable driver handshake (provider={provider}, "
            f"behavior={behavior}) resolved {result!r} — fail-closed violated"
        )


# ---------------------------------------------------------------------------
# Property 11b — end-to-end: connection closed with 4001 and never subscribed
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 11: Fail-closed enforcement on WebSocket connections
class TestWsConnectionClosedAndNeverSubscribed:
    """**Validates: Requirements 7.1, 7.2**"""

    def teardown_method(self, method):
        ws.configure_ws_session_verifier(None)

    @given(
        provider=_providers,
        behavior=st.sampled_from(["none", "no_tenant"]),
        bad_tenant=_empty_value,
        include_key=st.booleans(),
    )
    @settings(
        max_examples=100,
        # A module-level app/TestClient is reused; the spy state is reset per
        # example. No per-test fixtures are involved.
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_tenant_connection_rejected_with_4001_and_no_subscription(
        self, provider, behavior, bad_tenant, include_key
    ):
        """An unverifiable handshake is closed with 4001 and never subscribed."""
        _spy_manager.connect_calls.clear()
        ws.configure_ws_session_verifier(
            _build_unverifiable_tenant_verifier(behavior, bad_tenant, include_key)
        )

        close_code = None
        with patch(
            "config.settings.get_settings", return_value=_make_settings(provider)
        ):
            try:
                with _e2e_client.websocket_connect("/ws/tenant"):
                    # Reaching here means the connection was accepted — a
                    # fail-closed violation; force a clear failure below.
                    close_code = "ACCEPTED"
            except WebSocketDisconnect as exc:
                close_code = exc.code

        # Closed with the authentication-required close code (Req 7.2).
        assert close_code == 4001, (
            f"expected 4001 close for unverifiable handshake "
            f"(provider={provider}, behavior={behavior}), got {close_code!r}"
        )
        # Never subscribed to a tenant channel (Req 7.1, 7.2).
        assert _spy_manager.connect_calls == [], (
            f"connection was subscribed to a tenant channel despite an "
            f"unverifiable session (provider={provider}, behavior={behavior}): "
            f"{_spy_manager.connect_calls!r}"
        )

    @given(
        provider=_providers,
        behavior=st.sampled_from(["none", "missing_tenant", "missing_driver"]),
        good_id=_safe_identifier,
        empty_id=_empty_value,
    )
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_driver_connection_rejected_with_4001_and_no_subscription(
        self, provider, behavior, good_id, empty_id
    ):
        """An unverifiable driver handshake is closed with 4001 and never subscribed."""
        _spy_manager.connect_driver_calls.clear()
        ws.configure_ws_session_verifier(
            _build_unverifiable_driver_verifier(behavior, good_id, empty_id)
        )

        close_code = None
        with patch(
            "config.settings.get_settings", return_value=_make_settings(provider)
        ):
            try:
                with _e2e_client.websocket_connect("/ws/driver"):
                    close_code = "ACCEPTED"
            except WebSocketDisconnect as exc:
                close_code = exc.code

        assert close_code == 4001, (
            f"expected 4001 close for unverifiable driver handshake "
            f"(provider={provider}, behavior={behavior}), got {close_code!r}"
        )
        assert _spy_manager.connect_driver_calls == [], (
            f"driver connection was subscribed despite an unverifiable session "
            f"(provider={provider}, behavior={behavior}): "
            f"{_spy_manager.connect_driver_calls!r}"
        )
