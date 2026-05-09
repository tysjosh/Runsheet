"""Unit tests for ARAgingService.

Tests cover:
- compute_account_aging: bucket assignment based on days since issued_at,
  only includes open/partial/overdue invoices, integer cents
- compute_tenant_aging: aggregation across all accounts, top 50 by
  total_open_cents descending
- write_daily_snapshot: persists to ar_aging_snapshots with idempotent
  snapshot_id, includes account_count_with_balance

Validates: Requirements 7.1, 7.2, 9.4, C1, C2, C3
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from commerce.services.ar_aging_service import ARAgingService


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_abc"
_ACCOUNT_ID = "acct_test123"
_FIXED_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    return es


def _make_invoice_hit(
    *,
    invoice_id: str = "inv_001",
    account_id: str = _ACCOUNT_ID,
    issued_at: str | None = None,
    remaining_cents: int = 10000,
    status: str = "open",
) -> Dict[str, Any]:
    """Build a mock ES hit for an invoice."""
    return {
        "_source": {
            "invoice_id": invoice_id,
            "account_id": account_id,
            "issued_at": issued_at or _FIXED_NOW.isoformat(),
            "remaining_cents": remaining_cents,
            "status": status,
        }
    }


def _es_search_response(hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a mock ES search response."""
    return {
        "hits": {
            "hits": hits,
            "total": {"value": len(hits)},
        }
    }


def _es_agg_response(aggs: Dict[str, Any]) -> Dict[str, Any]:
    """Build a mock ES aggregation response."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": aggs,
    }


# ---------------------------------------------------------------------------
# compute_account_aging tests
# ---------------------------------------------------------------------------


class TestComputeAccountAging:
    """Tests for ARAgingService.compute_account_aging."""

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_empty_account_returns_zero_buckets(self, mock_utcnow):
        """An account with no open invoices returns all-zero buckets."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        service = ARAgingService(es)
        result = await service.compute_account_aging(_TENANT_ID, _ACCOUNT_ID)

        assert result == {
            "bucket_0_30_cents": 0,
            "bucket_31_60_cents": 0,
            "bucket_61_90_cents": 0,
            "bucket_90_plus_cents": 0,
            "total_open_cents": 0,
        }

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_invoice_in_0_30_bucket(self, mock_utcnow):
        """Invoice issued 10 days ago lands in 0-30 bucket."""
        issued = (_FIXED_NOW - timedelta(days=10)).isoformat()
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([
            _make_invoice_hit(issued_at=issued, remaining_cents=5000),
        ])

        service = ARAgingService(es)
        result = await service.compute_account_aging(_TENANT_ID, _ACCOUNT_ID)

        assert result["bucket_0_30_cents"] == 5000
        assert result["bucket_31_60_cents"] == 0
        assert result["bucket_61_90_cents"] == 0
        assert result["bucket_90_plus_cents"] == 0
        assert result["total_open_cents"] == 5000

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_invoice_in_31_60_bucket(self, mock_utcnow):
        """Invoice issued 45 days ago lands in 31-60 bucket."""
        issued = (_FIXED_NOW - timedelta(days=45)).isoformat()
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([
            _make_invoice_hit(issued_at=issued, remaining_cents=7500),
        ])

        service = ARAgingService(es)
        result = await service.compute_account_aging(_TENANT_ID, _ACCOUNT_ID)

        assert result["bucket_0_30_cents"] == 0
        assert result["bucket_31_60_cents"] == 7500
        assert result["total_open_cents"] == 7500

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_invoice_in_61_90_bucket(self, mock_utcnow):
        """Invoice issued 75 days ago lands in 61-90 bucket."""
        issued = (_FIXED_NOW - timedelta(days=75)).isoformat()
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([
            _make_invoice_hit(issued_at=issued, remaining_cents=3000),
        ])

        service = ARAgingService(es)
        result = await service.compute_account_aging(_TENANT_ID, _ACCOUNT_ID)

        assert result["bucket_0_30_cents"] == 0
        assert result["bucket_31_60_cents"] == 0
        assert result["bucket_61_90_cents"] == 3000
        assert result["bucket_90_plus_cents"] == 0
        assert result["total_open_cents"] == 3000

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_invoice_in_90_plus_bucket(self, mock_utcnow):
        """Invoice issued 120 days ago lands in 90+ bucket."""
        issued = (_FIXED_NOW - timedelta(days=120)).isoformat()
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([
            _make_invoice_hit(issued_at=issued, remaining_cents=20000),
        ])

        service = ARAgingService(es)
        result = await service.compute_account_aging(_TENANT_ID, _ACCOUNT_ID)

        assert result["bucket_0_30_cents"] == 0
        assert result["bucket_31_60_cents"] == 0
        assert result["bucket_61_90_cents"] == 0
        assert result["bucket_90_plus_cents"] == 20000
        assert result["total_open_cents"] == 20000

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_multiple_invoices_across_buckets(self, mock_utcnow):
        """Multiple invoices spread across different aging buckets."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([
            _make_invoice_hit(
                invoice_id="inv_1",
                issued_at=(_FIXED_NOW - timedelta(days=5)).isoformat(),
                remaining_cents=1000,
            ),
            _make_invoice_hit(
                invoice_id="inv_2",
                issued_at=(_FIXED_NOW - timedelta(days=40)).isoformat(),
                remaining_cents=2000,
            ),
            _make_invoice_hit(
                invoice_id="inv_3",
                issued_at=(_FIXED_NOW - timedelta(days=80)).isoformat(),
                remaining_cents=3000,
            ),
            _make_invoice_hit(
                invoice_id="inv_4",
                issued_at=(_FIXED_NOW - timedelta(days=100)).isoformat(),
                remaining_cents=4000,
            ),
        ])

        service = ARAgingService(es)
        result = await service.compute_account_aging(_TENANT_ID, _ACCOUNT_ID)

        assert result["bucket_0_30_cents"] == 1000
        assert result["bucket_31_60_cents"] == 2000
        assert result["bucket_61_90_cents"] == 3000
        assert result["bucket_90_plus_cents"] == 4000
        assert result["total_open_cents"] == 10000

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_boundary_30_days(self, mock_utcnow):
        """Invoice issued exactly 30 days ago lands in 0-30 bucket."""
        issued = (_FIXED_NOW - timedelta(days=30)).isoformat()
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([
            _make_invoice_hit(issued_at=issued, remaining_cents=1500),
        ])

        service = ARAgingService(es)
        result = await service.compute_account_aging(_TENANT_ID, _ACCOUNT_ID)

        assert result["bucket_0_30_cents"] == 1500
        assert result["bucket_31_60_cents"] == 0

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_boundary_31_days(self, mock_utcnow):
        """Invoice issued exactly 31 days ago lands in 31-60 bucket."""
        issued = (_FIXED_NOW - timedelta(days=31)).isoformat()
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([
            _make_invoice_hit(issued_at=issued, remaining_cents=1500),
        ])

        service = ARAgingService(es)
        result = await service.compute_account_aging(_TENANT_ID, _ACCOUNT_ID)

        assert result["bucket_0_30_cents"] == 0
        assert result["bucket_31_60_cents"] == 1500

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_zero_remaining_cents_excluded(self, mock_utcnow):
        """Invoices with remaining_cents <= 0 are excluded from aging."""
        issued = (_FIXED_NOW - timedelta(days=10)).isoformat()
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([
            _make_invoice_hit(issued_at=issued, remaining_cents=0),
        ])

        service = ARAgingService(es)
        result = await service.compute_account_aging(_TENANT_ID, _ACCOUNT_ID)

        assert result["total_open_cents"] == 0


# ---------------------------------------------------------------------------
# compute_tenant_aging tests
# ---------------------------------------------------------------------------


class TestComputeTenantAging:
    """Tests for ARAgingService.compute_tenant_aging."""

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_empty_tenant_returns_zero_buckets(self, mock_utcnow):
        """A tenant with no open invoices returns all-zero buckets."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        service = ARAgingService(es)
        result = await service.compute_tenant_aging(_TENANT_ID)

        assert result["bucket_0_30_cents"] == 0
        assert result["bucket_31_60_cents"] == 0
        assert result["bucket_61_90_cents"] == 0
        assert result["bucket_90_plus_cents"] == 0
        assert result["total_open_cents"] == 0
        assert result["by_account"] == []

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_aggregates_across_accounts(self, mock_utcnow):
        """Tenant aging aggregates invoices from multiple accounts."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([
            _make_invoice_hit(
                invoice_id="inv_1",
                account_id="acct_a",
                issued_at=(_FIXED_NOW - timedelta(days=10)).isoformat(),
                remaining_cents=5000,
            ),
            _make_invoice_hit(
                invoice_id="inv_2",
                account_id="acct_b",
                issued_at=(_FIXED_NOW - timedelta(days=50)).isoformat(),
                remaining_cents=8000,
            ),
        ])

        service = ARAgingService(es)
        result = await service.compute_tenant_aging(_TENANT_ID)

        assert result["bucket_0_30_cents"] == 5000
        assert result["bucket_31_60_cents"] == 8000
        assert result["total_open_cents"] == 13000
        assert len(result["by_account"]) == 2

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_by_account_sorted_by_total_open_desc(self, mock_utcnow):
        """by_account list is sorted by total_open_cents descending."""
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([
            _make_invoice_hit(
                invoice_id="inv_1",
                account_id="acct_small",
                issued_at=(_FIXED_NOW - timedelta(days=10)).isoformat(),
                remaining_cents=1000,
            ),
            _make_invoice_hit(
                invoice_id="inv_2",
                account_id="acct_large",
                issued_at=(_FIXED_NOW - timedelta(days=10)).isoformat(),
                remaining_cents=9000,
            ),
        ])

        service = ARAgingService(es)
        result = await service.compute_tenant_aging(_TENANT_ID)

        by_account = result["by_account"]
        assert len(by_account) == 2
        assert by_account[0]["account_id"] == "acct_large"
        assert by_account[0]["total_open_cents"] == 9000
        assert by_account[1]["account_id"] == "acct_small"
        assert by_account[1]["total_open_cents"] == 1000

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_by_account_capped_at_50(self, mock_utcnow):
        """by_account list is capped at 50 entries."""
        es = _make_es_service()
        # Create 60 accounts with one invoice each
        hits = [
            _make_invoice_hit(
                invoice_id=f"inv_{i}",
                account_id=f"acct_{i:03d}",
                issued_at=(_FIXED_NOW - timedelta(days=10)).isoformat(),
                remaining_cents=(60 - i) * 100,
            )
            for i in range(60)
        ]
        es.search_documents.return_value = _es_search_response(hits)

        service = ARAgingService(es)
        result = await service.compute_tenant_aging(_TENANT_ID)

        assert len(result["by_account"]) == 50
        # First entry should be the account with highest total
        assert result["by_account"][0]["total_open_cents"] == 6000


# ---------------------------------------------------------------------------
# write_daily_snapshot tests
# ---------------------------------------------------------------------------


class TestWriteDailySnapshot:
    """Tests for ARAgingService.write_daily_snapshot."""

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_writes_snapshot_with_correct_id(self, mock_utcnow):
        """Snapshot is written with id = '{tenant_id}:{YYYY-MM-DD}'."""
        es = _make_es_service()
        # First call: compute_tenant_aging query (invoices)
        # Second call: _count_accounts_with_balance query (agg)
        es.search_documents.side_effect = [
            _es_search_response([]),  # tenant aging query
            _es_agg_response({"account_count": {"value": 0}}),  # count query
        ]

        service = ARAgingService(es)
        result = await service.write_daily_snapshot(_TENANT_ID)

        expected_id = f"{_TENANT_ID}:2026-06-15"
        assert result["snapshot_id"] == expected_id
        assert result["tenant_id"] == _TENANT_ID
        assert result["snapshot_date"] == "2026-06-15"

        # Verify index_document was called with correct args
        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args[0][0] == "ar_aging_snapshots"
        assert call_args[0][1] == expected_id

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_snapshot_includes_aging_buckets(self, mock_utcnow):
        """Snapshot document includes all aging bucket values."""
        es = _make_es_service()
        es.search_documents.side_effect = [
            _es_search_response([
                _make_invoice_hit(
                    invoice_id="inv_1",
                    account_id="acct_a",
                    issued_at=(_FIXED_NOW - timedelta(days=10)).isoformat(),
                    remaining_cents=5000,
                ),
                _make_invoice_hit(
                    invoice_id="inv_2",
                    account_id="acct_b",
                    issued_at=(_FIXED_NOW - timedelta(days=95)).isoformat(),
                    remaining_cents=3000,
                ),
            ]),
            _es_agg_response({"account_count": {"value": 2}}),
        ]

        service = ARAgingService(es)
        result = await service.write_daily_snapshot(_TENANT_ID)

        assert result["bucket_0_30_cents"] == 5000
        assert result["bucket_31_60_cents"] == 0
        assert result["bucket_61_90_cents"] == 0
        assert result["bucket_90_plus_cents"] == 3000
        assert result["total_open_cents"] == 8000
        assert result["account_count_with_balance"] == 2

    @pytest.mark.asyncio
    @patch("commerce.services.ar_aging_service.utcnow", return_value=_FIXED_NOW)
    async def test_snapshot_idempotent_via_document_id(self, mock_utcnow):
        """Running write_daily_snapshot twice uses the same document ID (upsert)."""
        es = _make_es_service()
        es.search_documents.side_effect = [
            _es_search_response([]),
            _es_agg_response({"account_count": {"value": 0}}),
            _es_search_response([]),
            _es_agg_response({"account_count": {"value": 0}}),
        ]

        service = ARAgingService(es)

        result1 = await service.write_daily_snapshot(_TENANT_ID)
        result2 = await service.write_daily_snapshot(_TENANT_ID)

        # Both calls use the same snapshot_id
        assert result1["snapshot_id"] == result2["snapshot_id"]

        # index_document called twice with same doc ID (upsert semantics)
        assert es.index_document.call_count == 2
        first_call_id = es.index_document.call_args_list[0][0][1]
        second_call_id = es.index_document.call_args_list[1][0][1]
        assert first_call_id == second_call_id
