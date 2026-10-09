"""In-memory fakes for portal payments (FEAT-005).

* :class:`FakeAttemptDB` — ``portal_payment_attempts`` with transactions
  (undo log on rollback), the three unique constraints, the per-invoice
  advisory lock and per-row ``FOR UPDATE`` locks (asyncio locks), and fault
  injection. ``store()`` gives a :class:`PortalPaymentAttemptStore` over it.
* :class:`FakePortalConnector` — the portal half of ``StripeConnector``
  (``create_portal_ach_intent``/``cancel_intent``/``retrieve_intent``/
  ``get_publishable_key``) with Stripe's idempotency-key semantics, plus the
  webhook half (``verify_webhook_signature`` parses the JSON body,
  ``handle_webhook_event`` records the event).
* :class:`FakePaymentIdempotency` — the commerce ``IdempotencyService`` marker.

Importable from ``tests/postgres`` too (it imports nothing from conftest).
"""
from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from portal.services.portal_payment_service import (
    IN_FLIGHT_STATUSES,
    UPDATABLE_COLUMNS,
    PortalPaymentAttemptStore,
)


class FakeIntegrityError(Exception):
    pass


class FakeAttemptDB:
    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.ops: List[Tuple[str, Dict[str, Any]]] = []
        self._locks: Dict[str, asyncio.Lock] = {}
        #: The next ``n`` ``update`` calls raise (a DB error).
        self.fail_updates = 0
        self.commits = 0
        self.rollbacks = 0

    # -- helpers for tests ---------------------------------------------

    def store(self) -> PortalPaymentAttemptStore:
        return PortalPaymentAttemptStore(tx_factory=self.transaction)

    def add(self, **row: Any) -> Dict[str, Any]:
        full = {
            "stripe_payment_intent_id": None, "payment_id": None, "failure_code": None,
            "terminal_at": None, "account_id": "QA-ACCT-A", "idempotency_key": f"seed-{len(self.rows)}-key",
            **row,
        }
        full.setdefault("updated_at", full.get("created_at"))
        self.rows[full["payment_attempt_id"]] = full
        return copy.deepcopy(full)

    def get(self, attempt_id: str) -> Optional[Dict[str, Any]]:
        row = self.rows.get(attempt_id)
        return copy.deepcopy(row) if row is not None else None

    def for_invoice(self, invoice_id: str) -> List[Dict[str, Any]]:
        return [copy.deepcopy(r) for r in self.rows.values() if r["invoice_id"] == invoice_id]

    def inflight(self, invoice_id: str) -> List[Dict[str, Any]]:
        return [r for r in self.for_invoice(invoice_id) if r["status"] in IN_FLIGHT_STATUSES]

    def op_names(self) -> List[str]:
        return [name for name, _ in self.ops]

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    @asynccontextmanager
    async def transaction(self):
        tx = FakeAttemptTx(self)
        try:
            yield tx
        except BaseException:
            tx._rollback()
            self.rollbacks += 1
            raise
        else:
            self.commits += 1
        finally:
            tx._release()


class FakeAttemptTx:
    """Writes apply at once; rollback replays the undo log in reverse."""

    def __init__(self, db: FakeAttemptDB) -> None:
        self._db = db
        self._undo: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        self._held: List[asyncio.Lock] = []

    async def _acquire(self, key: str) -> None:
        lock = self._db._lock(key)
        if lock in self._held:
            return
        await lock.acquire()
        self._held.append(lock)

    def _release(self) -> None:
        for lock in reversed(self._held):
            lock.release()
        self._held.clear()

    def _rollback(self) -> None:
        for attempt_id, before in reversed(self._undo):
            if before is None:
                self._db.rows.pop(attempt_id, None)
            else:
                self._db.rows[attempt_id] = before
        self._undo.clear()

    def _log(self, name: str, **kw: Any) -> None:
        self._db.ops.append((name, kw))

    @staticmethod
    def _copy(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return copy.deepcopy(row) if row is not None else None

    async def lock_invoice(self, *, tenant_id: str, invoice_id: str) -> None:
        self._log("lock_invoice", tenant_id=tenant_id, invoice_id=invoice_id)
        await self._acquire(f"portal-pay:{tenant_id}:{invoice_id}")

    async def find_by_key(self, *, tenant_id, customer_id, actor_user_id, idempotency_key):
        self._log("find_by_key", tenant_id=tenant_id, customer_id=customer_id)
        for row in self._db.rows.values():
            if (row["tenant_id"], row["customer_id"], row["actor_user_id"], row["idempotency_key"]) == (
                tenant_id, customer_id, actor_user_id, idempotency_key
            ):
                return self._copy(row)
        return None

    async def inflight_for_update(self, *, tenant_id, customer_id, invoice_id):
        self._log("inflight_for_update", tenant_id=tenant_id, customer_id=customer_id)
        out = []
        for row in list(self._db.rows.values()):
            if (row["tenant_id"], row["customer_id"], row["invoice_id"]) == (
                tenant_id, customer_id, invoice_id
            ) and row["status"] in IN_FLIGHT_STATUSES:
                await self._acquire(f"row:{row['payment_attempt_id']}")
                out.append(self._copy(self._db.rows[row["payment_attempt_id"]]))
        return out

    async def insert(self, row: Mapping[str, Any]) -> None:
        self._log("insert", payment_attempt_id=row["payment_attempt_id"])
        for other in self._db.rows.values():
            if (other["tenant_id"], other["actor_user_id"], other["idempotency_key"]) == (
                row["tenant_id"], row["actor_user_id"], row["idempotency_key"]
            ):
                raise FakeIntegrityError("uq_ppa_idem")
            if (
                row["status"] in IN_FLIGHT_STATUSES
                and other["status"] in IN_FLIGHT_STATUSES
                and (other["tenant_id"], other["invoice_id"]) == (row["tenant_id"], row["invoice_id"])
            ):
                raise FakeIntegrityError("uq_ppa_inflight")
        self._undo.append((row["payment_attempt_id"], None))
        self._db.rows[row["payment_attempt_id"]] = copy.deepcopy(dict(row))

    async def update(self, *, payment_attempt_id, fields, now: datetime, expected_status=None) -> int:
        self._log("update", payment_attempt_id=payment_attempt_id, fields=dict(fields),
                  expected_status=expected_status)
        if self._db.fail_updates > 0:
            self._db.fail_updates -= 1
            raise RuntimeError("injected attempt update failure")
        assert not (set(fields) - UPDATABLE_COLUMNS), fields
        row = self._db.rows.get(payment_attempt_id)
        if row is None or (expected_status is not None and row["status"] != expected_status):
            return 0
        new = {**row, **copy.deepcopy(dict(fields)), "updated_at": now}
        if new["status"] in IN_FLIGHT_STATUSES:
            for other in self._db.rows.values():
                if (
                    other is not row
                    and other["status"] in IN_FLIGHT_STATUSES
                    and (other["tenant_id"], other["invoice_id"]) == (new["tenant_id"], new["invoice_id"])
                ):
                    raise FakeIntegrityError("uq_ppa_inflight")
        pi = new.get("stripe_payment_intent_id")
        if pi and any(o is not row and o.get("stripe_payment_intent_id") == pi for o in self._db.rows.values()):
            raise FakeIntegrityError("uq_ppa_stripe_payment_intent")
        self._undo.append((payment_attempt_id, copy.deepcopy(row)))
        self._db.rows[payment_attempt_id] = new
        return 1

    async def get(self, *, tenant_id, customer_id, payment_attempt_id):
        self._log("get", tenant_id=tenant_id, customer_id=customer_id)
        row = self._db.rows.get(payment_attempt_id)
        if row is None or (row["tenant_id"], row["customer_id"]) != (tenant_id, customer_id):
            return None
        return self._copy(row)

    async def newest_for_invoice(self, *, tenant_id, customer_id, invoice_id):
        self._log("newest_for_invoice", tenant_id=tenant_id, customer_id=customer_id)
        rows = [
            r for r in self._db.rows.values()
            if (r["tenant_id"], r["customer_id"], r["invoice_id"]) == (tenant_id, customer_id, invoice_id)
        ]
        rows.sort(key=lambda r: (r["created_at"], r["payment_attempt_id"]), reverse=True)
        return self._copy(rows[0]) if rows else None

    async def lock_for_webhook(self, *, tenant_id, payment_attempt_id):
        self._log("lock_for_webhook", tenant_id=tenant_id, payment_attempt_id=payment_attempt_id)
        row = self._db.rows.get(payment_attempt_id)
        if row is None or row["tenant_id"] != tenant_id:
            return None
        await self._acquire(f"row:{payment_attempt_id}")
        return self._copy(self._db.rows.get(payment_attempt_id))

    async def find_by_payment_intent(self, *, tenant_id, stripe_payment_intent_id):
        self._log("find_by_payment_intent", tenant_id=tenant_id)
        for row in self._db.rows.values():
            if row["tenant_id"] == tenant_id and row.get("stripe_payment_intent_id") == stripe_payment_intent_id:
                return self._copy(row)
        return None


class InvalidRequestError(Exception):
    """Shaped like ``stripe.error.InvalidRequestError`` (``code`` attribute)."""

    def __init__(self, message: str = "invalid request", code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


#: Text a provider error carries; it must never reach a response body.
STRIPE_SECRET_TEXT = "sk_live_LEAKED No such payment_intent: pi_internal_detail"


class FakePortalConnector:
    """The portal Stripe connector, with Stripe idempotency-key semantics."""

    publishable_key = "pk_test_portal_fake"

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Any]] = []
        self.intents: Dict[str, Dict[str, Any]] = {}
        self._by_key: Dict[str, Tuple[Dict[str, Any], str]] = {}
        self.create_error: Optional[BaseException] = None
        self.create_errors: List[BaseException] = []
        self.create_delay = 0.0
        #: ``async (call_index) -> None`` run inside create (before returning).
        self.during_create: Optional[Callable[[int], Awaitable[None]]] = None
        self.cancel_error: Optional[BaseException] = None
        self.cancel_delay = 0.0
        self.retrieve_error: Optional[BaseException] = None
        self.retrieve_status: Dict[str, str] = {}
        self.webhook_events: List[Dict[str, Any]] = []

    # -- portal --------------------------------------------------------

    def creates(self) -> List[Dict[str, Any]]:
        return [args for name, args in self.calls if name == "create"]

    def names(self) -> List[str]:
        return [name for name, _ in self.calls]

    async def create_portal_ach_intent(self, amount_cents, *, idempotency_key, metadata, description):
        body = {"amount_cents": amount_cents, "metadata": dict(metadata), "description": description}
        index = len(self.creates())
        self.calls.append(("create", {**body, "idempotency_key": idempotency_key,
                                      "body_json": json.dumps(body, sort_keys=True)}))
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        if self.during_create is not None:
            await self.during_create(index)
        if self.create_errors:
            raise self.create_errors.pop(0)
        if self.create_error is not None:
            raise self.create_error
        if idempotency_key in self._by_key:
            intent, body_json = self._by_key[idempotency_key]
            if body_json != json.dumps(body, sort_keys=True):
                raise InvalidRequestError("Keys for idempotent requests can only be used with the same parameters",
                                          code="idempotency_key_in_use")
            return dict(intent)
        pi_id = f"pi_fake_{len(self.intents) + 1}"
        intent = {"id": pi_id, "client_secret": f"{pi_id}_secret_fake", "status": "requires_payment_method"}
        self.intents[pi_id] = intent
        self._by_key[idempotency_key] = (intent, json.dumps(body, sort_keys=True))
        return dict(intent)

    async def cancel_intent(self, payment_intent_id):
        self.calls.append(("cancel", payment_intent_id))
        if self.cancel_delay:
            await asyncio.sleep(self.cancel_delay)
        if self.cancel_error is not None:
            raise self.cancel_error
        intent = self.intents.setdefault(payment_intent_id, {"id": payment_intent_id})
        intent["status"] = "canceled"
        return {"id": payment_intent_id, "status": "canceled"}

    async def retrieve_intent(self, payment_intent_id):
        self.calls.append(("retrieve", payment_intent_id))
        if self.retrieve_error is not None:
            raise self.retrieve_error
        intent = dict(self.intents.get(payment_intent_id) or {"id": payment_intent_id,
                                                               "client_secret": f"{payment_intent_id}_secret_fake"})
        if payment_intent_id in self.retrieve_status:
            intent["status"] = self.retrieve_status[payment_intent_id]
        return intent

    async def get_publishable_key(self):
        return self.publishable_key

    # -- webhook -------------------------------------------------------

    async def verify_webhook_signature(self, payload_bytes, signature_header):
        return json.loads(payload_bytes.decode("utf-8"))

    async def handle_webhook_event(self, event):
        self.webhook_events.append(event)
        return {"event_type": event.get("type"), "handled": False, "reason": "missing_reconciliation_id"}


class FakePaymentIdempotency:
    """``IdempotencyService`` marker store used by ``PaymentService.ingest``."""

    def __init__(self) -> None:
        self.keys: set = set()

    async def is_duplicate(self, key, tenant_id):
        return (tenant_id, key) in self.keys

    async def mark_processed(self, key, tenant_id):
        self.keys.add((tenant_id, key))

    def clear(self) -> None:
        self.keys.clear()


def intent_event(
    etype: str,
    attempt: Mapping[str, Any],
    *,
    pi_id: Optional[str] = None,
    amount: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
    **obj_fields: Any,
) -> Dict[str, Any]:
    """A verified ``payment_intent.*`` event for ``attempt`` (a plain dict)."""
    metadata = {
        "source": "runsheet_portal",
        "tenant_id": attempt["tenant_id"],
        "customer_id": attempt["customer_id"],
        "invoice_id": attempt["invoice_id"],
        "payment_attempt_id": attempt["payment_attempt_id"],
    }
    metadata.update(meta or {})
    obj = {
        "id": pi_id or attempt.get("stripe_payment_intent_id"),
        "object": "payment_intent",
        "amount": amount if amount is not None else attempt["amount_cents"],
        "amount_received": amount if amount is not None else attempt["amount_cents"],
        "metadata": metadata,
        **obj_fields,
    }
    return {"id": f"evt_{etype}_{obj['id']}", "type": etype, "data": {"object": obj}}


__all__ = [
    "FakeAttemptDB",
    "FakeIntegrityError",
    "FakePaymentIdempotency",
    "FakePortalConnector",
    "InvalidRequestError",
    "STRIPE_SECRET_TEXT",
    "intent_event",
]
