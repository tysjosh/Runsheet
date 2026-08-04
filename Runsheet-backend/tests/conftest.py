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

# Same rationale for ``prometheus_client``: ``tests/unit/test_compatibility_adapters.py``
# installs a ``MagicMock`` under that name at import time (guarded by
# ``if "prometheus_client" not in sys.modules``). When it is collected first,
# every metric built afterwards is a MagicMock, so counter assertions compare
# mock objects instead of numbers. Importing the real module here makes that
# guard a no-op regardless of collection order.
import prometheus_client  # noqa: F401,E402

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


@pytest.fixture(autouse=True)
def _reset_ref_resolver() -> Generator[None, None, None]:
    """Reset the process-wide ``RefResolver`` around every test.

    ``services.ref_resolver.get_ref_resolver`` is a lazily-built process-wide
    singleton, and endpoint modules fall back to it when no resolver is injected
    (e.g. ``fuel_ops_endpoints._get_ref_resolver``). Bootstrap-style tests
    register entity loaders (``customer``, ``order``, ...) on that singleton and
    never unregister them, so unrelated tests later in the session suddenly had
    write-time reference validation switched on and rejected fixture ids that
    do not exist in their fake data.

    ``configure_ref_resolver(None)`` is the module's documented test seam; using
    it on both sides of every test keeps reference validation opt-in per test.

    Several endpoint modules additionally cache their OWN ``_ref_resolver``
    override which takes precedence over the process-wide one
    (``_get_ref_resolver()`` returns the module global when set). A test that
    injects a fake there leaves it installed, so a later test calling the
    documented ``configure_ref_resolver`` seam was silently ignored. Clear those
    module-level overrides too, but only for modules already imported so this
    fixture never forces an import.
    """
    import sys

    from services.ref_resolver import configure_ref_resolver

    # Modules that shadow the process-wide resolver with a module-level override.
    _resolver_override_modules = (
        "fuel.api.order_endpoints",
        "fuel.api.fuel_ops_endpoints",
        "fuel.api.driver_endpoints",
        "scheduling.api.endpoints",
        "compliance.api.asset_certification_endpoints",
        "commerce.api.account_endpoints",
        "commerce.api.invoice_endpoints",
        "commerce.api.payment_endpoints",
    )

    def _reset() -> None:
        configure_ref_resolver(None)
        for name in _resolver_override_modules:
            mod = sys.modules.get(name)
            if mod is not None and getattr(mod, "_ref_resolver", None) is not None:
                mod._ref_resolver = None

    _reset()
    try:
        yield
    finally:
        _reset()


#: First observed route inventory of ``main.app``, captured by
#: :func:`_guard_main_app_routes` the first time a test leaves ``main`` imported.
_MAIN_APP_ROUTE_BASELINE: "list[tuple] | None" = None


@pytest.fixture(autouse=True)
def _guard_main_app_routes() -> Generator[None, None, None]:
    """Fail the test that mutates ``main.app``'s route table, not a later one.

    ``main.app`` is a module-level singleton cached in ``sys.modules``, so every
    test that does ``from main import app`` gets the *same* object. A test that
    mounts a router on it — most easily by using ``TestClient(app)`` as a context
    manager, which runs main's lifespan and boots the real bootstrap chain —
    leaves those routes behind for the rest of the session.

    That is invisible to the polluting test and breaks a later one:
    ``tests/unit/test_endpoint_registry.py`` generates the endpoint registry
    from ``main.app`` and compares it to the committed
    ``docs/endpoint-registry.md``, so extra routes made it fail in-suite while
    passing in isolation.

    This guard compares the route inventory after every test against the first
    one observed and fails immediately on a change, so the pollution is
    attributed to its source and cannot become someone else's flake. A test that
    genuinely needs a booted app should build its own ``FastAPI()`` instance.
    """
    global _MAIN_APP_ROUTE_BASELINE

    yield

    import sys

    main_module = sys.modules.get("main")
    if main_module is None or not hasattr(main_module, "app"):
        return

    routes = [
        (
            getattr(route, "path", None),
            tuple(sorted(getattr(route, "methods", None) or ())),
            type(route).__name__,
        )
        for route in main_module.app.routes
    ]

    if _MAIN_APP_ROUTE_BASELINE is None:
        _MAIN_APP_ROUTE_BASELINE = routes
        return

    if routes == _MAIN_APP_ROUTE_BASELINE:
        return

    baseline = _MAIN_APP_ROUTE_BASELINE
    # Re-baseline so the whole remaining session is not reported as failing.
    _MAIN_APP_ROUTE_BASELINE = routes
    added = [r for r in routes if routes.count(r) > baseline.count(r)]
    removed = [r for r in baseline if baseline.count(r) > routes.count(r)]
    pytest.fail(
        "this test mutated the shared `main.app` route table, which leaks into "
        f"every later test in the session ({len(baseline)} routes before, "
        f"{len(routes)} after).\n"
        f"  added:   {sorted({(p, m) for p, m, _ in added})}\n"
        f"  removed: {sorted({(p, m) for p, m, _ in removed})}\n"
        "Do not run main's lifespan against `main.app` (that boots the real "
        "bootstrap chain and mounts boot-only routers on it): build a local "
        "`FastAPI()` app instead, or construct `TestClient(app)` without using "
        "it as a context manager."
    )


#: A single reusable loop installed by :func:`_ensure_current_event_loop` when a
#: previous test left the thread without one. Reused (rather than creating a
#: fresh loop per test) so the guard cannot leak thousands of loops/file
#: descriptors across a full-suite run.
_SPARE_EVENT_LOOP: "asyncio.AbstractEventLoop | None" = None


def _ensure_current_event_loop() -> None:
    """Guarantee the main thread has a usable current asyncio event loop.

    ``asyncio.run`` — used by dozens of *sync* tests to drive a coroutine —
    installs a fresh loop and, on exit, sets the thread's current event loop
    back to ``None`` *and* marks it as explicitly set. From that moment
    ``asyncio.get_event_loop()`` raises ``RuntimeError: There is no current
    event loop in thread 'MainThread'`` instead of lazily creating one.

    That turns any later sync fixture which drives a coroutine via
    ``asyncio.get_event_loop().run_until_complete(...)`` into a setup ERROR —
    but only when it happens to run after such a test, which is why
    ``tests/integration`` passed alone and errored after ``tests/unit``.

    Restoring a current loop before every test makes the ambient asyncio state
    per-test deterministic regardless of collection order. pytest-asyncio still
    installs and tears down its own loop for async tests; this only repairs the
    *absent/closed* case.
    """
    global _SPARE_EVENT_LOOP

    policy = asyncio.get_event_loop_policy()
    try:
        loop = policy.get_event_loop()
    except RuntimeError:
        loop = None

    if loop is not None and not loop.is_closed():
        return

    if _SPARE_EVENT_LOOP is None or _SPARE_EVENT_LOOP.is_closed():
        _SPARE_EVENT_LOOP = policy.new_event_loop()
    policy.set_event_loop(_SPARE_EVENT_LOOP)


@pytest.fixture(autouse=True)
def _restore_current_event_loop() -> Generator[None, None, None]:
    """Autouse wrapper around :func:`_ensure_current_event_loop`."""
    _ensure_current_event_loop()
    yield


def _supertokens_sdk_live() -> bool:
    """Return whether the SuperTokens SDK singleton is currently initialized.

    Deliberately probes the SDK itself instead of
    ``auth.supertokens_init.is_supertokens_initialized()``: the SDK's ``reset()``
    test hook wipes the SDK singletons but not our module-level ``_initialized``
    flag, so only the SDK knows the truth.
    """
    try:
        from supertokens_python import Supertokens

        Supertokens.get_instance()
    except Exception:
        return False
    return True


@pytest.fixture(autouse=True)
def _restore_supertokens_init() -> Generator[None, None, None]:
    """Re-initialize the SuperTokens SDK if a test tears it down.

    ``main`` initializes the SuperTokens SDK at *import* time (module-level, not
    in the lifespan) because ``auth_provider=supertokens`` under
    ``ENVIRONMENT=test``. Since ``main`` is cached in ``sys.modules``, that
    initialization happens exactly once per session — whichever test imports
    ``main`` first pays for it, and every later test relies on it.

    ``tests/integration/test_supertokens_auth_flow.py`` resets the SDK
    singletons on fixture teardown for its own isolation. When ``main`` was
    imported *before* it (i.e. any earlier test did ``from main import app``,
    such as ``tests/unit/test_endpoint_registry.py``), that reset leaves the
    process with an app whose auth middleware is wired but whose SDK is gone, so
    every subsequent request through ``AuthEnforcementMiddleware`` raised
    ``GeneralError: Initialisation not done`` → HTTP 500. That is what broke
    ``tests/smoke/test_route_smoke.py`` only in the combined run.

    Restoring the SDK when a test leaves it uninitialized keeps that state
    per-test regardless of collection order, and cannot regress if another test
    file starts resetting the SDK too.
    """
    was_live = _supertokens_sdk_live()
    try:
        yield
    finally:
        if was_live and not _supertokens_sdk_live():
            import sys

            from auth.supertokens_init import init_supertokens
            from config.settings import get_settings

            main_module = sys.modules.get("main")
            settings = getattr(main_module, "_auth_settings", None)
            if settings is None:
                settings = get_settings()
            init_supertokens(settings)


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
