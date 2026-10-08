"""Customer-portal schema invariants on real Postgres (design §1.3; PRV-2).

Needs the migrated schema (``alembic upgrade head``), which the CI
migration-check job applies before running this directory. Every row uses a
unique tenant id and email and is deleted afterwards.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

_INSERT = text(
    "INSERT INTO auth_users (email, tenant_id, roles, has_pii_access, driver_id, customer_id) "
    "VALUES (:email, :tenant_id, CAST(:roles AS text[]), :pii, :driver_id, :customer_id)"
)


def _params(tenant_id, *, roles, customer_id=None, driver_id=None, pii=False):
    return {
        "email": f"pytest-portal-{uuid.uuid4().hex[:12]}@example.test",
        "tenant_id": tenant_id,
        "roles": "{" + ",".join(roles) + "}",
        "pii": pii,
        "driver_id": driver_id,
        "customer_id": customer_id,
    }


@pytest.mark.parametrize(
    "case",
    [
        {"roles": ["customer", "admin"], "customer_id": "QA-C1"},
        {"roles": ["customer"], "customer_id": "QA-C1", "driver_id": "QA-D1"},
        {"roles": ["dispatcher"], "customer_id": "QA-C1"},
        {"roles": ["customer"]},
        {"roles": ["customer"], "customer_id": "QA-C1", "pii": True},
    ],
    ids=["customer_plus_admin", "customer_with_driver", "binding_on_staff", "customer_unbound", "customer_with_pii"],
)
async def test_customer_binding_check(pg_engine, case):
    tenant_id = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    try:
        with pytest.raises(IntegrityError) as exc:
            async with pg_engine.begin() as conn:
                await conn.execute(_INSERT, _params(tenant_id, **case))
        assert "ck_auth_users_customer_binding" in str(exc.value)

        # The two valid shapes still insert.
        async with pg_engine.begin() as conn:
            await conn.execute(_INSERT, _params(tenant_id, roles=["customer"], customer_id="QA-C1"))
            await conn.execute(_INSERT, _params(tenant_id, roles=["dispatcher"]))
    finally:
        async with pg_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM auth_users WHERE tenant_id = :t"), {"t": tenant_id}
            )


# ---------------------------------------------------------------------------
# PortalAccessService on real Postgres (FEAT-002: F1 cap, ISO-T-4 grants list)
# ---------------------------------------------------------------------------


class _FakeST:
    """Minimal SuperTokens fake for the provisioner and the SDK seams."""

    def __init__(self):
        self.users = {}

    async def get_user_id_by_email(self, email):
        return self.users.get(email.casefold())

    async def create_user(self, email):
        import asyncio

        await asyncio.sleep(0.01)  # widen any race window
        uid = f"st-{uuid.uuid4().hex[:12]}"
        self.users[email.casefold()] = uid
        return uid

    async def set_user_roles(self, uid, roles):
        return None

    async def set_user_metadata(self, uid, metadata):
        return None

    async def noop(self, *args, **kwargs):
        return None

    async def mint(self, email, *, tenant_id):
        return None


class _Customers:
    def __init__(self, tenants):
        self._tenants = set(tenants)

    async def get(self, tenant_id, customer_id):
        from errors.exceptions import resource_not_found

        if tenant_id not in self._tenants:
            raise resource_not_found("Customer not found")
        return {"customer_id": customer_id, "tenant_id": tenant_id, "status": "active"}


def _access_service(pg_sessionmaker, tenants):
    from contextlib import asynccontextmanager

    from portal.services.portal_access_service import (
        PortalAccessService,
        PostgresPortalAccessUnitOfWork,
    )

    @asynccontextmanager
    async def uow():
        async with pg_sessionmaker() as session:
            async with session.begin():
                yield PostgresPortalAccessUnitOfWork(session)

    st = _FakeST()
    return PortalAccessService(
        customer_service=_Customers(tenants),
        uow_factory=uow,
        supertokens_admin=st,
        session_revoker=st.noop,
        user_deleter=st.noop,
        role_creator=st.noop,
        link_minter=st.mint,
        reset_emailer=st.noop,
    )


async def _cleanup(pg_engine, *tenants):
    async with pg_engine.begin() as conn:
        for t in tenants:
            await conn.execute(text("DELETE FROM portal_user_grants WHERE tenant_id = :t"), {"t": t})
            await conn.execute(text("DELETE FROM auth_users WHERE tenant_id = :t"), {"t": t})


async def test_concurrent_invites_cap(pg_engine, pg_sessionmaker):
    """FREEZE F1: 11 concurrent invites for one customer leave exactly 10 active."""
    import asyncio

    from auth.test_auth import issue_test_context
    from errors.exceptions import AppException

    tenant_id = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    service = _access_service(pg_sessionmaker, [tenant_id])
    admin = issue_test_context(tenant_id, roles=("admin",), user_id="pytest-admin")
    emails = [f"pytest-portal-{uuid.uuid4().hex[:8]}@example.test" for _ in range(11)]
    try:
        results = await asyncio.gather(
            *(service.invite(admin, "QA-C1", e) for e in emails), return_exceptions=True
        )
        ok = [r for r in results if not isinstance(r, BaseException)]
        errors = [r for r in results if isinstance(r, BaseException)]
        assert len(ok) == 10, errors
        assert len(errors) == 1 and isinstance(errors[0], AppException)
        assert errors[0].to_dict()["error_code"] == "PORTAL_USER_LIMIT_REACHED"
        async with pg_engine.connect() as conn:
            active = (await conn.execute(
                text("SELECT count(*) FROM portal_user_grants WHERE tenant_id = :t "
                     "AND customer_id = 'QA-C1' AND status = 'active'"), {"t": tenant_id},
            )).scalar_one()
            bound = (await conn.execute(
                text("SELECT count(*) FROM auth_users WHERE tenant_id = :t "
                     "AND customer_id = 'QA-C1' AND st_user_id IS NOT NULL"), {"t": tenant_id},
            )).scalar_one()
        assert active == 10 and bound == 10
    finally:
        await _cleanup(pg_engine, tenant_id)


async def test_grants_list_scoped_and_revoke_on_pg(pg_engine, pg_sessionmaker):
    """ISO-T-4 grants part: T1/A's list has no T2 rows (same customer id), and
    revoke deletes only the portal-only row."""
    from auth.test_auth import issue_test_context

    t1 = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    t2 = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    service = _access_service(pg_sessionmaker, [t1, t2])
    a1 = issue_test_context(t1, roles=("admin",), user_id="pytest-admin-1")
    a2 = issue_test_context(t2, roles=("admin",), user_id="pytest-admin-2")
    e1 = f"pytest-portal-{uuid.uuid4().hex[:8]}@example.test"
    e2 = f"pytest-portal-{uuid.uuid4().hex[:8]}@example.test"
    staff = f"pytest-portal-{uuid.uuid4().hex[:8]}@example.test"
    try:
        async with pg_engine.begin() as conn:
            await conn.execute(_INSERT, {**_params(t2, roles=["admin"]), "email": staff})
        g1 = await service.invite(a1, "QA-C1", e1)
        await service.invite(a2, "QA-C1", e2)

        listed = (await service.list(a1, "QA-C1")).data
        assert [g.grant_id for g in listed] == [g1.grant_id]
        assert listed[0].email == e1 and listed[0].status == "invited"

        # Case-insensitive re-invite is the same active grant (CITEXT).
        again = await service.invite(a1, "QA-C1", e1.upper())
        assert again.already_invited and again.grant_id == g1.grant_id

        await service.revoke(a1, "QA-C1", g1.grant_id)
        async with pg_engine.connect() as conn:
            rows = (await conn.execute(
                text("SELECT email FROM auth_users WHERE tenant_id IN (:a, :b) ORDER BY email"),
                {"a": t1, "b": t2},
            )).all()
            status = (await conn.execute(
                text("SELECT status, revoked_by FROM portal_user_grants WHERE grant_id = :g"),
                {"g": g1.grant_id},
            )).one()
        assert sorted(r[0] for r in rows) == sorted([e2, staff])
        assert tuple(status) == ("revoked", "pytest-admin-1")
        assert (await service.list(a1, "QA-C1")).data[0].status == "revoked"
    finally:
        await _cleanup(pg_engine, t1, t2)


# ---------------------------------------------------------------------------
# Portal payments on real Postgres (FEAT-005: PAY-1 [pg], PAY-5, PAY-6 [pg],
# ISO-T-4 attempts part). Invoices and Payments live in an in-memory
# document store; only portal_payment_attempts is real.
# ---------------------------------------------------------------------------

_PAY_TOTAL = 30660


@pytest.fixture
def pay_env(monkeypatch):
    """Commerce reads/writes on the document-store path only."""
    from config.settings import clear_settings_cache

    for name in ("COMMERCE_READ_FROM_POSTGRES", "COMMERCE_DUAL_WRITE_POSTGRES",
                 "COMMERCE_PAYMENTS_AUTHORITATIVE"):
        monkeypatch.setenv(name, "false")
    clear_settings_cache()
    yield
    clear_settings_cache()


def _pg_attempt_store(pg_sessionmaker):
    from contextlib import asynccontextmanager

    from portal.services.portal_payment_service import (
        PortalPaymentAttemptStore,
        PostgresAttemptTx,
    )

    @asynccontextmanager
    async def tx():
        async with pg_sessionmaker() as session:
            async with session.begin():
                yield PostgresAttemptTx(session)

    return PortalPaymentAttemptStore(tx_factory=tx)


def _scope(tenant_id, customer_id="QA-C1", user_id="pytest-portal-user"):
    from auth.test_auth import issue_test_context
    from portal.api._authz import PortalScope

    ctx = issue_test_context(tenant_id, roles=("customer",), user_id=user_id, customer_id=customer_id)
    return PortalScope(tenant_id=tenant_id, customer_id=customer_id, user_id=user_id, tenant=ctx)


class _PayHarness:
    def __init__(self, pg_sessionmaker, tenant_id, *, create_delay=0.0):
        from datetime import datetime, timedelta, timezone

        from commerce.models.invoice import Invoice
        from commerce.services.invoice_service import INVOICES_CURRENT_INDEX, InvoiceService
        from commerce.services.payment_service import PaymentService
        from portal.services.portal_payment_reconciler import PortalPaymentReconciler
        from portal.services.portal_payment_service import PortalPaymentService
        from portal.services.scoped_readers import PortalInvoiceReader
        from tests.portal._payment_fakes import FakePaymentIdempotency, FakePortalConnector
        from tests.unit._loading_plan_fakes import InMemoryDocumentStore

        self.tenant_id = tenant_id
        self.invoice_id = f"QA-PG-INV-{uuid.uuid4().hex[:8]}"
        self.docs = InMemoryDocumentStore()
        now = datetime.now(timezone.utc)
        doc = Invoice(
            invoice_id=self.invoice_id, tenant_id=tenant_id, customer_id="QA-C1",
            account_id="QA-ACCT-1", order_id="QA-ORDER-1", invoice_number="INV-PG-1",
            status="open",
            line_items=[{"line_id": "l1", "product_code": "PROPANE", "quantity_gallons": 100.0,
                         "unit_price_cents": 297, "unit_price_micros": 2_966_000,
                         "subtotal_cents": 29660}],
            subtotal_cents=29660, tax_cents=1000, total_cents=_PAY_TOTAL, amount_paid_cents=0,
            remaining_cents=_PAY_TOTAL, issued_at=now, due_date=(now + timedelta(days=30)).date(),
        ).model_dump(mode="json")
        doc["created_at"] = now.isoformat()
        self.docs.seed(INVOICES_CURRENT_INDEX, self.invoice_id, doc)
        self.invoice_service = InvoiceService(es_service=self.docs)
        self.connector = FakePortalConnector()
        self.connector.create_delay = create_delay
        self.store = _pg_attempt_store(pg_sessionmaker)

        async def _factory(_tenant_id):
            return self.connector

        self.service = PortalPaymentService(
            store=self.store, invoice_reader=PortalInvoiceReader(self.invoice_service),
            connector_factory=_factory,
        )
        self.payment_service = PaymentService(
            es_service=self.docs, idempotency_service=FakePaymentIdempotency(),
            invoice_service=self.invoice_service,
        )
        self.reconciler = PortalPaymentReconciler(
            store=self.store, payment_service=self.payment_service,
            invoice_service=self.invoice_service,
        )

    def payments(self):
        from commerce.services.commerce_es_mappings import PAYMENTS_CURRENT_INDEX

        return list(self.docs.docs[PAYMENTS_CURRENT_INDEX].values())


async def _attempt_rows(pg_engine, tenant_id):
    async with pg_engine.connect() as conn:
        rows = (await conn.execute(
            text("SELECT payment_attempt_id, status, stripe_payment_intent_id, payment_id "
                 "FROM portal_payment_attempts WHERE tenant_id = :t ORDER BY created_at"),
            {"t": tenant_id},
        )).all()
    return [tuple(r) for r in rows]


async def _cleanup_attempts(pg_engine, *tenants):
    async with pg_engine.begin() as conn:
        for t in tenants:
            await conn.execute(text("DELETE FROM portal_payment_attempts WHERE tenant_id = :t"), {"t": t})


async def test_concurrent_same_key(pg_engine, pg_sessionmaker, pay_env):
    """PAY-1 [pg]: two same-key creates in parallel: one row, one Stripe call,
    both answers carry the same attempt id (one 201, one 200)."""
    import asyncio

    tenant_id = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    h = _PayHarness(pg_sessionmaker, tenant_id, create_delay=0.3)
    scope = _scope(tenant_id)
    try:
        results = await asyncio.gather(*(
            h.service.create(scope, h.invoice_id, idempotency_key="pytest-same-key-1",
                             amount_cents=None)
            for _ in range(2)
        ))
        assert sorted(r.status_code for r in results) == [200, 201]
        assert len({r.attempt["payment_attempt_id"] for r in results}) == 1
        replay = next(r for r in results if r.status_code == 200)
        if replay.attempt["status"] == "creating":
            assert replay.retry_after == 2 and replay.client_secret is None
        assert len(h.connector.creates()) == 1
        rows = await _attempt_rows(pg_engine, tenant_id)
        assert len(rows) == 1 and rows[0][1] == "created"
        assert h.connector.creates()[0]["idempotency_key"] == f"portal_pa_{rows[0][0]}"
    finally:
        await _cleanup_attempts(pg_engine, tenant_id)


async def test_concurrent_creates_serialized(pg_engine, pg_sessionmaker, pay_env):
    """PAY-5 (F2): 10 concurrent creates with distinct keys on one invoice
    leave exactly one in-flight row; the others get 409."""
    import asyncio

    from errors.exceptions import AppException

    tenant_id = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    h = _PayHarness(pg_sessionmaker, tenant_id, create_delay=0.5)
    scope = _scope(tenant_id)
    try:
        results = await asyncio.gather(*(
            h.service.create(scope, h.invoice_id, idempotency_key=f"pytest-key-{i:04d}",
                             amount_cents=None)
            for i in range(10)
        ), return_exceptions=True)
        ok = [r for r in results if not isinstance(r, BaseException)]
        errors = [r for r in results if isinstance(r, BaseException)]
        assert len(ok) == 1 and ok[0].status_code == 201, errors
        assert len(errors) == 9 and all(isinstance(e, AppException) for e in errors)
        assert {e.to_dict()["error_code"] for e in errors} == {"PAYMENT_IN_PROGRESS"}
        rows = await _attempt_rows(pg_engine, tenant_id)
        assert [r[1] for r in rows] == ["created"]
        assert len(h.connector.creates()) == 1
    finally:
        await _cleanup_attempts(pg_engine, tenant_id)


async def test_concurrent_duplicate_succeeded_one_payment(pg_engine, pg_sessionmaker, pay_env):
    """PAY-6 [pg]: the same succeeded event delivered twice concurrently gives
    one Payment, applied once; the attempt is succeeded."""
    import asyncio

    from commerce.services.commerce_es_mappings import INVOICE_EVENTS_INDEX
    from tests.portal._payment_fakes import intent_event

    tenant_id = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    h = _PayHarness(pg_sessionmaker, tenant_id)
    scope = _scope(tenant_id)
    try:
        created = await h.service.create(scope, h.invoice_id, idempotency_key="pytest-pay6-key",
                                         amount_cents=None)
        event = intent_event("payment_intent.succeeded", created.attempt)
        h.docs.set_yield_schedule([1], repeat=True)  # interleave the two deliveries
        results = await asyncio.gather(
            h.reconciler.handle(tenant_id, event), h.reconciler.handle(tenant_id, event)
        )
        assert all(r["portal"] and r["handled"] for r in results)
        (payment,) = h.payments()
        rows = await _attempt_rows(pg_engine, tenant_id)
        assert rows == [(created.attempt["payment_attempt_id"], "succeeded",
                         created.attempt["stripe_payment_intent_id"], payment["payment_id"])]
        applied = [e for e in h.docs.docs[INVOICE_EVENTS_INDEX].values()
                   if (e.get("payload") or {}).get("payment_id") == payment["payment_id"]]
        assert len(applied) == 1
    finally:
        await _cleanup_attempts(pg_engine, tenant_id)


async def test_attempt_queries_scoped(pg_engine, pg_sessionmaker, pay_env):
    """ISO-T-4 (attempts part): a T2 row with the same customer and invoice ids
    is invisible to T1's scope on every store query."""
    from datetime import datetime, timezone

    from errors.exceptions import AppException

    t1 = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    t2 = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    store = _pg_attempt_store(pg_sessionmaker)
    now = datetime.now(timezone.utc)
    row = {
        "payment_attempt_id": f"ppa_{uuid.uuid4()}", "tenant_id": t2, "customer_id": "QA-C1",
        "invoice_id": "QA-INV-1", "account_id": "QA-ACCT-1", "actor_user_id": "pytest-u",
        "idempotency_key": "pytest-scope-key", "amount_cents": 500, "status": "created",
        "stripe_payment_intent_id": f"pi_{uuid.uuid4().hex[:10]}", "payment_id": None,
        "failure_code": None, "created_at": now, "updated_at": now, "terminal_at": None,
    }
    try:
        async with store.transaction() as tx:
            await tx.insert(row)
        scope_t1, scope_t2 = _scope(t1, user_id="pytest-u"), _scope(t2, user_id="pytest-u")
        with pytest.raises(AppException) as exc:
            await store.get(scope_t1, row["payment_attempt_id"])
        assert exc.value.to_dict()["error_code"] == "RESOURCE_NOT_FOUND"
        assert (await store.get(scope_t2, row["payment_attempt_id"]))["tenant_id"] == t2
        assert await store.newest_for_invoice(scope_t1, "QA-INV-1") is None
        async with store.transaction() as tx:
            assert await tx.find_by_key(tenant_id=t1, customer_id="QA-C1", actor_user_id="pytest-u",
                                        idempotency_key="pytest-scope-key") is None
            assert await tx.inflight_for_update(tenant_id=t1, customer_id="QA-C1",
                                                invoice_id="QA-INV-1") == []
            assert await tx.lock_for_webhook(tenant_id=t1,
                                             payment_attempt_id=row["payment_attempt_id"]) is None
            assert await tx.find_by_payment_intent(
                tenant_id=t1, stripe_payment_intent_id=row["stripe_payment_intent_id"]) is None
            # Another customer of the same tenant can't see it either.
            assert await tx.get(tenant_id=t2, customer_id="QA-C2",
                                payment_attempt_id=row["payment_attempt_id"]) is None

        # Backstops: same (tenant, user, key) and a second in-flight row.
        for dup, code in (
            ({"payment_attempt_id": f"ppa_{uuid.uuid4()}", "invoice_id": "QA-INV-2"}, "IDEMPOTENCY_CONFLICT"),
            ({"payment_attempt_id": f"ppa_{uuid.uuid4()}", "idempotency_key": "pytest-other-key",
              "stripe_payment_intent_id": None}, "PAYMENT_IN_PROGRESS"),
        ):
            with pytest.raises(AppException) as exc:
                async with store.transaction() as tx:
                    await tx.insert({**row, **dup})
            assert exc.value.to_dict()["error_code"] == code

        # Conditional update: matches only the expected status.
        async with store.transaction() as tx:
            assert await tx.update(payment_attempt_id=row["payment_attempt_id"],
                                   fields={"status": "failed"}, now=now,
                                   expected_status="creating") == 0
            assert await tx.update(payment_attempt_id=row["payment_attempt_id"],
                                   fields={"status": "pending"}, now=now,
                                   expected_status="created") == 1
        assert (await store.get(scope_t2, row["payment_attempt_id"]))["status"] == "pending"
    finally:
        await _cleanup_attempts(pg_engine, t1, t2)
