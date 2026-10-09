"""Unit tests for ``bootstrap.inventory.initialize``.

This module had zero test coverage, which the changed-file coverage gate
caught after the Elasticsearch index/lifecycle removal dropped its
``setup_inventory_indices()`` call (Phase 6 of the ES→Postgres migration —
the document plane is now a Postgres table, so there is no index to create
or validate at boot).

These tests pin what remains: the bootstrap module wires ``InventoryService``
and ``TenantInventoryConfigService`` onto the container and configures the
inventory API, with the WS manager and Redis client both being optional
(fail-open) dependencies — a tenant with neither still gets a working
inventory service.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bootstrap.container import ServiceContainer
import bootstrap.inventory as bootstrap_inventory


def _es() -> MagicMock:
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    es.index_document = AsyncMock()
    es.update_document = AsyncMock()
    return es


def _container(*, with_ws_manager: bool = False, with_redis: bool = False) -> ServiceContainer:
    container = ServiceContainer()
    container.es_service = _es()
    if with_ws_manager:
        container.fleet_ws_manager = MagicMock()
    if with_redis:
        container.redis_client = MagicMock()
    return container


class TestServiceRegistration:
    """The two services this module exists to construct and register."""

    @pytest.mark.asyncio
    async def test_registers_inventory_service(self):
        container = _container()

        await bootstrap_inventory.initialize(MagicMock(), container)

        assert container.has("inventory_service")

    @pytest.mark.asyncio
    async def test_registers_tenant_inventory_config(self):
        container = _container()

        await bootstrap_inventory.initialize(MagicMock(), container)

        assert container.has("tenant_inventory_config")

    @pytest.mark.asyncio
    async def test_inventory_service_is_constructed_with_the_container_es_service(self):
        container = _container()
        es = container.es_service

        with patch("inventory.service.InventoryService") as mock_cls:
            mock_cls.return_value = MagicMock()
            await bootstrap_inventory.initialize(MagicMock(), container)

        mock_cls.assert_called_once()
        _, kwargs = mock_cls.call_args
        assert mock_cls.call_args[0][0] is es or kwargs.get("es_service") is es


class TestOptionalWsManager:
    """The fleet WS manager (for stock alerts) is optional, not required."""

    @pytest.mark.asyncio
    async def test_boots_without_a_ws_manager(self):
        container = _container(with_ws_manager=False)

        # Must not raise despite no fleet_ws_manager on the container.
        await bootstrap_inventory.initialize(MagicMock(), container)

        assert container.has("inventory_service")

    @pytest.mark.asyncio
    async def test_passes_the_ws_manager_through_when_present(self):
        container = _container(with_ws_manager=True)
        ws_manager = container.fleet_ws_manager

        with patch("inventory.service.InventoryService") as mock_cls:
            mock_cls.return_value = MagicMock()
            await bootstrap_inventory.initialize(MagicMock(), container)

        _, kwargs = mock_cls.call_args
        assert kwargs.get("ws_manager") is ws_manager


class TestOptionalRedisClient:
    """TenantInventoryConfigService is fail-open without Redis."""

    @pytest.mark.asyncio
    async def test_boots_without_a_redis_client(self):
        container = _container(with_redis=False)

        await bootstrap_inventory.initialize(MagicMock(), container)

        assert container.has("tenant_inventory_config")

    @pytest.mark.asyncio
    async def test_passes_the_redis_client_through_when_present(self):
        container = _container(with_redis=True)
        redis_client = container.redis_client

        with patch("inventory.tenant_config.TenantInventoryConfigService") as mock_cls:
            mock_cls.return_value = MagicMock()
            await bootstrap_inventory.initialize(MagicMock(), container)

        _, kwargs = mock_cls.call_args
        assert kwargs.get("redis_client") is redis_client


class TestApiWiring:
    """The inventory API must be configured with the constructed service."""

    @pytest.mark.asyncio
    async def test_configures_the_inventory_api(self):
        container = _container()

        with patch("inventory.api.endpoints.configure_inventory_api") as mock_configure:
            await bootstrap_inventory.initialize(MagicMock(), container)

        mock_configure.assert_called_once()
        _, kwargs = mock_configure.call_args
        assert kwargs.get("inventory_service") is container.inventory_service
