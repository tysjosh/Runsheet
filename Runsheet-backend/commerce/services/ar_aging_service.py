"""Aging bucket computation + snapshots.

Implements the ARAgingService with compute_account_aging,
compute_tenant_aging, and write_daily_snapshot methods.

Aging buckets are computed based on days since invoice issued_at:
  - 0-30 days
  - 31-60 days
  - 61-90 days
  - 90+ days

Only invoices with status in (open, partial, overdue) are included.
All monetary values are integer cents (Constraint C1).
All queries use inject_tenant_filter (Constraint C3).
Timestamps use utcnow() (Constraint C2).

Validates: Requirements 7.1, 7.2, 9.4, C1, C2, C3
"""

from __future__ import annotations

import logging
from datetime import timezone
from typing import Any, Dict, List

from commerce.services.commerce_es_mappings import (
    ACCOUNTS_CURRENT_INDEX,
    AR_AGING_SNAPSHOTS_INDEX,
    INVOICES_CURRENT_INDEX,
)
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Invoice statuses that contribute to AR aging
_AGING_STATUSES = ["open", "partial", "overdue"]

# Top N accounts returned in tenant-level aging
_TOP_ACCOUNTS_LIMIT = 50


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ARAgingService:
    """Service layer for AR aging bucket computation and daily snapshots.

    Computes aging buckets (0-30, 31-60, 61-90, 90+ days) based on the
    number of days since each invoice's ``issued_at`` date relative to
    ``utcnow()``.

    Every public method takes ``tenant_id`` and every ES query passes
    through ``inject_tenant_filter`` (Constraint C3).

    All monetary values are integer cents (Constraint C1).
    """

    def __init__(self, es_service: ElasticsearchService) -> None:
        self._es = es_service

    # ------------------------------------------------------------------
    # compute_account_aging (Req 7.1)
    # ------------------------------------------------------------------

    async def compute_account_aging(
        self, tenant_id: str, account_id: str
    ) -> Dict[str, Any]:
        """Compute aging buckets for a single account.

        Returns a dict with:
          - bucket_0_30_cents: int
          - bucket_31_60_cents: int
          - bucket_61_90_cents: int
          - bucket_90_plus_cents: int
          - total_open_cents: int

        Only includes invoices with status in (open, partial, overdue).
        Aging is computed from the invoice's issued_at relative to utcnow().

        Validates: Requirements 7.1, C1, C2, C3
        """
        now = utcnow()

        # Query all open invoices for this account with their issued_at
        # and remaining_cents. We use a scroll-style approach with a
        # reasonable size limit since accounts typically don't have
        # thousands of open invoices.
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"account_id": account_id}},
                        {"terms": {"status": _AGING_STATUSES}},
                        {"exists": {"field": "issued_at"}},
                    ]
                }
            },
            "size": 10000,
            "_source": ["invoice_id", "issued_at", "remaining_cents"],
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, query, size=10000
        )

        hits = response["hits"]["hits"]

        # Compute buckets
        bucket_0_30 = 0
        bucket_31_60 = 0
        bucket_61_90 = 0
        bucket_90_plus = 0

        for hit in hits:
            source = hit["_source"]
            remaining_cents = int(source.get("remaining_cents", 0))
            issued_at_raw = source.get("issued_at")

            if issued_at_raw is None or remaining_cents <= 0:
                continue

            days_aged = self._compute_days_aged(issued_at_raw, now)

            if days_aged <= 30:
                bucket_0_30 += remaining_cents
            elif days_aged <= 60:
                bucket_31_60 += remaining_cents
            elif days_aged <= 90:
                bucket_61_90 += remaining_cents
            else:
                bucket_90_plus += remaining_cents

        total_open_cents = bucket_0_30 + bucket_31_60 + bucket_61_90 + bucket_90_plus

        return {
            "bucket_0_30_cents": bucket_0_30,
            "bucket_31_60_cents": bucket_31_60,
            "bucket_61_90_cents": bucket_61_90,
            "bucket_90_plus_cents": bucket_90_plus,
            "total_open_cents": total_open_cents,
        }

    # ------------------------------------------------------------------
    # compute_tenant_aging (Req 7.2)
    # ------------------------------------------------------------------

    async def compute_tenant_aging(
        self, tenant_id: str
    ) -> Dict[str, Any]:
        """Compute aging buckets aggregated across all accounts for a tenant.

        Returns a dict with:
          - bucket_0_30_cents: int
          - bucket_31_60_cents: int
          - bucket_61_90_cents: int
          - bucket_90_plus_cents: int
          - total_open_cents: int
          - by_account: list of top 50 accounts by total_open_cents desc

        Only includes invoices with status in (open, partial, overdue).
        Aging is computed from the invoice's issued_at relative to utcnow().

        Validates: Requirements 7.2, C1, C2, C3
        """
        now = utcnow()

        # Query all open invoices for this tenant
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"terms": {"status": _AGING_STATUSES}},
                        {"exists": {"field": "issued_at"}},
                    ]
                }
            },
            "size": 10000,
            "_source": ["invoice_id", "account_id", "issued_at", "remaining_cents"],
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, query, size=10000
        )

        hits = response["hits"]["hits"]

        # Aggregate buckets at tenant level and per-account
        tenant_bucket_0_30 = 0
        tenant_bucket_31_60 = 0
        tenant_bucket_61_90 = 0
        tenant_bucket_90_plus = 0

        # Per-account tracking
        account_aging: Dict[str, Dict[str, int]] = {}

        for hit in hits:
            source = hit["_source"]
            remaining_cents = int(source.get("remaining_cents", 0))
            issued_at_raw = source.get("issued_at")
            acct_id = source.get("account_id", "unknown")

            if issued_at_raw is None or remaining_cents <= 0:
                continue

            days_aged = self._compute_days_aged(issued_at_raw, now)

            # Initialize account entry if needed
            if acct_id not in account_aging:
                account_aging[acct_id] = {
                    "account_id": acct_id,
                    "bucket_0_30_cents": 0,
                    "bucket_31_60_cents": 0,
                    "bucket_61_90_cents": 0,
                    "bucket_90_plus_cents": 0,
                    "total_open_cents": 0,
                }

            # Assign to bucket
            if days_aged <= 30:
                tenant_bucket_0_30 += remaining_cents
                account_aging[acct_id]["bucket_0_30_cents"] += remaining_cents
            elif days_aged <= 60:
                tenant_bucket_31_60 += remaining_cents
                account_aging[acct_id]["bucket_31_60_cents"] += remaining_cents
            elif days_aged <= 90:
                tenant_bucket_61_90 += remaining_cents
                account_aging[acct_id]["bucket_61_90_cents"] += remaining_cents
            else:
                tenant_bucket_90_plus += remaining_cents
                account_aging[acct_id]["bucket_90_plus_cents"] += remaining_cents

            account_aging[acct_id]["total_open_cents"] += remaining_cents

        total_open_cents = (
            tenant_bucket_0_30
            + tenant_bucket_31_60
            + tenant_bucket_61_90
            + tenant_bucket_90_plus
        )

        # Sort accounts by total_open_cents descending, take top 50
        sorted_accounts = sorted(
            account_aging.values(),
            key=lambda a: a["total_open_cents"],
            reverse=True,
        )
        top_accounts = sorted_accounts[:_TOP_ACCOUNTS_LIMIT]

        return {
            "bucket_0_30_cents": tenant_bucket_0_30,
            "bucket_31_60_cents": tenant_bucket_31_60,
            "bucket_61_90_cents": tenant_bucket_61_90,
            "bucket_90_plus_cents": tenant_bucket_90_plus,
            "total_open_cents": total_open_cents,
            "by_account": top_accounts,
        }

    # ------------------------------------------------------------------
    # write_daily_snapshot (Req 9.4)
    # ------------------------------------------------------------------

    async def write_daily_snapshot(
        self, tenant_id: str
    ) -> Dict[str, Any]:
        """Persist the tenant-level aging to ar_aging_snapshots.

        Idempotent via snapshot_id = '{tenant_id}:{YYYY-MM-DD}'.
        If a snapshot for today already exists, it is overwritten
        (upsert semantics via the deterministic document ID).

        Also computes account_count_with_balance: the number of distinct
        accounts that have a non-zero open balance.

        Validates: Requirements 9.4, C1, C2, C3
        """
        now = utcnow()
        snapshot_date = now.date().isoformat()
        snapshot_id = f"{tenant_id}:{snapshot_date}"

        # Compute tenant-level aging
        aging = await self.compute_tenant_aging(tenant_id)

        # Count distinct accounts with a balance
        account_count_with_balance = sum(
            1 for acct in aging.get("by_account", [])
            if acct.get("total_open_cents", 0) > 0
        )

        # If there are more accounts beyond the top 50 that have balances,
        # we need to count them from the full query. The by_account list
        # is capped at 50, so we do a separate count query.
        account_count_with_balance = await self._count_accounts_with_balance(
            tenant_id
        )

        snapshot_doc: Dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "tenant_id": tenant_id,
            "snapshot_date": snapshot_date,
            "total_open_cents": aging["total_open_cents"],
            "bucket_0_30_cents": aging["bucket_0_30_cents"],
            "bucket_31_60_cents": aging["bucket_31_60_cents"],
            "bucket_61_90_cents": aging["bucket_61_90_cents"],
            "bucket_90_plus_cents": aging["bucket_90_plus_cents"],
            "account_count_with_balance": account_count_with_balance,
        }

        # Persist using snapshot_id as the document ID for idempotency
        await self._es.index_document(
            AR_AGING_SNAPSHOTS_INDEX, snapshot_id, snapshot_doc
        )

        logger.info(
            "Wrote daily AR aging snapshot for tenant %s date %s "
            "(total_open: %d cents, accounts_with_balance: %d)",
            tenant_id,
            snapshot_date,
            aging["total_open_cents"],
            account_count_with_balance,
        )

        return snapshot_doc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_days_aged(issued_at_raw, now) -> int:
        """Compute the number of days between issued_at and now.

        Handles both ISO string and epoch-millis formats that ES may
        return. Returns a non-negative integer.
        """
        from datetime import datetime

        if isinstance(issued_at_raw, (int, float)):
            # Epoch millis from ES
            issued_dt = datetime.fromtimestamp(
                issued_at_raw / 1000.0, tz=timezone.utc
            )
        elif isinstance(issued_at_raw, str):
            # ISO format string
            issued_at_str = issued_at_raw
            # Handle various ISO formats
            if issued_at_str.endswith("Z"):
                issued_at_str = issued_at_str[:-1] + "+00:00"
            try:
                issued_dt = datetime.fromisoformat(issued_at_str)
            except ValueError:
                # Fallback: try parsing as date only
                from datetime import date as date_type

                d = date_type.fromisoformat(issued_at_str[:10])
                issued_dt = datetime(
                    d.year, d.month, d.day, tzinfo=timezone.utc
                )
        elif isinstance(issued_at_raw, datetime):
            issued_dt = issued_at_raw
        else:
            return 0

        # Ensure timezone-aware
        if issued_dt.tzinfo is None:
            issued_dt = issued_dt.replace(tzinfo=timezone.utc)

        # Ensure now is timezone-aware
        now_aware = now
        if now_aware.tzinfo is None:
            now_aware = now_aware.replace(tzinfo=timezone.utc)

        delta = now_aware - issued_dt
        return max(0, delta.days)

    async def _count_accounts_with_balance(
        self, tenant_id: str
    ) -> int:
        """Count distinct accounts that have at least one open invoice.

        Uses a cardinality aggregation on account_id filtered to
        invoices with status in (open, partial, overdue) and
        remaining_cents > 0.

        Validates: Constraint C3
        """
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"terms": {"status": _AGING_STATUSES}},
                        {"range": {"remaining_cents": {"gt": 0}}},
                    ]
                }
            },
            "size": 0,
            "aggs": {
                "account_count": {
                    "cardinality": {"field": "account_id"}
                }
            },
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, query, size=0
        )

        aggs = response.get("aggregations", {})
        count = aggs.get("account_count", {}).get("value", 0)
        return int(count)
