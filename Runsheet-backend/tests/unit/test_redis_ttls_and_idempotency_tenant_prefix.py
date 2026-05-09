"""
Regression tests for the two code-review findings that small-footprint
fixes landed in this sprint:

* **F17** (Redis writes without TTL) — ``TenantSettingsService``,
  ``TenantInventoryConfigService``, ``RiskRegistry``,
  ``AutonomyConfigService`` all now write per-tenant config keys with
  a 30-day TTL. Each write refreshes the TTL, so active tenants never
  expire and deleted tenants don't leak keys forever.

* **F19** (Idempotency not tenant-scoped) — ``IdempotencyService`` now
  composes tenant-scoped keys (``idemp:{tenant_id}:{event_id}``) so two
  tenants with the same upstream ``event_id`` cannot collide. The
  legacy no-tenant shape is still accepted for internal healthchecks.

Validates code-review findings F17 and F19.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


# Stub out ES at import time so the modules under test don't try to
# connect to a real cluster when the test collector imports their
# transitive dependencies.
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)


# ===========================================================================
# F17 — Redis writes now carry a TTL
# ===========================================================================


_EXPECTED_TTL_SECONDS = 30 * 24 * 60 * 60


class TestTenantSettingsServiceTTL:
    """``TenantSettingsService.set`` must pass ``ex=30d`` to redis.set."""

    @pytest.mark.asyncio
    async def test_set_passes_ttl(self) -> None:
        from services.tenant_settings import (
            TenantSettings,
            TenantSettingsService,
            default_tenant_settings,
        )

        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(return_value=True)
        service = TenantSettingsService(redis_client=mock_redis)

        await service.set("tenant-a", default_tenant_settings())

        mock_redis.set.assert_awaited_once()
        _, kwargs = mock_redis.set.call_args
        assert kwargs.get("ex") == _EXPECTED_TTL_SECONDS, mock_redis.set.call_args


class TestInventoryConfigServiceTTL:
    """Inventory tenant-config writes must carry a TTL."""

    @pytest.mark.asyncio
    async def test_set_settings_passes_ttl(self) -> None:
        from inventory.tenant_config import (
            InventorySettings,
            TenantInventoryConfigService,
        )

        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(return_value=True)
        service = TenantInventoryConfigService(redis_client=mock_redis)

        # Minimal InventorySettings that will JSON-serialise cleanly.
        settings = InventorySettings()

        await service.set_settings("tenant-a", settings)

        mock_redis.set.assert_awaited_once()
        _, kwargs = mock_redis.set.call_args
        assert kwargs.get("ex") == _EXPECTED_TTL_SECONDS


class TestRiskRegistryTTL:
    """Risk override writes must carry a TTL."""

    @pytest.mark.asyncio
    async def test_set_override_passes_ttl(self) -> None:
        from Agents.risk_registry import RiskLevel, RiskRegistry

        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(return_value=True)
        registry = RiskRegistry(redis_client=mock_redis)

        await registry.set_override(
            "cancel_job", RiskLevel.LOW, tenant_id="tenant-a"
        )

        mock_redis.set.assert_awaited_once()
        _, kwargs = mock_redis.set.call_args
        assert kwargs.get("ex") == _EXPECTED_TTL_SECONDS


class TestAutonomyConfigServiceTTL:
    """Autonomy-level writes must carry a TTL."""

    @pytest.mark.asyncio
    async def test_set_level_passes_ttl(self) -> None:
        from Agents.autonomy_config_service import AutonomyConfigService

        mock_redis = MagicMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock(return_value=True)
        service = AutonomyConfigService(redis_client=mock_redis)

        await service.set_level("tenant-a", "auto-low")

        mock_redis.set.assert_awaited_once()
        _, kwargs = mock_redis.set.call_args
        assert kwargs.get("ex") == _EXPECTED_TTL_SECONDS


# ===========================================================================
# F19 — Idempotency keys are tenant-scoped
# ===========================================================================


class TestIdempotencyTenantScopedKeys:
    """``IdempotencyService`` composes ``idemp:{tenant_id}:{event_id}``
    when a tenant id is supplied, preserving the legacy
    ``idemp:{event_id}`` shape for no-tenant callers."""

    def test_tenant_scoped_key(self) -> None:
        from ops.ingestion.idempotency import IdempotencyService

        service = IdempotencyService(redis_url="redis://localhost:6379")
        key = service._get_key("evt-123", tenant_id="tenant-a")
        assert key == "idemp:tenant-a:evt-123"

    def test_legacy_key_when_no_tenant(self) -> None:
        from ops.ingestion.idempotency import IdempotencyService

        service = IdempotencyService(redis_url="redis://localhost:6379")
        key = service._get_key("evt-123")
        assert key == "idemp:evt-123"

    @pytest.mark.asyncio
    async def test_two_tenants_same_event_id_do_not_collide(self) -> None:
        """Tenant A processing ``evt-123`` must not mark it as a
        duplicate for tenant B — the key shapes differ, so Redis
        tracks them independently."""
        from ops.ingestion.idempotency import IdempotencyService

        # In-memory fake that mirrors the Redis ``exists`` + ``setex``
        # contract enough for this test.
        store: set[str] = set()

        class _FakeRedis:
            async def exists(self, key: str) -> int:
                return 1 if key in store else 0

            async def setex(self, key: str, ttl: int, value: str) -> None:
                store.add(key)

        service = IdempotencyService(redis_url="redis://localhost:6379")
        service.client = _FakeRedis()

        await service.mark_processed("evt-123", tenant_id="tenant-a")

        assert await service.is_duplicate("evt-123", tenant_id="tenant-a") is True
        # Same event_id under a different tenant is NOT yet a duplicate.
        assert await service.is_duplicate("evt-123", tenant_id="tenant-b") is False

        # After tenant-b records it, only its own key flips.
        await service.mark_processed("evt-123", tenant_id="tenant-b")
        assert await service.is_duplicate("evt-123", tenant_id="tenant-b") is True

        # The legacy (no-tenant) namespace is still independent.
        assert await service.is_duplicate("evt-123") is False
