"""
Tests for the driver-surface post-init assertion in bootstrap/core.py.

``assert_driver_surface_wired`` runs from the post-initialization block in
``bootstrap/__init__.py`` after every module in ``_BOOT_ORDER`` has run. It
fails loudly outside production when ``order_service`` is missing from the
container or a declared driver index is absent, and degrades to an ERROR log
inside production.

Validates: Requirements 4.1, 15.12
"""
import pytest
from unittest.mock import MagicMock

from bootstrap.container import ServiceContainer
from bootstrap.core import (
    assert_driver_surface_wired,
    DriverBootstrapMisconfigurationError,
)
from config.settings import Environment
from driver.services.driver_es_mappings import DRIVER_INDEX_MAPPINGS


def _es_service(missing_indices=()):
    """An es_service double whose indices.exists reports every index but ``missing``."""
    es_service = MagicMock()
    es_service.client.indices.exists.side_effect = (
        lambda index: index not in set(missing_indices)
    )
    return es_service


@pytest.fixture
def container():
    """Container in a fully-wired development state, on the Elasticsearch backend.

    ``document_store_is_postgres`` is set EXPLICITLY rather than left to the
    ``MagicMock``. A mock answers every attribute with a truthy mock, so when the
    index-presence check learned to skip itself on the Postgres backend, these tests
    silently stopped checking anything and the ``exists`` assertion collapsed to
    ``set() == {11 indices}``. Any new setting this assertion consults has to be
    pinned here for the same reason.
    """
    c = ServiceContainer()
    settings = MagicMock()
    settings.environment = Environment.DEVELOPMENT
    settings.document_store_is_postgres = False
    c.settings = settings
    c.order_service = MagicMock()
    c.es_service = _es_service()
    return c


class TestDriverSurfaceAssertion:
    """Tests for assert_driver_surface_wired."""

    @pytest.mark.asyncio
    async def test_passes_when_fully_wired(self, container):
        """No raise when order_service is registered and every index exists."""
        await assert_driver_surface_wired(container)

    @pytest.mark.asyncio
    async def test_checks_every_declared_index(self, container):
        """Presence is asserted for the whole registry, not a hand-picked subset."""
        await assert_driver_surface_wired(container)

        checked = {
            call.kwargs["index"]
            for call in container.es_service.client.indices.exists.call_args_list
        }
        assert checked == set(DRIVER_INDEX_MAPPINGS)

    @pytest.mark.asyncio
    async def test_index_presence_is_not_asserted_on_the_postgres_backend(
        self, container, caplog
    ):
        """There are no indices to be present, so the check has nothing to say.

        The failure it guards against is a write auto-creating an index with
        ``dynamic: true``. The Postgres document store is one table created by a
        migration, so that cannot happen — and leaving the check in place would
        report all eleven indices missing and refuse to boot, which is exactly what
        happened when the cluster was stopped before this branch existed.

        What ``dynamic: strict`` did provide and jsonb does not is rejection of an
        undeclared field. The compensating control is ``extra="forbid"`` on the
        driver-surface Pydantic models, which is upstream of the store.
        """
        container.settings.document_store_is_postgres = True

        await assert_driver_surface_wired(container)

        container.es_service.client.indices.exists.assert_not_called()

    @pytest.mark.asyncio
    async def test_order_service_is_still_asserted_on_the_postgres_backend(
        self, container
    ):
        """Skipping the index check must not skip the rest of the assertion."""
        container.settings.document_store_is_postgres = True
        c = ServiceContainer()
        c.settings = container.settings
        c.es_service = container.es_service

        with pytest.raises(DriverBootstrapMisconfigurationError) as exc_info:
            await assert_driver_surface_wired(c)

        assert "order_service" in str(exc_info.value)

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
    async def test_raises_when_order_service_absent_on_booted_container(self, container):
        """A booted container (settings present) missing order_service still raises."""
        c = ServiceContainer()
        c.settings = container.settings
        c.es_service = MagicMock()
        c.es_service.client = None

        with pytest.raises(DriverBootstrapMisconfigurationError) as exc_info:
            await assert_driver_surface_wired(c)

        assert "order_service" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_production_missing_order_service_logs_and_returns(
        self, container, caplog
    ):
        """In production a missing order_service logs an ERROR instead of raising."""
        c = ServiceContainer()
        container.settings.environment = Environment.PRODUCTION
        c.settings = container.settings
        c.es_service = container.es_service

        with caplog.at_level("ERROR"):
            await assert_driver_surface_wired(c)

        assert any(
            "order_service" in record.message
            for record in caplog.records
            if record.levelname == "ERROR"
        )

    @pytest.mark.asyncio
    async def test_raises_when_declared_index_absent(self, container):
        """A missing driver index is fatal outside production (Requirement 15.12)."""
        absent = "duty_status_events"
        container.es_service = _es_service(missing_indices=[absent])

        with pytest.raises(DriverBootstrapMisconfigurationError) as exc_info:
            await assert_driver_surface_wired(container)

        assert absent in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_production_degrades_with_error_log(self, container, caplog):
        """In production the same condition logs an ERROR instead of raising."""
        container.settings.environment = Environment.PRODUCTION
        container.es_service = _es_service(missing_indices=["driver_devices"])

        with caplog.at_level("ERROR"):
            await assert_driver_surface_wired(container)

        assert any(
            "driver_devices" in record.message
            for record in caplog.records
            if record.levelname == "ERROR"
        )

    @pytest.mark.asyncio
    async def test_index_check_skipped_without_es_client(self, container):
        """An unconnected cluster is a different fault — it is not a violation."""
        container.es_service = MagicMock()
        container.es_service.client = None

        await assert_driver_surface_wired(container)

    @pytest.mark.asyncio
    async def test_es_failure_does_not_block_boot(self, container):
        """A failing exists() call degrades rather than raising."""
        container.es_service.client.indices.exists.side_effect = RuntimeError("boom")

        await assert_driver_surface_wired(container)
