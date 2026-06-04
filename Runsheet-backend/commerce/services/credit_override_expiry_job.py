"""Scheduled job that expires credit overrides past their expiry time.

Runs every 10 minutes and scans accounts_current for accounts where
credit_state == "override" and credit_override_expires_at is in the past.
For each such account, calls CreditService.expire_override() to transition
the account back to the appropriate state (ok or hold).

Registered via the existing scheduler infrastructure (asyncio background
task pattern used throughout bootstrap/).

Validates: Requirements 2.6, Task 4.4
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from commerce.models.account import CreditState
from commerce.services.commerce_es_mappings import ACCOUNTS_CURRENT_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# Interval between expiry scans (seconds).
CREDIT_OVERRIDE_EXPIRY_INTERVAL_SECONDS = 600  # 10 minutes


async def run_credit_override_expiry_cycle(
    es_service: ElasticsearchService,
    credit_service: Any,
) -> int:
    """Scan for expired credit overrides and expire them.

    Queries accounts_current for all accounts where:
    - credit_state == "override"
    - credit_override_expires_at <= utcnow()

    For each matching account, calls credit_service.expire_override().

    Returns the number of overrides expired in this cycle.
    """
    now = utcnow()

    # Read-cutover: serve the cross-tenant sweep from Postgres when on. Each
    # ``expire_override`` call below stays tenant-scoped.
    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER,
        read_accounts_expired_overrides_all_tenants,
    )

    pg = await read_accounts_expired_overrides_all_tenants(
        credit_state=CreditState.OVERRIDE.value, expires_on_or_before=now
    )
    if pg is not _NOT_CUT_OVER:
        hits = [{"_source": doc} for doc in pg]
    else:
        # Query for accounts with expired overrides — no tenant filter here
        # because this is a system-level sweep across all tenants. Each
        # expire_override call is tenant-scoped internally.
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"credit_state": CreditState.OVERRIDE.value}},
                        {
                            "range": {
                                "credit_override_expires_at": {
                                    "lte": now.isoformat(),
                                }
                            }
                        },
                    ]
                }
            },
            "size": 100,
            "_source": ["account_id", "tenant_id", "credit_override_expires_at"],
        }

        try:
            response = await es_service.search_documents(
                ACCOUNTS_CURRENT_INDEX, query, size=100
            )
        except Exception as exc:
            logger.error("Credit override expiry scan failed: %s", exc)
            return 0

        hits = response.get("hits", {}).get("hits", [])
    if not hits:
        logger.debug("No expired credit overrides found")
        return 0

    expired_count = 0
    for hit in hits:
        source = hit["_source"]
        account_id = source.get("account_id")
        tenant_id = source.get("tenant_id")

        if not account_id or not tenant_id:
            logger.warning(
                "Skipping expired override with missing account_id or tenant_id: %s",
                hit.get("_id"),
            )
            continue

        try:
            await credit_service.expire_override(
                tenant_id=tenant_id,
                account_id=account_id,
            )
            expired_count += 1
            logger.info(
                "Expired credit override for account %s tenant %s",
                account_id,
                tenant_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to expire override for account %s tenant %s: %s",
                account_id,
                tenant_id,
                exc,
            )

    if expired_count > 0:
        logger.info(
            "Credit override expiry cycle complete: %d override(s) expired",
            expired_count,
        )

    return expired_count
