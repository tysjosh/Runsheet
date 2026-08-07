"""
Tests for the driver-surface post-init assertion in bootstrap/core.py.

``assert_driver_surface_wired`` runs from the post-initialization block in
``bootstrap/__init__.py`` after every module in ``_BOOT_ORDER`` has run. It fails
loudly outside production when ``order_service`` is missing from the container, and
degrades to an ERROR log inside production.

The declared-index half of this file is gone with the Elasticsearch cluster. Six
tests asserted that every index in ``DRIVER_INDEX_MAPPINGS`` was present, that a
missing one was fatal outside production, that it degraded in production, and that
an unconnected or failing client was not treated as a violation. The document store
is one Postgres table created by migration ``0009_es_documents``, so the failure they
guarded — a write auto-creating an index with ``dynamic: true``, silently discarding
the ``dynamic: strict`` declaration — has no mechanism.

**This file also hid a real defect, which is why the fixture below changed.** Phase 5
scoped the index check with ``settings.document_store_is_postgres``; Phase 6 deleted
that property and left the read in ``bootstrap/core.py``. The resulting
``AttributeError`` propagated into the caller's ``except`` in ``bootstrap/__init__.py``,
so the *entire* assertion — including the ``order_service`` check that is the whole
point of it — silently stopped running in every environment. These tests passed
throughout, because the fixture set ``document_store_is_postgres`` on a ``MagicMock``
and thereby supplied an attribute production settings no longer had. It was found by
booting the image with ``ENVIRONMENT=staging``.

So the fixture now uses a **real** ``Settings`` object. A ``MagicMock`` answers every
attribute with a truthy mock, which makes it structurally incapable of catching this
class of bug; a real instance raises ``AttributeError`` exactly as production does.

Validates: Requirements 4.1, 15.12
"""
import pytest
from unittest.mock import MagicMock

from bootstrap.container import ServiceContainer
from bootstrap.core import (
    assert_driver_surface_wired,
    DriverBootstrapMisconfigurationError,
)
from config.settings import Environment, Settings


def _real_settings(environment=Environment.DEVELOPMENT) -> Settings:
    """A genuine ``Settings`` instance, not a mock.

    Deliberately real: the assertion reads attributes off this object, and the only
    way a test can notice that it reads one which no longer exists is for the object
    to be the same type production uses.

    Production validation is strictly harder than development's — it requires a
    database, Redis, the SuperTokens managed-core pair, an LLM credential and
    non-localhost CORS origins — so those are supplied rather than the environment
    being faked. None of them is dialled; only the branch on
    ``settings.environment`` is under test.
    """
    if environment == Environment.DEVELOPMENT:
        return Settings(environment=environment)
    return Settings(
        environment=environment,
        database_url="postgresql+psycopg://u:p@db.internal:5432/runsheet",
        redis_url="redis://redis.internal:6379",
        supertokens_connection_uri="https://core.supertokens.example.com",
        supertokens_api_key="st-managed-core-api-key",
        gemini_api_key="test-gemini-key",
        cors_origins=["https://app.runsheet.example.com"],
        # Pinned, not inherited. ``COMMERCE_BACKBONE_ENABLED`` leaks into
        # ``os.environ`` from earlier tests in a full-suite run, and production
        # refuses that flag without dual-write — so leaving these to the ambient
        # environment made this test pass alone and fail in the suite. Both are set
        # explicitly so the object is self-consistent either way.
        commerce_backbone_enabled=True,
        commerce_dual_write_postgres=True,
    )


@pytest.fixture
def container():
    """Container in a fully-wired development state."""
    c = ServiceContainer()
    c.settings = _real_settings()
    c.order_service = MagicMock()
    c.es_service = MagicMock()
    return c


class TestTheAssertionOnlyReadsSettingsThatExist:
    """The regression that let the whole assertion stop running.

    Not a test of the driver surface at all — a test that this check cannot be
    silently disabled by referring to a setting that has been removed.
    """

    @pytest.mark.asyncio
    async def test_it_completes_against_real_settings(self, container):
        """The assertion must not raise AttributeError on a real Settings object.

        ``bootstrap/__init__.py`` wraps this call in ``except Exception`` and logs a
        WARNING, so any attribute error here does not crash the boot — it removes the
        check. That is why this is asserted directly rather than left to the tests
        below, which would all still pass with the assertion body dead.
        """
        await assert_driver_surface_wired(container)

    @pytest.mark.asyncio
    async def test_a_removed_setting_would_be_caught(self, container):
        """Proves the test above has teeth.

        Reading a non-existent attribute off real settings raises, and the raise is
        what a mock-based fixture converted into a silent pass.
        """
        with pytest.raises(AttributeError):
            _ = container.settings.document_store_is_postgres


class TestDriverSurfaceAssertion:
    """Tests for assert_driver_surface_wired."""

    @pytest.mark.asyncio
    async def test_passes_when_fully_wired(self, container):
        """No raise when order_service is registered."""
        await assert_driver_surface_wired(container)

    @pytest.mark.asyncio
    async def test_raises_when_order_service_absent(self, container):
        """order_service missing is fatal outside production (Requirement 4.1)."""
        c = ServiceContainer()
        c.settings = container.settings
        c.es_service = container.es_service

        with pytest.raises(DriverBootstrapMisconfigurationError) as exc_info:
            await assert_driver_surface_wired(c)

        assert "order_service" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_bare_container_is_skipped(self, caplog):
        """A boot that never reached core is not a driver misconfiguration.

        ``settings`` is the first service ``bootstrap/core.py`` registers. If it
        is absent the whole boot fell over, ``initialize_all`` already logged
        the per-module failure, and this assertion must stay silent so the
        fail-open contract (Requirement 1.5, Correctness Property P3) holds.
        """
        with caplog.at_level("WARNING"):
            await assert_driver_surface_wired(ServiceContainer())

        assert not any(
            record.levelname == "ERROR" for record in caplog.records
        )
        assert any(
            "settings absent from container" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_production_missing_order_service_logs_and_returns(self, caplog):
        """In production a missing order_service logs an ERROR instead of raising."""
        c = ServiceContainer()
        c.settings = _real_settings(Environment.PRODUCTION)
        c.es_service = MagicMock()

        with caplog.at_level("ERROR"):
            await assert_driver_surface_wired(c)

        assert any(
            "order_service" in record.message
            for record in caplog.records
            if record.levelname == "ERROR"
        )

    @pytest.mark.asyncio
    async def test_an_absent_es_service_is_not_a_violation(self, container):
        """Nothing about the document store is this assertion's business now."""
        c = ServiceContainer()
        c.settings = container.settings
        c.order_service = MagicMock()

        await assert_driver_surface_wired(c)
