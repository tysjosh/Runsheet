"""Portal PaymentIntent webhook reconciliation (design §6.3, F2, F8).

PAY-6, PAY-6c (1)-(4), PAY-6d, PAY-6e, PAY-8, PAY-9 through the real
``POST /webhooks/stripe/{tenant_id}`` on ``main.app``. The commerce
``PaymentService`` and ``InvoiceService`` are real, over the invoice
harness's in-memory document store; the attempt store and Stripe connector
are fakes (``payments`` fixture).
"""
from __future__ import annotations

import logging

import pytest

from commerce.models.events import InvoiceEventType
from commerce.services.commerce_es_mappings import (
    INVOICE_EVENTS_INDEX,
    INVOICES_CURRENT_INDEX,
    PAYMENTS_CURRENT_INDEX,
)
from portal.services.portal_payment_reconciler import _event_type
from tests.portal._payment_fakes import intent_event
from tests.portal.conftest import CUSTOMER_A, T1, call

INVOICE = "QA-PAY-INV-1"
TOTAL = 30660


def _created_attempt(client, payments, session, invoice_id=INVOICE, **kw):
    """A real create: the row is ``created`` with ``pi_fake_<n>``."""
    if payments.invoice_doc(invoice_id) is None:
        payments.invoice(invoice_id, **kw)
    resp = payments.create(client, session, invoice_id)
    assert resp.status_code == 201, resp.text
    return payments.db.get(resp.json()["data"]["payment_attempt_id"])


class _Spy:
    """Count calls of an async method on an instance (pass-through)."""

    def __init__(self, monkeypatch, obj, name):
        self.count = 0
        original = getattr(obj, name)

        async def wrapper(*args, **kwargs):
            self.count += 1
            return await original(*args, **kwargs)

        monkeypatch.setattr(obj, name, wrapper)


def _ok(resp, *, handled=True):
    assert resp.status_code == 200, resp.text
    assert resp.json()["handled"] is handled, resp.json()
    return resp.json()


# ---------------------------------------------------------------------------
# PAY-6 / PAY-8
# ---------------------------------------------------------------------------


def test_duplicate_succeeded_one_payment(client, portal_on, payments, cA, monkeypatch):
    row = _created_attempt(client, payments, cA)
    ingest = _Spy(monkeypatch, payments.payment_service, "ingest")
    event = intent_event("payment_intent.succeeded", row)
    _ok(payments.webhook(client, event))
    _ok(payments.webhook(client, event))
    (payment,) = payments.payments()
    assert ingest.count == 1
    assert payment["external_id"] == row["stripe_payment_intent_id"]
    assert payment["source"] == "stripe" and payment["method"] == "ach"
    assert payment["amount_cents"] == TOTAL and payment["account_id"] == "QA-ACCT-A"
    attempt = payments.db.get(row["payment_attempt_id"])
    assert attempt["status"] == "succeeded" and attempt["payment_id"] == payment["payment_id"]
    assert attempt["failure_code"] is None and attempt["terminal_at"] is not None
    assert len(payments.applied_events(INVOICE, payment["payment_id"])) == 1
    assert payments.invoice_doc(INVOICE)["status"] == "paid"


def test_webhook_transitions(client, portal_on, payments, cA, sessions, portal_fakes, caplog):
    """PAY-8 (AC12)."""
    # processing -> pending; then succeeded records a partial payment.
    payments.invoice()
    resp = payments.create(client, cA, amount=10000)
    row = payments.db.get(resp.json()["data"]["payment_attempt_id"])
    _ok(payments.webhook(client, intent_event("payment_intent.processing", row)))
    assert payments.db.get(row["payment_attempt_id"])["status"] == "pending"
    _ok(payments.webhook(client, intent_event("payment_intent.succeeded", row)))
    assert payments.db.get(row["payment_attempt_id"])["status"] == "succeeded"
    inv = payments.invoice_doc(INVOICE)
    assert inv["status"] == "partial" and inv["remaining_cents"] == TOTAL - 10000
    # processing after succeeded changes nothing.
    _ok(payments.webhook(client, intent_event("payment_intent.processing", row)))
    assert payments.db.get(row["payment_attempt_id"])["status"] == "succeeded"

    # payment_failed / canceled record no Payment.
    for n, etype in enumerate(("payment_intent.payment_failed", "payment_intent.canceled")):
        user = sessions.customer(T1, CUSTOMER_A)
        portal_fakes.grants.grant(user)
        other = _created_attempt(client, payments, user, f"QA-PAY-INV-F{n}")
        event = intent_event(etype, other, last_payment_error={"code": "account_closed"})
        _ok(payments.webhook(client, event))
        stored = payments.db.get(other["payment_attempt_id"])
        assert stored["status"] == ("failed" if "failed" in etype else "canceled")
        if "failed" in etype:
            assert stored["failure_code"] == "account_closed"
    assert len(payments.payments()) == 1

    # succeeded after canceled records the payment (late success).
    late = payments.db.get(other["payment_attempt_id"])
    assert late["status"] == "canceled"
    caplog.set_level(logging.WARNING)
    _ok(payments.webhook(client, intent_event("payment_intent.succeeded", late)))
    assert payments.db.get(late["payment_attempt_id"])["status"] == "succeeded"
    assert len(payments.payments()) == 2
    assert any(r.getMessage() == "portal_payment_late_success" for r in caplog.records)


def test_ingest_app_exception_is_apply_rejected(client, portal_on, payments, cA, monkeypatch, caplog):
    from errors.exceptions import validation_error

    row = _created_attempt(client, payments, cA)

    async def _refuse(**_kw):
        raise validation_error("amount_cents must be positive")

    monkeypatch.setattr(payments.payment_service, "ingest", _refuse)
    caplog.set_level(logging.ERROR)
    _ok(payments.webhook(client, intent_event("payment_intent.succeeded", row)))
    stored = payments.db.get(row["payment_attempt_id"])
    assert stored["status"] == "succeeded" and stored["payment_id"] is None
    assert stored["failure_code"] == "apply_rejected:VALIDATION_ERROR"
    assert [r for r in caplog.records if r.getMessage() == "portal_payment_unapplied"]


def test_db_error_is_500_and_redelivery_converges(client, portal_on, payments, cA):
    row = _created_attempt(client, payments, cA)
    payments.db.fail_updates = 1
    resp = payments.webhook(client, intent_event("payment_intent.processing", row))
    assert resp.status_code == 500
    assert payments.db.get(row["payment_attempt_id"])["status"] == "created"
    _ok(payments.webhook(client, intent_event("payment_intent.processing", row)))
    assert payments.db.get(row["payment_attempt_id"])["status"] == "pending"


# ---------------------------------------------------------------------------
# PAY-6c: partial ingest outcomes (F8)
# ---------------------------------------------------------------------------


def test_attempt_update_fails_after_ingest(client, portal_on, payments, cA, monkeypatch):
    """(1) ingest commits, the attempt update raises: 500; the redelivery
    (Redis marker cleared, payments non-authoritative) neither ingests nor
    applies again."""
    row = _created_attempt(client, payments, cA)
    event = intent_event("payment_intent.succeeded", row)
    payments.db.fail_updates = 1
    assert payments.webhook(client, event).status_code == 500
    (payment,) = payments.payments()
    assert payments.db.get(row["payment_attempt_id"])["status"] == "created"

    payments.idempotency.clear()
    ingest = _Spy(monkeypatch, payments.payment_service, "ingest")
    apply = _Spy(monkeypatch, payments.invoices.service, "apply_payment")
    _ok(payments.webhook(client, event))
    assert ingest.count == 0 and apply.count == 0
    assert len(payments.payments()) == 1
    stored = payments.db.get(row["payment_attempt_id"])
    assert stored["status"] == "succeeded" and stored["payment_id"] == payment["payment_id"]
    assert len(payments.applied_events(INVOICE, payment["payment_id"])) == 1


def test_ingest_dies_before_apply(client, portal_on, payments, cA, monkeypatch):
    """(2) ingest raises after the Payment is indexed and before the invoice
    is applied: 500; the redelivery applies it exactly once."""
    row = _created_attempt(client, payments, cA)
    event = intent_event("payment_intent.succeeded", row)
    payments.invoices.store.fail_on("index_document", INVOICE_EVENTS_INDEX,
                                    exc=RuntimeError("invoice event write lost"))
    assert payments.webhook(client, event).status_code == 500
    (payment,) = payments.payments()
    assert payments.applied_events(INVOICE) == []

    ingest = _Spy(monkeypatch, payments.payment_service, "ingest")
    apply = _Spy(monkeypatch, payments.invoices.service, "apply_payment")
    _ok(payments.webhook(client, event))
    assert ingest.count == 0 and apply.count == 1
    assert len(payments.payments()) == 1
    assert len(payments.applied_events(INVOICE, payment["payment_id"])) == 1
    assert payments.invoice_doc(INVOICE)["status"] in ("partial", "paid")
    stored = payments.db.get(row["payment_attempt_id"])
    assert stored["status"] == "succeeded" and stored["payment_id"] == payment["payment_id"]

    # A third delivery changes nothing.
    _ok(payments.webhook(client, event))
    assert apply.count == 1 and len(payments.applied_events(INVOICE)) == 1


def test_authoritative_dedupe_unapplied(client, portal_on, payments, cA, monkeypatch):
    """(3) payments authoritative: the payment index write raises once after
    the Postgres insert. The redelivery misses find_by_external_id, ingest
    returns the existing Payment (PaymentAlreadyExists), and it is applied
    once with its full amount. PaymentService.apply is never called."""
    import commerce.services.commerce_persistence_bridge as bridge

    pg_rows = {}

    async def _authoritative(doc):
        key = (doc["tenant_id"], doc["source"], doc["external_id"])
        if key in pg_rows:
            raise bridge.PaymentAlreadyExists(dict(pg_rows[key]))
        pg_rows[key] = dict(doc)
        return dict(doc)

    monkeypatch.setattr(bridge, "create_payment_authoritative", _authoritative)
    row = _created_attempt(client, payments, cA)
    event = intent_event("payment_intent.succeeded", row)
    payments.invoices.store.fail_on("index_document", PAYMENTS_CURRENT_INDEX,
                                    exc=RuntimeError("projection write lost"))
    assert payments.webhook(client, event).status_code == 500
    assert payments.payments() == [] and len(pg_rows) == 1
    (existing,) = pg_rows.values()

    apply_spy = _Spy(monkeypatch, payments.payment_service, "apply")
    applied = []
    original_apply_payment = payments.invoices.service.apply_payment

    async def _apply_payment(**kw):
        applied.append(kw)
        return await original_apply_payment(**kw)

    monkeypatch.setattr(payments.invoices.service, "apply_payment", _apply_payment)
    _ok(payments.webhook(client, event))
    assert apply_spy.count == 0
    assert [(a["payment_id"], a["amount_cents"]) for a in applied] == [
        (existing["payment_id"], TOTAL)
    ]
    assert len(payments.applied_events(INVOICE, existing["payment_id"])) == 1
    assert payments.invoice_doc(INVOICE)["status"] in ("partial", "paid")
    stored = payments.db.get(row["payment_attempt_id"])
    assert stored["status"] == "succeeded" and stored["payment_id"] == existing["payment_id"]


@pytest.mark.parametrize(
    ("change", "code"),
    [("staff_payment", "OVERPAYMENT_UNAPPLIED"), ("voided", "INVOICE_NOT_PAYABLE")],
)
def test_reapply_capped_by_remaining(client, portal_on, payments, cA, monkeypatch, caplog, change, code):
    """(4) as (2), but the invoice changed before the redelivery: nothing is
    applied, the attempt is succeeded with apply_rejected, one ERROR line."""
    row = _created_attempt(client, payments, cA)
    event = intent_event("payment_intent.succeeded", row)
    payments.invoices.store.fail_on("index_document", INVOICE_EVENTS_INDEX,
                                    exc=RuntimeError("invoice event write lost"))
    assert payments.webhook(client, event).status_code == 500
    if change == "staff_payment":
        payments.invoices.store.poke(INVOICES_CURRENT_INDEX, INVOICE, status="partial",
                                     amount_paid_cents=TOTAL - 500, remaining_cents=500)
    else:
        payments.invoices.store.poke(INVOICES_CURRENT_INDEX, INVOICE, status="void")

    apply = _Spy(monkeypatch, payments.invoices.service, "apply_payment")
    caplog.set_level(logging.ERROR)
    caplog.clear()
    _ok(payments.webhook(client, event))
    assert apply.count == 0
    stored = payments.db.get(row["payment_attempt_id"])
    assert stored["status"] == "succeeded" and stored["payment_id"] is None
    assert stored["failure_code"] == f"apply_rejected:{code}"
    lines = [r for r in caplog.records if r.getMessage() == "portal_payment_unapplied"]
    assert len(lines) == 1 and lines[0].levelno == logging.ERROR
    assert len(payments.payments()) == 1 and payments.applied_events(INVOICE) == []


def test_event_type_normalization():
    """PAY-6e."""
    assert _event_type({"event_type": InvoiceEventType.PAYMENT_APPLIED}) == "payment_applied"
    assert _event_type({"event_type": "payment_applied"}) == "payment_applied"
    assert _event_type({}) == ""
    assert _event_type({"event_type": None}) == ""


# ---------------------------------------------------------------------------
# PAY-6d / PAY-9 / refunds
# ---------------------------------------------------------------------------


def test_webhook_dict_access_and_pi_fill(client, portal_on, payments, cA):
    payments.invoice()
    payments.db.add(
        payment_attempt_id="ppa_nopi", tenant_id=T1, customer_id=CUSTOMER_A, invoice_id=INVOICE,
        actor_user_id=cA.user_id, idempotency_key="qa-nopi-key-01", amount_cents=TOTAL,
        status="created", created_at=payments.clock[0],
    )
    row = payments.db.get("ppa_nopi")
    event = intent_event("payment_intent.payment_failed", row, pi_id="pi_external_9",
                         last_payment_error=None)
    _ok(payments.webhook(client, event))
    stored = payments.db.get("ppa_nopi")
    assert stored["status"] == "failed" and stored["failure_code"] == "unknown"
    assert stored["stripe_payment_intent_id"] == "pi_external_9"

    # Null-safe access: no data object, no metadata.
    assert payments.webhook(client, {"type": "payment_intent.succeeded"}).status_code == 200
    assert payments.webhook(client, {"type": "payment_intent.succeeded", "data": None}).status_code == 200


def test_mismatched_metadata_writes_nothing(client, portal_on, payments, cA, caplog):
    row = _created_attempt(client, payments, cA)
    caplog.set_level(logging.WARNING)
    for meta in ({"customer_id": "QA-PORTAL-CUST-B"}, {"invoice_id": "QA-OTHER"},
                 {"payment_attempt_id": "ppa_unknown"}):
        _ok(payments.webhook(client, intent_event("payment_intent.succeeded", row, meta=meta)),
            handled=False)
    other_pi = intent_event("payment_intent.succeeded", row, pi_id="pi_someone_else")
    _ok(payments.webhook(client, other_pi), handled=False)
    assert payments.db.get(row["payment_attempt_id"])["status"] == "created"
    assert payments.payments() == []
    mismatches = [r.extra_data for r in caplog.records
                  if r.name == "portal_audit" and r.extra_data.get("outcome") == "webhook_mismatch"]
    assert len(mismatches) == 4


def test_reconciliation_path_unchanged(client, portal_on, payments, cA):
    """PAY-9: non-portal events still reach connector.handle_webhook_event."""
    recon = {"id": "evt_1", "type": "payment_intent.succeeded",
             "data": {"object": {"id": "pi_recon", "metadata": {"reconciliation_id": "rec-1"}}}}
    body = _ok(payments.webhook(client, recon), handled=False)
    assert body["reason"] == "missing_reconciliation_id"  # the fake connector's answer
    assert payments.connector.webhook_events == [recon]

    # A charge.refunded for a non-portal intent falls through too.
    charge = {"id": "evt_2", "type": "charge.refunded",
              "data": {"object": {"id": "ch_1", "payment_intent": "pi_recon"}}}
    payments.webhook(client, charge)
    assert payments.connector.webhook_events[-1] == charge


def test_refund_or_dispute_is_audited_only(client, portal_on, payments, cA, caplog):
    """R6.15: WARN portal_audit, no state change, existing path not called."""
    row = _created_attempt(client, payments, cA)
    _ok(payments.webhook(client, intent_event("payment_intent.succeeded", row)))
    before = payments.db.get(row["payment_attempt_id"])
    caplog.set_level(logging.WARNING)
    for etype in ("charge.refunded", "charge.dispute.created"):
        event = {"id": f"evt_{etype}", "type": etype,
                 "data": {"object": {"id": "ch_9", "payment_intent": row["stripe_payment_intent_id"]}}}
        _ok(payments.webhook(client, event))
    assert payments.db.get(row["payment_attempt_id"]) == before
    assert payments.connector.webhook_events == []
    lines = [r.extra_data for r in caplog.records
             if r.name == "portal_audit" and r.extra_data.get("action") == "payment_refund_or_dispute"]
    assert len(lines) == 2 and all(r.levelno == logging.WARNING for r in caplog.records
                                   if r.name == "portal_audit"
                                   and r.extra_data.get("action") == "payment_refund_or_dispute")
    assert lines[0]["target_ids"]["payment_attempt_id"] == row["payment_attempt_id"]
    assert len(payments.payments()) == 1


def test_unconfigured_portal_handler_falls_through(client, portal_on, payments, cA):
    from integrations.api import stripe_endpoints as se

    row = _created_attempt(client, payments, cA)
    se._portal_payment_handler = None  # the fixture restores the wiring
    event = intent_event("payment_intent.succeeded", row)
    _ok(payments.webhook(client, event), handled=False)
    assert payments.connector.webhook_events == [event]
    assert payments.db.get(row["payment_attempt_id"])["status"] == "created"


def test_payment_attempt_status_after_webhook(client, portal_on, payments, cA):
    row = _created_attempt(client, payments, cA)
    _ok(payments.webhook(client, intent_event("payment_intent.succeeded", row)))
    resp = call(client, "GET", f"/api/portal/payment-attempts/{row['payment_attempt_id']}", cA)
    assert resp.json()["data"]["status_code"] == "succeeded"
    assert resp.json()["data"]["status_label"] == "Paid"
