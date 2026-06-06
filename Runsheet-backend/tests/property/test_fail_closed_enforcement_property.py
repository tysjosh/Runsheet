"""
Property-based test for HTTP fail-closed authentication enforcement.

# Feature: supertokens-auth-migration, Property 1: Fail-closed enforcement on every non-allowlisted route

**Validates: Requirements 1.5, 2.6, 3.1, 3.2, 6.1, 6.2**

Property 1: Fail-closed enforcement on every non-allowlisted route — for any
request to a route that is not on the Public_Route_Allowlist and that does not
present a verifiable SuperTokens session (absent, malformed, tampered, or
expired-without-refresh), the request is rejected with a **401** authentication
error and the route handler **never executes** (no ``TenantContext`` /
``Auth_Context`` is produced).

How the property is exercised
-----------------------------
A FastAPI app is built with the real :class:`AuthEnforcementMiddleware` and a
catch-all *protected* route whose handler increments a module-level sentinel
counter as its very first statement. Crucially the handler does **not** depend
on ``get_tenant_context`` — so the *only* thing standing between the request and
the sentinel is the middleware. If the gate ever let a sessionless request
through, the handler body would run and the sentinel would change, failing the
property. This makes the test a genuine probe of the middleware's enforcement
rather than of the per-handler dependency.

For each example we:

* pick an enforcing ``auth_provider`` (``supertokens`` or ``dual``);
* install a fake :class:`SessionVerifier` via ``configure_session_verifier``
  that models "no verifiable session" — either it returns ``None`` (no session
  token present) or it raises the same ``unauthorized`` (HTTP 401) error the
  real verifier raises for a malformed / tampered / expired session;
* issue a request to a generated, non-allowlisted path with a varied HTTP
  method and **no** ``Authorization: Bearer`` header (so ``dual`` mode cannot
  fall back to a legacy token);

and assert the response is 401 and the sentinel is untouched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from errors.exceptions import unauthorized
from middleware.auth_enforcement import AuthEnforcementMiddleware, is_public_route
from ops.middleware.tenant_guard import (
    VerifiedSession,
    configure_session_verifier,
)


# ---------------------------------------------------------------------------
# Fake SessionVerifier seam — models "no verifiable session"
# ---------------------------------------------------------------------------
class _NoSessionVerifier:
    """A :class:`SessionVerifier` that never yields a verified session.

    Two behaviors model the spectrum of "not verifiable":

    * ``"absent"`` — no SuperTokens session token on the request, so ``verify``
      returns ``None`` (the dual path would then look for a legacy token, of
      which there is none).
    * ``"invalid"`` — a session token is present but malformed / tampered /
      expired, so ``verify`` raises the exact ``unauthorized`` (HTTP 401) error
      the real ``_SuperTokensSessionVerifier`` raises on
      ``SuperTokensSessionError``.
    """

    def __init__(self, behavior: str):
        self._behavior = behavior

    async def verify(self, request):  # noqa: ANN001 - mirrors the protocol seam
        if self._behavior == "invalid":
            raise unauthorized(
                message="Invalid or expired session",
                details={"reason": "SuperTokens session verification failed"},
            )
        return None


# A verifier that, were the gate broken, would happily authenticate — used only
# as a guard assertion is NOT needed here; kept minimal intentionally.


def _build_app(sentinel: dict) -> FastAPI:
    """Build a FastAPI app with the enforcement middleware and a protected
    catch-all handler that records execution in ``sentinel``.

    The handler deliberately has no auth dependency: the middleware is the sole
    gate, so any execution of the handler body proves a fail-closed violation.
    """
    app = FastAPI()
    app.add_middleware(AuthEnforcementMiddleware)

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    def protected_handler(full_path: str):
        # If we ever reach here for a sessionless request, enforcement failed.
        sentinel["executed"] += 1
        return {"ok": True, "path": full_path}

    return app


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Path segments built from URL-safe characters. Paths are assembled from one or
# more segments; non-allowlisted paths are selected via ``assume`` below.
_segment = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=1,
    max_size=12,
)

_paths = st.lists(_segment, min_size=1, max_size=4).map(
    lambda segs: "/" + "/".join(segs)
)

_methods = st.sampled_from(["GET", "POST", "PUT", "PATCH", "DELETE"])
_providers = st.sampled_from(["supertokens", "dual"])
_behaviors = st.sampled_from(["absent", "invalid"])


# ---------------------------------------------------------------------------
# Property 1 — fail-closed enforcement on every non-allowlisted route
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 1: Fail-closed enforcement on every non-allowlisted route
class TestFailClosedEnforcement:
    """**Validates: Requirements 1.5, 2.6, 3.1, 3.2, 6.1, 6.2**"""

    def teardown_method(self, method):
        # Reset the verifier seam so nothing leaks into other tests.
        configure_session_verifier(None)

    @given(
        path=_paths,
        http_method=_methods,
        provider=_providers,
        behavior=_behaviors,
    )
    @settings(
        max_examples=100,
        # A fresh app/TestClient is built per example by design (the route set
        # is fixed but the sentinel must be isolated); suppress the
        # function-scoped-fixture health check (no fixtures are used).
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_sessionless_request_is_rejected_and_handler_never_runs(
        self,
        path: str,
        http_method: str,
        provider: str,
        behavior: str,
    ):
        """A request without a verifiable session to a non-allowlisted route is
        rejected with 401 and the protected handler never executes."""
        # Only non-allowlisted routes are in scope for this property; an
        # allowlisted path (health/docs/auth/webhook) is intentionally public.
        assume(not is_public_route(path))

        sentinel = {"executed": 0}
        app = _build_app(sentinel)
        configure_session_verifier(_NoSessionVerifier(behavior))

        client = TestClient(app, raise_server_exceptions=False)

        fake_settings = MagicMock(auth_provider=provider)
        with patch(
            "config.settings.get_settings", return_value=fake_settings
        ):
            response = client.request(http_method, path)

        # Fail-closed: the request is rejected with a 401 (Req 6.1, 6.2, 3.2).
        assert response.status_code == 401, (
            f"{http_method} {path!r} under provider={provider!r} "
            f"behavior={behavior!r} expected 401, got {response.status_code}: "
            f"{response.text}"
        )

        # The handler never ran — no Auth_Context was produced and no protected
        # logic executed (Req 1.5, 2.6, 3.1).
        assert sentinel["executed"] == 0, (
            f"protected handler executed for sessionless {http_method} {path!r} "
            f"under provider={provider!r} behavior={behavior!r} — "
            f"fail-closed enforcement violated"
        )
