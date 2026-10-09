"""Portal responses carry only allowlisted fields (design §2.1 E10; ISO-C-6,
AC6). FEAT-003 covers orders and tanks; FEAT-004 adds invoices (PDF, CSV).

[real-auth] ``main.app`` with the :class:`OrderHarness` store.
"""
from __future__ import annotations

from datetime import timedelta

from fuel.order_models import PORTAL_REVIEW_HOLD_REASON
from tests.portal.conftest import CUSTOMER_A, T1, call, iso

TANK = "QA-TANK-A1"

#: Keys that must never appear anywhere in a portal order/tank response.
RESTRICTED_KEYS = {
    "assigned_driver_id", "assigned_asset_id", "assigned_run_id", "assigned_claim_id",
    "driver_id", "recipient_name", "photo_refs", "signature_ref", "geotag", "pod_otp",
    "pod_otp_generated_at", "otp_verified", "customer_phone", "customer_email",
    "special_instructions", "hold_reason", "intake_metadata", "unit_price_cents",
    "unit_price_micros", "subtotal_cents", "tax_cents", "total_cents", "pod_id",
    "meter_ticket_ref", "bol_ref", "pod_hash", "location_lat", "location_lon",
    "k_factor", "source_system", "ship_to_address", "ship_to_lat", "ship_to_lon",
    "customer_name", "trace_id",
}

#: Values seeded on the restricted fields; none may leak.
RESTRICTED_VALUES = [
    "QA-DRV-SECRET", "QA-TRUCK-SECRET", "QA-RUN-SECRET", "QA-CLAIM-SECRET",
    "Pat Secret-Recipient", "photo-secret.jpg", "sig-secret", "918273", "+15550199",
    "secret@example.test", "gate code 4321", "QA-ERP-SECRET", "1 Secret Lane",
    "pod-secret", "bol-secret", "34.0101", "-77.0202", "41.7777",
]


def _keys(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            out.add(key)
            _keys(value, out)
    elif isinstance(node, list):
        for value in node:
            _keys(value, out)
    return out


def test_order_and_tank_responses_hide_restricted_fields(client, portal_on, portal_orders, cA):
    h = portal_orders
    h.add_tank(T1, CUSTOMER_A, TANK, lat=41.7777, lon=-88.1)
    created = h.now - timedelta(days=1)
    h.add_order(
        T1, CUSTOMER_A, "QA-ORD-FULL", customer_tank_id=TANK, status="delivered",
        created_at=created,
        customer_phone="+15550199", customer_email="secret@example.test",
        special_instructions="gate code 4321", ship_to_address="1 Secret Lane",
        ship_to_lat=34.0101, ship_to_lon=-77.0202,
        assigned_driver_id="QA-DRV-SECRET", assigned_asset_id="QA-TRUCK-SECRET",
        assigned_run_id="QA-RUN-SECRET", assigned_claim_id="QA-CLAIM-SECRET",
        pod_otp="918273", unit_price_cents=399, subtotal_cents=39900, total_cents=42000,
        intake_metadata={"source_system": "QA-ERP-SECRET", "source_record_id": "r-1"},
        delivery_result={
            "pod_id": "pod-secret", "actual_gallons": 98.5, "actual_gallons_source": "meter",
            "delivered_at": iso(created + timedelta(hours=3)),
            "recipient_name": "Pat Secret-Recipient", "driver_id": "QA-DRV-SECRET",
            "signature_ref": "sig-secret", "photo_refs": ["photo-secret.jpg"],
            "bol_ref": "bol-secret", "geotag": {"lat": 34.0101, "lon": -77.0202},
            "otp_verified": True, "ticket_number": "TCK-9",
        },
    )
    h.add_order(
        T1, CUSTOMER_A, "QA-ORD-HELD", customer_tank_id=TANK, status="on_hold",
        hold_reason=PORTAL_REVIEW_HOLD_REASON, channel="web_portal",
        special_instructions="gate code 4321", customer_phone="+15550199",
    )

    responses = [
        call(client, "GET", "/api/portal/orders", cA),
        call(client, "GET", "/api/portal/orders/QA-ORD-FULL", cA),
        call(client, "GET", "/api/portal/orders/QA-ORD-HELD", cA),
        call(client, "GET", "/api/portal/tanks", cA),
        call(client, "GET", f"/api/portal/tanks/{TANK}", cA),
        call(client, "GET", f"/api/portal/tanks/{TANK}/deliveries", cA),
    ]
    for resp in responses:
        assert resp.status_code == 200, resp.text
        leaked = _keys(resp.json(), set()) & RESTRICTED_KEYS
        assert leaked == set(), (resp.url, leaked)
        for value in RESTRICTED_VALUES:
            assert value not in resp.text, (resp.url, value)

    full = responses[1].json()["data"]
    assert full["delivered_gallons"] == 98.5
    assert full["ticket_number"] == "TCK-9"
    assert full["status_code"] == "delivered"


# ---------------------------------------------------------------------------
# Invoices (FEAT-004): list, detail, PDF text and CSV
# ---------------------------------------------------------------------------

#: Keys that must never appear in a portal invoice response.
RESTRICTED_INVOICE_KEYS = {
    "driver_id", "recipient_name", "photo_refs", "signature_ref", "geotag", "pod_id",
    "meter_ticket_ref", "meter_number", "bol_id", "bol_ref", "pod_hash", "otp_verified",
    "actual_gallons_source", "delivery_result", "customer_id", "account_id", "order_id",
    "tenant_id", "line_id", "unit_price_micros", "external_refs", "qbo_push_state",
    "qbo_push_last_error", "void_reason", "location_mismatch",
}


def test_invoice_responses_hide_restricted_fields(client, portal_on, portal_fakes, cA):
    import csv
    import io

    from pypdf import PdfReader

    from tests.portal.conftest import RESTRICTED_DELIVERY_RESULT, RESTRICTED_INVOICE_VALUES

    h = portal_fakes.invoices
    h.accounts.add(T1, CUSTOMER_A, "QA-ACCT-ID-SECRET", "Main account")
    h.add_invoice(
        T1, CUSTOMER_A, "QA-INV-FULL", status="paid", account_id="QA-ACCT-ID-SECRET",
        delivery_result=dict(RESTRICTED_DELIVERY_RESULT), void_reason="QA-VOID-SECRET",
        qbo_push_last_error="QA-QBO-SECRET", external_refs={"qbo": "QA-EXT-SECRET"},
    )
    values = RESTRICTED_INVOICE_VALUES + ("QA-VOID-SECRET", "QA-QBO-SECRET", "QA-EXT-SECRET")

    for path in ("/api/portal/invoices", "/api/portal/invoices/QA-INV-FULL"):
        resp = call(client, "GET", path, cA)
        assert resp.status_code == 200, resp.text
        leaked = _keys(resp.json(), set()) & RESTRICTED_INVOICE_KEYS
        assert leaked == set(), (path, leaked)
        for value in values:
            assert value not in resp.text, (path, value)

    detail = call(client, "GET", "/api/portal/invoices/QA-INV-FULL", cA).json()["data"]
    assert detail["delivery"] == {
        "delivered_at": "2026-10-01T15:30:00Z", "actual_gallons": 123.5,
        "ticket_number": "TKT-4471",
    }
    assert detail["account_display_name"] == "Main account"

    pdf = call(client, "GET", "/api/portal/invoices/QA-INV-FULL/pdf", cA)
    assert pdf.status_code == 200
    text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "TKT-4471" in text and "123.50" in text
    for value in values:
        assert value not in text, ("pdf", value)

    exported = call(client, "GET", "/api/portal/invoices/export", cA)
    assert exported.status_code == 200
    body = exported.content.decode("utf-8")
    rows = list(csv.reader(io.StringIO(body.lstrip("\ufeff"))))
    assert [r[0] for r in rows[1:]] == ["INV-QA-INV-FULL"]
    for value in values:
        assert value not in body, ("csv", value)
