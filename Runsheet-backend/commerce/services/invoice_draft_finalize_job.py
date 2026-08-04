"""Scheduled job that finalizes draft invoices once their grace window expires.

Requirement 5.2 specifies that a generated Invoice "SHALL be ``draft`` for
``draft_grace_seconds`` (tenant-configurable, default 300 seconds) to allow
finalize-time adjustments, then auto-transition to ``open``". Nothing performed
that transition: ``InvoiceService.finalize_draft`` had exactly one caller, the
``POST /api/commerce/invoices/{invoice_id}/finalize`` endpoint, so an invoice
generated from a delivered order sat in ``draft`` until a human clicked it.

That left the delivery→ERP loop open. Everything downstream of finalization was
already built — ``finalize_draft`` durably records ``qbo_push_state=pending``,
fires ``on_invoice_finalized`` to enqueue the QBO push, and
:class:`InvoiceERPExportWorker` retries anything the immediate callback missed —
so the only missing link was the trigger. It also meant the design's own
``CommerceInvoiceStuckInDraft`` alert (which fires at 15 minutes and describes
itself as "5x the default grace window") would fire for *every* invoice.

Mirrors :mod:`commerce.services.invoice_overdue_job`, the Requirement 5.4
sweeper, in structure: a bounded cross-tenant scan grouped per tenant, one
tenant-scoped service call per invoice, and failures isolated to the invoice
that caused them.

Validates: Requirement 5.2
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any, Dict, List, Optional

from commerce.models.invoice import InvoiceStatus
from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX
from commerce.services.invoice_service import _DEFAULT_DRAFT_GRACE_SECONDS
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

#: How often to sweep for expired draft invoices.
#:
#: Deliberately much shorter than the 300s grace window: the sweep interval adds
#: to the time an invoice spends in draft, and ``CommerceInvoiceStuckInDraft``
#: fires at 15 minutes. A 60s cycle keeps the worst case near 360s, comfortably
#: inside that alert.
INVOICE_DRAFT_FINALIZE_INTERVAL_SECONDS = 60

#: Maximum invoices to finalize per cycle. Bounded so one enormous backlog
#: cannot monopolise the event loop or the QBO push queue.
_MAX_SCAN_SIZE = 500

#: Redis key holding a tenant's override of the grace window, in seconds.
#: Mirrors the ``variance_alert_pct:{tenant_id}`` convention used by
#: :mod:`services.reconciliation_service`.
_DRAFT_GRACE_KEY_PATTERN = "draft_grace_seconds:{tenant_id}"


async def resolve_draft_grace_seconds(
    tenant_id: str,
    redis_client: Optional[Any] = None,
    default_seconds: int = _DEFAULT_DRAFT_GRACE_SECONDS,
) -> int:
    """Grace window for a tenant, falling back to the platform default.

    Requirement 5.2 makes this tenant-configurable. A missing, unparseable or
    negative value falls back to the default rather than raising: a bad config
    value must not strand every invoice of that tenant in draft forever, which
    is the failure mode this job exists to prevent.
    """
    if redis_client is None:
        return default_seconds
    try:
        raw = await redis_client.get(
            _DRAFT_GRACE_KEY_PATTERN.format(tenant_id=tenant_id)
        )
    except Exception as exc:
        logger.warning(
            "Draft grace lookup failed for tenant %s, using default %ds: %s",
            tenant_id,
            default_seconds,
            exc,
        )
        return default_seconds

    if raw is None:
        return default_seconds
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Draft grace for tenant %s is not an integer (%r); using %ds",
            tenant_id,
            raw,
            default_seconds,
        )
        return default_seconds
    if seconds < 0:
        logger.warning(
            "Draft grace for tenant %s is negative (%d); using %ds",
            tenant_id,
            seconds,
            default_seconds,
        )
        return default_seconds
    return seconds


async def run_invoice_draft_finalize_cycle(
    es_service: ElasticsearchService,
    invoice_service: Any,
    redis_client: Optional[Any] = None,
) -> int:
    """Finalize every draft invoice whose grace window has elapsed.

    The scan uses the platform-default grace as a coarse pre-filter so the query
    stays a single bounded request, then re-checks each candidate against its own
    tenant's configured window. A tenant with a *longer* window than the default
    is therefore protected by the second check; a tenant with a shorter one is
    picked up on a later cycle, which is the safe direction to err.

    Returns the number of invoices transitioned draft → open.
    """
    now = utcnow()
    coarse_cutoff = now - timedelta(seconds=_DEFAULT_DRAFT_GRACE_SECONDS)

    query: Dict[str, Any] = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"status": InvoiceStatus.DRAFT.value}},
                    {"range": {"created_at": {"lte": coarse_cutoff.isoformat()}}},
                ]
            }
        },
        "size": _MAX_SCAN_SIZE,
        "sort": [{"created_at": {"order": "asc"}}],
        "_source": ["invoice_id", "tenant_id", "created_at", "status"],
    }

    try:
        response = await es_service.search_documents(
            INVOICES_CURRENT_INDEX, query, size=_MAX_SCAN_SIZE
        )
    except Exception as exc:
        logger.error("Invoice draft-finalize scan failed: %s", exc)
        return 0

    hits = (response.get("hits") or {}).get("hits") or []
    if not hits:
        logger.debug("No draft invoices past their grace window")
        return 0

    tenant_invoices: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for hit in hits:
        source = hit.get("_source") or {}
        invoice_id = source.get("invoice_id")
        tenant_id = source.get("tenant_id")
        if not invoice_id or not tenant_id:
            logger.warning(
                "Skipping draft invoice with missing invoice_id or tenant_id: %s",
                hit.get("_id"),
            )
            continue
        tenant_invoices[tenant_id].append(source)

    finalized_count = 0

    for tenant_id, invoices in tenant_invoices.items():
        grace_seconds = await resolve_draft_grace_seconds(
            tenant_id, redis_client=redis_client
        )
        tenant_cutoff = now - timedelta(seconds=grace_seconds)

        for invoice_data in invoices:
            invoice_id = invoice_data["invoice_id"]

            if not _created_at_or_before(invoice_data.get("created_at"), tenant_cutoff):
                # Tenant configured a longer window than the platform default.
                continue

            try:
                await invoice_service.finalize_draft(
                    tenant_id=tenant_id,
                    invoice_id=invoice_id,
                    actor="draft_grace_job",
                )
                finalized_count += 1
                logger.info(
                    "Auto-finalized invoice %s for tenant %s after %ds grace",
                    invoice_id,
                    tenant_id,
                    grace_seconds,
                )
            except Exception as exc:
                # One invoice must not stop the batch. A persistent failure
                # stays visible: the invoice remains in draft and trips
                # CommerceInvoiceStuckInDraft.
                logger.error(
                    "Failed to auto-finalize invoice %s for tenant %s: %s",
                    invoice_id,
                    tenant_id,
                    exc,
                )

    if finalized_count:
        logger.info(
            "Invoice draft-finalize cycle complete: %d invoice(s) across "
            "%d tenant(s)",
            finalized_count,
            len(tenant_invoices),
        )

    return finalized_count


def _created_at_or_before(created_at: Any, cutoff) -> bool:
    """Whether ``created_at`` is at or before ``cutoff``.

    Unparseable timestamps return ``False`` — the invoice is left in draft for a
    human rather than finalized on a guess, because finalization is the
    idempotency cutoff and cannot be undone.
    """
    if created_at is None:
        return False
    if isinstance(created_at, str):
        from datetime import datetime

        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            logger.warning("Unparseable invoice created_at %r; leaving in draft", created_at)
            return False
    else:
        parsed = created_at

    if parsed.tzinfo is None:
        from datetime import timezone

        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= cutoff
