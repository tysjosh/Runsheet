"""Portal ACH payment create and attempt reads (design §6.2; PAY-1..4, 7, 10, 11, 14).

[real-auth] ``main.app`` with fake session verifiers; the attempt store,
Stripe connector and invoices are in-memory fakes (``payments`` fixture).
The payment limit is 5 creates/min per user, so tests mint extra sessions
for the same customer with :func:`_user` where they need more calls.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest

from portal.services.portal_payment_service import (
    PORTAL_CREATING_STALE_SECONDS,
    _portal_intent_body,
)
from tests.portal._payment_fakes import STRIPE_SECRET_TEXT, InvalidRequestError, intent_event
from tests.portal.conftest import CUSTOMER_A, T1, call

INVOICE = "QA-PAY-INV-1"
TOTAL = 30660  # add_invoice: 29660 subtotal + 1000 tax, nothing paid


def _user(sessions, portal_fakes):
    s = sessions.customer(T1, CUSTOMER_A)
    portal_fakes.grants.grant(s)
    return s


def _data(resp):
    return resp.json()["data"]


def _provider_error_lines(caplog):
    return [r for r in caplog.records
            if r.getMessage() == "portal_payment_provider_error" and r.levelno == logging.ERROR]


def _seed(payments, session, *, status, age_seconds=0.0, key="qa-seed-key-0001", pi=None,
          attempt_id="ppa_seed_1", amount=TOTAL, invoice_id=INVOICE):
    return payments.db.add(
        payment_attempt_id=attempt_id, tenant_id=T1, customer_id=CUSTOMER_A,
        invoice_id=invoice_id, account_id="QA-ACCT-A", actor_user_id=session.user_id,
        idempotency_key=key, amount_cents=amount, status=status,
        stripe_payment_intent_id=pi,
        created_at=payments.clock[0] - timedelta(seconds=age_seconds),
    )


# ---------------------------------------------------------------------------
# PAY-1 / PAY-2
# ---------------------------------------------------------------------------


def test_same_key_one_attempt_one_intent(client, portal_on, payments, cA):
    payments.invoice()
    first = payments.create(client, cA, key="qa-same-key-0001")
    assert first.status_code == 201, first.text
    created = _data(first)
    attempt_id = created["payment_attempt_id"]
    assert attempt_id.startswith("ppa_")
    assert created["status_code"] == "created"
    assert created["amount_cents"] == TOTAL
    assert created["client_secret"] == "pi_fake_1_secret_fake"
    assert created["publishable_key"] == payments.connector.publishable_key

    again = payments.create(client, cA, key="qa-same-key-0001")
    assert again.status_code == 200, again.text
    assert _data(again)["payment_attempt_id"] == attempt_id
    assert _data(again)["client_secret"] == "pi_fake_1_secret_fake"  # replay of `created`

    assert len(payments.db.for_invoice(INVOICE)) == 1
    creates = payments.connector.creates()
    assert len(creates) == 1
    assert creates[0]["idempotency_key"] == f"portal_pa_{attempt_id}"
    assert creates[0]["metadata"] == {
        "source": "runsheet_portal", "tenant_id": T1, "customer_id": CUSTOMER_A,
        "invoice_id": INVOICE, "payment_attempt_id": attempt_id,
    }
    assert creates[0]["description"] == f"Invoice INV-{INVOICE}"
    row = payments.db.get(attempt_id)
    assert row["status"] == "created" and row["stripe_payment_intent_id"] == "pi_fake_1"
    assert row["actor_user_id"] == cA.user_id and row["account_id"] == "QA-ACCT-A"


def test_same_key_creating_replay_and_stale_redrive(client, portal_on, payments, cA):
    """PAY-1 stale threshold: 119 s is the ``creating`` replay; 121 s re-drives."""
    payments.invoice()
    key = "qa-stale-key-0001"
    _seed(payments, cA, status="creating", age_seconds=PORTAL_CREATING_STALE_SECONDS - 1, key=key)

    young = payments.create(client, cA, key=key)
    assert young.status_code == 200, young.text
    assert young.headers["Retry-After"] == "2"
    assert _data(young)["status_code"] == "creating"
    assert _data(young)["client_secret"] is None
    assert payments.connector.calls == []

    payments.advance(2)  # now 121 s old
    stale = payments.create(client, cA, key=key)
    assert stale.status_code == 200, stale.text
    assert _data(stale)["payment_attempt_id"] == "ppa_seed_1"
    assert _data(stale)["status_code"] == "created"
    assert _data(stale)["client_secret"] == "pi_fake_1_secret_fake"
    assert "Retry-After" not in stale.headers
    creates = payments.connector.creates()
    assert [c["idempotency_key"] for c in creates] == ["portal_pa_ppa_seed_1"]
    assert payments.db.get("ppa_seed_1")["status"] == "created"
    assert len(payments.db.for_invoice(INVOICE)) == 1


def test_same_key_different_amount_conflict(client, portal_on, payments, cA):
    payments.invoice()
    payments.invoice("QA-PAY-INV-2")
    key = "qa-conflict-key-01"
    assert payments.create(client, cA, key=key, amount=5000).status_code == 201
    other_amount = payments.create(client, cA, key=key, amount=6000)
    assert other_amount.status_code == 409
    assert other_amount.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
    other_invoice = payments.create(client, cA, "QA-PAY-INV-2", key=key, amount=5000)
    assert other_invoice.status_code == 409
    assert other_invoice.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert len(payments.connector.creates()) == 1
    assert len(payments.db.rows) == 1


# ---------------------------------------------------------------------------
# PAY-3 / PAY-4
# ---------------------------------------------------------------------------


def test_second_key_while_pending_409(client, portal_on, payments, cA):
    payments.invoice()
    _seed(payments, cA, status="pending", pi="pi_pending_1")
    resp = payments.create(client, cA, key="qa-second-key-01")
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "PAYMENT_IN_PROGRESS"
    assert payments.connector.calls == []
    assert len(payments.db.rows) == 1


def test_second_key_supersedes_created(client, portal_on, payments, cA):
    payments.invoice()
    first = _data(payments.create(client, cA, key="qa-first-key-0001"))
    second = payments.create(client, cA, key="qa-second-key-001")
    assert second.status_code == 201, second.text
    assert ("cancel", "pi_fake_1") in payments.connector.calls
    old = payments.db.get(first["payment_attempt_id"])
    assert old["status"] == "canceled" and old["terminal_at"] is not None
    new = payments.db.get(_data(second)["payment_attempt_id"])
    assert new["status"] == "created" and new["stripe_payment_intent_id"] == "pi_fake_2"
    assert [r["payment_attempt_id"] for r in payments.db.inflight(INVOICE)] == [new["payment_attempt_id"]]


def test_supersede_when_money_is_moving_409(client, portal_on, payments, cA):
    payments.invoice()
    first = _data(payments.create(client, cA, key="qa-first-key-0002"))
    payments.connector.cancel_error = InvalidRequestError(
        "This PaymentIntent's status is processing", code="payment_intent_unexpected_state"
    )
    payments.connector.retrieve_status["pi_fake_1"] = "processing"
    resp = payments.create(client, cA, key="qa-second-key-002")
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "PAYMENT_IN_PROGRESS"
    assert payments.db.get(first["payment_attempt_id"])["status"] == "pending"
    assert len(payments.db.rows) == 1
    assert len(payments.connector.creates()) == 1


@pytest.mark.parametrize("mode", ["error", "unexpected_state_other_status", "timeout"])
def test_supersede_cancel_failure_502_changes_nothing(client, portal_on, payments, cA, caplog, mode):
    payments.invoice()
    first = _data(payments.create(client, cA, key="qa-first-key-0003"))
    if mode == "error":
        payments.connector.cancel_error = RuntimeError(STRIPE_SECRET_TEXT)
    elif mode == "unexpected_state_other_status":
        payments.connector.cancel_error = InvalidRequestError(code="payment_intent_unexpected_state")
        payments.connector.retrieve_status["pi_fake_1"] = "requires_payment_method"
    else:
        payments.connector.cancel_delay = 2.0  # > the harness's 0.5 s bound
    caplog.set_level(logging.INFO)
    caplog.clear()
    resp = payments.create(client, cA, key="qa-second-key-003")
    assert resp.status_code == 502
    assert resp.json()["error_code"] == "PAYMENT_PROVIDER_ERROR"
    assert STRIPE_SECRET_TEXT not in resp.text and "sk_live" not in resp.text
    old = payments.db.get(first["payment_attempt_id"])
    assert old["status"] == "created"
    assert len(payments.db.rows) == 1
    assert len(payments.connector.creates()) == 1
    lines = _provider_error_lines(caplog)
    assert len(lines) == 1
    assert first["payment_attempt_id"] in lines[0].extra_data["payment_attempt_ids"]
    assert STRIPE_SECRET_TEXT not in caplog.text


def test_stale_creating_superseded_at_121s(client, portal_on, payments, cA):
    """PAY-4: a stale ``creating`` (other key) is 409 at 119 s, superseded at 121 s."""
    payments.invoice()
    _seed(payments, cA, status="creating", age_seconds=PORTAL_CREATING_STALE_SECONDS - 1)
    resp = payments.create(client, cA, key="qa-new-key-00001")
    assert resp.status_code == 409 and resp.json()["error_code"] == "PAYMENT_IN_PROGRESS"
    assert "cancel" not in payments.connector.names()

    payments.advance(2)
    resp = payments.create(client, cA, key="qa-new-key-00002")
    assert resp.status_code == 201, resp.text
    creates = payments.connector.creates()
    # The re-drive (old key) learns the intent id, then the new attempt.
    assert creates[0]["idempotency_key"] == "portal_pa_ppa_seed_1"
    assert ("cancel", "pi_fake_1") in payments.connector.calls
    assert payments.db.get("ppa_seed_1")["status"] == "canceled"
    assert _data(resp)["status_code"] == "created"


# ---------------------------------------------------------------------------
# PAY-7
# ---------------------------------------------------------------------------


def test_amount_validation(client, portal_on, payments, sessions, portal_fakes, cA):
    payments.invoice()
    payments.invoice("QA-PAY-PAID", status="paid")
    payments.invoice("QA-PAY-VOID", status="void")
    payments.invoice("QA-PAY-DRAFT", status="draft")
    for amount in (99, TOTAL + 1):
        resp = payments.create(client, cA, amount=amount)
        assert resp.status_code == 422, (amount, resp.text)
        assert resp.json()["error_code"] == "PAYMENT_AMOUNT_INVALID"
        assert resp.json()["details"]["max_cents"] == TOTAL
    for invoice_id in ("QA-PAY-PAID", "QA-PAY-VOID"):
        resp = payments.create(client, cA, invoice_id)
        assert resp.status_code == 409 and resp.json()["error_code"] == "INVOICE_NOT_PAYABLE"
    other = _user(sessions, portal_fakes)
    draft = payments.create(client, other, "QA-PAY-DRAFT")
    assert draft.status_code == 404 and draft.json()["error_code"] == "RESOURCE_NOT_FOUND"  # DV8
    malformed_amount = payments.create(client, other, amount="500")  # type: ignore[arg-type]
    assert malformed_amount.status_code == 422
    assert payments.db.rows == {} and payments.connector.calls == []


def test_idempotency_key_header_rules(client, portal_on, payments, cA):
    payments.invoice()
    path = f"/api/portal/invoices/{INVOICE}/payments"
    for headers in ({}, {"Idempotency-Key": "short"}, {"Idempotency-Key": "bad key with spaces"},
                    {"Idempotency-Key": "x" * 256}):
        resp = call(client, "POST", path, cA, json={}, headers=headers)
        assert resp.status_code == 400, headers
        assert resp.json()["error_code"] == "MISSING_IDEMPOTENCY_KEY"
    extra = call(client, "POST", path, cA, json={"tenant_id": "qa-tenant-b"},
                 headers={"Idempotency-Key": "qa-extra-field-1"})
    assert extra.status_code == 422
    assert payments.db.rows == {}


def test_partial_amount_and_default_remaining(client, portal_on, payments, cA):
    payments.invoice()
    resp = payments.create(client, cA, amount=100)
    assert resp.status_code == 201 and _data(resp)["amount_cents"] == 100
    assert payments.connector.creates()[0]["amount_cents"] == 100


# ---------------------------------------------------------------------------
# PAY-10 / PAY-11
# ---------------------------------------------------------------------------


def test_no_stripe_409(client, portal_on, payments, cA):
    payments.invoice()
    assert _data(call(client, "GET", "/api/portal/me", cA))["payments_available"] is True
    assert _data(call(client, "GET", f"/api/portal/invoices/{INVOICE}", cA))["payable"] is True

    payments.connected = False
    resp = payments.create(client, cA)
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "PORTAL_PAYMENTS_UNAVAILABLE"
    assert _data(call(client, "GET", "/api/portal/me", cA))["payments_available"] is False
    assert _data(call(client, "GET", f"/api/portal/invoices/{INVOICE}", cA))["payable"] is False
    assert payments.db.rows == {}


@pytest.mark.parametrize("mode", ["error", "timeout"])
def test_provider_error_502(client, portal_on, payments, cA, caplog, mode):
    payments.invoice()
    if mode == "error":
        payments.connector.create_error = RuntimeError(STRIPE_SECRET_TEXT)
    else:
        payments.connector.create_delay = 2.0  # > the harness's 0.5 s bound
    caplog.set_level(logging.INFO)
    resp = payments.create(client, cA)
    assert resp.status_code == 502
    body = resp.json()
    assert body["error_code"] == "PAYMENT_PROVIDER_ERROR"
    assert STRIPE_SECRET_TEXT not in resp.text and "pi_internal" not in resp.text
    (row,) = payments.db.for_invoice(INVOICE)
    assert row["status"] == "failed" and row["failure_code"] == "provider_error"
    assert row["terminal_at"] is not None
    lines = _provider_error_lines(caplog)
    assert len(lines) == 1 and lines[0].extra_data["payment_attempt_ids"] == [row["payment_attempt_id"]]
    assert STRIPE_SECRET_TEXT not in caplog.text
    # A failed attempt isn't in flight: the next create may start.
    payments.connector.create_error = None
    payments.connector.create_delay = 0
    assert payments.create(client, cA).status_code == 201


# ---------------------------------------------------------------------------
# PAY-14
# ---------------------------------------------------------------------------


def test_conditional_post_stripe_update(client, portal_on, payments, cA, sessions, portal_fakes):
    """PAY-14 (1): a webhook moves the row on while Stripe create is in flight."""
    payments.invoice()

    async def _webhook_first(_index):
        (row,) = payments.db.for_invoice(INVOICE)
        event = intent_event("payment_intent.processing", row, pi_id="pi_fake_1")
        await payments.reconciler.handle(T1, event)

    payments.connector.during_create = _webhook_first
    resp = payments.create(client, cA)
    assert resp.status_code == 201, resp.text
    assert _data(resp)["status_code"] == "pending"
    assert _data(resp)["client_secret"] is None
    (row,) = payments.db.for_invoice(INVOICE)
    assert row["status"] == "pending" and row["stripe_payment_intent_id"] == "pi_fake_1"

    # The same with a failing create: the row stays pending, the answer is 502.
    payments.invoice("QA-PAY-INV-2")

    async def _webhook_then_fail(_index):
        (row2,) = payments.db.for_invoice("QA-PAY-INV-2")
        await payments.reconciler.handle(
            T1, intent_event("payment_intent.processing", row2, pi_id="pi_fake_9"))
        raise RuntimeError(STRIPE_SECRET_TEXT)

    payments.connector.during_create = _webhook_then_fail
    resp = payments.create(client, _user(sessions, portal_fakes), "QA-PAY-INV-2")
    assert resp.status_code == 502
    (row2,) = payments.db.for_invoice("QA-PAY-INV-2")
    assert row2["status"] == "pending"


def test_poll_does_not_call_stripe(client, portal_on, payments, sessions, portal_fakes, cA):
    """PAY-14 (2)."""
    payments.invoice()
    attempt_id = _data(payments.create(client, cA))["payment_attempt_id"]
    payments.connector.calls.clear()
    path = f"/api/portal/payment-attempts/{attempt_id}"

    polled = call(client, "GET", path, cA)
    assert polled.status_code == 200, polled.text
    data = _data(polled)
    assert data["status_code"] == "created" and data["status_label"] == "Payment processing"
    assert data["client_secret"] is None and data["invoice_id"] == INVOICE
    assert payments.connector.calls == []

    with_secret = call(client, "GET", f"{path}?include_client_secret=true", cA)
    assert with_secret.status_code == 200
    assert _data(with_secret)["client_secret"] == "pi_fake_1_secret_fake"
    assert _data(with_secret)["publishable_key"] == payments.connector.publishable_key
    assert payments.connector.names() == ["retrieve"]

    explicit_false = call(client, "GET", f"{path}?include_client_secret=false", cA)
    assert _data(explicit_false)["client_secret"] is None
    assert call(client, "GET", f"{path}?include_client_secret=yes", cA).status_code == 422

    # Another user of the same customer: status yes, secret no (404).
    colleague = _user(sessions, portal_fakes)
    assert call(client, "GET", path, colleague).status_code == 200
    denied = call(client, "GET", f"{path}?include_client_secret=true", colleague)
    assert denied.status_code == 404 and denied.json()["error_code"] == "RESOURCE_NOT_FOUND"
    assert payments.connector.names() == ["retrieve"]

    payments.connector.retrieve_error = RuntimeError(STRIPE_SECRET_TEXT)
    failed = call(client, "GET", f"{path}?include_client_secret=true", cA)
    assert failed.status_code == 502 and STRIPE_SECRET_TEXT not in failed.text

    assert call(client, "GET", "/api/portal/payment-attempts/ppa_unknown", cA).status_code == 404
    assert call(client, "GET", "/api/portal/payment-attempts/bad%20id", cA).status_code == 404


async def test_supersede_redrive_failure_byte_identical(portal_on, payments):
    """PAY-14 (3): the stale row's re-drive raises; it ends failed/provider_error,
    the new attempt is created, and the re-drive body equals the original."""
    from auth.test_auth import issue_test_context
    from portal.api._authz import PortalScope

    payments.invoice()
    ctx = issue_test_context(T1, roles=("customer",), user_id="qa-user-1", customer_id=CUSTOMER_A)
    scope = PortalScope(tenant_id=T1, customer_id=CUSTOMER_A, user_id="qa-user-1", tenant=ctx)
    service = payments.service
    service.stripe_timeout_seconds = 30.0
    payments.connector.create_delay = 5.0

    # A first request "crashes" while Stripe create is in flight: the row is
    # left in `creating` and its original body is recorded.
    task = asyncio.create_task(service.create(scope, INVOICE, idempotency_key="qa-crash-key-01",
                                              amount_cents=None))
    while not payments.connector.creates():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (stale,) = payments.db.for_invoice(INVOICE)
    assert stale["status"] == "creating"

    payments.connector.create_delay = 0
    payments.connector.create_errors = [RuntimeError(STRIPE_SECRET_TEXT)]  # the re-drive
    payments.advance(PORTAL_CREATING_STALE_SECONDS + 1)
    result = await service.create(scope, INVOICE, idempotency_key="qa-after-key-01", amount_cents=None)
    assert result.status_code == 201 and result.attempt["status"] == "created"

    old = payments.db.get(stale["payment_attempt_id"])
    assert old["status"] == "failed" and old["failure_code"] == "provider_error"
    original, redrive, new = payments.connector.creates()
    assert redrive["idempotency_key"] == original["idempotency_key"]
    assert redrive["body_json"] == original["body_json"]
    expected = _portal_intent_body(stale, f"INV-{INVOICE}")
    assert original["metadata"] == expected["metadata"]
    assert new["idempotency_key"] == f"portal_pa_{result.attempt['payment_attempt_id']}"
    assert "cancel" not in payments.connector.names()


# ---------------------------------------------------------------------------
# Invoice detail payment_attempt (R6.13)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "label"),
    [("creating", "Payment processing"), ("created", "Payment processing"),
     ("pending", "Payment processing"), ("succeeded", "Paid"),
     ("failed", "Payment failed, try again"), ("canceled", "Payment failed, try again")],
)
def test_invoice_detail_payment_attempt_label(client, portal_on, payments, cA, status, label):
    payments.invoice()
    _seed(payments, cA, status=status, attempt_id="ppa_old", age_seconds=600)
    _seed(payments, cA, status=status, attempt_id="ppa_newest", key="qa-seed-key-0002")
    resp = call(client, "GET", f"/api/portal/invoices/{INVOICE}", cA)
    assert resp.status_code == 200
    attempt = _data(resp)["payment_attempt"]
    assert attempt["payment_attempt_id"] == "ppa_newest"
    assert attempt["status_code"] == status and attempt["status_label"] == label
    assert attempt["amount_cents"] == TOTAL
    # The list never carries it.
    listed = call(client, "GET", "/api/portal/invoices", cA)
    assert all(i["payment_attempt"] is None for i in _data(listed))


def test_invoice_detail_without_attempt_is_null(client, portal_on, payments, cA):
    payments.invoice()
    assert _data(call(client, "GET", f"/api/portal/invoices/{INVOICE}", cA))["payment_attempt"] is None
