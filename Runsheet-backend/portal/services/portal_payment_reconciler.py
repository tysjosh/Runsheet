"""Portal PaymentIntent webhook reconciliation (design §6.3, FREEZE F2, F8).

``integrations/api/stripe_endpoints.receive_stripe_webhook`` hands a verified
event here when ``type`` starts with ``payment_intent.`` and
``metadata.source == "runsheet_portal"``, and for ``charge.refunded`` /
``charge.dispute.created``. :meth:`PortalPaymentReconciler.handle` holds the
attempt row lock (``SELECT ... FOR UPDATE``) for the whole handling, so
deliveries for one attempt are serialized. ``payment_service.ingest`` opens
and commits its own sessions: the lock serializes deliveries, it doesn't make
``ingest`` atomic with the attempt update (review M4). The F8 ensure routine
covers every partial-``ingest`` outcome.

Returns ``{portal, handled, reason}``. The endpoint answers 200 for every
return; an exception propagates as a 500 so Stripe retries.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from commerce.models.events import InvoiceEventType
from errors.exceptions import AppException
from portal.audit import emit_portal_audit
from portal.services.portal_payment_service import (
    PORTAL_PAYMENT_SOURCE,
    PortalPaymentAttemptStore,
)

logger = logging.getLogger(__name__)

PAYMENT_APPLIED = InvoiceEventType.PAYMENT_APPLIED.value
CHARGE_EVENT_TYPES = frozenset({"charge.refunded", "charge.dispute.created"})
_PAYABLE = ("open", "partial", "overdue")


class _ApplyRejected(Exception):
    """Reconciler-private: the Payment is recorded but must not be auto-applied."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _event_type(e: dict) -> str:
    """Normalize an invoice event's type (enum member or stored string) to str."""
    et = e.get("event_type")
    return str(getattr(et, "value", et) or "")


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _error_code(exc: AppException) -> str:
    return str(getattr(exc.error_code, "value", exc.error_code))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PortalPaymentReconciler:
    def __init__(
        self,
        *,
        store: PortalPaymentAttemptStore,
        payment_service: Any,
        invoice_service: Any,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self.payment_service = payment_service
        self.invoice_service = invoice_service
        self._clock = clock

    # -- F8 ------------------------------------------------------------

    async def _ensure_recorded_and_applied(self, t, row, obj, now) -> str:
        payment_service, invoice_service = self.payment_service, self.invoice_service
        actor = f"portal:{row['actor_user_id']}"
        payment = await payment_service.find_by_external_id(
            tenant_id=t, source="stripe", external_id=obj["id"])
        if payment is None:
            # May return an existing, unapplied Payment (Postgres dedupe branch).
            payment = await payment_service.ingest(
                tenant_id=t, invoice_id=row["invoice_id"], account_id=row["account_id"],
                amount_cents=obj.get("amount_received") or obj.get("amount"),
                source="stripe", method="ach", external_id=obj["id"], reference=None,
                received_at=now, actor=actor)
        pid = payment["payment_id"]
        events = await invoice_service.get_events(tenant_id=t, invoice_id=row["invoice_id"])
        if any(_event_type(e) == PAYMENT_APPLIED
               and (e.get("payload") or {}).get("payment_id") == pid for e in events):
            return pid
        invoice = await invoice_service.get(tenant_id=t, invoice_id=row["invoice_id"])
        if invoice.get("status") not in _PAYABLE:
            raise _ApplyRejected("INVOICE_NOT_PAYABLE")
        if payment.get("status") == "reversed":
            raise _ApplyRejected("PAYMENT_ALREADY_REVERSED")
        if payment["amount_cents"] > invoice.get("remaining_cents", 0):
            raise _ApplyRejected("OVERPAYMENT_UNAPPLIED")
        await invoice_service.apply_payment(
            tenant_id=t, invoice_id=row["invoice_id"], amount_cents=payment["amount_cents"],
            payment_id=pid, actor=actor)
        return pid

    # -- entry point ---------------------------------------------------

    async def handle(self, path_tenant_id: str, event: Mapping[str, Any]) -> Dict[str, Any]:
        etype = str(event.get("type") or "")
        obj = _dict(_dict(event.get("data")).get("object"))
        if etype in CHARGE_EVENT_TYPES:
            return await self._handle_charge(path_tenant_id, etype, obj)
        return await self._handle_intent(path_tenant_id, etype, obj)

    async def _handle_charge(self, t: str, etype: str, obj: Dict[str, Any]) -> Dict[str, Any]:
        """R6.15: a WARN audit line for a portal payment; no state change."""
        pi_id = obj.get("payment_intent")
        if not isinstance(pi_id, str) or not pi_id:
            return {"portal": False, "handled": False, "reason": "not_portal_attempt"}
        async with self._store.transaction() as tx:
            row = await tx.find_by_payment_intent(tenant_id=t, stripe_payment_intent_id=pi_id)
        if row is None:
            return {"portal": False, "handled": False, "reason": "not_portal_attempt"}
        emit_portal_audit(
            level=logging.WARNING,
            tenant_id=t,
            actor_user_id=row["actor_user_id"],
            customer_id=row["customer_id"],
            action="payment_refund_or_dispute",
            target_ids={
                "payment_attempt_id": row["payment_attempt_id"],
                "invoice_id": row["invoice_id"],
            },
            outcome="refund_or_dispute",
            request_id=None,
            channel="stripe_webhook",
            reason=etype,
        )
        return {"portal": True, "handled": True, "reason": "refund_or_dispute_logged"}

    def _mismatch(self, t: str, meta: Mapping[str, Any], etype: str) -> Dict[str, Any]:
        emit_portal_audit(
            level=logging.WARNING,
            tenant_id=t,
            actor_user_id=None,
            customer_id=None,
            action="payment_webhook",
            target_ids={
                k: str(meta[k])
                for k in ("payment_attempt_id", "invoice_id")
                if isinstance(meta.get(k), str) and meta.get(k)
            },
            outcome="webhook_mismatch",
            request_id=None,
            channel="stripe_webhook",
            reason=etype,
        )
        return {"portal": True, "handled": False, "reason": "webhook_mismatch"}

    async def _handle_intent(self, t: str, etype: str, obj: Dict[str, Any]) -> Dict[str, Any]:
        meta = _dict(obj.get("metadata"))
        attempt_id = meta.get("payment_attempt_id")
        pi_id = obj.get("id")
        async with self._store.transaction() as tx:
            row = None
            if isinstance(attempt_id, str) and attempt_id:
                row = await tx.lock_for_webhook(tenant_id=t, payment_attempt_id=attempt_id)
            # 2. Verify everything; any mismatch writes nothing (R6.11).
            if (
                row is None
                or meta.get("source") != PORTAL_PAYMENT_SOURCE
                or not (row["tenant_id"] == t == meta.get("tenant_id"))
                or meta.get("customer_id") != row["customer_id"]
                or meta.get("invoice_id") != row["invoice_id"]
                or not isinstance(pi_id, str)
                or not pi_id
                or row.get("stripe_payment_intent_id") not in (None, pi_id)
            ):
                return self._mismatch(t, meta, etype)

            now = self._clock()
            fields: Dict[str, Any] = {}
            if row.get("stripe_payment_intent_id") is None:
                # The create request's own update lost a race or never ran.
                fields["stripe_payment_intent_id"] = pi_id
            status = row["status"]
            reason = "ignored_event_type"

            # 3. Transitions. Terminal states never move back, except succeeded.
            if status == "succeeded":
                reason = "already_succeeded"
            elif etype == "payment_intent.processing":
                if status in ("creating", "created"):
                    fields["status"] = "pending"
                    reason = "pending"
                else:
                    reason = "no_transition"
            elif etype == "payment_intent.succeeded":
                if status in ("failed", "canceled"):
                    logger.warning(
                        "portal_payment_late_success",
                        extra={"extra_data": {"payment_attempt_id": row["payment_attempt_id"],
                                              "previous_status": status}},
                    )
                try:
                    payment_id = await self._ensure_recorded_and_applied(t, row, obj, now)
                except (_ApplyRejected, AppException) as exc:
                    # 4. Recorded (or refused) but not applied: staff resolve it.
                    code = exc.code if isinstance(exc, _ApplyRejected) else _error_code(exc)
                    fields.update(
                        status="succeeded",
                        payment_id=None,
                        failure_code=f"apply_rejected:{code}"[:64],
                        terminal_at=now,
                    )
                    logger.error(
                        "portal_payment_unapplied",
                        extra={"extra_data": {"payment_attempt_id": row["payment_attempt_id"],
                                              "invoice_id": row["invoice_id"],
                                              "code": code}},
                    )
                    reason = "apply_rejected"
                else:
                    fields.update(status="succeeded", payment_id=payment_id,
                                  failure_code=None, terminal_at=now)
                    reason = "succeeded"
            elif etype == "payment_intent.payment_failed":
                code = (_dict(obj.get("last_payment_error")).get("code") or "unknown")
                fields.update(status="failed", failure_code=str(code)[:64],
                              terminal_at=row.get("terminal_at") or now)
                reason = "failed"
            elif etype == "payment_intent.canceled":
                fields.update(status="canceled", terminal_at=row.get("terminal_at") or now)
                reason = "canceled"

            if fields:
                await tx.update(
                    payment_attempt_id=row["payment_attempt_id"], fields=fields, now=now
                )
        return {"portal": True, "handled": True, "reason": reason}


__all__ = [
    "CHARGE_EVENT_TYPES",
    "PAYMENT_APPLIED",
    "PortalPaymentReconciler",
    "_ApplyRejected",
    "_event_type",
]
