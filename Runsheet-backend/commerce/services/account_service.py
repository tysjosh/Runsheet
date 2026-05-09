"""CRUD + credit state machine for Account.

Implements the AccountService with create, get, list, update, and
compute_open_balance methods. Every method takes tenant_id as its first
parameter and every ES query passes through inject_tenant_filter.

Writes account_events on every state-changing operation for audit and
idempotent projection replay.

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, C1, C2, C3
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from commerce.models.account import (
    AccountStatus,
    AccountTier,
    CreditState,
    PaymentMethodPreference,
    _MAX_CREDIT_LIMIT_CENTS,
    _VALID_NET_TERMS_DAYS,
)
from commerce.models.events import AccountEvent, AccountEventType
from commerce.services.commerce_es_mappings import (
    ACCOUNTS_CURRENT_INDEX,
    ACCOUNT_EVENTS_INDEX,
    CUSTOMERS_CURRENT_INDEX,
    INVOICES_CURRENT_INDEX,
)
from errors.exceptions import resource_not_found, validation_error
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_PAGE_LIMIT = 50
_MAX_PAGE_LIMIT = 200


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AccountService:
    """Service layer for Account CRUD and credit state management.

    Every public method takes ``tenant_id`` as its first positional argument
    and every Elasticsearch query is wrapped with ``inject_tenant_filter``
    to enforce strict tenant isolation (Constraint C3).

    Every state-changing operation writes an ``AccountEvent`` to the
    ``account_events`` index for audit and idempotent projection replay.
    """

    def __init__(self, es_service: ElasticsearchService) -> None:
        self._es = es_service

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_credit_limit_cents(value: int) -> int:
        """Validate credit_limit_cents is in [0, 999_999_999_999].

        Validates: Requirement 2.2
        """
        if not isinstance(value, int):
            raise validation_error(
                "credit_limit_cents must be an integer",
                details={"credit_limit_cents": value},
            )
        if value < 0:
            raise validation_error(
                "credit_limit_cents must be >= 0",
                details={"credit_limit_cents": value},
            )
        if value > _MAX_CREDIT_LIMIT_CENTS:
            raise validation_error(
                f"credit_limit_cents must be <= {_MAX_CREDIT_LIMIT_CENTS}",
                details={"credit_limit_cents": value},
            )
        return value

    @staticmethod
    def _validate_net_terms_days(value: int) -> int:
        """Validate net_terms_days is in {0, 7, 15, 30, 45, 60, 90}.

        Validates: Requirement 2.3
        """
        if value not in _VALID_NET_TERMS_DAYS:
            raise validation_error(
                f"net_terms_days must be one of {sorted(_VALID_NET_TERMS_DAYS)}, got {value}",
                details={"net_terms_days": value},
            )
        return value

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    async def _get_next_sequence_number(
        self, tenant_id: str, account_id: str
    ) -> int:
        """Get the next sequence number for an account's event log.

        Queries the account_events index for the highest sequence_number
        for this account and returns max + 1.
        """
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"account_id": account_id}},
                    ]
                }
            },
            "size": 0,
            "aggs": {
                "max_seq": {"max": {"field": "sequence_number"}},
            },
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            ACCOUNT_EVENTS_INDEX, query, size=0
        )

        aggs = response.get("aggregations", {})
        max_seq = aggs.get("max_seq", {}).get("value")

        if max_seq is None:
            return 1
        return int(max_seq) + 1

    async def _write_account_event(
        self,
        tenant_id: str,
        account_id: str,
        event_type: AccountEventType,
        payload: Dict[str, Any],
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Write an AccountEvent to the account_events index.

        Returns the event document as a dict.
        """
        seq = await self._get_next_sequence_number(tenant_id, account_id)
        now = utcnow()

        event = AccountEvent(
            account_id=account_id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload=payload,
            occurred_at=now,
            actor=actor,
            sequence_number=seq,
        )

        event_doc = event.model_dump()
        # Serialize datetime to ISO string for ES
        event_doc["occurred_at"] = event_doc["occurred_at"].isoformat()

        await self._es.index_document(
            ACCOUNT_EVENTS_INDEX, event_doc["event_id"], event_doc
        )

        logger.info(
            "Wrote account event %s (type=%s, seq=%d) for account %s tenant %s",
            event_doc["event_id"],
            event_type.value,
            seq,
            account_id,
            tenant_id,
        )
        return event_doc

    # ------------------------------------------------------------------
    # Customer existence check
    # ------------------------------------------------------------------

    async def _assert_customer_exists(
        self, tenant_id: str, customer_id: str
    ) -> None:
        """Assert that the referenced Customer exists under the caller's tenant.

        Validates: Requirement 2.1
        """
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"customer_id": customer_id}},
                    ]
                }
            },
            "size": 1,
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            CUSTOMERS_CURRENT_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise validation_error(
                f"Customer '{customer_id}' not found under tenant '{tenant_id}'",
                details={"customer_id": customer_id, "tenant_id": tenant_id},
            )

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(
        self,
        tenant_id: str,
        *,
        customer_id: str,
        display_name: str,
        credit_limit_cents: int = 0,
        net_terms_days: int = 30,
        billing_address: Optional[Dict[str, Any]] = None,
        payment_method_preference: str = "invoice",
        status: str = "active",
        tier: str = "default",
        external_refs: Optional[Dict[str, Any]] = None,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Create a new Account record.

        Assigns a server-generated ``account_id`` of shape ``acct_<uuid4>``,
        asserts the referenced Customer exists under the caller's tenant,
        validates credit_limit_cents and net_terms_days, and persists to
        ``accounts_current``.

        Writes an ``AccountEvent`` of type ``created`` to ``account_events``.

        Validates: Requirements 2.1, 2.2, 2.3, C1, C2, C3
        """
        # Validate inputs
        self._validate_credit_limit_cents(credit_limit_cents)
        self._validate_net_terms_days(net_terms_days)

        # Assert customer exists under this tenant
        await self._assert_customer_exists(tenant_id, customer_id)

        now = utcnow()
        account_id = f"acct_{uuid4()}"

        # Compute initial derived fields
        open_balance_cents = 0
        available_credit_cents = credit_limit_cents - open_balance_cents

        doc: Dict[str, Any] = {
            "account_id": account_id,
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "display_name": display_name,
            "status": status,
            "credit_limit_cents": credit_limit_cents,
            "open_balance_cents": open_balance_cents,
            "available_credit_cents": available_credit_cents,
            "credit_balance_cents": 0,
            "credit_state": CreditState.OK.value,
            "credit_override_expires_at": None,
            "net_terms_days": net_terms_days,
            "tier": tier,
            "billing_address": billing_address,
            "payment_method_preference": payment_method_preference,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "external_refs": external_refs or {},
        }

        await self._es.index_document(ACCOUNTS_CURRENT_INDEX, account_id, doc)

        # Write the created event
        await self._write_account_event(
            tenant_id=tenant_id,
            account_id=account_id,
            event_type=AccountEventType.CREATED,
            payload={
                "customer_id": customer_id,
                "display_name": display_name,
                "credit_limit_cents": credit_limit_cents,
                "net_terms_days": net_terms_days,
                "credit_state": CreditState.OK.value,
            },
            actor=actor,
        )

        logger.info(
            "Created account %s for customer %s tenant %s",
            account_id,
            customer_id,
            tenant_id,
        )
        return doc

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

    async def get(self, tenant_id: str, account_id: str) -> Dict[str, Any]:
        """Retrieve a single Account by ID, scoped to tenant.

        Includes computed fields: open_balance_cents, available_credit_cents,
        oldest_open_invoice_days, and credit_state (Req 2.4).

        Validates: Requirements 2.4, C3
        """
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"account_id": account_id}},
                    ]
                }
            },
            "size": 1,
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            ACCOUNTS_CURRENT_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"Account '{account_id}' not found",
                details={"account_id": account_id},
            )

        account = hits[0]["_source"]

        # Compute live open_balance and derived fields
        open_balance_cents = await self.compute_open_balance(
            tenant_id, account_id
        )
        credit_limit_cents = account.get("credit_limit_cents", 0)
        available_credit_cents = credit_limit_cents - open_balance_cents

        # Compute oldest_open_invoice_days
        oldest_open_invoice_days = await self._compute_oldest_open_invoice_days(
            tenant_id, account_id
        )

        # Attach computed fields to the response
        account["open_balance_cents"] = open_balance_cents
        account["available_credit_cents"] = available_credit_cents
        account["oldest_open_invoice_days"] = oldest_open_invoice_days

        return account

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def list(
        self,
        tenant_id: str,
        *,
        customer_id: Optional[str] = None,
        status: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> Dict[str, Any]:
        """List Accounts for a tenant with cursor/limit pagination.

        Default limit is 50, max 200. Cursor is the ``account_id`` of the
        last item on the previous page (keyset pagination via search_after).

        Validates: Constraint C3
        """
        # Clamp limit
        if limit < 1:
            limit = _DEFAULT_PAGE_LIMIT
        if limit > _MAX_PAGE_LIMIT:
            limit = _MAX_PAGE_LIMIT

        must_clauses: List[Dict[str, Any]] = []
        if customer_id:
            must_clauses.append({"term": {"customer_id": customer_id}})
        if status:
            must_clauses.append({"term": {"status": status}})

        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": must_clauses if must_clauses else [{"match_all": {}}],
                }
            },
            "size": limit,
            "sort": [
                {"created_at": {"order": "desc"}},
                {"account_id": {"order": "asc"}},
            ],
        }

        # Cursor-based pagination using search_after
        if cursor:
            base_query["search_after"] = [cursor, cursor]

        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            ACCOUNTS_CURRENT_INDEX, query, size=limit
        )

        hits = response["hits"]["hits"]
        items = [hit["_source"] for hit in hits]

        # Determine next cursor
        next_cursor: Optional[str] = None
        if hits and len(hits) == limit:
            last_sort = hits[-1].get("sort")
            if last_sort and len(last_sort) >= 2:
                next_cursor = hits[-1]["_source"]["account_id"]

        return {
            "items": items,
            "next_cursor": next_cursor,
            "limit": limit,
        }

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(
        self,
        tenant_id: str,
        account_id: str,
        *,
        display_name: Optional[str] = None,
        credit_limit_cents: Optional[int] = None,
        net_terms_days: Optional[int] = None,
        billing_address: Optional[Dict[str, Any]] = ...,  # type: ignore[assignment]
        payment_method_preference: Optional[str] = None,
        status: Optional[str] = None,
        tier: Optional[str] = None,
        external_refs: Optional[Dict[str, Any]] = None,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Update an existing Account record.

        Only non-None fields are applied. Validates credit_limit_cents and
        net_terms_days when provided. Writes an account_event if the update
        changes credit-relevant fields (credit_limit_cents, status).

        Validates: Requirements 2.2, 2.3, C1, C3
        """
        # Fetch existing account (validates tenant scope)
        existing = await self._get_raw(tenant_id, account_id)

        partial: Dict[str, Any] = {}

        if display_name is not None:
            partial["display_name"] = display_name

        if credit_limit_cents is not None:
            self._validate_credit_limit_cents(credit_limit_cents)
            partial["credit_limit_cents"] = credit_limit_cents

        if net_terms_days is not None:
            self._validate_net_terms_days(net_terms_days)
            partial["net_terms_days"] = net_terms_days

        # billing_address uses sentinel to distinguish "not provided" from "set to None"
        if billing_address is not ...:
            partial["billing_address"] = billing_address

        if payment_method_preference is not None:
            partial["payment_method_preference"] = payment_method_preference

        if status is not None:
            partial["status"] = status

        if tier is not None:
            partial["tier"] = tier

        if external_refs is not None:
            partial["external_refs"] = external_refs

        if not partial:
            return existing

        partial["updated_at"] = utcnow().isoformat()

        # If credit_limit_cents changed, recompute available_credit
        if "credit_limit_cents" in partial:
            open_balance = await self.compute_open_balance(tenant_id, account_id)
            new_limit = partial["credit_limit_cents"]
            partial["open_balance_cents"] = open_balance
            partial["available_credit_cents"] = new_limit - open_balance

            # Check if credit state needs to transition
            old_state = existing.get("credit_state", CreditState.OK.value)
            if old_state != CreditState.OVERRIDE.value:
                if open_balance > new_limit:
                    if old_state != CreditState.HOLD.value:
                        partial["credit_state"] = CreditState.HOLD.value
                        await self._write_account_event(
                            tenant_id=tenant_id,
                            account_id=account_id,
                            event_type=AccountEventType.CREDIT_STATE_CHANGED,
                            payload={
                                "old_state": old_state,
                                "new_state": CreditState.HOLD.value,
                                "reason": "credit_limit_reduced",
                                "open_balance_cents": open_balance,
                                "credit_limit_cents": new_limit,
                            },
                            actor=actor,
                        )
                else:
                    if old_state == CreditState.HOLD.value:
                        partial["credit_state"] = CreditState.OK.value
                        await self._write_account_event(
                            tenant_id=tenant_id,
                            account_id=account_id,
                            event_type=AccountEventType.CREDIT_STATE_CHANGED,
                            payload={
                                "old_state": old_state,
                                "new_state": CreditState.OK.value,
                                "reason": "credit_limit_increased",
                                "open_balance_cents": open_balance,
                                "credit_limit_cents": new_limit,
                            },
                            actor=actor,
                        )

        await self._es.update_document(ACCOUNTS_CURRENT_INDEX, account_id, partial)

        merged = {**existing, **partial}
        logger.info("Updated account %s for tenant %s", account_id, tenant_id)
        return merged

    # ------------------------------------------------------------------
    # Compute open balance
    # ------------------------------------------------------------------

    async def compute_open_balance(
        self, tenant_id: str, account_id: str
    ) -> int:
        """Compute the open balance for an account.

        Sums ``remaining_cents`` from all non-void invoices for the account.
        Only invoices with status in (open, partial, overdue, draft) are
        included — void and paid invoices are excluded.

        Returns the total as an integer (cents). Constraint C1 ensures no
        float math.

        Validates: Requirements 2.4, C1, C3
        """
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"account_id": account_id}},
                        {
                            "terms": {
                                "status": ["open", "partial", "overdue", "draft"]
                            }
                        },
                    ]
                }
            },
            "size": 0,
            "aggs": {
                "total_remaining": {"sum": {"field": "remaining_cents"}},
            },
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, query, size=0
        )

        aggs = response.get("aggregations", {})
        total = aggs.get("total_remaining", {}).get("value", 0)

        # Ensure integer (ES sum can return float for long fields)
        return int(total)

    # ------------------------------------------------------------------
    # Oldest open invoice days
    # ------------------------------------------------------------------

    async def _compute_oldest_open_invoice_days(
        self, tenant_id: str, account_id: str
    ) -> int:
        """Compute the number of days since the oldest open invoice was issued.

        Returns 0 if there are no open invoices.

        Validates: Requirement 2.4
        """
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"account_id": account_id}},
                        {
                            "terms": {
                                "status": ["open", "partial", "overdue"]
                            }
                        },
                    ]
                }
            },
            "size": 0,
            "aggs": {
                "oldest_issued": {"min": {"field": "issued_at"}},
            },
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, query, size=0
        )

        aggs = response.get("aggregations", {})
        oldest_ms = aggs.get("oldest_issued", {}).get("value")

        if oldest_ms is None:
            return 0

        # ES returns epoch millis for date aggregations
        from datetime import timezone

        oldest_dt = datetime.fromtimestamp(oldest_ms / 1000.0, tz=timezone.utc)
        now = utcnow()
        # Ensure both are timezone-aware for subtraction
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        delta = now - oldest_dt
        return max(0, delta.days)

    # ------------------------------------------------------------------
    # Raw get (without computed fields, for internal use)
    # ------------------------------------------------------------------

    async def _get_raw(self, tenant_id: str, account_id: str) -> Dict[str, Any]:
        """Retrieve a single Account by ID without computing derived fields.

        Used internally for update operations where we need the stored
        document but don't need to recompute projections.
        """
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"account_id": account_id}},
                    ]
                }
            },
            "size": 1,
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            ACCOUNTS_CURRENT_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"Account '{account_id}' not found",
                details={"account_id": account_id},
            )

        return hits[0]["_source"]

    # ------------------------------------------------------------------
    # Refresh open balance on account projection
    # ------------------------------------------------------------------

    async def refresh_open_balance(
        self,
        tenant_id: str,
        account_id: str,
        *,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Recompute and persist open_balance_cents and available_credit_cents.

        Called after invoice or payment mutations to keep the account
        projection current. Evaluates credit state transitions.

        Validates: Requirements 2.4, 2.5, C1, C3
        """
        existing = await self._get_raw(tenant_id, account_id)

        open_balance = await self.compute_open_balance(tenant_id, account_id)
        credit_limit = existing.get("credit_limit_cents", 0)
        available_credit = credit_limit - open_balance

        partial: Dict[str, Any] = {
            "open_balance_cents": open_balance,
            "available_credit_cents": available_credit,
            "updated_at": utcnow().isoformat(),
        }

        # Evaluate credit state transition (Req 2.5)
        old_state = existing.get("credit_state", CreditState.OK.value)

        # Don't transition if in override state
        if old_state != CreditState.OVERRIDE.value:
            if open_balance > credit_limit and old_state != CreditState.HOLD.value:
                # Transition to hold
                partial["credit_state"] = CreditState.HOLD.value
                await self._write_account_event(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    event_type=AccountEventType.CREDIT_STATE_CHANGED,
                    payload={
                        "old_state": old_state,
                        "new_state": CreditState.HOLD.value,
                        "reason": "over_limit",
                        "open_balance_cents": open_balance,
                        "credit_limit_cents": credit_limit,
                    },
                    actor=actor,
                )
            elif open_balance <= credit_limit and old_state == CreditState.HOLD.value:
                # Transition back to ok
                partial["credit_state"] = CreditState.OK.value
                await self._write_account_event(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    event_type=AccountEventType.CREDIT_STATE_CHANGED,
                    payload={
                        "old_state": old_state,
                        "new_state": CreditState.OK.value,
                        "reason": "payment_applied",
                        "open_balance_cents": open_balance,
                        "credit_limit_cents": credit_limit,
                    },
                    actor=actor,
                )

        await self._es.update_document(ACCOUNTS_CURRENT_INDEX, account_id, partial)

        merged = {**existing, **partial}
        logger.info(
            "Refreshed open balance for account %s: %d cents (available: %d)",
            account_id,
            open_balance,
            available_credit,
        )
        return merged
