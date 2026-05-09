"""Scheduled job that writes daily AR aging snapshots for all commerce tenants.

Runs every 24 hours and queries for all distinct tenant_ids that have
invoices in invoices_current (indicating commerce is active for that
tenant), then calls ARAgingService.write_daily_snapshot(tenant_id) for
each.

Registered via the existing scheduler infrastructure (asyncio background
task pattern used throughout bootstrap/).

Validates: Requirements 9.4, Task 10.2
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)

# Interval between snapshot runs (seconds). Daily per design §3.8.
AR_AGING_SNAPSHOT_INTERVAL_SECONDS = 86400  # 24 hours


async def run_ar_aging_snapshot_cycle(
    es_service: ElasticsearchService,
    ar_aging_service: Any,
) -> int:
    """Write daily AR aging snapshots for every tenant with commerce data.

    Queries invoices_current for all distinct tenant_ids (indicating
    commerce.backbone_enabled is on for those tenants), then calls
    ar_aging_service.write_daily_snapshot(tenant_id) for each.

    Returns the number of tenants for which snapshots were written.
    """
    # Use a terms aggregation to find all distinct tenant_ids with
    # invoice data. This is a system-level sweep across all tenants.
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
        logger.error("AR aging snapshot tenant scan failed: %s", exc)
        return 0

    # Extract distinct tenant_ids from the aggregation
    buckets = (
        response.get("aggregations", {})
        .get("tenants", {})
        .get("buckets", [])
    )

    if not buckets:
        logger.debug("No tenants with invoice data found for AR aging snapshots")
        return 0

    tenant_ids: List[str] = [
        bucket["key"] for bucket in buckets if bucket.get("key")
    ]

    if not tenant_ids:
        logger.debug("No valid tenant_ids extracted from aggregation")
        return 0

    snapshot_count = 0

    for tenant_id in tenant_ids:
        try:
            await ar_aging_service.write_daily_snapshot(tenant_id=tenant_id)
            snapshot_count += 1
            logger.info(
                "Wrote AR aging snapshot for tenant %s",
                tenant_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to write AR aging snapshot for tenant %s: %s",
                tenant_id,
                exc,
            )

    if snapshot_count > 0:
        logger.info(
            "AR aging snapshot cycle complete: %d snapshot(s) written across %d tenant(s)",
            snapshot_count,
            len(tenant_ids),
        )

    return snapshot_count
