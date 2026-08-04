"""Tests for durable recovery of pending invoice ERP exports."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX
from commerce.services.invoice_erp_export_worker import InvoiceERPExportWorker


def _es_with(*invoices: dict) -> MagicMock:
    es = MagicMock()
    es.search_documents = AsyncMock(
        return_value={
            "hits": {
                "hits": [
                    {"_id": invoice["invoice_id"], "_source": invoice}
                    for invoice in invoices
                ]
            }
        }
    )
    return es


@pytest.mark.asyncio
async def test_replays_pending_and_retry_invoices():
    pending = {
        "invoice_id": "inv-pending",
        "tenant_id": "tenant-1",
        "status": "open",
        "qbo_push_state": "pending",
        "qbo_push_attempts": 0,
    }
    retry = {
        "invoice_id": "inv-retry",
        "tenant_id": "tenant-1",
        "status": "open",
        "qbo_push_state": "retry",
        "qbo_push_attempts": 1,
    }
    es = _es_with(pending, retry)
    external_sync = MagicMock()
    external_sync.on_invoice_finalized = AsyncMock()
    worker = InvoiceERPExportWorker(
        es_service=es,
        external_sync=external_sync,
    )

    counts = await worker.export_pending()

    assert counts == {
        "examined": 2,
        "attempted": 2,
        "failed": 0,
        "skipped_locked": 0,
    }
    assert external_sync.on_invoice_finalized.await_count == 2
    query = es.search_documents.await_args.args[1]
    assert {
        "terms": {"qbo_push_state": ["pending", "retry"]}
    } in query["query"]["bool"]["must"]
    assert es.search_documents.await_args.args[0] == INVOICES_CURRENT_INDEX


@pytest.mark.asyncio
async def test_distributed_lock_prevents_duplicate_export_attempt():
    invoice = {
        "invoice_id": "inv-locked",
        "tenant_id": "tenant-1",
        "status": "open",
        "qbo_push_state": "pending",
        "qbo_push_attempts": 0,
    }
    redis = MagicMock()
    redis.set = AsyncMock(return_value=False)
    redis.delete = AsyncMock()
    external_sync = MagicMock()
    external_sync.on_invoice_finalized = AsyncMock()
    worker = InvoiceERPExportWorker(
        es_service=_es_with(invoice),
        external_sync=external_sync,
        redis_client=redis,
    )

    counts = await worker.export_pending()

    assert counts["skipped_locked"] == 1
    external_sync.on_invoice_finalized.assert_not_awaited()
    redis.delete.assert_not_awaited()
