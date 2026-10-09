"""Aging bucket computation + snapshots.

Implements the ARAgingService with compute_account_aging,
compute_tenant_aging, and write_daily_snapshot methods.

Aging buckets are computed from days past due
(``today - due_date`` in UTC calendar days), see :func:`bucket_for_invoice`:
  - current      not yet due (<= 0 days past due)
  - 0-30 key     1-30 days past due (key kept for compatibility)
  - 31-60        31-60 days past due
  - 61-90        61-90 days past due
  - 90+          more than 90 days past due
An invoice with no due_date falls back to issued_at (due on receipt); one
with neither is skipped. Snapshots written before this change have
``bucket_current_cents`` null and were aged by issued_at.

Only invoices with status in (open, partial, overdue) are included.
All monetary values are integer cents (Constraint C1).
All queries use inject_tenant_filter (Constraint C3).
Timestamps use utcnow() (Constraint C2).

Validates: Requirements 7.1, 7.2, 9.4, C1, C2, C3
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

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

#: Bucket keys in display order. ``bucket_0_30_cents`` means 1-30 days past
#: due; the key is unchanged so stored snapshots and clients keep working.
BUCKET_KEYS = (
    "bucket_current_cents",
    "bucket_0_30_cents",
    "bucket_31_60_cents",
    "bucket_61_90_cents",
    "bucket_90_plus_cents",
)


def _to_datetime(raw: Any) -> Optional[datetime]:
    """Parse an ES/PG date value (ISO string, epoch millis or datetime)."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, (int, float)):
        dt = datetime.fromtimestamp(raw / 1000.0, tz=timezone.utc)
    elif isinstance(raw, str):
        text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                d = date.fromisoformat(text[:10])
            except ValueError:
                return None
            dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def days_past_due(source: Dict[str, Any], now: datetime) -> Optional[int]:
    """UTC calendar days past ``due_date`` (falling back to ``issued_at``).

    Negative or zero means not yet due. None when the invoice has neither.
    """
    due = _to_datetime(source.get("due_date")) or _to_datetime(source.get("issued_at"))
    if due is None:
        return None
    now_aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return (now_aware.astimezone(timezone.utc).date() - due.astimezone(timezone.utc).date()).days


def bucket_for_invoice(source: Dict[str, Any], now: datetime) -> Optional[str]:
    """Bucket key for one open invoice, or None to skip it (no dates)."""
    days = days_past_due(source, now)
    if days is None:
        return None
    if days <= 0:
        return "bucket_current_cents"
    if days <= 30:
        return "bucket_0_30_cents"
    if days <= 60:
        return "bucket_31_60_cents"
    if days <= 90:
        return "bucket_61_90_cents"
    return "bucket_90_plus_cents"


def empty_buckets() -> Dict[str, int]:
    return {key: 0 for key in BUCKET_KEYS}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ARAgingService:
    """Service layer for AR aging bucket computation and daily snapshots.

    Computes aging buckets (current, 1-30, 31-60, 61-90, 90+ days past due)
    from each invoice's ``due_date`` relative to ``utcnow()`` (see
    :func:`bucket_for_invoice`).

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
          - bucket_current_cents: int (not yet due)
          - bucket_0_30_cents: int (1-30 days past due)
          - bucket_31_60_cents: int
          - bucket_61_90_cents: int
          - bucket_90_plus_cents: int
          - total_open_cents: int (includes current)

        Only includes invoices with status in (open, partial, overdue).
        Aging is days past due_date relative to utcnow().

        Validates: Requirements 7.1, C1, C2, C3
        """
        now = utcnow()

        hits = await self._fetch_open_invoices(tenant_id, account_id=account_id)

        buckets = empty_buckets()
        for source in hits:
            remaining_cents = int(source.get("remaining_cents", 0))
            if remaining_cents <= 0:
                continue
            key = bucket_for_invoice(source, now)
            if key is None:
                continue
            buckets[key] += remaining_cents

        return {**buckets, "total_open_cents": sum(buckets.values())}

    # ------------------------------------------------------------------
    # compute_tenant_aging (Req 7.2)
    # ------------------------------------------------------------------

    async def compute_tenant_aging(
        self, tenant_id: str
    ) -> Dict[str, Any]:
        """Compute aging buckets aggregated across all accounts for a tenant.

        Returns a dict with the bucket keys of
        :meth:`compute_account_aging`, ``total_open_cents`` and
        ``by_account`` (top 50 accounts by total_open_cents desc).

        Only includes invoices with status in (open, partial, overdue).
        Aging is days past due_date relative to utcnow().

        Validates: Requirements 7.2, C1, C2, C3
        """
        now = utcnow()

        hits = await self._fetch_open_invoices(tenant_id)

        tenant_buckets = empty_buckets()
        account_aging: Dict[str, Dict[str, Any]] = {}

        for source in hits:
            remaining_cents = int(source.get("remaining_cents", 0))
            acct_id = source.get("account_id", "unknown")
            if remaining_cents <= 0:
                continue
            key = bucket_for_invoice(source, now)
            if key is None:
                continue

            if acct_id not in account_aging:
                account_aging[acct_id] = {
                    "account_id": acct_id,
                    **empty_buckets(),
                    "total_open_cents": 0,
                }
            tenant_buckets[key] += remaining_cents
            account_aging[acct_id][key] += remaining_cents
            account_aging[acct_id]["total_open_cents"] += remaining_cents

        sorted_accounts = sorted(
            account_aging.values(),
            key=lambda a: a["total_open_cents"],
            reverse=True,
        )
        top_accounts = sorted_accounts[:_TOP_ACCOUNTS_LIMIT]

        return {
            **tenant_buckets,
            "total_open_cents": sum(tenant_buckets.values()),
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
            "bucket_current_cents": aging["bucket_current_cents"],
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

        # Dual-write the AR aging snapshot to the Postgres source-of-truth.
        from commerce.services.commerce_persistence_bridge import (
            mirror_ar_aging_snapshot,
        )
        await mirror_ar_aging_snapshot(snapshot_doc)

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

    async def _fetch_open_invoices(
        self, tenant_id: str, *, account_id: str | None = None,
        require_issued_at: bool = False,
    ) -> List[Dict[str, Any]]:
        """Fetch open invoices for aging, from Postgres when cut over else ES.

        Returns a list of ``invoices_current`` documents (``_source`` shape)
        so the bucket math is identical on both paths.
        """
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_invoices_open_for_aggregation,
        )

        pg = await read_invoices_open_for_aggregation(
            tenant_id, statuses=_AGING_STATUSES, account_id=account_id,
            require_issued_at=require_issued_at,
        )
        if pg is not _NOT_CUT_OVER:
            return pg

        must: List[Dict[str, Any]] = [{"terms": {"status": _AGING_STATUSES}}]
        if account_id:
            must.append({"term": {"account_id": account_id}})
        if require_issued_at:
            must.append({"exists": {"field": "issued_at"}})
        query: Dict[str, Any] = {
            "query": {"bool": {"must": must}},
            "size": 10000,
        }
        query = inject_tenant_filter(query, tenant_id)
        response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, query, size=10000
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]

    async def _count_accounts_with_balance(
        self, tenant_id: str
    ) -> int:
        """Count distinct accounts that have at least one open invoice.

        Uses a cardinality aggregation on account_id filtered to
        invoices with status in (open, partial, overdue) and
        remaining_cents > 0.

        Validates: Constraint C3
        """
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_invoice_count_accounts_with_balance,
        )

        pg = await read_invoice_count_accounts_with_balance(
            tenant_id, statuses=_AGING_STATUSES
        )
        if pg is not _NOT_CUT_OVER:
            return int(pg)

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
