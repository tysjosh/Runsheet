"""Scheduled job that transitions overdue invoices.

Runs every hour and scans invoices_current for invoices where:
- status in (open, partial)
- due_date <= utcnow() (past due)

For each matching invoice, calls InvoiceService.mark_overdue() to
transition it to overdue status. Batches per-tenant for throughput.

Registered via the existing scheduler infrastructure (asyncio background
task pattern used throughout bootstrap/).

Validates: Requirements 5.4, Task 7.4
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Dict, List

from commerce.models.invoice import InvoiceStatus
from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# Interval between overdue scans (seconds). Hourly per design §15.
INVOICE_OVERDUE_INTERVAL_SECONDS = 3600  # 1 hour

# Maximum invoices to process per scan cycle.
_MAX_SCAN_SIZE = 500


async def run_invoice_overdue_cycle(
    es_service: ElasticsearchService,
    invoice_service: Any,
) -> int:
    """Scan for past-due invoices and transition them to overdue.

    Queries invoices_current for all invoices where:
    - status in (open, partial)
    - due_date <= utcnow()

    Groups results by tenant_id and processes each tenant's invoices
    together for throughput (batch per-tenant).

    Returns the number of invoices transitioned to overdue in this cycle.
    """
    now = utcnow()

    # Read-cutover: serve the cross-tenant sweep from Postgres when on. PG
    # ``due_date`` is a pure date column, so the boundary is compared against
    # ``now.date()`` (the ES doc stores a datetime, but for a daily/hourly
    # sweep the date boundary is the meaningful one). Each ``mark_overdue``
    # call below stays tenant-scoped.
    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER,
        read_invoices_due_all_tenants,
    )

    _OVERDUE_STATUSES = [InvoiceStatus.OPEN.value, InvoiceStatus.PARTIAL.value]

    pg = await read_invoices_due_all_tenants(
        statuses=_OVERDUE_STATUSES, due_on_or_before=now.date()
    )
    if pg is not _NOT_CUT_OVER:
        hits = [{"_source": doc} for doc in pg]
    else:
        # Query for invoices past their due date that are still open/partial.
        # This is a system-level sweep across all tenants. Each mark_overdue
        # call is tenant-scoped internally.
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {
                            "terms": {
                                "status": _OVERDUE_STATUSES
                            }
                        },
                        {
                            "range": {
                                "due_date": {
                                    "lte": now.isoformat(),
                                }
                            }
                        },
                    ]
                }
            },
            "size": _MAX_SCAN_SIZE,
            "_source": ["invoice_id", "tenant_id", "due_date", "status"],
        }

        try:
            response = await es_service.search_documents(
                INVOICES_CURRENT_INDEX, query, size=_MAX_SCAN_SIZE
            )
        except Exception as exc:
            logger.error("Invoice overdue scan failed: %s", exc)
            return 0

        hits = response.get("hits", {}).get("hits", [])
    if not hits:
        logger.debug("No past-due invoices found for overdue transition")
        return 0

    # Group by tenant_id for batch processing (throughput optimization)
    tenant_invoices: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for hit in hits:
        source = hit["_source"]
        invoice_id = source.get("invoice_id")
        tenant_id = source.get("tenant_id")

        if not invoice_id or not tenant_id:
            logger.warning(
                "Skipping past-due invoice with missing invoice_id or tenant_id: %s",
                hit.get("_id"),
            )
            continue

        tenant_invoices[tenant_id].append(source)

    overdue_count = 0

    # Process per-tenant batches
    for tenant_id, invoices in tenant_invoices.items():
        for invoice_data in invoices:
            invoice_id = invoice_data["invoice_id"]
            try:
                await invoice_service.mark_overdue(
                    tenant_id=tenant_id,
                    invoice_id=invoice_id,
                )
                overdue_count += 1
                logger.info(
                    "Transitioned invoice %s to overdue for tenant %s (due_date: %s)",
                    invoice_id,
                    tenant_id,
                    invoice_data.get("due_date"),
                )
            except Exception as exc:
                logger.error(
                    "Failed to mark invoice %s as overdue for tenant %s: %s",
                    invoice_id,
                    tenant_id,
                    exc,
                )

    if overdue_count > 0:
        logger.info(
            "Invoice overdue cycle complete: %d invoice(s) transitioned across %d tenant(s)",
            overdue_count,
            len(tenant_invoices),
        )

    return overdue_count
