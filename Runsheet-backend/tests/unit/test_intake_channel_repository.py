"""
Unit tests for :mod:`fuel.intake_channel_repository`.

Tests cover:
- ``create`` — persists channel, vaults secret, returns plaintext once.
- ``get`` — tenant-scoped retrieval, cross-tenant returns None.
- ``list_for_tenant`` — tenant isolation on listing.
- ``update`` — partial update with protected field stripping.
- ``delete`` — removes channel and vault credential.
- ``rotate_secret`` — generates new secret, bumps secret_version, returns
  new plaintext once.
- Tenant isolation — every query contains tenant_id filter.

Uses a thin fake ES service mirroring the pattern in
``test_fuel_order_repository.py``.

Validates: Requirements 2.1.3, 2.1.4, 2.1.6.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict, List, Optional

from fuel.intake_channel_repository import (
    IntakeChannelRepository,
    IntakeChannelCrossTenantAccessError,
)
from fuel.intake_channel_models import IntakeChannel


# ---------------------------------------------------------------------------
# Fake ES Service
# ---------------------------------------------------------------------------


class FakeESService:
    """Minimal fake ES service that records calls and returns canned data."""

    def __init__(self):
        self.indexed_docs: Dict[str, Dict[str, Any]] = {}
        self.deleted_docs: List[str] = []
        self.search_responses: List[Dict[str, Any]] = []
        self._search_call_count = 0

    async def index_document(self, index: str, doc_id: str, document: dict):
        self.indexed_docs[doc_id] = {"index": index, "document": document}

    async def search_documents(self, index: str, query: dict, size: int = 10):
        if self._search_call_count < len(self.search_responses):
            resp = self.search_responses[self._search_call_count]
            self._search_call_count += 1
            return resp
        return {"hits": {"hits": [], "total": {"value": 0}}}

    async def delete_document(self, index: str, doc_id: str):
        self.deleted_docs.append(doc_id)
        return True

    async def update_document(self, index: str, doc_id: str, partial_doc: dict):
        if doc_id in self.indexed_docs:
            self.indexed_docs[doc_id]["document"].update(partial_doc)


# ---------------------------------------------------------------------------
# Fake Credentials Vault
# ---------------------------------------------------------------------------


class FakeCredentialsVault:
    """Minimal fake vault that records put/get/delete calls."""

    def __init__(self):
        self.stored: Dict[str, Dict[str, Any]] = {}
        self.deleted_refs: List[str] = []
        self._put_count = 100  # Start high to avoid collisions with test data

    async def put(
        self,
        tenant_id: str,
        key: str,
        plaintext: Dict[str, Any],
        provider_name: Optional[str] = None,
        kms_key_id: Optional[str] = None,
    ) -> str:
        self._put_count += 1
        ref = f"cred:{tenant_id}:{key}:{self._put_count}"
        self.stored[ref] = {
            "tenant_id": tenant_id,
            "key": key,
            "plaintext": plaintext,
            "provider_name": provider_name,
        }
        return ref

    async def get(self, tenant_id: str, ref: str) -> Dict[str, Any]:
        if ref not in self.stored:
            raise KeyError(ref)
        entry = self.stored[ref]
        if entry["tenant_id"] != tenant_id:
            raise PermissionError("cross_tenant_credential_access_denied")
        return entry["plaintext"]

    async def delete(self, tenant_id: str, ref: str) -> bool:
        self.deleted_refs.append(ref)
        if ref in self.stored:
            del self.stored[ref]
            return True
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_channel_source(
    channel_id: str = "test-channel-01",
    tenant_id: str = "tenant-a",
    **overrides,
) -> Dict[str, Any]:
    """Build a raw ES source dict for an IntakeChannel."""
    base = {
        "channel_id": channel_id,
        "tenant_id": tenant_id,
        "channel_type": "api_partner",
        "display_name": "Test Channel",
        "hmac_secret_ref": "cred:tenant-a:intake_channel_hmac:test-channel-01:1",
        "supported_schema_versions": ["1.0"],
        "rate_limit_per_minute": None,
        "secret_version": 1,
        "enabled": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def _wrap_in_search_response(sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Wrap source dicts in an ES search response envelope."""
    return {
        "hits": {
            "hits": [{"_source": s} for s in sources],
            "total": {"value": len(sources)},
        }
    }


# ---------------------------------------------------------------------------
# Tests — Create
# ---------------------------------------------------------------------------


class TestCreate:
    """Tests for IntakeChannelRepository.create."""

    @pytest.mark.asyncio
    async def test_create_returns_channel_and_plaintext_secret(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        channel, secret = await repo.create(
            tenant_id="tenant-a",
            channel_id="my-channel-01",
            channel_type="api_partner",
            display_name="My Partner",
            supported_schema_versions=["1.0", "2.0"],
        )

        assert isinstance(channel, IntakeChannel)
        assert channel.channel_id == "my-channel-01"
        assert channel.tenant_id == "tenant-a"
        assert channel.channel_type == "api_partner"
        assert channel.display_name == "My Partner"
        assert channel.supported_schema_versions == ["1.0", "2.0"]
        assert channel.secret_version == 1
        assert channel.enabled is True
        assert isinstance(secret, str)
        assert len(secret) > 0

    @pytest.mark.asyncio
    async def test_create_stores_secret_in_vault(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        channel, secret = await repo.create(
            tenant_id="tenant-a",
            channel_id="my-channel-01",
            channel_type="voice",
            display_name="Voice AI",
            supported_schema_versions=["1.0"],
        )

        # Vault should have one entry
        assert len(vault.stored) == 1
        ref = channel.hmac_secret_ref
        assert ref in vault.stored
        assert vault.stored[ref]["plaintext"] == {"secret": secret}
        assert vault.stored[ref]["tenant_id"] == "tenant-a"
        assert vault.stored[ref]["provider_name"] == "intake_channel"

    @pytest.mark.asyncio
    async def test_create_persists_to_es(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        channel, _ = await repo.create(
            tenant_id="tenant-a",
            channel_id="my-channel-01",
            channel_type="dispatcher",
            display_name="Dispatcher",
            supported_schema_versions=["1.0"],
            rate_limit_per_minute=100,
        )

        assert "my-channel-01" in es.indexed_docs
        doc = es.indexed_docs["my-channel-01"]["document"]
        assert doc["channel_id"] == "my-channel-01"
        assert doc["tenant_id"] == "tenant-a"
        assert doc["rate_limit_per_minute"] == 100
        assert doc["secret_version"] == 1

    @pytest.mark.asyncio
    async def test_create_rejects_empty_tenant_id(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        with pytest.raises(ValueError, match="tenant_id"):
            await repo.create(
                tenant_id="",
                channel_id="my-channel-01",
                channel_type="voice",
                display_name="Voice",
                supported_schema_versions=["1.0"],
            )


# ---------------------------------------------------------------------------
# Tests — Get
# ---------------------------------------------------------------------------


class TestGet:
    """Tests for IntakeChannelRepository.get."""

    @pytest.mark.asyncio
    async def test_get_returns_channel_for_matching_tenant(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        source = _make_channel_source()
        es.search_responses.append(_wrap_in_search_response([source]))

        result = await repo.get("tenant-a", "test-channel-01")
        assert result is not None
        assert isinstance(result, IntakeChannel)
        assert result.channel_id == "test-channel-01"
        assert result.tenant_id == "tenant-a"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_cross_tenant(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        source = _make_channel_source(tenant_id="tenant-b")
        es.search_responses.append(_wrap_in_search_response([source]))

        result = await repo.get("tenant-a", "test-channel-01")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_none_when_not_found(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        es.search_responses.append(_wrap_in_search_response([]))

        result = await repo.get("tenant-a", "nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_rejects_empty_channel_id(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        with pytest.raises(ValueError, match="channel_id"):
            await repo.get("tenant-a", "")


# ---------------------------------------------------------------------------
# Tests — List for tenant
# ---------------------------------------------------------------------------


class TestListForTenant:
    """Tests for IntakeChannelRepository.list_for_tenant."""

    @pytest.mark.asyncio
    async def test_list_returns_only_matching_tenant(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        sources = [
            _make_channel_source("channel-1", "tenant-a"),
            _make_channel_source("channel-2", "tenant-a"),
        ]
        es.search_responses.append(_wrap_in_search_response(sources))

        result = await repo.list_for_tenant("tenant-a")
        assert len(result) == 2
        assert all(c.tenant_id == "tenant-a" for c in result)

    @pytest.mark.asyncio
    async def test_list_drops_cross_tenant_docs(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        sources = [
            _make_channel_source("channel-1", "tenant-a"),
            _make_channel_source("channel-2", "tenant-b"),  # wrong tenant
        ]
        es.search_responses.append(_wrap_in_search_response(sources))

        result = await repo.list_for_tenant("tenant-a")
        assert len(result) == 1
        assert result[0].channel_id == "channel-1"

    @pytest.mark.asyncio
    async def test_list_returns_empty_for_no_channels(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        es.search_responses.append(_wrap_in_search_response([]))

        result = await repo.list_for_tenant("tenant-a")
        assert result == []

    @pytest.mark.asyncio
    async def test_list_rejects_invalid_size(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        with pytest.raises(ValueError, match="size"):
            await repo.list_for_tenant("tenant-a", size=0)


# ---------------------------------------------------------------------------
# Tests — Update
# ---------------------------------------------------------------------------


class TestUpdate:
    """Tests for IntakeChannelRepository.update."""

    @pytest.mark.asyncio
    async def test_update_applies_partial_changes(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        source = _make_channel_source()
        es.search_responses.append(_wrap_in_search_response([source]))

        result = await repo.update(
            "tenant-a", "test-channel-01", {"display_name": "Updated Name"}
        )
        assert result is not None
        assert result.display_name == "Updated Name"

    @pytest.mark.asyncio
    async def test_update_strips_protected_fields(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        source = _make_channel_source()
        es.search_responses.append(_wrap_in_search_response([source]))

        result = await repo.update(
            "tenant-a",
            "test-channel-01",
            {
                "display_name": "New Name",
                "hmac_secret_ref": "hacked-ref",
                "secret_version": 999,
                "tenant_id": "hacked-tenant",
                "channel_id": "hacked-id",
            },
        )
        assert result is not None
        # Protected fields should NOT have changed
        assert result.hmac_secret_ref == source["hmac_secret_ref"]
        assert result.secret_version == 1
        assert result.tenant_id == "tenant-a"
        assert result.channel_id == "test-channel-01"
        # Non-protected field should have changed
        assert result.display_name == "New Name"

    @pytest.mark.asyncio
    async def test_update_returns_none_when_not_found(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        es.search_responses.append(_wrap_in_search_response([]))

        result = await repo.update(
            "tenant-a", "nonexistent", {"display_name": "X"}
        )
        assert result is None


# ---------------------------------------------------------------------------
# Tests — Delete
# ---------------------------------------------------------------------------


class TestDelete:
    """Tests for IntakeChannelRepository.delete."""

    @pytest.mark.asyncio
    async def test_delete_removes_channel_and_vault_credential(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        source = _make_channel_source()
        es.search_responses.append(_wrap_in_search_response([source]))

        result = await repo.delete("tenant-a", "test-channel-01")
        assert result is True
        assert "test-channel-01" in es.deleted_docs
        assert source["hmac_secret_ref"] in vault.deleted_refs

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        es.search_responses.append(_wrap_in_search_response([]))

        result = await repo.delete("tenant-a", "nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_cross_tenant_returns_false(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        # Channel belongs to tenant-b
        source = _make_channel_source(tenant_id="tenant-b")
        es.search_responses.append(_wrap_in_search_response([source]))

        result = await repo.delete("tenant-a", "test-channel-01")
        assert result is False


# ---------------------------------------------------------------------------
# Tests — Rotate Secret
# ---------------------------------------------------------------------------


class TestRotateSecret:
    """Tests for IntakeChannelRepository.rotate_secret."""

    @pytest.mark.asyncio
    async def test_rotate_returns_new_secret_and_bumps_version(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        source = _make_channel_source(secret_version=1)
        es.search_responses.append(_wrap_in_search_response([source]))

        channel, new_secret = await repo.rotate_secret(
            "tenant-a", "test-channel-01"
        )

        assert isinstance(new_secret, str)
        assert len(new_secret) > 0
        assert channel.secret_version == 2
        assert channel.hmac_secret_ref != source["hmac_secret_ref"]

    @pytest.mark.asyncio
    async def test_rotate_stores_new_secret_in_vault(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        source = _make_channel_source()
        es.search_responses.append(_wrap_in_search_response([source]))

        channel, new_secret = await repo.rotate_secret(
            "tenant-a", "test-channel-01"
        )

        # New secret should be in the vault
        new_ref = channel.hmac_secret_ref
        assert new_ref in vault.stored
        assert vault.stored[new_ref]["plaintext"] == {"secret": new_secret}

    @pytest.mark.asyncio
    async def test_rotate_deletes_old_vault_credential(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        old_ref = "cred:tenant-a:intake_channel_hmac:test-channel-01:1"
        source = _make_channel_source(hmac_secret_ref=old_ref)
        es.search_responses.append(_wrap_in_search_response([source]))

        await repo.rotate_secret("tenant-a", "test-channel-01")

        assert old_ref in vault.deleted_refs

    @pytest.mark.asyncio
    async def test_rotate_persists_updated_channel_to_es(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        source = _make_channel_source(secret_version=3)
        es.search_responses.append(_wrap_in_search_response([source]))

        channel, _ = await repo.rotate_secret("tenant-a", "test-channel-01")

        assert "test-channel-01" in es.indexed_docs
        doc = es.indexed_docs["test-channel-01"]["document"]
        assert doc["secret_version"] == 4

    @pytest.mark.asyncio
    async def test_rotate_raises_when_channel_not_found(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        es.search_responses.append(_wrap_in_search_response([]))

        with pytest.raises(ValueError, match="not found"):
            await repo.rotate_secret("tenant-a", "nonexistent")

    @pytest.mark.asyncio
    async def test_rotate_cross_tenant_raises_value_error(self):
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        # Channel belongs to tenant-b, so get() returns None for tenant-a
        source = _make_channel_source(tenant_id="tenant-b")
        es.search_responses.append(_wrap_in_search_response([source]))

        with pytest.raises(ValueError, match="not found"):
            await repo.rotate_secret("tenant-a", "test-channel-01")


# ---------------------------------------------------------------------------
# Tests — Tenant Isolation (query structure)
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """Verify every query emitted contains tenant_id filter."""

    @pytest.mark.asyncio
    async def test_get_query_contains_tenant_filter(self):
        """The search query for get must include a tenant_id term filter."""
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        # Patch search_documents to capture the query
        captured_queries = []
        original_search = es.search_documents

        async def capturing_search(index, query, size=10):
            captured_queries.append(query)
            return {"hits": {"hits": [], "total": {"value": 0}}}

        es.search_documents = capturing_search

        await repo.get("tenant-x", "some-channel")

        assert len(captured_queries) == 1
        query = captured_queries[0]
        # inject_tenant_filter wraps the query in a bool filter with tenant_id
        query_str = str(query)
        assert "tenant_id" in query_str
        assert "tenant-x" in query_str

    @pytest.mark.asyncio
    async def test_list_query_contains_tenant_filter(self):
        """The search query for list_for_tenant must include tenant_id."""
        es = FakeESService()
        vault = FakeCredentialsVault()
        repo = IntakeChannelRepository(es, vault)

        captured_queries = []

        async def capturing_search(index, query, size=10):
            captured_queries.append(query)
            return {"hits": {"hits": [], "total": {"value": 0}}}

        es.search_documents = capturing_search

        await repo.list_for_tenant("tenant-y")

        assert len(captured_queries) == 1
        query_str = str(captured_queries[0])
        assert "tenant_id" in query_str
        assert "tenant-y" in query_str


# ---------------------------------------------------------------------------
# Tests — Constructor validation
# ---------------------------------------------------------------------------


class TestConstructor:
    """Tests for IntakeChannelRepository constructor validation."""

    def test_rejects_none_es_service(self):
        vault = FakeCredentialsVault()
        with pytest.raises(ValueError, match="es_service"):
            IntakeChannelRepository(None, vault)

    def test_rejects_none_credentials_vault(self):
        es = FakeESService()
        with pytest.raises(ValueError, match="credentials_vault"):
            IntakeChannelRepository(es, None)
