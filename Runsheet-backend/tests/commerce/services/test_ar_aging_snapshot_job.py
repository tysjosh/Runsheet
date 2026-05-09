"""Unit tests for the AR aging snapshot scheduled job.

Tests cover:
- Discovering tenants via ES aggregation and calling write_daily_snapshot for each
- No-op when no tenants have invoice data
- Graceful handling of individual tenant snapshot failures
- ES search failure handling
- Interval constant validation

Validates: Requirements 9.4, Task 10.2
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from commerce.services.ar_aging_snapshot_job import (
    AR_AGING_SNAPSHOT_INTERVAL_SECONDS,
    run_ar_aging_snapshot_cycle,
)


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.search_documents = AsyncMock(return_value={"aggregations": {"tenants": {"buckets": []}}})
    return es


def _make_ar_aging_service() -> AsyncMock:
    """Create a mocked ARAgingService."""
    svc = AsyncMock()
    svc.write_daily_snapshot = AsyncMock(return_value={
        "snapshot_id": "tenant_a:2026-06-01",
        "tenant_id": "tenant_a",
        "snapshot_date": "2026-06-01",
        "total_open_cents": 100000,
        "bucket_0_30_cents": 50000,
        "bucket_31_60_cents": 30000,
        "bucket_61_90_cents": 15000,
        "bucket_90_plus_cents": 5000,
        "account_count_with_balance": 3,
    })
    return svc


def _es_agg_response(tenant_ids: list[str]) -> Dict[str, Any]:
    """Build a mock ES aggregation response with tenant_id buckets."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {
            "tenants": {
                "buckets": [
                    {"key": tid, "doc_count": 10} for tid in tenant_ids
                ]
            }
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestARAgingSnapshotJob:
    """Tests for run_ar_aging_snapshot_cycle."""

    @pytest.mark.asyncio
    async def test_writes_snapshot_for_each_tenant(self):
        """Calls write_daily_snapshot for each tenant with invoice data."""
        es = _make_es_service()
        ar_aging_service = _make_ar_aging_service()

        es.search_documents = AsyncMock(
            return_value=_es_agg_response(["tenant_a", "tenant_b", "tenant_c"])
        )

        result = await run_ar_aging_snapshot_cycle(
            es_service=es, ar_aging_service=ar_aging_service
        )

        assert result == 3
        assert ar_aging_service.write_daily_snapshot.call_count == 3

        # Verify correct tenant_ids were called
        calls = ar_aging_service.write_daily_snapshot.call_args_list
        called_tenants = [c.kwargs["tenant_id"] for c in calls]
        assert called_tenants == ["tenant_a", "tenant_b", "tenant_c"]

    @pytest.mark.asyncio
    async def test_no_tenants_returns_zero(self):
        """Returns 0 when no tenants have invoice data."""
        es = _make_es_service()
        ar_aging_service = _make_ar_aging_service()

        es.search_documents = AsyncMock(
            return_value=_es_agg_response([])
        )

        result = await run_ar_aging_snapshot_cycle(
            es_service=es, ar_aging_service=ar_aging_service
        )

        assert result == 0
        ar_aging_service.write_daily_snapshot.assert_not_called()

    @pytest.mark.asyncio
    async def test_continues_on_individual_tenant_failure(self):
        """Continues processing remaining tenants when one fails."""
        es = _make_es_service()
        ar_aging_service = _make_ar_aging_service()

        es.search_documents = AsyncMock(
            return_value=_es_agg_response(["tenant_fail", "tenant_ok"])
        )

        # First call fails, second succeeds
        ar_aging_service.write_daily_snapshot = AsyncMock(
            side_effect=[Exception("ES timeout"), {"snapshot_id": "tenant_ok:2026-06-01"}]
        )

        result = await run_ar_aging_snapshot_cycle(
            es_service=es, ar_aging_service=ar_aging_service
        )

        # Only the second one succeeded
        assert result == 1
        assert ar_aging_service.write_daily_snapshot.call_count == 2

    @pytest.mark.asyncio
    async def test_handles_es_search_failure_gracefully(self):
        """Returns 0 when the ES aggregation query fails."""
        es = _make_es_service()
        ar_aging_service = _make_ar_aging_service()

        es.search_documents = AsyncMock(side_effect=Exception("Connection refused"))

        result = await run_ar_aging_snapshot_cycle(
            es_service=es, ar_aging_service=ar_aging_service
        )

        assert result == 0
        ar_aging_service.write_daily_snapshot.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_empty_aggregation_response(self):
        """Returns 0 when the aggregation response has no buckets key."""
        es = _make_es_service()
        ar_aging_service = _make_ar_aging_service()

        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}, "aggregations": {}}
        )

        result = await run_ar_aging_snapshot_cycle(
            es_service=es, ar_aging_service=ar_aging_service
        )

        assert result == 0
        ar_aging_service.write_daily_snapshot.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_empty_tenant_id_keys(self):
        """Skips buckets with empty or falsy tenant_id keys."""
        es = _make_es_service()
        ar_aging_service = _make_ar_aging_service()

        # Include a bucket with an empty key
        response = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "tenants": {
                    "buckets": [
                        {"key": "", "doc_count": 5},
                        {"key": "tenant_valid", "doc_count": 10},
                    ]
                }
            },
        }
        es.search_documents = AsyncMock(return_value=response)

        result = await run_ar_aging_snapshot_cycle(
            es_service=es, ar_aging_service=ar_aging_service
        )

        assert result == 1
        ar_aging_service.write_daily_snapshot.assert_called_once_with(
            tenant_id="tenant_valid"
        )

    def test_interval_is_24_hours(self):
        """Verify the interval constant is 86400 seconds (24 hours)."""
        assert AR_AGING_SNAPSHOT_INTERVAL_SECONDS == 86400

    @pytest.mark.asyncio
    async def test_queries_invoices_current_with_terms_aggregation(self):
        """Verifies the ES query uses a terms aggregation on tenant_id."""
        es = _make_es_service()
        ar_aging_service = _make_ar_aging_service()

        es.search_documents = AsyncMock(
            return_value=_es_agg_response([])
        )

        await run_ar_aging_snapshot_cycle(
            es_service=es, ar_aging_service=ar_aging_service
        )

        # Verify the query structure
        call_args = es.search_documents.call_args
        index_name = call_args[0][0]  # First positional arg is the index
        query = call_args[0][1]  # Second positional arg is the query body

        assert index_name == "invoices_current"
        assert query["size"] == 0
        assert "aggs" in query
        assert "tenants" in query["aggs"]
        assert query["aggs"]["tenants"]["terms"]["field"] == "tenant_id"
