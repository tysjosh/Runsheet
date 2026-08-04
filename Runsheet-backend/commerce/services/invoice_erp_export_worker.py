"""Recover finalized invoices whose ERP export did not finish.

Invoice finalization durably records ``qbo_push_state=pending`` before the
best-effort immediate callback runs. This worker scans that persisted state so
a process exit, dropped callback, or transient connector failure cannot strand
an invoice indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from commerce.models.invoice import InvoiceStatus, QBOPushState
from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX

logger = logging.getLogger(__name__)

ERP_EXPORT_INTERVAL_SECONDS = 60
ERP_EXPORT_BATCH_SIZE = 100
_ERP_EXPORT_LOCK_TTL_SECONDS = 120

_EXPORTABLE_STATUSES = (
    InvoiceStatus.OPEN.value,
    InvoiceStatus.PARTIAL.value,
    InvoiceStatus.PAID.value,
    InvoiceStatus.OVERDUE.value,
)
_REPAIRABLE_PUSH_STATES = (
    QBOPushState.PENDING.value,
    QBOPushState.RETRY.value,
)


class InvoiceERPExportWorker:
    """Retry persisted pending/retry invoice exports across all tenants."""

    def __init__(
        self,
        *,
        es_service: Any,
        external_sync: Any,
        redis_client: Optional[Any] = None,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service is required")
        if external_sync is None:
            raise ValueError("external_sync is required")
        self._es = es_service
        self._external_sync = external_sync
        self._redis = redis_client
        self._local_locks: dict[str, asyncio.Lock] = {}

    async def export_pending(
        self, *, limit: int = ERP_EXPORT_BATCH_SIZE
    ) -> dict[str, int]:
        """Attempt one bounded oldest-first export batch."""
        response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX,
            {
                "query": {
                    "bool": {
                        "must": [
                            {
                                "terms": {
                                    "qbo_push_state": list(
                                        _REPAIRABLE_PUSH_STATES
                                    )
                                }
                            },
                            {
                                "terms": {
                                    "status": list(_EXPORTABLE_STATUSES)
                                }
                            },
                            {"range": {"qbo_push_attempts": {"lt": 3}}},
                        ]
                    }
                },
                "sort": [
                    {"updated_at": {"order": "asc", "unmapped_type": "date"}},
                    {"invoice_id": {"order": "asc"}},
                ],
                "size": limit,
            },
            limit,
        )
        hits = (response.get("hits") or {}).get("hits") or []
        counts = {
            "examined": len(hits),
            "attempted": 0,
            "failed": 0,
            "skipped_locked": 0,
        }
        for hit in hits:
            invoice = dict(hit.get("_source") or {})
            invoice.setdefault("invoice_id", hit.get("_id"))
            async with self._export_lock(invoice) as acquired:
                if not acquired:
                    counts["skipped_locked"] += 1
                    continue
                try:
                    # CommerceExternalSync persists pushed/retry/dead-letter
                    # state and isolates provider failures.
                    await self._external_sync.on_invoice_finalized(invoice)
                    counts["attempted"] += 1
                except Exception:
                    counts["failed"] += 1
                    logger.exception(
                        "ERP export recovery failed unexpectedly: "
                        "tenant=%s invoice=%s",
                        invoice.get("tenant_id"),
                        invoice.get("invoice_id"),
                    )
        return counts

    @asynccontextmanager
    async def _export_lock(self, invoice: dict) -> AsyncIterator[bool]:
        tenant_id = invoice.get("tenant_id") or "unknown"
        invoice_id = invoice.get("invoice_id") or "unknown"
        key = f"invoice_erp_export:{tenant_id}:{invoice_id}"
        if self._redis is not None:
            acquired = bool(
                await self._redis.set(
                    key,
                    "1",
                    ex=_ERP_EXPORT_LOCK_TTL_SECONDS,
                    nx=True,
                )
            )
            try:
                yield acquired
            finally:
                if acquired:
                    try:
                        await self._redis.delete(key)
                    except Exception:
                        logger.warning(
                            "Failed to release ERP export lock %s", key
                        )
            return

        lock = self._local_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            yield False
            return
        await lock.acquire()
        try:
            yield True
        finally:
            lock.release()
