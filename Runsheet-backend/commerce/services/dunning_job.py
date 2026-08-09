"""Scheduled job that evaluates and enqueues dunning notifications.

Runs daily and, for every tenant with commerce data (discovered via
distinct ``tenant_id`` values in ``invoices_current``), calls
``DunningService.evaluate_and_enqueue(tenant_id)``. The service itself
enforces the ``commerce.dunning_enabled`` feature flag per tenant and
de-duplicates via the ``dunning_events`` index, so a tenant with the
flag off or with no crossed thresholds is a fast no-op.

Registered via the existing scheduler infrastructure (asyncio background
task pattern used throughout bootstrap/), following the same shape as
``ar_aging_snapshot_job.py``.

Validates: Requirements 7.3, 7.4
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)

# Interval between dunning evaluation runs (seconds). Daily — thresholds
# are measured in days, so sub-daily polling has no added value.
DUNNING_EVALUATION_INTERVAL_SECONDS = 86400  # 24 hours


async def run_dunning_evaluation_cycle(
    es_service: ElasticsearchService,
    dunning_service: Any,
) -> int:
    """Evaluate and enqueue dunning notifications for every commerce tenant.

    Queries ``invoices_current`` for all distinct ``tenant_id`` values
    (indicating commerce is active for that tenant), then calls
    ``dunning_service.evaluate_and_enqueue(tenant_id)`` for each. The
    per-tenant feature-flag check and duplicate-prevention logic live in
    :class:`DunningService` itself.

    Returns the total number of notifications enqueued across all tenants
    in this cycle.
    """
    query: Dict[str, Any] = {
        "size": 0,
        "aggs": {
            "tenants": {
                "terms": {
                    "field": "tenant_id",
                    "size": 10000,  # Support up to 10k tenants
                }
            }
        },
    }

    try:
        response = await es_service.search_documents(
            INVOICES_CURRENT_INDEX, query, size=0
        )
    except Exception as exc:
        logger.error("Dunning evaluation tenant scan failed: %s", exc)
        return 0

    buckets = (
        response.get("aggregations", {})
        .get("tenants", {})
        .get("buckets", [])
    )

    if not buckets:
        logger.debug("No tenants with invoice data found for dunning evaluation")
        return 0

    tenant_ids: List[str] = [
        bucket["key"] for bucket in buckets if bucket.get("key")
    ]

    if not tenant_ids:
        logger.debug("No valid tenant_ids extracted from aggregation")
        return 0

    total_enqueued = 0
    tenants_processed = 0

    for tenant_id in tenant_ids:
        try:
            result = await dunning_service.evaluate_and_enqueue(tenant_id)
            enqueued = result.get("notifications_enqueued", 0)
            total_enqueued += enqueued
            tenants_processed += 1
            if enqueued:
                logger.info(
                    "Dunning evaluation for tenant %s: %d notification(s) enqueued",
                    tenant_id,
                    enqueued,
                )
        except Exception as exc:
            logger.error(
                "Dunning evaluation failed for tenant %s: %s",
                tenant_id,
                exc,
            )

    if total_enqueued > 0:
        logger.info(
            "Dunning evaluation cycle complete: %d notification(s) enqueued "
            "across %d tenant(s)",
            total_enqueued,
            tenants_processed,
        )

    return total_enqueued
