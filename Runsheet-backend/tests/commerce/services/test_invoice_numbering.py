"""Unit tests for InvoiceNumberingService.

Tests the per-tenant monotonic counter via Redis INCR with daily
ES checkpoint and reseed-from-checkpoint on Redis loss.

Validates: Requirements 5.1, C2, C3
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from commerce.services.invoice_numbering import InvoiceNumberingService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT_ID = "tenant_abc"


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    # Default: no documents found
    es.search_documents.return_value = {
        "hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}
    }
    es.index_document.return_value = None
    return es


def _make_redis_client() -> AsyncMock:
    """Create a mocked async Redis client."""
    redis = AsyncMock()
    redis.exists.return_value = True
    redis.incr.return_value = 1
    redis.get.return_value = None
    redis.set.return_value = True
    return redis


def _checkpoint_doc(tenant_id: str, max_seq: int, date: str = "2026-01-15"):
    """Build a checkpoint document as returned from ES."""
    return {
        "tenant_id": tenant_id,
        "max_seq": max_seq,
        "checkpoint_date": date,
        "created_at": f"{date}T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# next_number tests
# ---------------------------------------------------------------------------


class TestNextNumber:
    """Tests for InvoiceNumberingService.next_number."""

    @pytest.mark.asyncio
    async def test_increments_via_redis_when_key_exists(self):
        """When the Redis key exists, INCR is called and the result returned."""
        es = _make_es_service()
        redis = _make_redis_client()
        redis.exists.return_value = True
        redis.incr.return_value = 42

        service = InvoiceNumberingService(es, redis)
        result = await service.next_number(TENANT_ID)

        assert result == 42
        redis.incr.assert_called_once_with(f"commerce:invoice_seq:{TENANT_ID}")

    @pytest.mark.asyncio
    async def test_reseeds_when_key_does_not_exist(self):
        """When the Redis key doesn't exist, reseed is called before INCR."""
        es = _make_es_service()
        redis = _make_redis_client()
        redis.exists.return_value = False
        # After reseed, INCR returns the next value
        redis.incr.return_value = 11

        # Simulate no checkpoint, 10 invoices in ES
        es.search_documents.side_effect = [
            # _get_latest_checkpoint query (no checkpoint)
            {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}},
            # _count_invoices_since query (10 invoices)
            {"hits": {"hits": [], "total": {"value": 10, "relation": "eq"}}},
            # The INCR call doesn't hit ES again
        ]

        service = InvoiceNumberingService(es, redis)
        result = await service.next_number(TENANT_ID)

        assert result == 11
        # Redis.set should have been called during reseed
        redis.set.assert_called_once_with(
            f"commerce:invoice_seq:{TENANT_ID}", "10"
        )
        redis.incr.assert_called_once()

    @pytest.mark.asyncio
    async def test_reseeds_from_checkpoint_plus_invoice_count(self):
        """Reseed uses checkpoint max_seq + invoices since checkpoint."""
        es = _make_es_service()
        redis = _make_redis_client()
        redis.exists.return_value = False
        redis.incr.return_value = 108

        checkpoint = _checkpoint_doc(TENANT_ID, 100, "2026-01-15")

        es.search_documents.side_effect = [
            # _get_latest_checkpoint query
            {"hits": {"hits": [{"_source": checkpoint}], "total": {"value": 1, "relation": "eq"}}},
            # _count_invoices_since query (7 invoices since checkpoint)
            {"hits": {"hits": [], "total": {"value": 7, "relation": "eq"}}},
        ]

        service = InvoiceNumberingService(es, redis)
        result = await service.next_number(TENANT_ID)

        assert result == 108
        # Reseed should set to 100 + 7 = 107
        redis.set.assert_called_once_with(
            f"commerce:invoice_seq:{TENANT_ID}", "107"
        )

    @pytest.mark.asyncio
    async def test_no_redis_uses_es_only_path(self):
        """When no Redis client is provided, uses ES-only fallback."""
        es = _make_es_service()

        # No checkpoint, 5 invoices total
        es.search_documents.side_effect = [
            # _get_latest_checkpoint
            {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}},
            # _count_invoices_since (all invoices)
            {"hits": {"hits": [], "total": {"value": 5, "relation": "eq"}}},
        ]

        service = InvoiceNumberingService(es, redis_client=None)
        result = await service.next_number(TENANT_ID)

        assert result == 6  # 5 existing + 1

    @pytest.mark.asyncio
    async def test_redis_incr_failure_falls_back_to_es(self):
        """When Redis INCR raises, falls back to ES-only path."""
        es = _make_es_service()
        redis = _make_redis_client()
        redis.exists.return_value = True
        redis.incr.side_effect = Exception("Redis connection lost")

        # ES fallback path
        es.search_documents.side_effect = [
            # _get_latest_checkpoint
            {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}},
            # _count_invoices_since
            {"hits": {"hits": [], "total": {"value": 3, "relation": "eq"}}},
        ]

        service = InvoiceNumberingService(es, redis)
        result = await service.next_number(TENANT_ID)

        assert result == 4  # 3 existing + 1

    @pytest.mark.asyncio
    async def test_redis_exists_failure_triggers_reseed(self):
        """When Redis exists check fails, reseed is triggered."""
        es = _make_es_service()
        redis = _make_redis_client()
        redis.exists.side_effect = Exception("Redis timeout")
        redis.incr.return_value = 1

        # Reseed path: no checkpoint, 0 invoices
        es.search_documents.side_effect = [
            {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}},
            {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}},
        ]

        service = InvoiceNumberingService(es, redis)
        result = await service.next_number(TENANT_ID)

        assert result == 1

    @pytest.mark.asyncio
    async def test_monotonic_sequence(self):
        """Successive calls return strictly increasing values."""
        es = _make_es_service()
        redis = _make_redis_client()
        redis.exists.return_value = True

        call_count = 0

        async def mock_incr(key):
            nonlocal call_count
            call_count += 1
            return call_count

        redis.incr.side_effect = mock_incr

        service = InvoiceNumberingService(es, redis)

        results = []
        for _ in range(5):
            results.append(await service.next_number(TENANT_ID))

        assert results == [1, 2, 3, 4, 5]
        # Each value is strictly greater than the previous
        for i in range(1, len(results)):
            assert results[i] > results[i - 1]


# ---------------------------------------------------------------------------
# write_checkpoint tests
# ---------------------------------------------------------------------------


class TestWriteCheckpoint:
    """Tests for InvoiceNumberingService.write_checkpoint."""

    @pytest.mark.asyncio
    async def test_writes_checkpoint_from_redis_value(self):
        """Checkpoint reads current value from Redis and writes to ES."""
        es = _make_es_service()
        redis = _make_redis_client()
        redis.get.return_value = b"50"

        service = InvoiceNumberingService(es, redis)

        with patch("commerce.services.invoice_numbering.utcnow") as mock_now:
            from datetime import datetime, timezone

            mock_now.return_value = datetime(2026, 1, 16, 12, 0, 0, tzinfo=timezone.utc)
            result = await service.write_checkpoint(TENANT_ID)

        assert result["tenant_id"] == TENANT_ID
        assert result["max_seq"] == 50
        assert result["checkpoint_date"] == "2026-01-16"

        # Verify ES index_document was called with correct doc_id
        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args[0][0] == "invoice_counter_checkpoints"
        assert call_args[0][1] == f"{TENANT_ID}:2026-01-16"

    @pytest.mark.asyncio
    async def test_writes_checkpoint_from_es_when_no_redis(self):
        """When no Redis, checkpoint computes max_seq from ES."""
        es = _make_es_service()

        # No checkpoint, 25 invoices
        es.search_documents.side_effect = [
            {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}},
            {"hits": {"hits": [], "total": {"value": 25, "relation": "eq"}}},
        ]

        service = InvoiceNumberingService(es, redis_client=None)

        with patch("commerce.services.invoice_numbering.utcnow") as mock_now:
            from datetime import datetime, timezone

            mock_now.return_value = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
            result = await service.write_checkpoint(TENANT_ID)

        assert result["max_seq"] == 25
        assert result["checkpoint_date"] == "2026-02-01"

    @pytest.mark.asyncio
    async def test_checkpoint_is_idempotent_same_day(self):
        """Writing checkpoint twice on the same day uses the same doc_id (upsert)."""
        es = _make_es_service()
        redis = _make_redis_client()
        redis.get.return_value = b"10"

        service = InvoiceNumberingService(es, redis)

        with patch("commerce.services.invoice_numbering.utcnow") as mock_now:
            from datetime import datetime, timezone

            mock_now.return_value = datetime(2026, 3, 5, 8, 0, 0, tzinfo=timezone.utc)
            await service.write_checkpoint(TENANT_ID)
            redis.get.return_value = b"12"
            await service.write_checkpoint(TENANT_ID)

        # Both calls use the same doc_id
        calls = es.index_document.call_args_list
        assert len(calls) == 2
        assert calls[0][0][1] == f"{TENANT_ID}:2026-03-05"
        assert calls[1][0][1] == f"{TENANT_ID}:2026-03-05"


# ---------------------------------------------------------------------------
# reseed_from_checkpoint tests
# ---------------------------------------------------------------------------


class TestReseedFromCheckpoint:
    """Tests for InvoiceNumberingService.reseed_from_checkpoint."""

    @pytest.mark.asyncio
    async def test_reseed_with_checkpoint_and_invoices(self):
        """Reseed = checkpoint max_seq + invoices since checkpoint."""
        es = _make_es_service()
        redis = _make_redis_client()

        checkpoint = _checkpoint_doc(TENANT_ID, 200, "2026-01-10")

        es.search_documents.side_effect = [
            # _get_latest_checkpoint
            {"hits": {"hits": [{"_source": checkpoint}], "total": {"value": 1, "relation": "eq"}}},
            # _count_invoices_since (15 invoices since checkpoint)
            {"hits": {"hits": [], "total": {"value": 15, "relation": "eq"}}},
        ]

        service = InvoiceNumberingService(es, redis)
        result = await service.reseed_from_checkpoint(TENANT_ID)

        assert result == 215  # 200 + 15
        redis.set.assert_called_once_with(
            f"commerce:invoice_seq:{TENANT_ID}", "215"
        )

    @pytest.mark.asyncio
    async def test_reseed_without_checkpoint(self):
        """When no checkpoint exists, reseed = total invoice count."""
        es = _make_es_service()
        redis = _make_redis_client()

        es.search_documents.side_effect = [
            # _get_latest_checkpoint (none found)
            {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}},
            # _count_invoices_since (all invoices = 30)
            {"hits": {"hits": [], "total": {"value": 30, "relation": "eq"}}},
        ]

        service = InvoiceNumberingService(es, redis)
        result = await service.reseed_from_checkpoint(TENANT_ID)

        assert result == 30
        redis.set.assert_called_once_with(
            f"commerce:invoice_seq:{TENANT_ID}", "30"
        )

    @pytest.mark.asyncio
    async def test_reseed_with_zero_invoices_since_checkpoint(self):
        """When no invoices since checkpoint, reseed = checkpoint max_seq."""
        es = _make_es_service()
        redis = _make_redis_client()

        checkpoint = _checkpoint_doc(TENANT_ID, 50, "2026-01-15")

        es.search_documents.side_effect = [
            {"hits": {"hits": [{"_source": checkpoint}], "total": {"value": 1, "relation": "eq"}}},
            {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}},
        ]

        service = InvoiceNumberingService(es, redis)
        result = await service.reseed_from_checkpoint(TENANT_ID)

        assert result == 50
        redis.set.assert_called_once_with(
            f"commerce:invoice_seq:{TENANT_ID}", "50"
        )

    @pytest.mark.asyncio
    async def test_reseed_redis_set_failure_still_returns_value(self):
        """Even if Redis SET fails during reseed, the computed value is returned."""
        es = _make_es_service()
        redis = _make_redis_client()
        redis.set.side_effect = Exception("Redis write failed")

        es.search_documents.side_effect = [
            {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}},
            {"hits": {"hits": [], "total": {"value": 7, "relation": "eq"}}},
        ]

        service = InvoiceNumberingService(es, redis)
        result = await service.reseed_from_checkpoint(TENANT_ID)

        # Still returns the computed value even though Redis failed
        assert result == 7

    @pytest.mark.asyncio
    async def test_reseed_no_redis_client(self):
        """When no Redis client, reseed computes value but doesn't set."""
        es = _make_es_service()

        checkpoint = _checkpoint_doc(TENANT_ID, 100, "2026-01-10")

        es.search_documents.side_effect = [
            {"hits": {"hits": [{"_source": checkpoint}], "total": {"value": 1, "relation": "eq"}}},
            {"hits": {"hits": [], "total": {"value": 5, "relation": "eq"}}},
        ]

        service = InvoiceNumberingService(es, redis_client=None)
        result = await service.reseed_from_checkpoint(TENANT_ID)

        assert result == 105


# ---------------------------------------------------------------------------
# Tenant isolation tests
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """Tests verifying per-tenant isolation of invoice numbering."""

    @pytest.mark.asyncio
    async def test_different_tenants_use_different_keys(self):
        """Each tenant has its own Redis key for the counter."""
        es = _make_es_service()
        redis = _make_redis_client()
        redis.exists.return_value = True

        incr_calls = []

        async def track_incr(key):
            incr_calls.append(key)
            return len(incr_calls)

        redis.incr.side_effect = track_incr

        service = InvoiceNumberingService(es, redis)

        await service.next_number("tenant_a")
        await service.next_number("tenant_b")
        await service.next_number("tenant_a")

        assert incr_calls == [
            "commerce:invoice_seq:tenant_a",
            "commerce:invoice_seq:tenant_b",
            "commerce:invoice_seq:tenant_a",
        ]
