"""Unit tests for the invoice overdue scheduled job.

Tests cover:
- Scanning for past-due invoices and calling mark_overdue for each
- No-op when no past-due invoices exist
- Graceful handling of individual invoice failures
- Skipping records with missing invoice_id or tenant_id
- Batch per-tenant grouping for throughput
- ES search failure handling

Validates: Requirements 5.4, Task 7.4
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from commerce.services.invoice_overdue_job import (
    INVOICE_OVERDUE_INTERVAL_SECONDS,
    run_invoice_overdue_cycle,
)


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    return es


def _make_invoice_service() -> AsyncMock:
    """Create a mocked InvoiceService."""
    svc = AsyncMock()
    svc.mark_overdue = AsyncMock(return_value={"status": "overdue"})
    return svc


def _es_search_response(hits: list[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a mock ES search response."""
    return {
        "hits": {
            "hits": [
                {"_id": h.get("invoice_id", "unknown"), "_source": h}
                for h in hits
            ],
            "total": {"value": len(hits)},
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInvoiceOverdueJob:
    """Tests for run_invoice_overdue_cycle."""

    @pytest.mark.asyncio
    @patch(
        "commerce.services.invoice_overdue_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_transitions_past_due_invoices_to_overdue(self, mock_utcnow):
        """Calls mark_overdue for each invoice past its due_date."""
        es = _make_es_service()
        invoice_service = _make_invoice_service()

        past_due_invoices = [
            {
                "invoice_id": "inv_001",
                "tenant_id": "tenant_a",
                "due_date": (_FIXED_NOW - timedelta(days=1)).date().isoformat(),
                "status": "open",
            },
            {
                "invoice_id": "inv_002",
                "tenant_id": "tenant_a",
                "due_date": (_FIXED_NOW - timedelta(days=5)).date().isoformat(),
                "status": "partial",
            },
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(past_due_invoices)
        )

        result = await run_invoice_overdue_cycle(
            es_service=es, invoice_service=invoice_service
        )

        assert result == 2
        assert invoice_service.mark_overdue.call_count == 2

        # Verify correct tenant/invoice pairs were called
        calls = invoice_service.mark_overdue.call_args_list
        assert calls[0].kwargs == {
            "tenant_id": "tenant_a",
            "invoice_id": "inv_001",
        }
        assert calls[1].kwargs == {
            "tenant_id": "tenant_a",
            "invoice_id": "inv_002",
        }

    @pytest.mark.asyncio
    @patch(
        "commerce.services.invoice_overdue_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_no_past_due_invoices_returns_zero(self, mock_utcnow):
        """Returns 0 when no past-due invoices are found."""
        es = _make_es_service()
        invoice_service = _make_invoice_service()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([])
        )

        result = await run_invoice_overdue_cycle(
            es_service=es, invoice_service=invoice_service
        )

        assert result == 0
        invoice_service.mark_overdue.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "commerce.services.invoice_overdue_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_batches_per_tenant(self, mock_utcnow):
        """Groups invoices by tenant_id and processes each tenant's batch."""
        es = _make_es_service()
        invoice_service = _make_invoice_service()

        # Invoices from multiple tenants
        past_due_invoices = [
            {
                "invoice_id": "inv_001",
                "tenant_id": "tenant_a",
                "due_date": (_FIXED_NOW - timedelta(days=2)).date().isoformat(),
                "status": "open",
            },
            {
                "invoice_id": "inv_002",
                "tenant_id": "tenant_b",
                "due_date": (_FIXED_NOW - timedelta(days=3)).date().isoformat(),
                "status": "open",
            },
            {
                "invoice_id": "inv_003",
                "tenant_id": "tenant_a",
                "due_date": (_FIXED_NOW - timedelta(days=1)).date().isoformat(),
                "status": "partial",
            },
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(past_due_invoices)
        )

        result = await run_invoice_overdue_cycle(
            es_service=es, invoice_service=invoice_service
        )

        assert result == 3
        assert invoice_service.mark_overdue.call_count == 3

        # Verify all calls have correct tenant_id
        calls = invoice_service.mark_overdue.call_args_list
        tenant_a_calls = [c for c in calls if c.kwargs["tenant_id"] == "tenant_a"]
        tenant_b_calls = [c for c in calls if c.kwargs["tenant_id"] == "tenant_b"]
        assert len(tenant_a_calls) == 2
        assert len(tenant_b_calls) == 1

    @pytest.mark.asyncio
    @patch(
        "commerce.services.invoice_overdue_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_continues_on_individual_failure(self, mock_utcnow):
        """Continues processing remaining invoices when one fails."""
        es = _make_es_service()
        invoice_service = _make_invoice_service()

        past_due_invoices = [
            {
                "invoice_id": "inv_fail",
                "tenant_id": "tenant_a",
                "due_date": (_FIXED_NOW - timedelta(days=1)).date().isoformat(),
                "status": "open",
            },
            {
                "invoice_id": "inv_ok",
                "tenant_id": "tenant_a",
                "due_date": (_FIXED_NOW - timedelta(days=2)).date().isoformat(),
                "status": "open",
            },
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(past_due_invoices)
        )

        # First call fails, second succeeds
        invoice_service.mark_overdue = AsyncMock(
            side_effect=[Exception("ES timeout"), {"status": "overdue"}]
        )

        result = await run_invoice_overdue_cycle(
            es_service=es, invoice_service=invoice_service
        )

        # Only the second one succeeded
        assert result == 1
        assert invoice_service.mark_overdue.call_count == 2

    @pytest.mark.asyncio
    @patch(
        "commerce.services.invoice_overdue_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_skips_records_with_missing_invoice_id(self, mock_utcnow):
        """Skips records that have no invoice_id."""
        es = _make_es_service()
        invoice_service = _make_invoice_service()

        past_due_invoices = [
            {
                "invoice_id": None,
                "tenant_id": "tenant_a",
                "due_date": (_FIXED_NOW - timedelta(days=1)).date().isoformat(),
                "status": "open",
            },
            {
                "invoice_id": "inv_valid",
                "tenant_id": "tenant_a",
                "due_date": (_FIXED_NOW - timedelta(days=1)).date().isoformat(),
                "status": "open",
            },
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(past_due_invoices)
        )

        result = await run_invoice_overdue_cycle(
            es_service=es, invoice_service=invoice_service
        )

        assert result == 1
        invoice_service.mark_overdue.assert_called_once_with(
            tenant_id="tenant_a",
            invoice_id="inv_valid",
        )

    @pytest.mark.asyncio
    @patch(
        "commerce.services.invoice_overdue_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_skips_records_with_missing_tenant_id(self, mock_utcnow):
        """Skips records that have no tenant_id."""
        es = _make_es_service()
        invoice_service = _make_invoice_service()

        past_due_invoices = [
            {
                "invoice_id": "inv_001",
                "tenant_id": None,
                "due_date": (_FIXED_NOW - timedelta(days=1)).date().isoformat(),
                "status": "open",
            },
        ]
        es.search_documents = AsyncMock(
            return_value=_es_search_response(past_due_invoices)
        )

        result = await run_invoice_overdue_cycle(
            es_service=es, invoice_service=invoice_service
        )

        assert result == 0
        invoice_service.mark_overdue.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "commerce.services.invoice_overdue_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_handles_es_search_failure_gracefully(self, mock_utcnow):
        """Returns 0 when the ES search itself fails."""
        es = _make_es_service()
        invoice_service = _make_invoice_service()

        es.search_documents = AsyncMock(side_effect=Exception("Connection refused"))

        result = await run_invoice_overdue_cycle(
            es_service=es, invoice_service=invoice_service
        )

        assert result == 0
        invoice_service.mark_overdue.assert_not_called()

    def test_interval_is_one_hour(self):
        """Verify the interval constant is 3600 seconds (1 hour)."""
        assert INVOICE_OVERDUE_INTERVAL_SECONDS == 3600

    @pytest.mark.asyncio
    @patch(
        "commerce.services.invoice_overdue_job.utcnow",
        return_value=_FIXED_NOW,
    )
    async def test_queries_correct_statuses_and_due_date(self, mock_utcnow):
        """Verifies the ES query filters for open/partial status and past due_date."""
        es = _make_es_service()
        invoice_service = _make_invoice_service()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([])
        )

        await run_invoice_overdue_cycle(
            es_service=es, invoice_service=invoice_service
        )

        # Verify the query structure
        call_args = es.search_documents.call_args
        query = call_args[0][1]  # Second positional arg is the query body
        bool_must = query["query"]["bool"]["must"]

        # Check status filter
        status_filter = bool_must[0]
        assert "terms" in status_filter
        assert set(status_filter["terms"]["status"]) == {"open", "partial"}

        # Check due_date range filter
        date_filter = bool_must[1]
        assert "range" in date_filter
        assert "due_date" in date_filter["range"]
        assert "lte" in date_filter["range"]["due_date"]
