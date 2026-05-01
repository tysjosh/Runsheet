"""
Unit tests for bootstrap/middleware.py.

Requirements: 1.1, 1.2, 1.7
"""
from unittest.mock import MagicMock, patch
import sys

import pytest

from bootstrap.container import ServiceContainer


@pytest.fixture
def container():
    c = ServiceContainer()
    c.settings = MagicMock(
        cors_origins=["http://localhost:3000"],
        rate_limit_requests_per_minute=100,
        rate_limit_ai_requests_per_minute=10,
    )
    return c


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.add_middleware = MagicMock()
    return app


class TestMiddlewareBootstrap:
    """Tests for bootstrap/middleware.py initialize()."""

    @pytest.mark.asyncio
    async def test_registers_all_middleware(self, mock_app, container):
        """Verify CORS, RequestID, RateLimit, and SecurityHeaders are registered."""
        with patch("middleware.rate_limiter.setup_rate_limiting") as mock_rate, \
             patch("middleware.security_headers.setup_security_headers") as mock_security:

            if "bootstrap.middleware" in sys.modules:
                del sys.modules["bootstrap.middleware"]
            from bootstrap.middleware import initialize
            await initialize(mock_app, container)

            # CORS and RequestID are added via add_middleware
            assert mock_app.add_middleware.call_count >= 2
            mock_rate.assert_called_once()
            mock_security.assert_called_once_with(mock_app)
