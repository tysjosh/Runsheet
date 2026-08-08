"""
Unit tests for bootstrap/middleware.py.

This file used to assert that ``initialize`` registered four pieces of middleware,
and it passed for as long as that was false in production. ``initialize`` runs inside
the lifespan startup, where Starlette refuses ``add_middleware``, so every call raised
and ``bootstrap/__init__.py`` swallowed it. Asserting against a ``MagicMock`` app
hid that completely: ``mock_app.add_middleware`` records a call whether or not the
real thing would have accepted one.

Registration now happens at import time via ``register_at_import``, called from
``main.py``. These tests cover that function and the deliberate no-op ``initialize``.
Whether the middleware actually ends up on the running application is asserted
against the real app object in ``test_middleware_actually_installed.py`` — a mock
cannot answer that question, which is the lesson this file encodes.

Requirements: 1.1, 1.2, 1.7
"""
from unittest.mock import MagicMock, patch

import pytest

from bootstrap.container import ServiceContainer


@pytest.fixture
def settings():
    return MagicMock(
        cors_origins=["http://localhost:3000"],
        rate_limit_requests_per_minute=100,
        rate_limit_ai_requests_per_minute=10,
    )


@pytest.fixture
def container(settings):
    c = ServiceContainer()
    c.settings = settings
    return c


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.add_middleware = MagicMock()
    return app


class TestRegisterAtImport:
    """The import-time registration path that main.py calls."""

    def test_registers_request_id_and_security_headers_and_rate_limits(
        self, mock_app, settings
    ):
        from bootstrap.middleware import register_at_import

        with patch("middleware.rate_limiter.setup_rate_limiting") as mock_rate, \
             patch("middleware.security_headers.setup_security_headers") as mock_security:
            register_at_import(mock_app, settings)

        from middleware.request_id import RequestIDMiddleware

        mock_app.add_middleware.assert_called_once_with(RequestIDMiddleware)
        mock_security.assert_called_once_with(mock_app)
        mock_rate.assert_called_once()
        # The configured limits must be passed through, not defaulted: a silent
        # fallback to slowapi's 100/min would ignore RATE_LIMIT_REQUESTS_PER_MINUTE.
        assert mock_rate.call_args.kwargs["api_rate_limit"] == 100
        assert mock_rate.call_args.kwargs["ai_rate_limit"] == 10

    def test_does_not_register_cors(self, mock_app, settings):
        """
        CORS is main.py's, and must stay there. Registering it here as well would put a
        second CORS layer inside the first, and the inner one would answer preflights
        with whatever origin list this module happened to read.
        """
        from starlette.middleware.cors import CORSMiddleware

        from bootstrap.middleware import register_at_import

        with patch("middleware.rate_limiter.setup_rate_limiting"), \
             patch("middleware.security_headers.setup_security_headers"):
            register_at_import(mock_app, settings)

        added = [c.args[0] for c in mock_app.add_middleware.call_args_list if c.args]
        assert CORSMiddleware not in added


class TestInitializeIsANoOp:
    """``initialize`` must not attempt registration; it would raise and be swallowed."""

    @pytest.mark.asyncio
    async def test_adds_no_middleware(self, mock_app, container):
        from bootstrap.middleware import initialize

        await initialize(mock_app, container)

        mock_app.add_middleware.assert_not_called()

    @pytest.mark.asyncio
    async def test_still_satisfies_the_bootstrap_contract(self, mock_app, container):
        """
        It stays in ``_BOOT_ORDER`` and stays awaitable. Removing it would change the
        documented boot sequence, which several tests pin by exact list and length.
        """
        from bootstrap import _BOOT_ORDER
        from bootstrap.middleware import initialize

        assert "middleware" in _BOOT_ORDER
        assert await initialize(mock_app, container) is None
