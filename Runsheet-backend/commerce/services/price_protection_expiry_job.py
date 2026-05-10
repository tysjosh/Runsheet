"""Scheduled job that transitions price-protection contracts to terminal states.

Runs daily (see ``PRICE_PROTECTION_EXPIRY_INTERVAL_SECONDS``) and, for
every tenant with rows in ``price_protection_contracts``, invokes
:meth:`PriceProtectionService.check_expiry` to transition
``active → exhausted`` (zero gallons remaining) or
``active → expired`` (past ``end_date``). Returns the total number of
contracts transitioned across all tenants in the cycle.

Registered via the existing scheduler infrastructure (asyncio
background task pattern used throughout ``bootstrap/``). Wired from
``bootstrap/compliance.py`` alongside the rest of the compliance
domain services.

Validates: Requirement 3.6
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from commerce.services.price_protection_service import PriceProtectionService
from compliance.services.compliance_es_mappings import (
    PRICE_PROTECTION_CONTRACTS_INDEX,
)
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)

# Interval between expiry sweeps (seconds). Daily per Req 3.6 — the
# transition is time-based (end_date) so more frequent scans do not
# add value, and less frequent scans risk stranded-active contracts
# in settlement-variance reports. Kept as a module constant so the
# bootstrap cron can import it alongside the cycle function.
PRICE_PROTECTION_EXPIRY_INTERVAL_SECONDS = 86_400  # 24 hours

# Maximum distinct tenants we will expand in a single aggregation.
# Matches the cap used by the AR aging snapshot job so behaviour is
# uniform across commerce crons.
_MAX_TENANTS_PER_SWEEP = 10_000


async def run_price_protection_expiry_cycle(
    es_service: ElasticsearchService,
) -> int:
    """Transition terminal price-protection contracts for every tenant.

    Discovers tenants via a ``terms`` aggregation on
    ``price_protection_contracts.tenant_id`` (so tenants without any
    contracts incur zero cost) and invokes ``check_expiry`` on a
    freshly-built :class:`PriceProtectionService` scoped to each
    tenant. Exceptions from a single tenant are logged and do not
    abort the sweep — one tenant's broken data cannot block lifecycle
    transitions for the rest of the customer base.

    Args:
        es_service: Elasticsearch facade used to enumerate tenants and
            passed through to the per-tenant service instances.

    Returns:
        The total number of contracts transitioned across all tenants
        during this cycle.
    """
    # Terms aggregation over ``tenant_id`` — same shape as
    # ``ar_aging_snapshot_job.run_ar_aging_snapshot_cycle``.
    query: Dict[str, Any] = {
        "size": 0,
        "aggs": {
            "tenants": {
                "terms": {
                    "field": "tenant_id",
                    "size": _MAX_TENANTS_PER_SWEEP,
                }
            }
        },
    }

    try:
        response = await es_service.search_documents(
            PRICE_PROTECTION_CONTRACTS_INDEX, query, size=0
        )
    except Exception as exc:
        logger.error(
            "Price protection expiry tenant scan failed: %s", exc
        )
        return 0

    buckets = (
        response.get("aggregations", {})
        .get("tenants", {})
        .get("buckets", [])
    )
    if not buckets:
        logger.debug(
            "No tenants with price_protection_contracts — expiry cycle "
            "is a no-op"
        )
        return 0

    tenant_ids: List[str] = [
        bucket["key"] for bucket in buckets if bucket.get("key")
    ]
    if not tenant_ids:
        logger.debug(
            "No valid tenant_ids extracted from price_protection_contracts "
            "aggregation"
        )
        return 0

    total_transitioned = 0
    for tenant_id in tenant_ids:
        try:
            service = PriceProtectionService(es_service, tenant_id=tenant_id)
            transitioned = await service.check_expiry()
            total_transitioned += len(transitioned)
            if transitioned:
                logger.info(
                    "Price protection expiry: transitioned %d contract(s) "
                    "for tenant %s",
                    len(transitioned),
                    tenant_id,
                )
        except Exception as exc:
            logger.error(
                "Price protection expiry cycle failed for tenant %s: %s",
                tenant_id,
                exc,
            )

    if total_transitioned > 0:
        logger.info(
            "Price protection expiry cycle complete: %d contract(s) "
            "transitioned across %d tenant(s)",
            total_transitioned,
            len(tenant_ids),
        )

    return total_transitioned
