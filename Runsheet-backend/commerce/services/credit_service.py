"""Credit check + hold transitions.

Implements the CreditService with check, apply_override, expire_override,
and on_payment_applied methods. Enforces the credit state machine from
design §4.2:

    States: ok, hold, override
    ok → hold: when open_balance_cents > credit_limit_cents
    hold → ok: payment brings account back under limit
    ok|hold → override: apply_override records a one-shot bypass
    override → ok|hold: override expires, re-run limit check

Every state transition writes an AccountEvent to account_events for audit.

Validates: Requirements 2.5, 2.6, 4.3, 4.4, C1, C2, C3
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict

from commerce.models.account import CreditState
from commerce.models.events import AccountEvent, AccountEventType
from commerce.services.commerce_es_mappings import (
    ACCOUNTS_CURRENT_INDEX,
    ACCOUNT_EVENTS_INDEX,
)
from errors.exceptions import resource_not_found, validation_error
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreditDecision:
    """Result of a credit check evaluation.

    Attributes:
        approved: Whether the order can proceed without a hold.
        reason: Human-readable explanation of the decision.
        hold_required: Whether the order should be placed on hold.
        override_active: Whether a credit override is currently active.
    """

    approved: bool
    reason: str
    hold_required: bool
    override_active: bool


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CreditService:
    """Credit check and hold transition service.

    Enforces the credit state machine defined in design §4.2. Every
    state transition writes an AccountEvent to the account_events index
    for audit and idempotent projection replay.

    All methods take ``tenant_id`` and all queries use
    ``inject_tenant_filter`` (Constraint C3).
    """

    def __init__(self, es_service: ElasticsearchService) -> None:
        self._es = es_service

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_account(self, tenant_id: str, account_id: str) -> Dict[str, Any]:
        """Retrieve an account document scoped to tenant.

        Raises resource_not_found if the account does not exist under
        the given tenant.
        """
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_account_get_or_none,
        )

        pg = await read_account_get_or_none(tenant_id, account_id)
        if pg is not _NOT_CUT_OVER:
            if pg is None:
                raise resource_not_found(
                    f"Account '{account_id}' not found",
                    details={"account_id": account_id, "tenant_id": tenant_id},
                )
            return pg

        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"account_id": account_id}},
                    ]
                }
            },
            "size": 1,
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            ACCOUNTS_CURRENT_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"Account '{account_id}' not found",
                details={"account_id": account_id, "tenant_id": tenant_id},
            )

        return hits[0]["_source"]

    async def _compute_open_balance(self, tenant_id: str, account_id: str) -> int:
        """Compute the open balance for an account.

        Sums remaining_cents from all non-void, non-paid invoices.
        Returns integer cents (Constraint C1).
        """
        from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX

        _OPEN_STATUSES = ["open", "partial", "overdue", "draft"]

        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_invoice_sum_remaining,
        )

        pg = await read_invoice_sum_remaining(
            tenant_id, account_id, statuses=_OPEN_STATUSES
        )
        if pg is not _NOT_CUT_OVER:
            return int(pg)

        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"account_id": account_id}},
                        {
                            "terms": {
                                "status": _OPEN_STATUSES
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

        # Ensure integer — ES sum can return float for long fields
        return int(total)

    async def _get_next_sequence_number(
        self, tenant_id: str, account_id: str
    ) -> int:
        """Get the next sequence number for an account's event log."""
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

    async def _update_account(
        self, account_id: str, partial: Dict[str, Any]
    ) -> None:
        """Update the account projection in accounts_current."""
        partial["updated_at"] = utcnow().isoformat()
        await self._es.update_document(ACCOUNTS_CURRENT_INDEX, account_id, partial)

    async def _evaluate_credit_state(
        self, tenant_id: str, account_id: str
    ) -> CreditState:
        """Evaluate what the credit state should be based on current balances.

        Returns CreditState.HOLD if open_balance > credit_limit,
        otherwise CreditState.OK.
        """
        account = await self._get_account(tenant_id, account_id)
        open_balance = await self._compute_open_balance(tenant_id, account_id)
        credit_limit = account.get("credit_limit_cents", 0)

        if open_balance > credit_limit:
            return CreditState.HOLD
        return CreditState.OK

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(
        self,
        *,
        tenant_id: str,
        account_id: str,
        order_total_cents: int,
    ) -> CreditDecision:
        """Evaluate credit state for an account and return a CreditDecision.

        The decision considers:
        1. Whether a credit override is currently active.
        2. Whether the account is already on hold.
        3. Whether the new order would push the account over its limit.

        Validates: Requirements 2.5, 4.3, C1, C3
        """
        account = await self._get_account(tenant_id, account_id)

        credit_state = account.get("credit_state", CreditState.OK.value)
        credit_limit = account.get("credit_limit_cents", 0)
        open_balance = await self._compute_open_balance(tenant_id, account_id)
        override_expires_at = account.get("credit_override_expires_at")

        # Check if override is active
        override_active = False
        if credit_state == CreditState.OVERRIDE.value:
            now = utcnow()
            if override_expires_at:
                # Parse the expiry if it's a string
                if isinstance(override_expires_at, str):
                    from datetime import timezone

                    expires_at = datetime.fromisoformat(
                        override_expires_at.replace("Z", "+00:00")
                    )
                else:
                    expires_at = override_expires_at

                # Ensure timezone-aware comparison
                if expires_at.tzinfo is None:
                    from datetime import timezone

                    expires_at = expires_at.replace(tzinfo=timezone.utc)

                if now < expires_at:
                    override_active = True

            if override_active:
                return CreditDecision(
                    approved=True,
                    reason="credit_override_active",
                    hold_required=False,
                    override_active=True,
                )

        # If account is already on hold, the order should be held
        if credit_state == CreditState.HOLD.value:
            return CreditDecision(
                approved=False,
                reason="credit_limit_exceeded",
                hold_required=True,
                override_active=False,
            )

        # Check if the new order would push the account over its limit
        # credit_limit_cents == 0 means "cash on delivery only" — always hold
        projected_balance = open_balance + order_total_cents
        if credit_limit == 0 or projected_balance > credit_limit:
            return CreditDecision(
                approved=False,
                reason="credit_limit_exceeded",
                hold_required=True,
                override_active=False,
            )

        return CreditDecision(
            approved=True,
            reason="within_credit_limit",
            hold_required=False,
            override_active=False,
        )

    async def apply_override(
        self,
        *,
        tenant_id: str,
        account_id: str,
        reason: str,
        authorized_by: str,
        expires_at: datetime,
    ) -> None:
        """Record a one-shot credit override that bypasses credit checks.

        Transitions the account's credit_state to 'override' regardless of
        whether it was previously 'ok' or 'hold'. The override remains active
        until expires_at OR until a single order clears (whichever comes first).

        Writes an account_events row for audit (Req 2.6).

        Validates: Requirements 2.6, C2, C3
        """
        # Validate inputs
        if not reason or not reason.strip():
            raise validation_error(
                "reason is required for credit override",
                details={"reason": reason},
            )
        if not authorized_by or not authorized_by.strip():
            raise validation_error(
                "authorized_by is required for credit override",
                details={"authorized_by": authorized_by},
            )

        now = utcnow()
        if expires_at.tzinfo is None:
            from datetime import timezone

            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at <= now:
            raise validation_error(
                "expires_at must be in the future",
                details={"expires_at": expires_at.isoformat()},
            )

        account = await self._get_account(tenant_id, account_id)
        old_state = account.get("credit_state", CreditState.OK.value)

        # Transition to override state
        await self._update_account(
            account_id,
            {
                "credit_state": CreditState.OVERRIDE.value,
                "credit_override_expires_at": expires_at.isoformat(),
            },
        )

        # Write audit event
        await self._write_account_event(
            tenant_id=tenant_id,
            account_id=account_id,
            event_type=AccountEventType.OVERRIDE_APPLIED,
            payload={
                "old_state": old_state,
                "new_state": CreditState.OVERRIDE.value,
                "reason": reason,
                "authorized_by": authorized_by,
                "expires_at": expires_at.isoformat(),
            },
            actor=authorized_by,
        )

        logger.info(
            "Applied credit override for account %s tenant %s (authorized_by=%s, expires_at=%s)",
            account_id,
            tenant_id,
            authorized_by,
            expires_at.isoformat(),
        )

    async def expire_override(
        self,
        *,
        tenant_id: str,
        account_id: str,
    ) -> None:
        """Expire an active credit override and re-evaluate credit state.

        After expiring the override, re-runs the limit check to determine
        whether the account should transition to 'ok' or 'hold'.

        Writes an account_events row for audit.

        Validates: Requirements 2.6, C2, C3
        """
        account = await self._get_account(tenant_id, account_id)
        current_state = account.get("credit_state", CreditState.OK.value)

        # Only expire if currently in override state
        if current_state != CreditState.OVERRIDE.value:
            logger.info(
                "Account %s is not in override state (current=%s), skipping expire",
                account_id,
                current_state,
            )
            return

        # Re-evaluate what the state should be
        new_state = await self._evaluate_credit_state(tenant_id, account_id)

        # Update the account projection
        await self._update_account(
            account_id,
            {
                "credit_state": new_state.value,
                "credit_override_expires_at": None,
            },
        )

        # Write audit event for override expiry
        await self._write_account_event(
            tenant_id=tenant_id,
            account_id=account_id,
            event_type=AccountEventType.OVERRIDE_EXPIRED,
            payload={
                "old_state": CreditState.OVERRIDE.value,
                "new_state": new_state.value,
                "reason": "override_expired",
            },
            actor="system",
        )

        # If the new state is different from ok, also write a credit_state_changed event
        if new_state == CreditState.HOLD:
            await self._write_account_event(
                tenant_id=tenant_id,
                account_id=account_id,
                event_type=AccountEventType.CREDIT_STATE_CHANGED,
                payload={
                    "old_state": CreditState.OVERRIDE.value,
                    "new_state": CreditState.HOLD.value,
                    "reason": "override_expired_still_over_limit",
                    "open_balance_cents": await self._compute_open_balance(
                        tenant_id, account_id
                    ),
                    "credit_limit_cents": account.get("credit_limit_cents", 0),
                },
                actor="system",
            )

        logger.info(
            "Expired credit override for account %s tenant %s → new state: %s",
            account_id,
            tenant_id,
            new_state.value,
        )

    async def on_payment_applied(
        self,
        *,
        tenant_id: str,
        account_id: str,
    ) -> None:
        """Re-evaluate credit state after a payment is applied.

        If the account is on hold and the payment brings the open balance
        back under the credit limit, transitions the account to 'ok'.

        Idempotent: if the account is already in 'ok' state, this is a no-op.

        Validates: Requirements 2.5, 4.4, C1, C3
        """
        account = await self._get_account(tenant_id, account_id)
        current_state = account.get("credit_state", CreditState.OK.value)

        # Only transition if currently on hold
        if current_state != CreditState.HOLD.value:
            logger.debug(
                "Account %s is not on hold (current=%s), no transition needed",
                account_id,
                current_state,
            )
            return

        # Re-evaluate the credit state
        open_balance = await self._compute_open_balance(tenant_id, account_id)
        credit_limit = account.get("credit_limit_cents", 0)

        if open_balance <= credit_limit:
            # Payment brought account back under limit — transition to ok
            await self._update_account(
                account_id,
                {
                    "credit_state": CreditState.OK.value,
                    "open_balance_cents": open_balance,
                    "available_credit_cents": credit_limit - open_balance,
                },
            )

            # Write credit_state_changed event
            await self._write_account_event(
                tenant_id=tenant_id,
                account_id=account_id,
                event_type=AccountEventType.CREDIT_STATE_CHANGED,
                payload={
                    "old_state": CreditState.HOLD.value,
                    "new_state": CreditState.OK.value,
                    "reason": "payment_applied",
                    "open_balance_cents": open_balance,
                    "credit_limit_cents": credit_limit,
                },
                actor="system",
            )

            logger.info(
                "Payment released credit hold for account %s tenant %s "
                "(balance=%d, limit=%d)",
                account_id,
                tenant_id,
                open_balance,
                credit_limit,
            )
        else:
            # Still over limit — update balance projection but stay on hold
            await self._update_account(
                account_id,
                {
                    "open_balance_cents": open_balance,
                    "available_credit_cents": credit_limit - open_balance,
                },
            )

            logger.info(
                "Payment applied but account %s still over limit "
                "(balance=%d, limit=%d), remaining on hold",
                account_id,
                open_balance,
                credit_limit,
            )
