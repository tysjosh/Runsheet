"""Contract tests for POD actual gallons through invoice and ERP handoff.

The first case exercises a short delivery. The second replays a published
City of Joliet diesel transaction whose $2.9660 per-gallon rate previously
lost $42 when truncated to whole cents.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from commerce.hooks.order_delivered_subscriber import (
    OrderDeliveredInvoiceSubscriber,
)
from commerce.services.commerce_external_sync import CommerceExternalSync
from driver.models import PODRequest
from driver.services.pod_service import PODSubmissionService
from driver.services.work_ref import WorkRef
from fuel.services.order_service import OrderService
from integrations.quickbooks_online import _build_invoice_body_from_canonical
from services.money import (
    legacy_unit_price_cents,
    line_subtotal_cents,
    unit_price_usd,
)
from services.pod_hash_chain_writer import PodHashChainWriter

pytestmark = pytest.mark.integration

TENANT_ID = "tenant_midwest_fuels"
DRIVER_ID = "driver_42"
ORDER_ID = "erp-order-104882"
REQUESTED_GALLONS = 7_500.0
ACTUAL_GALLONS = 7_386.4
UNIT_PRICE_MICROS = 3_290_000
ORDER_TAX_CENTS = 75_000
PRODUCT_CODE = "ULSD"
SOURCE_SYSTEM = "legacy_erp"
SOURCE_RECORD_ID = "SO-104882"


class InMemoryOrderRepository:
    def __init__(self, order: dict) -> None:
        self.order = order
        self.events: list[dict] = []

    async def append_event(self, tenant_id: str, event: dict) -> None:
        assert tenant_id == TENANT_ID
        self.events.append(event)

    async def upsert_with_last_event_timestamp(
        self, tenant_id: str, order: dict
    ) -> None:
        assert tenant_id == TENANT_ID
        self.order = dict(order)


class InvoiceRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generate_from_order(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {
            "invoice_id": "inv-real-world-104882",
            "total_cents": (
                sum(item["subtotal_cents"] for item in kwargs["line_items"])
                + kwargs["tax_cents"]
            ),
        }


def _make_es() -> MagicMock:
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    return es


def _file_storage() -> MagicMock:
    storage = MagicMock()
    storage.validate_ref = MagicMock(return_value=True)
    return storage


@pytest.mark.asyncio
async def test_short_delivery_uses_pod_gallons_in_invoice_and_qbo_payload():
    order = {
        "order_id": ORDER_ID,
        "tenant_id": TENANT_ID,
        "status": "in_transit",
        "assigned_driver_id": DRIVER_ID,
        "customer_id": "customer-riverbend-farms",
        "account_id": "account-riverbend-farms",
        "product_code": PRODUCT_CODE,
        "gallons_requested": REQUESTED_GALLONS,
        "unit_price_micros": UNIT_PRICE_MICROS,
        "unit_price_cents": legacy_unit_price_cents(UNIT_PRICE_MICROS),
        "subtotal_cents": line_subtotal_cents(
            REQUESTED_GALLONS,
            UNIT_PRICE_MICROS,
        ),
        "tax_cents": ORDER_TAX_CENTS,
        "delivery_window_start": "2026-07-29T12:00:00Z",
        "delivery_window_end": "2026-07-29T16:00:00Z",
        "intake_metadata": {
            "source_system": SOURCE_SYSTEM,
            "source_record_id": SOURCE_RECORD_ID,
        },
    }
    order_repo = InMemoryOrderRepository(order)
    invoice_recorder = InvoiceRecorder()
    order_service = OrderService(
        order_repo=order_repo,
        ws_manager=SimpleNamespace(broadcast=AsyncMock()),
    )
    order_service.subscribe(
        "order.delivered",
        OrderDeliveredInvoiceSubscriber(invoice_recorder),
    )

    es = _make_es()
    pod_service = PODSubmissionService(
        es_service=es,
        order_service=order_service,
        file_storage_service=_file_storage(),
        pod_hash_chain_writer=PodHashChainWriter(es_service=es),
    )
    work_ref = WorkRef(
        tenant_id=TENANT_ID,
        driver_id=DRIVER_ID,
        kind="order",
        work_id=ORDER_ID,
        order_doc=order,
    )
    pod = PODRequest(
        recipient_name="Morgan Lee",
        customer_id="customer-riverbend-farms",
        signature_ref=(
            f"tenants/{TENANT_ID}/signature/2026/07/29/"
            "11111111-1111-1111-1111-111111111111.png"
        ),
        photo_refs=[
            (
                f"tenants/{TENANT_ID}/photo/2026/07/29/"
                "22222222-2222-2222-2222-222222222222.jpg"
            )
        ],
        delivered_gallons=ACTUAL_GALLONS,
        geotag={"lat": 40.4167, "lng": -86.8753},
        timestamp="2026-07-29T14:42:18-04:00",
    )

    with patch(
        "commerce.hooks.order_delivered_subscriber.get_settings",
        return_value=SimpleNamespace(commerce_invoicing_enabled=True),
    ):
        result = await pod_service.submit(
            work_ref,
            pod,
            request_id="req-real-world-104882",
        )

    delivery_result = order_repo.order["delivery_result"]
    assert order_repo.order["status"] == "delivered"
    assert delivery_result["actual_gallons"] == ACTUAL_GALLONS
    assert delivery_result["source_system"] == SOURCE_SYSTEM
    assert delivery_result["source_record_id"] == SOURCE_RECORD_ID
    assert result["data"]["pod_status_transition"] == "completed"

    assert len(invoice_recorder.calls) == 1
    invoice_call = invoice_recorder.calls[0]
    expected_subtotal = line_subtotal_cents(
        ACTUAL_GALLONS,
        UNIT_PRICE_MICROS,
    )
    expected_tax = round(
        ORDER_TAX_CENTS * ACTUAL_GALLONS / REQUESTED_GALLONS
    )
    assert invoice_call["line_items"] == [
        {
            "product_code": PRODUCT_CODE,
            "quantity_gallons": ACTUAL_GALLONS,
            "unit_price_cents": legacy_unit_price_cents(
                UNIT_PRICE_MICROS
            ),
            "unit_price_micros": UNIT_PRICE_MICROS,
            "subtotal_cents": expected_subtotal,
        }
    ]
    assert invoice_call["tax_cents"] == expected_tax
    assert invoice_call["delivery_result"] == delivery_result

    invoice = {
        "invoice_id": "inv-real-world-104882",
        "invoice_number": "INV-104882",
        "tenant_id": TENANT_ID,
        "order_id": ORDER_ID,
        "customer_id": order["customer_id"],
        "account_id": order["account_id"],
        "status": "finalized",
        "delivered_at": delivery_result["delivered_at"],
        "line_items": invoice_call["line_items"],
        "subtotal_cents": expected_subtotal,
        "tax_cents": expected_tax,
        "total_cents": expected_subtotal + expected_tax,
        "external_refs": {
            "source_system": SOURCE_SYSTEM,
            "source_record_id": SOURCE_RECORD_ID,
        },
    }
    qbo_connector = SimpleNamespace(
        sync_push=AsyncMock(
            return_value=SimpleNamespace(
                status="success",
                run_id="sync-qbo-104882",
                record_counts={"invoices_pushed": 1},
                result_metadata={"external_invoice_id": "98431"},
            )
        )
    )
    invoice_es = SimpleNamespace(update_document=AsyncMock())
    external_sync = CommerceExternalSync(
        qbo_connector=qbo_connector,
        stripe_connector=None,
        invoice_service=SimpleNamespace(_es=invoice_es),
        payment_service=AsyncMock(),
    )

    with patch(
        "commerce.services.commerce_persistence_bridge.mirror_invoice_fields",
        new=AsyncMock(),
    ):
        await external_sync.on_invoice_finalized(invoice)

    qbo_payload = qbo_connector.sync_push.await_args.args[0]
    assert qbo_payload["reconciliation_id"] == ORDER_ID
    assert qbo_payload["delivered_gallons"] == ACTUAL_GALLONS
    assert qbo_payload["subtotal_cents"] == expected_subtotal
    assert qbo_payload["tax_cents"] == expected_tax
    assert qbo_payload["external_refs"]["source_record_id"] == SOURCE_RECORD_ID
    assert qbo_payload["line_items"] == invoice_call["line_items"]
    assert qbo_payload["unit_price_usd"] == float(
        unit_price_usd(UNIT_PRICE_MICROS)
    )

    qbo_body = _build_invoice_body_from_canonical(qbo_payload)
    qbo_line = qbo_body["Line"][0]
    assert qbo_line["Amount"] == expected_subtotal / 100
    assert qbo_line["SalesItemLineDetail"]["Qty"] == ACTUAL_GALLONS
    assert qbo_line["SalesItemLineDetail"]["UnitPrice"] == float(
        unit_price_usd(UNIT_PRICE_MICROS)
    )

    update = invoice_es.update_document.await_args.args[2]
    assert update["qbo_push_state"] == "pushed"
    assert update["external_refs"]["qbo"] == "inv:98431"


@pytest.mark.asyncio
async def test_public_joliet_invoice_preserves_fractional_cent_price(
    monkeypatch,
):
    fixture_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "pod_erp_public_records"
        / "city_of_joliet_w1736698.json"
    )
    fixture = json.loads(fixture_path.read_text())
    transaction = fixture["transaction"]

    # Keep POD media, driver, and QBO provider IDs as test doubles; replace
    # every material commercial value with the published transaction.
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "TENANT_ID", "tenant_city_joliet")
    monkeypatch.setattr(module, "ORDER_ID", transaction["source_record_id"])
    monkeypatch.setattr(
        module,
        "REQUESTED_GALLONS",
        transaction["delivered_gallons"],
    )
    monkeypatch.setattr(
        module,
        "ACTUAL_GALLONS",
        transaction["delivered_gallons"],
    )
    monkeypatch.setattr(
        module,
        "UNIT_PRICE_MICROS",
        transaction["unit_price_micros"],
    )
    monkeypatch.setattr(module, "ORDER_TAX_CENTS", 0)
    monkeypatch.setattr(
        module,
        "PRODUCT_CODE",
        transaction["product_code"],
    )
    monkeypatch.setattr(
        module,
        "SOURCE_SYSTEM",
        transaction["source_system"],
    )
    monkeypatch.setattr(
        module,
        "SOURCE_RECORD_ID",
        transaction["source_record_id"],
    )

    assert line_subtotal_cents(
        transaction["delivered_gallons"],
        transaction["unit_price_micros"],
    ) == transaction["invoice_total_cents"]

    await test_short_delivery_uses_pod_gallons_in_invoice_and_qbo_payload()
