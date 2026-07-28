"""
Shared pytest fixtures and configuration for all tests.
"""
# Register the real Elasticsearch service module before any test module is
# collected. Many test files call
# ``sys.modules.setdefault("services.elasticsearch_service", MagicMock())`` at
# import time; whichever was collected first poisoned the shared module slot
# for the whole session, so later production modules importing the real class
# bound a MagicMock and their tests failed in-suite (but passed in isolation).
# Importing the real module here makes those ``setdefault`` calls no-ops.
# ``ElasticsearchService.connect()`` short-circuits under ENVIRONMENT=test, so
# this performs no network I/O.
import services.elasticsearch_service  # noqa: F401,E402

import pytest
import asyncio
from typing import Generator, AsyncGenerator
from unittest.mock import MagicMock, AsyncMock

# Hypothesis configuration for property-based testing (Requirement 11.1)
from hypothesis import settings, Verbosity, Phase

# Configure Hypothesis profiles for different environments
# Default profile: balanced for local development
settings.register_profile(
    "default",
    max_examples=100,
    verbosity=Verbosity.normal,
    deadline=None,  # Disable deadline for async tests
    print_blob=True,  # Print failing examples for debugging
)

# CI profile: more thorough testing for continuous integration
settings.register_profile(
    "ci",
    max_examples=200,
    verbosity=Verbosity.verbose,
    deadline=None,
    print_blob=True,
    derandomize=True,  # Reproducible results in CI
)

# Debug profile: minimal examples for quick debugging
settings.register_profile(
    "debug",
    max_examples=10,
    verbosity=Verbosity.verbose,
    deadline=None,
    print_blob=True,
    phases=[Phase.explicit, Phase.reuse, Phase.generate],  # Skip shrinking for speed
)

# Fast profile: quick smoke tests
settings.register_profile(
    "fast",
    max_examples=20,
    verbosity=Verbosity.normal,
    deadline=None,
)

# Load profile from environment variable HYPOTHESIS_PROFILE, default to "default"
import os
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Generator[None, None, None]:
    """Clear the cached ``Settings`` singleton around every test.

    ``config.settings.get_settings`` is an ``lru_cache``d singleton. Several
    tests (notably ``tests/unit/test_settings.py``) build settings inside
    ``patch.dict(os.environ, ..., clear=True)``, which wipes ``ENVIRONMENT=test``
    and the real Elastic config. Those tests cleared the cache *before* running
    but never *after*, so the cache kept a ``Settings`` object constructed from
    the throwaway environment (``environment=development``, dummy endpoints) and
    every later test in the session silently read that poisoned config. That is
    what made ~117 unit tests fail in-suite while passing in isolation.

    Clearing on both sides of every test makes settings per-test deterministic
    regardless of collection order, without each test file having to remember.
    """
    from config.settings import clear_settings_cache

    clear_settings_cache()
    try:
        yield
    finally:
        clear_settings_cache()


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_elasticsearch() -> MagicMock:
    """Create a mock Elasticsearch client for unit tests."""
    mock = MagicMock()
    mock.search = AsyncMock(return_value={"hits": {"hits": [], "total": {"value": 0}}})
    mock.index = AsyncMock(return_value={"result": "created"})
    mock.bulk = AsyncMock(return_value={"errors": False, "items": []})
    mock.ping = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def mock_redis() -> MagicMock:
    """Create a mock Redis client for unit tests."""
    mock = MagicMock()
    mock.get = AsyncMock(return_value=None)
    mock.setex = AsyncMock(return_value=True)
    mock.delete = AsyncMock(return_value=1)
    mock.ping = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def sample_truck_data() -> dict:
    """Sample truck data for testing."""
    return {
        "truck_id": "TRUCK-001",
        "driver_name": "John Doe",
        "latitude": 37.7749,
        "longitude": -122.4194,
        "status": "active",
        "speed_kmh": 65.5
    }


@pytest.fixture
def sample_location_update() -> dict:
    """Sample location update payload for testing."""
    return {
        "truck_id": "TRUCK-001",
        "latitude": 37.7749,
        "longitude": -122.4194,
        "timestamp": "2024-01-15T10:30:00Z",
        "speed_kmh": 65.5,
        "heading": 180.0
    }


@pytest.fixture
def sample_error_response() -> dict:
    """Sample error response structure for testing."""
    return {
        "error_code": "VALIDATION_ERROR",
        "message": "Invalid request payload",
        "details": {"field": "latitude", "error": "Value out of range"},
        "request_id": "req_test123"
    }
