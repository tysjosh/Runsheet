"""
Unit tests for the voice self-authentication allowlist in the global auth gate.

# Feature: dinee-voice-integration

The Dinee voice surfaces self-authenticate — Surface A (``POST /voice-intake``)
verifies the Dinee HMAC signature in the bridge/pipeline, and Surface B
(``/voice/*`` read/driver endpoints) authenticates via the per-tenant Bearer
API key in ``fuel.voice.voice_auth.get_voice_tenant``. Because they do their own
authentication (exactly like the ``/webhooks/*`` HMAC routes), they must be
allowlisted from the global SuperTokens session gate so it does not pre-empt
them with a 401.

These tests pin ``middleware.auth_enforcement.is_public_route`` to:
    * return ``True`` for ``/voice-intake`` and the Surface B ``/voice/*`` paths;
    * return ``False`` for the collision paths owned by OTHER routers
      (``/customers/lookup``, ``/orders/lookup``, ``/products``,
      ``/products/validate``, ``/api/orders/x``) so those stay protected.
"""

from __future__ import annotations

import pytest

from middleware.auth_enforcement import (
    VOICE_SELF_AUTH_PREFIXES,
    VOICE_SELF_AUTH_ROUTES,
    is_public_route,
)


# ---------------------------------------------------------------------------
# The voice surfaces are allowlisted (self-authenticating)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/voice-intake",           # Surface A — HMAC in bridge/pipeline
        "/voice/auth/ping",        # Surface B — credential test
        "/voice/customers/lookup",
        "/voice/customers/cust-1/sites",
        "/voice/customers/cust-1/tanks",
        "/voice/customers/cust-1/deliveries",
        "/voice/products/validate",
        "/voice/orders/lookup",
        "/voice/orders/ord-1/status",
        "/voice/orders/ord-1/eta",
        "/voice/drivers/verify",
        "/voice/drivers/drv-1/active-assignment",
        "/voice/drivers/drv-1/assignments/asg-1/reports",
    ],
)
def test_voice_routes_are_public(path):
    """Every self-authenticating voice surface is allowlisted."""
    assert is_public_route(path) is True


# ---------------------------------------------------------------------------
# Collision paths owned by OTHER routers stay protected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/customers/lookup",    # non-voice customer router
        "/orders/lookup",       # non-voice orders router
        "/products",            # non-voice products router
        "/products/validate",   # non-voice products router (not /voice-prefixed)
        "/api/orders/x",        # non-voice order status router
        "/customers/cust-1/sites",
        "/drivers/verify",
        "/orders/ord-1/status",
    ],
)
def test_non_voice_collision_paths_stay_protected(path):
    """Bare (non-``/voice``) paths owned by other routers are NOT allowlisted."""
    assert is_public_route(path) is False


# ---------------------------------------------------------------------------
# /voice-intake is an exact path, NOT matched by the /voice/ prefix
# ---------------------------------------------------------------------------


def test_voice_intake_is_exact_and_not_prefix_matched():
    """``/voice-intake`` is a distinct exact route, not under the ``/voice/`` prefix."""
    assert "/voice-intake" in VOICE_SELF_AUTH_ROUTES
    assert VOICE_SELF_AUTH_PREFIXES == ("/voice/",)
    # It is not under the /voice/ prefix, so it only matches via the exact set.
    assert not "/voice-intake".startswith("/voice/")
    assert is_public_route("/voice-intake") is True


def test_voice_prefix_does_not_leak_to_similar_names():
    """A path that merely starts with ``/voice`` (no slash) is not allowlisted."""
    # e.g. a hypothetical sibling router must not be swept in by the prefix.
    assert is_public_route("/voicemail") is False
    assert is_public_route("/voice") is False
