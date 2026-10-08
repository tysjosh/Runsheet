"""Portal ACH payments: create, replay and supersede (design §3.1, §6.1, §6.2).

* :func:`configure_portal_payments` registers the portal Stripe factory
  (``_portal_stripe_connector_factory`` in ``bootstrap/agents.py``: enabled
  instances only) and the commerce ``PaymentService``. :func:`portal_connector`
  answers "may the portal take payments" for ``/me``, payment create, replay
  ``retrieve`` and ``GET /payment-attempts/{id}``.
* :class:`PortalPaymentAttemptStore` owns ``portal_payment_attempts``. Every
  customer-facing query filters on ``tenant_id`` AND ``customer_id``.
* :class:`PortalPaymentService` implements ``POST
  /api/portal/invoices/{invoice_id}/payments`` exactly as design §6.2 steps
  1-4 (FREEZE F2 advisory lock per invoice, F4 supersede and stale re-drive,
  DV3, DV8) and the attempt read.

The payment area is frozen: behavior here follows the design text; non-HIGH
edge cases are accepted or backlogged, not iterated on.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    Union,
)

from errors.codes import ErrorCode
from errors.exceptions import (
    AppException,
    idempotency_conflict,
    invoice_not_payable,
    missing_idempotency_key,
    payment_amount_invalid,
    payment_in_progress,
    payment_provider_error,
    portal_payments_unavailable,
    portal_unavailable,
    resource_not_found,
)

logger = logging.getLogger(__name__)

#: A ``creating`` row at least this old is treated as abandoned (F4).
PORTAL_CREATING_STALE_SECONDS = 120
#: Every portal Stripe call is bounded by ``asyncio.wait_for`` (§6.2).
STRIPE_TIMEOUT_SECONDS = 10.0
#: ``Retry-After`` on the ``creating`` replay (review M6).
CREATING_RETRY_AFTER_SECONDS = 2

IN_FLIGHT_STATUSES: Tuple[str, ...] = ("creating", "created", "pending")
PAYABLE_INVOICE_STATUSES: Tuple[str, ...] = ("open", "partial", "overdue")
MIN_PAYMENT_CENTS = 100
PORTAL_PAYMENT_SOURCE = "runsheet_portal"
_UNEXPECTED_STATE = "payment_intent_unexpected_state"

#: ``Idempotency-Key``: 8-255 chars of ``[A-Za-z0-9_-]`` (§6.2).
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{8,255}$")


# ---------------------------------------------------------------------------
# Registry (design §3.1)
# ---------------------------------------------------------------------------

#: ``async (tenant_id) -> connector | None``: an *enabled* Stripe connector.
PortalConnectorFactory = Callable[[str], Awaitable[Optional[Any]]]

_connector_factory: Optional[PortalConnectorFactory] = None
_payment_service: Optional[Any] = None
_portal_service: Optional["PortalPaymentService"] = None


def configure_portal_payments(
    connector_factory: Optional[PortalConnectorFactory] = None,
    payment_service: Optional[Any] = None,
) -> None:
    """Register the portal Stripe factory and the commerce payment service."""
    global _connector_factory, _payment_service
    _connector_factory = connector_factory
    _payment_service = payment_service


def get_commerce_payment_service() -> Optional[Any]:
    """The commerce ``PaymentService`` registered with the portal factory."""
    return _payment_service


def configure_portal_payment_service(service: Optional["PortalPaymentService"]) -> None:
    """Install a :class:`PortalPaymentService` (tests); ``None`` restores the default."""
    global _portal_service
    _portal_service = service


def get_configured_portal_payment_service() -> Optional["PortalPaymentService"]:
    return _portal_service


def get_portal_payment_service() -> "PortalPaymentService":
    """The installed service, else one over the Postgres attempt store."""
    return _portal_service if _portal_service is not None else PortalPaymentService()


async def portal_connector(tenant_id: str) -> Optional[Any]:
    """The tenant's enabled portal connector, or ``None``.

    ``None`` when no factory is configured, the factory finds no enabled
    instance, or the factory raises (logged at WARN).
    """
    if _connector_factory is None:
        return None
    try:
        return await _connector_factory(tenant_id)
    except Exception as exc:  # noqa: BLE001 — unavailable, never an error
        logger.warning(
            "Portal payment connector lookup failed for tenant=%s: %s",
            tenant_id,
            type(exc).__name__,
        )
        return None


# ---------------------------------------------------------------------------
# Storage (design §6.1)
# ---------------------------------------------------------------------------

ATTEMPT_COLUMNS: Tuple[str, ...] = (
    "payment_attempt_id",
    "tenant_id",
    "customer_id",
    "invoice_id",
    "account_id",
    "actor_user_id",
    "idempotency_key",
    "amount_cents",
    "status",
    "stripe_payment_intent_id",
    "payment_id",
    "failure_code",
    "created_at",
    "updated_at",
    "terminal_at",
)
_SELECT = ", ".join(ATTEMPT_COLUMNS)
#: Columns an UPDATE may set (``updated_at`` is always stamped).
UPDATABLE_COLUMNS = frozenset(
    {"status", "stripe_payment_intent_id", "payment_id", "failure_code", "terminal_at"}
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row(row: Any) -> Dict[str, Any]:
    return dict(zip(ATTEMPT_COLUMNS, tuple(row)))


class AttemptTx(Protocol):
    """One transaction over ``portal_payment_attempts``.

    Implementations: :class:`PostgresAttemptTx` and the in-memory fake in
    ``tests/portal/_payment_fakes.py``.
    """

    async def lock_invoice(self, *, tenant_id: str, invoice_id: str) -> None: ...

    async def find_by_key(
        self, *, tenant_id: str, customer_id: str, actor_user_id: str, idempotency_key: str
    ) -> Optional[Dict[str, Any]]: ...

    async def inflight_for_update(
        self, *, tenant_id: str, customer_id: str, invoice_id: str
    ) -> List[Dict[str, Any]]: ...

    async def insert(self, row: Mapping[str, Any]) -> None: ...

    async def update(
        self,
        *,
        payment_attempt_id: str,
        fields: Mapping[str, Any],
        now: datetime,
        expected_status: Optional[str] = None,
    ) -> int: ...

    async def get(
        self, *, tenant_id: str, customer_id: str, payment_attempt_id: str
    ) -> Optional[Dict[str, Any]]: ...

    async def newest_for_invoice(
        self, *, tenant_id: str, customer_id: str, invoice_id: str
    ) -> Optional[Dict[str, Any]]: ...

    async def lock_for_webhook(
        self, *, tenant_id: str, payment_attempt_id: str
    ) -> Optional[Dict[str, Any]]: ...

    async def find_by_payment_intent(
        self, *, tenant_id: str, stripe_payment_intent_id: str
    ) -> Optional[Dict[str, Any]]: ...


class PostgresAttemptTx:
    """:class:`AttemptTx` bound to one ``AsyncSession`` (the caller commits)."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def _exec(self, sql: str, params: Mapping[str, Any]) -> Any:
        from sqlalchemy import text

        return await self._session.execute(text(sql), dict(params))

    async def _one(self, sql: str, params: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        row = (await self._exec(sql, params)).first()
        return None if row is None else _row(row)

    async def lock_invoice(self, *, tenant_id: str, invoice_id: str) -> None:
        # FREEZE F2: payment creates for one invoice are serialized.
        await self._exec(
            "SELECT pg_advisory_xact_lock("
            "hashtext('portal-pay:' || :tenant_id || ':' || :invoice_id))",
            {"tenant_id": tenant_id, "invoice_id": invoice_id},
        )

    async def find_by_key(
        self, *, tenant_id: str, customer_id: str, actor_user_id: str, idempotency_key: str
    ) -> Optional[Dict[str, Any]]:
        return await self._one(
            f"SELECT {_SELECT} FROM portal_payment_attempts "
            "WHERE tenant_id = :tenant_id AND customer_id = :customer_id "
            "AND actor_user_id = :actor_user_id AND idempotency_key = :key",
            {
                "tenant_id": tenant_id,
                "customer_id": customer_id,
                "actor_user_id": actor_user_id,
                "key": idempotency_key,
            },
        )

    async def inflight_for_update(
        self, *, tenant_id: str, customer_id: str, invoice_id: str
    ) -> List[Dict[str, Any]]:
        rows = (
            await self._exec(
                f"SELECT {_SELECT} FROM portal_payment_attempts "
                "WHERE tenant_id = :tenant_id AND customer_id = :customer_id "
                "AND invoice_id = :invoice_id "
                "AND status IN ('creating', 'created', 'pending') FOR UPDATE",
                {"tenant_id": tenant_id, "customer_id": customer_id, "invoice_id": invoice_id},
            )
        ).all()
        return [_row(r) for r in rows]

    async def insert(self, row: Mapping[str, Any]) -> None:
        from sqlalchemy.exc import IntegrityError

        cols = [c for c in ATTEMPT_COLUMNS if c in row]
        try:
            await self._exec(
                f"INSERT INTO portal_payment_attempts ({', '.join(cols)}) "
                f"VALUES ({', '.join(':' + c for c in cols)})",
                {c: row[c] for c in cols},
            )
        except IntegrityError as exc:
            # Backstops only: the advisory lock and step 1b answer these
            # cases first (same user + key on two invoices at once).
            if "uq_ppa_inflight" in str(exc):
                raise payment_in_progress() from None
            raise idempotency_conflict(
                "This payment reference was already used for another request."
            ) from None

    async def update(
        self,
        *,
        payment_attempt_id: str,
        fields: Mapping[str, Any],
        now: datetime,
        expected_status: Optional[str] = None,
    ) -> int:
        unknown = set(fields) - UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        sets = [f"{name} = :{name}" for name in fields] + ["updated_at = :updated_at"]
        params: Dict[str, Any] = {**fields, "updated_at": now, "id": payment_attempt_id}
        where = "payment_attempt_id = :id"
        if expected_status is not None:
            where += " AND status = :expected_status"
            params["expected_status"] = expected_status
        result = await self._exec(
            f"UPDATE portal_payment_attempts SET {', '.join(sets)} WHERE {where}", params
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def get(
        self, *, tenant_id: str, customer_id: str, payment_attempt_id: str
    ) -> Optional[Dict[str, Any]]:
        return await self._one(
            f"SELECT {_SELECT} FROM portal_payment_attempts "
            "WHERE tenant_id = :tenant_id AND customer_id = :customer_id "
            "AND payment_attempt_id = :id",
            {"tenant_id": tenant_id, "customer_id": customer_id, "id": payment_attempt_id},
        )

    async def newest_for_invoice(
        self, *, tenant_id: str, customer_id: str, invoice_id: str
    ) -> Optional[Dict[str, Any]]:
        return await self._one(
            f"SELECT {_SELECT} FROM portal_payment_attempts "
            "WHERE tenant_id = :tenant_id AND customer_id = :customer_id "
            "AND invoice_id = :invoice_id "
            "ORDER BY created_at DESC, payment_attempt_id DESC LIMIT 1",
            {"tenant_id": tenant_id, "customer_id": customer_id, "invoice_id": invoice_id},
        )

    async def lock_for_webhook(
        self, *, tenant_id: str, payment_attempt_id: str
    ) -> Optional[Dict[str, Any]]:
        # FREEZE F2: deliveries for one attempt are serialized by the row lock.
        return await self._one(
            f"SELECT {_SELECT} FROM portal_payment_attempts "
            "WHERE tenant_id = :tenant_id AND payment_attempt_id = :id FOR UPDATE",
            {"tenant_id": tenant_id, "id": payment_attempt_id},
        )

    async def find_by_payment_intent(
        self, *, tenant_id: str, stripe_payment_intent_id: str
    ) -> Optional[Dict[str, Any]]:
        return await self._one(
            f"SELECT {_SELECT} FROM portal_payment_attempts "
            "WHERE tenant_id = :tenant_id AND stripe_payment_intent_id = :pi",
            {"tenant_id": tenant_id, "pi": stripe_payment_intent_id},
        )


@asynccontextmanager
async def default_attempt_tx() -> AsyncIterator[AttemptTx]:
    """One ``session_scope`` transaction: commit on clean exit, else roll back."""
    from persistence.database import is_persistence_enabled, session_scope

    if not is_persistence_enabled():
        raise portal_unavailable()
    async with session_scope() as session:
        yield PostgresAttemptTx(session)


AttemptTxFactory = Callable[[], Any]  # () -> async context manager of AttemptTx


def payment_attempt_not_found(payment_attempt_id: str) -> AppException:
    """Missing, out of scope, and malformed ids all answer this (§3.2)."""
    return resource_not_found(
        f"Payment attempt '{payment_attempt_id}' not found",
        details={"payment_attempt_id": payment_attempt_id},
    )


class PortalPaymentAttemptStore:
    """``portal_payment_attempts`` access. Customer-facing reads always carry
    both ``tenant_id`` and ``customer_id`` (ISO-T-4)."""

    def __init__(self, tx_factory: Optional[AttemptTxFactory] = None) -> None:
        self._tx_factory = tx_factory

    def available(self) -> bool:
        """``False`` only for the default store while persistence is dormant."""
        if self._tx_factory is not None:
            return True
        from persistence.database import is_persistence_enabled

        return is_persistence_enabled()

    def transaction(self):
        """``async with store.transaction() as tx`` — one transaction."""
        return (self._tx_factory or default_attempt_tx)()

    async def get(self, scope: Any, payment_attempt_id: str) -> Dict[str, Any]:
        """The scope's attempt, else 404 (missing or another customer's)."""
        async with self.transaction() as tx:
            row = await tx.get(
                tenant_id=scope.tenant_id,
                customer_id=scope.customer_id,
                payment_attempt_id=payment_attempt_id,
            )
        if row is None:
            raise payment_attempt_not_found(payment_attempt_id)
        return row

    async def newest_for_invoice(self, scope: Any, invoice_id: str) -> Optional[Dict[str, Any]]:
        async with self.transaction() as tx:
            return await tx.newest_for_invoice(
                tenant_id=scope.tenant_id, customer_id=scope.customer_id, invoice_id=invoice_id
            )


# ---------------------------------------------------------------------------
# Service (design §6.2)
# ---------------------------------------------------------------------------


def validate_idempotency_key(value: Optional[str]) -> str:
    """The ``Idempotency-Key`` header, else 400 ``MISSING_IDEMPOTENCY_KEY``."""
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY.match(value):
        raise missing_idempotency_key(
            "An Idempotency-Key header of 8-255 letters, digits, '_' or '-' is required."
        )
    return value


def _invoice_number(invoice: Mapping[str, Any]) -> str:
    return str(invoice.get("invoice_number") or invoice.get("invoice_id") or "")


def _portal_intent_body(attempt: Mapping[str, Any], invoice_number: str) -> Dict[str, Any]:
    """The ONLY PaymentIntent body builder (§6.2 step 4).

    A stale re-drive reuses the attempt's Stripe idempotency key, and Stripe
    rejects a reused key with a different body, so create and re-drive must
    build byte-identical bodies from the same row and invoice number.
    """
    return {
        "amount_cents": int(attempt["amount_cents"]),
        "metadata": {
            "source": PORTAL_PAYMENT_SOURCE,
            "tenant_id": str(attempt["tenant_id"]),
            "customer_id": str(attempt["customer_id"]),
            "invoice_id": str(attempt["invoice_id"]),
            "payment_attempt_id": str(attempt["payment_attempt_id"]),
        },
        "description": f"Invoice {invoice_number}",
    }


def stripe_idempotency_key(attempt: Mapping[str, Any]) -> str:
    return f"portal_pa_{attempt['payment_attempt_id']}"


def _is_unexpected_state(exc: BaseException) -> bool:
    """``stripe.error.InvalidRequestError(code="payment_intent_unexpected_state")``.

    Matched on ``code`` so the stripe package isn't imported here.
    """
    return getattr(exc, "code", None) == _UNEXPECTED_STATE


def _aware(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            return _aware(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


@dataclass
class PaymentCreateResult:
    """What the create endpoint answers (201 new, 200 replay)."""

    status_code: int
    attempt: Dict[str, Any]
    client_secret: Optional[str] = None
    publishable_key: Optional[str] = None
    retry_after: Optional[int] = None


@dataclass
class PaymentAttemptView:
    attempt: Dict[str, Any]
    client_secret: Optional[str] = None
    publishable_key: Optional[str] = None


@dataclass
class _Deferred:
    """An error answered only after Transaction A commits its writes."""

    error: AppException


_TxAOutcome = Union[PaymentCreateResult, _Deferred, Tuple[Dict[str, Any], str]]


@dataclass
class PortalPaymentService:
    """``create`` (design §6.2) and the attempt read."""

    store: PortalPaymentAttemptStore = field(default_factory=PortalPaymentAttemptStore)
    #: ``PortalInvoiceReader``; ``None`` resolves the configured one per call.
    invoice_reader: Any = None
    #: ``async (tenant_id) -> connector | None``; ``None`` uses :func:`portal_connector`.
    connector_factory: Optional[PortalConnectorFactory] = None
    clock: Callable[[], datetime] = _utcnow
    stripe_timeout_seconds: float = STRIPE_TIMEOUT_SECONDS

    # -- collaborators -------------------------------------------------

    async def _connector(self, tenant_id: str) -> Optional[Any]:
        if self.connector_factory is not None:
            return await self.connector_factory(tenant_id)
        return await portal_connector(tenant_id)

    def _reader(self) -> Any:
        if self.invoice_reader is not None:
            return self.invoice_reader
        from portal.services.portal_invoice_service import get_configured_invoice_service

        service = get_configured_invoice_service()
        if service is None:
            raise AppException(ErrorCode.INVOICING_DISABLED, "Invoices are not available")
        return service.reader

    async def _bounded(self, awaitable: Awaitable[Any]) -> Any:
        return await asyncio.wait_for(awaitable, self.stripe_timeout_seconds)

    def _is_stale(self, row: Mapping[str, Any], now: datetime) -> bool:
        created = _aware(row.get("created_at"))
        if created is None:
            return False
        return (now - created).total_seconds() >= PORTAL_CREATING_STALE_SECONDS

    @staticmethod
    def _provider_error_log(*attempt_ids: Optional[str], stage: str) -> None:
        # No Stripe message, key or client secret is ever logged.
        logger.error(
            "portal_payment_provider_error",
            extra={
                "extra_data": {
                    "payment_attempt_ids": [a for a in attempt_ids if a],
                    "stage": stage,
                }
            },
        )

    async def _create_intent(self, connector: Any, attempt: Mapping[str, Any], invoice_number: str):
        body = _portal_intent_body(attempt, invoice_number)
        return await self._bounded(
            connector.create_portal_ach_intent(
                body["amount_cents"],
                idempotency_key=stripe_idempotency_key(attempt),
                metadata=body["metadata"],
                description=body["description"],
            )
        )

    async def _secret_for(self, connector: Any, row: Mapping[str, Any]) -> Tuple[str, str]:
        """``(client_secret, publishable_key)`` for a ``created`` row; 502 on error."""
        try:
            intent = await self._bounded(
                connector.retrieve_intent(row["stripe_payment_intent_id"])
            )
            secret = (intent or {}).get("client_secret")
            if not secret:
                raise RuntimeError("retrieve returned no client_secret")
            publishable_key = await connector.get_publishable_key()
        except Exception:  # noqa: BLE001 — never surface Stripe text
            self._provider_error_log(row.get("payment_attempt_id"), stage="retrieve")
            raise payment_provider_error() from None
        return secret, publishable_key

    # -- replay rules (step 2, step 1b) --------------------------------

    @staticmethod
    def _check_same_request(
        row: Mapping[str, Any], invoice_id: str, amount_cents: Optional[int]
    ) -> None:
        # An omitted amount defaults to remaining_cents, which is only known
        # after the invoice read; a replay without an amount matches any.
        if row["invoice_id"] != invoice_id or (
            amount_cents is not None and int(row["amount_cents"]) != amount_cents
        ):
            raise idempotency_conflict(
                "This payment reference was already used for a different payment."
            )

    async def _replay(
        self, connector: Any, row: Dict[str, Any], invoice_id: str, amount_cents: Optional[int]
    ) -> PaymentCreateResult:
        self._check_same_request(row, invoice_id, amount_cents)
        status = row["status"]
        if status == "creating":
            return PaymentCreateResult(200, row, retry_after=CREATING_RETRY_AFTER_SECONDS)
        if status == "created" and row.get("stripe_payment_intent_id"):
            secret, publishable_key = await self._secret_for(connector, row)
            return PaymentCreateResult(200, row, client_secret=secret, publishable_key=publishable_key)
        return PaymentCreateResult(200, row)

    async def _redrive_same_key(
        self, tx: AttemptTx, connector: Any, row: Dict[str, Any], invoice_number: str, now: datetime
    ) -> Union[PaymentCreateResult, _Deferred]:
        """Step 1b: a same-key row stuck in ``creating`` past the threshold is
        re-driven with its own Stripe key, then ``created`` or ``failed``."""
        try:
            publishable_key = await connector.get_publishable_key()
            intent = await self._create_intent(connector, row, invoice_number)
            pi_id, secret = intent["id"], intent["client_secret"]
            if not pi_id or not secret:
                raise RuntimeError("create returned no id/client_secret")
        except Exception:  # noqa: BLE001
            await tx.update(
                payment_attempt_id=row["payment_attempt_id"],
                fields={"status": "failed", "failure_code": "provider_error", "terminal_at": now},
                now=now,
                expected_status="creating",
            )
            self._provider_error_log(row["payment_attempt_id"], stage="redrive")
            return _Deferred(payment_provider_error())
        moved = await tx.update(
            payment_attempt_id=row["payment_attempt_id"],
            fields={"status": "created", "stripe_payment_intent_id": pi_id},
            now=now,
            expected_status="creating",
        )
        if moved:
            updated = {**row, "status": "created", "stripe_payment_intent_id": pi_id, "updated_at": now}
            return PaymentCreateResult(200, updated, client_secret=secret, publishable_key=publishable_key)
        current = await tx.get(
            tenant_id=row["tenant_id"],
            customer_id=row["customer_id"],
            payment_attempt_id=row["payment_attempt_id"],
        ) or row
        return PaymentCreateResult(
            200,
            current,
            client_secret=secret if current.get("status") == "created" else None,
            publishable_key=publishable_key if current.get("status") == "created" else None,
        )

    # -- supersede (step 3.5, F4) --------------------------------------

    async def _supersede(
        self,
        tx: AttemptTx,
        connector: Any,
        old: Dict[str, Any],
        invoice_number: str,
        new_attempt_id: str,
        now: datetime,
    ) -> Optional[_Deferred]:
        """Cancel ``old`` at Stripe so a new attempt may start.

        Returns ``None`` to continue, ``_Deferred(409)`` when money is already
        moving (commit, then answer), or raises 502 to roll back Transaction A.
        """
        old_id = old["payment_attempt_id"]
        pi_id = old.get("stripe_payment_intent_id")
        if old["status"] == "creating" and not pi_id:
            # Re-drive with the old Stripe key to learn the intent id.
            try:
                intent = await self._create_intent(connector, old, invoice_number)
                pi_id = intent["id"]
                if not pi_id:
                    raise RuntimeError("create returned no id")
            except Exception:  # noqa: BLE001
                # No client secret was ever issued: it can't be confirmed.
                await tx.update(
                    payment_attempt_id=old_id,
                    fields={"status": "failed", "failure_code": "provider_error", "terminal_at": now},
                    now=now,
                    expected_status="creating",
                )
                logger.warning(
                    "portal_payment_redrive_failed",
                    extra={"extra_data": {"payment_attempt_ids": [old_id, new_attempt_id]}},
                )
                return None

        try:
            await self._bounded(connector.cancel_intent(pi_id))
        except Exception as exc:  # noqa: BLE001
            if _is_unexpected_state(exc):
                try:
                    intent = await self._bounded(connector.retrieve_intent(pi_id))
                    intent_status = (intent or {}).get("status")
                except Exception:  # noqa: BLE001 — treated as any other error
                    intent_status = None
                if intent_status in ("processing", "succeeded"):
                    await tx.update(
                        payment_attempt_id=old_id,
                        fields={"status": "pending", "stripe_payment_intent_id": pi_id},
                        now=now,
                        expected_status=old["status"],
                    )
                    return _Deferred(payment_in_progress())
            self._provider_error_log(old_id, new_attempt_id, stage="cancel")
            raise payment_provider_error() from None

        await tx.update(
            payment_attempt_id=old_id,
            fields={"status": "canceled", "stripe_payment_intent_id": pi_id, "terminal_at": now},
            now=now,
            expected_status=old["status"],
        )
        return None

    # -- Transaction A (step 3, F2) ------------------------------------

    async def _transaction_a(
        self,
        scope: Any,
        connector: Any,
        invoice_id: str,
        idempotency_key: str,
        amount_cents: Optional[int],
    ) -> _TxAOutcome:
        new_attempt_id = f"ppa_{uuid.uuid4()}"
        async with self.store.transaction() as tx:
            await tx.lock_invoice(tenant_id=scope.tenant_id, invoice_id=invoice_id)
            now = self.clock()

            # 1b. Same-key re-check under the lock.
            existing = await tx.find_by_key(
                tenant_id=scope.tenant_id,
                customer_id=scope.customer_id,
                actor_user_id=scope.user_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                if existing["status"] == "creating" and self._is_stale(existing, now):
                    self._check_same_request(existing, invoice_id, amount_cents)
                    invoice = await self._reader().get(scope, existing["invoice_id"])
                    return await self._redrive_same_key(
                        tx, connector, existing, _invoice_number(invoice), now
                    )
                return await self._replay(connector, existing, invoice_id, amount_cents)

            # 2-4. The invoice, read fresh (drafts and other customers: 404, DV8).
            invoice = await self._reader().get(scope, invoice_id)
            if invoice.get("status") not in PAYABLE_INVOICE_STATUSES:
                raise invoice_not_payable()
            remaining = int(invoice.get("remaining_cents") or 0)
            amount = remaining if amount_cents is None else int(amount_cents)
            if amount < MIN_PAYMENT_CENTS or amount > remaining:
                raise payment_amount_invalid(details={"max_cents": remaining})
            invoice_number = _invoice_number(invoice)

            # 5. In-flight attempts for the invoice.
            for row in await tx.inflight_for_update(
                tenant_id=scope.tenant_id, customer_id=scope.customer_id, invoice_id=invoice_id
            ):
                if row["status"] == "pending":
                    raise payment_in_progress()
                if row["status"] == "creating" and not self._is_stale(row, now):
                    raise payment_in_progress()
                deferred = await self._supersede(
                    tx, connector, row, invoice_number, new_attempt_id, now
                )
                if deferred is not None:
                    return deferred

            # 6. The new attempt.
            attempt = {
                "payment_attempt_id": new_attempt_id,
                "tenant_id": scope.tenant_id,
                "customer_id": scope.customer_id,
                "invoice_id": invoice_id,
                "account_id": str(invoice.get("account_id") or ""),
                "actor_user_id": scope.user_id,
                "idempotency_key": idempotency_key,
                "amount_cents": amount,
                "status": "creating",
                "stripe_payment_intent_id": None,
                "payment_id": None,
                "failure_code": None,
                "created_at": now,
                "updated_at": now,
                "terminal_at": None,
            }
            await tx.insert(attempt)
        return attempt, invoice_number

    # -- public --------------------------------------------------------

    async def create(
        self,
        scope: Any,
        invoice_id: str,
        *,
        idempotency_key: str,
        amount_cents: Optional[int],
    ) -> PaymentCreateResult:
        """Start (or replay) an ACH payment for one invoice (§6.2 steps 1-4)."""
        # 1. Gates (the invoicing 404 is the route dependency).
        connector = await self._connector(scope.tenant_id)
        if connector is None:
            raise portal_payments_unavailable()

        # 2. Replay check in its own short transaction.
        async with self.store.transaction() as tx:
            existing = await tx.find_by_key(
                tenant_id=scope.tenant_id,
                customer_id=scope.customer_id,
                actor_user_id=scope.user_id,
                idempotency_key=idempotency_key,
            )
        if existing is not None and not (
            existing["status"] == "creating" and self._is_stale(existing, self.clock())
        ):
            return await self._replay(connector, existing, invoice_id, amount_cents)
        # A stale ``creating`` same-key row is re-driven by step 1b, under the
        # lock, so the client isn't left polling a row that never moves.

        # 3. Transaction A.
        outcome = await self._transaction_a(
            scope, connector, invoice_id, idempotency_key, amount_cents
        )
        if isinstance(outcome, PaymentCreateResult):
            return outcome
        if isinstance(outcome, _Deferred):
            raise outcome.error
        attempt, invoice_number = outcome

        # 4. Stripe, bounded; both post-Stripe writes are conditional.
        attempt_id = attempt["payment_attempt_id"]
        try:
            publishable_key = await connector.get_publishable_key()
            intent = await self._create_intent(connector, attempt, invoice_number)
            pi_id, secret = intent["id"], intent["client_secret"]
            if not pi_id or not secret:
                raise RuntimeError("create returned no id/client_secret")
        except Exception:  # noqa: BLE001 — never surface Stripe text
            now = self.clock()
            async with self.store.transaction() as tx:
                await tx.update(
                    payment_attempt_id=attempt_id,
                    fields={"status": "failed", "failure_code": "provider_error", "terminal_at": now},
                    now=now,
                    expected_status="creating",
                )
            self._provider_error_log(attempt_id, stage="create")
            raise payment_provider_error() from None

        now = self.clock()
        async with self.store.transaction() as tx:
            moved = await tx.update(
                payment_attempt_id=attempt_id,
                fields={"status": "created", "stripe_payment_intent_id": pi_id},
                now=now,
                expected_status="creating",
            )
            current = None
            if not moved:
                # A webhook already moved it on: answer its current state.
                current = await tx.get(
                    tenant_id=scope.tenant_id,
                    customer_id=scope.customer_id,
                    payment_attempt_id=attempt_id,
                )
        if moved:
            created = {**attempt, "status": "created", "stripe_payment_intent_id": pi_id, "updated_at": now}
            return PaymentCreateResult(201, created, client_secret=secret, publishable_key=publishable_key)
        current = current or attempt
        is_created = current.get("status") == "created"
        return PaymentCreateResult(
            201,
            current,
            client_secret=secret if is_created else None,
            publishable_key=publishable_key if is_created else None,
        )

    async def get_attempt(
        self, scope: Any, payment_attempt_id: str, *, include_client_secret: bool = False
    ) -> PaymentAttemptView:
        """One of the customer's attempts. The client secret only for its
        own actor, only while ``created``, and only when asked (§6.2)."""
        row = await self.store.get(scope, payment_attempt_id)
        if not include_client_secret:
            return PaymentAttemptView(row)
        if row["actor_user_id"] != scope.user_id:
            raise payment_attempt_not_found(payment_attempt_id)
        if row["status"] != "created" or not row.get("stripe_payment_intent_id"):
            return PaymentAttemptView(row)
        connector = await self._connector(scope.tenant_id)
        if connector is None:
            return PaymentAttemptView(row)
        secret, publishable_key = await self._secret_for(connector, row)
        return PaymentAttemptView(row, client_secret=secret, publishable_key=publishable_key)


async def latest_payment_attempt(scope: Any, invoice_id: str) -> Optional[Dict[str, Any]]:
    """The newest attempt row for one of the scope's invoices (§5), or ``None``.

    ``None`` when persistence is dormant; a store error is logged at WARN and
    also reads as ``None`` (display only: the create path re-checks).
    """
    store = get_portal_payment_service().store
    if not store.available():
        return None
    try:
        return await store.newest_for_invoice(scope, invoice_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "portal invoice: payment attempt read failed for tenant=%s: %s",
            scope.tenant_id,
            type(exc).__name__,
        )
        return None


__all__ = [
    "ATTEMPT_COLUMNS",
    "AttemptTx",
    "CREATING_RETRY_AFTER_SECONDS",
    "IN_FLIGHT_STATUSES",
    "PORTAL_CREATING_STALE_SECONDS",
    "PORTAL_PAYMENT_SOURCE",
    "PaymentAttemptView",
    "PaymentCreateResult",
    "PortalConnectorFactory",
    "PortalPaymentAttemptStore",
    "PortalPaymentService",
    "PostgresAttemptTx",
    "STRIPE_TIMEOUT_SECONDS",
    "UPDATABLE_COLUMNS",
    "_portal_intent_body",
    "configure_portal_payment_service",
    "configure_portal_payments",
    "default_attempt_tx",
    "get_commerce_payment_service",
    "get_configured_portal_payment_service",
    "get_portal_payment_service",
    "latest_payment_attempt",
    "payment_attempt_not_found",
    "portal_connector",
    "stripe_idempotency_key",
    "validate_idempotency_key",
]
