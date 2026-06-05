"""Tests for Phase 6 index retirement: cross-tenant/find_one reads, delete
mirror, and the retired-ES-index write gate.

These cover the pieces that make a *permanent* drop of a migrated index safe:
  * every read path the index served is cut over to Postgres (incl. the
    webhook channel-resolution lookups),
  * service-level deletes remove the PG source-of-truth row, and
  * writes to a retired index are skipped (no silent dynamic-mapping recreate).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.read_repositories import HybridReadRepository
from persistence.repositories import CurrentStateRepository

TENANT = "demo-tenant"


@pytest.fixture
def read_from_pg(monkeypatch):
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()
    assert get_settings().commerce_read_from_postgres is True
    yield
    clear_settings_cache()


async def _seed_channel(channel_id, tenant_id=TENANT, channel_type="api_partner"):
    doc = {
        "channel_id": channel_id, "tenant_id": tenant_id,
        "channel_type": channel_type, "display_name": f"Chan {channel_id}",
        "hmac_secret_ref": f"vault://{channel_id}",
        "supported_schema_versions": ["1.0"],
        "secret_version": 1, "enabled": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    async with session_scope() as s:
        await CurrentStateRepository("intake_channel").upsert(s, doc=doc)


# ---------------------------------------------------------------------------
# Tenant-agnostic get + find_one (webhook channel resolution)
# ---------------------------------------------------------------------------


async def test_get_any_ignores_tenant(engine, read_from_pg):
    await _seed_channel("global-1", tenant_id="tenant-x")
    async with session_scope() as s:
        doc = await HybridReadRepository("intake_channel").get_any(s, "global-1")
    assert doc is not None
    assert doc["tenant_id"] == "tenant-x"


async def test_find_one_matches_document_field(engine, read_from_pg):
    await _seed_channel("disp-1", channel_type="dispatcher")
    await _seed_channel("api-1", channel_type="api_partner")
    async with session_scope() as s:
        doc = await HybridReadRepository("intake_channel").find_one(
            s, TENANT, term_filters={"channel_type": "dispatcher"})
    assert doc is not None
    assert doc["channel_id"] == "disp-1"


async def test_find_one_tenant_isolated(engine, read_from_pg):
    await _seed_channel("other-disp", tenant_id="other", channel_type="dispatcher")
    async with session_scope() as s:
        doc = await HybridReadRepository("intake_channel").find_one(
            s, TENANT, term_filters={"channel_type": "dispatcher"})
    assert doc is None


async def test_repo_webhook_lookups_served_from_postgres(engine, read_from_pg):
    await _seed_channel("chan-resolve", channel_type="dispatcher")

    def _es_guard():
        es = AsyncMock()
        es.search_documents = AsyncMock(
            side_effect=AssertionError("ES read used after cutover"))
        return es

    from fuel.intake_channel_repository import IntakeChannelRepository
    repo = IntakeChannelRepository(_es_guard(), credentials_vault=AsyncMock())

    by_id = await repo.get_by_channel_id("chan-resolve")
    assert by_id is not None and by_id.channel_id == "chan-resolve"

    disp = await repo.get_dispatcher_channel(TENANT)
    assert disp is not None and disp.channel_id == "chan-resolve"


# ---------------------------------------------------------------------------
# Delete mirror — service delete removes the PG source-of-truth row
# ---------------------------------------------------------------------------


async def test_current_state_delete_removes_row(engine, read_from_pg):
    await _seed_channel("to-delete")
    async with session_scope() as s:
        ok = await CurrentStateRepository("intake_channel").delete(s, TENANT, "to-delete")
    assert ok is True
    async with session_scope() as s:
        doc = await HybridReadRepository("intake_channel").get(s, TENANT, "to-delete")
    assert doc is None


async def test_current_state_delete_tenant_isolated(engine, read_from_pg):
    await _seed_channel("owned", tenant_id="owner")
    async with session_scope() as s:
        ok = await CurrentStateRepository("intake_channel").delete(s, TENANT, "owned")
    assert ok is False  # wrong tenant cannot delete
    async with session_scope() as s:
        assert await HybridReadRepository("intake_channel").get_any(s, "owned") is not None


# ---------------------------------------------------------------------------
# Retired-index write gate
# ---------------------------------------------------------------------------


async def test_retired_index_skips_writes(monkeypatch):
    monkeypatch.setenv("RETIRED_ES_INDICES", "intake_channels,terminals")
    clear_settings_cache()
    assert "intake_channels" in get_settings().retired_es_indices

    from services.elasticsearch_service import ElasticsearchService
    es = ElasticsearchService.__new__(ElasticsearchService)  # no connect()

    assert es._is_retired_index("intake_channels") is True
    assert es._is_retired_index("terminals") is True
    assert es._is_retired_index("customers_current") is False

    # Writes to a retired index are skipped without touching the client.
    es.client = None  # would AttributeError if a write were attempted
    res = await es.index_document("intake_channels", "c1", {"channel_id": "c1"})
    assert res == {"result": "skipped_retired_index"}
    upd = await es.update_document("intake_channels", "c1", {"x": 1})
    assert upd == {"result": "skipped_retired_index"}
    deleted = await es.delete_document("intake_channels", "c1")
    assert deleted is False
    clear_settings_cache()


async def test_non_retired_index_not_gated(monkeypatch):
    monkeypatch.setenv("RETIRED_ES_INDICES", "intake_channels")
    clear_settings_cache()
    from services.elasticsearch_service import ElasticsearchService
    es = ElasticsearchService.__new__(ElasticsearchService)
    assert es._is_retired_index("customers_current") is False
    clear_settings_cache()


class _PassthroughBreaker:
    """Minimal circuit breaker stub that just runs the wrapped coroutine."""

    async def execute(self, fn):
        return await fn()


class _NotFound(Exception):
    """Mimics an elasticsearch NotFoundError carrying a 404 status_code."""

    status_code = 404


async def test_get_document_returns_none_on_404(monkeypatch):
    """A missing document is an expected outcome for existence/idempotency
    probes — get_document returns None quietly, not raising or logging ERROR."""
    from unittest.mock import MagicMock

    from services.elasticsearch_service import ElasticsearchService

    es = ElasticsearchService.__new__(ElasticsearchService)
    es._read_circuit_breaker = _PassthroughBreaker()
    client = MagicMock()
    client.get = MagicMock(side_effect=_NotFound("not_found"))
    es.client = client

    result = await es.get_document("weather_alerts", "missing-id")
    assert result is None


async def test_get_document_raises_on_non_404(monkeypatch):
    """A genuine ES failure (non-404) still raises through the error handler."""
    from unittest.mock import MagicMock

    from errors.exceptions import AppException
    from services.elasticsearch_service import ElasticsearchService

    es = ElasticsearchService.__new__(ElasticsearchService)
    es._read_circuit_breaker = _PassthroughBreaker()

    class _ServerError(Exception):
        status_code = 503

    client = MagicMock()
    client.get = MagicMock(side_effect=_ServerError("unavailable"))
    es.client = client

    with pytest.raises(AppException):
        await es.get_document("weather_alerts", "any-id")
