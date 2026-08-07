"""
Guard: RequestID, rate limiting and security headers are actually installed.

The bug this exists to prevent was not a missing call — ``bootstrap/middleware.py``
called all three, correctly, and every existing test asserted that it did. The calls
raised ``RuntimeError: Cannot add middleware after an application has started``
because ``initialize`` runs inside the lifespan startup, and
``bootstrap/__init__.py`` catches module failures and continues. So the middleware
was absent from the running app on every boot, in every environment, while the unit
tests were green: they asserted the *call*, and the call happened.

These tests therefore assert on the OUTCOME — the middleware present on the app
object and the behaviour visible in a response — rather than on any function being
invoked. That distinction is the whole point of the file.

Verified against the deployed API before the fix: no ``x-request-id``, no
``content-security-policy``, no ``x-frame-options``, no ``x-content-type-options``.
"""

import pytest
from fastapi.testclient import TestClient

from middleware.request_id import REQUEST_ID_HEADER, RequestIDMiddleware
from middleware.security_headers import SecurityHeadersMiddleware


def _installed_middleware_classes(app) -> list:
    """The middleware classes registered on an app, innermost-last ordering aside."""
    return [m.cls for m in app.user_middleware]


class TestMiddlewareIsOnTheApp:
    """Presence on the real application object, not on a mock."""

    def test_request_id_middleware_is_installed(self):
        import main

        assert RequestIDMiddleware in _installed_middleware_classes(main.app), (
            "RequestIDMiddleware is not on the app. It was previously added from "
            "bootstrap/middleware.py inside the lifespan, where add_middleware raises."
        )

    def test_security_headers_middleware_is_installed(self):
        import main

        assert SecurityHeadersMiddleware in _installed_middleware_classes(main.app), (
            "SecurityHeadersMiddleware is not on the app."
        )

    def test_rate_limit_handler_is_registered(self):
        """
        The limits themselves work without this — ``@limiter.limit`` closes over the
        module-level Limiter. What needs registering is the handler that turns an
        over-limit request into a 429 rather than letting it fall through to the
        generic 500 path.
        """
        import main
        from slowapi.errors import RateLimitExceeded

        assert RateLimitExceeded in main.app.exception_handlers, (
            "No RateLimitExceeded handler: an over-limit request would return 500 "
            "with no Retry-After instead of 429."
        )
        assert getattr(main.app.state, "limiter", None) is not None, (
            "app.state.limiter unset; the default slowapi handler dereferences it."
        )

    def test_cors_remains_the_outermost_layer(self):
        """
        CORS is added last in main.py and Starlette inserts at position 0, so the last
        added is outermost. It has to stay there: the auth gate returns 401 and the
        limiter returns 429 from inside, and neither would carry CORS headers if
        something were registered outside CORS. A browser would then report those as
        opaque network failures rather than as the status codes they are.
        """
        from starlette.middleware.cors import CORSMiddleware

        import main

        classes = _installed_middleware_classes(main.app)
        assert classes[0] is CORSMiddleware, (
            f"CORS is not outermost; stack starts with {classes[0]}"
        )
        assert classes.index(SecurityHeadersMiddleware) > 0
        assert classes.index(RequestIDMiddleware) > classes.index(
            SecurityHeadersMiddleware
        ), "RequestID should sit inside SecurityHeaders so 401s still get a request id"


class TestObservableBehaviour:
    """What a client actually sees, which is what was missing in production."""

    @pytest.fixture(scope="class")
    def client(self):
        import main

        # raise_server_exceptions=False so a handler failure surfaces as a response
        # rather than propagating and masking the header assertions.
        return TestClient(main.app, raise_server_exceptions=False)

    def test_response_carries_a_request_id_header(self, client):
        response = client.get("/api/health")
        assert REQUEST_ID_HEADER in response.headers, (
            f"no {REQUEST_ID_HEADER} on the response; correlation is broken"
        )
        assert response.headers[REQUEST_ID_HEADER]

    def test_supplied_request_id_is_echoed_rather_than_replaced(self, client):
        """A caller-supplied id must survive, or traces break at the service boundary."""
        supplied = "test-correlation-id-9f3a"
        response = client.get("/api/health", headers={REQUEST_ID_HEADER: supplied})
        assert response.headers[REQUEST_ID_HEADER] == supplied

    def test_response_carries_security_headers(self, client):
        response = client.get("/api/health")
        assert response.headers.get("x-content-type-options") == "nosniff"
        assert response.headers.get("x-frame-options") == "DENY"
        assert "content-security-policy" in response.headers, (
            "no CSP on API responses"
        )
