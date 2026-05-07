"""
Unit tests for BOLService — BOL PDF render, upload, and persistence.

Covers Requirements 4.3.1 (BOL_Service.generate returns BOLDocument), 4.3.2
(PDF contents include tenant logo, BOL number, product + fuel_grade, gross
gallons, origin depot, destination, driver + CDL, truck/compartments,
timestamps, shipper + recipient signatures), and 4.3.3 (PDF uploaded via
FileStorageService with category "bol" and record persisted to the
``bill_of_lading`` ES index with hash).

``FileStorageService`` and the ES client are mocked — no boto3 / S3 /
Elasticsearch calls are made. PDF bytes are verified by opening them with
``pypdf`` and by asserting the reportlab canvas invocation order.
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from services.bol_service import (
    BILL_OF_LADING_INDEX,
    BOLDocument,
    BOLFields,
    BOLRenderInputs,
    BOLService,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeFileStorage:
    """Mimic the subset of :class:`FileStorageService` that BOLService calls."""

    def __init__(self) -> None:
        self.put_calls: List[Dict[str, Any]] = []
        self._next_ref: Optional[str] = None

    def put(
        self,
        *,
        tenant_id: str,
        category: str,
        content_bytes: bytes,
        content_type: str,
        actor: Optional[str] = None,
    ) -> str:
        self.put_calls.append(
            {
                "tenant_id": tenant_id,
                "category": category,
                "content_bytes": content_bytes,
                "content_type": content_type,
                "actor": actor,
            }
        )
        if self._next_ref is not None:
            return self._next_ref
        return f"tenants/{tenant_id}/{category}/2025/01/15/fake-ref.pdf"

    def set_next_ref(self, ref: str) -> None:
        self._next_ref = ref


class _FakeES:
    """Minimal ES stub capturing ``index_document`` calls."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Dict[str, Any]] = []

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any]):
        self.calls.append({"index": index, "doc_id": doc_id, "document": dict(document)})
        self.docs[doc_id] = dict(document)
        return {"result": "created"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inputs(**overrides: Any) -> BOLRenderInputs:
    """Build a complete :class:`BOLRenderInputs` with optional overrides."""
    base: Dict[str, Any] = {
        "tenant_id": "tenant-a",
        "tenant_name": "Acme Fuel Co.",
        "tenant_logo_bytes": None,
        "pod": {
            "pod_id": "pod-0001",
            "order_id": "order-1",
            "delivered_gallons": 742.5,
            "delivered_at": datetime(2025, 1, 15, 14, 30, tzinfo=timezone.utc),
            "signature_url": "tenants/tenant-a/signature/2025/01/15/abc.png",
            "shipper_signature_ref": "tenants/tenant-a/signature/2025/01/15/ship.png",
        },
        "order": {
            "order_id": "order-1",
            "product_name": "Diesel #2",
            "product_code": "DIESEL_2",
            "fuel_grade": "DIESEL_2",
            "loaded_at": datetime(2025, 1, 15, 8, 0, tzinfo=timezone.utc),
            "departed_at": datetime(2025, 1, 15, 9, 15, tzinfo=timezone.utc),
            "arrived_at": datetime(2025, 1, 15, 14, 10, tzinfo=timezone.utc),
        },
        "depot": {
            "depot_id": "depot-1",
            "name": "Springfield Depot",
            "address": "100 Depot Rd, Springfield, IL",
        },
        "driver": {
            "driver_id": "drv-1",
            "name": "Alex Driver",
            "cdl": "CDL-98765",
        },
        "truck": {
            "truck_id": "TRUCK-42",
            "compartments": ["c1", "c2"],
        },
        "destination": {
            "destination_id": "dest-1",
            "name": "Keep-Full Residential — 42 Oak St",
        },
    }
    base.update(overrides)
    return BOLRenderInputs(**base)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBOLServiceConstruction:
    """Guardrails on constructor validation."""

    def test_requires_file_storage(self):
        es = _FakeES()
        with pytest.raises(ValueError, match="file_storage"):
            BOLService(file_storage=None, es_service=es)

    def test_requires_es_service(self):
        fs = _FakeFileStorage()
        with pytest.raises(ValueError, match="es_service"):
            BOLService(file_storage=fs, es_service=None)


class TestBOLServiceGenerate:
    """Happy-path generate() + tenant isolation + field preservation."""

    @pytest.mark.asyncio
    async def test_generate_returns_bol_document_with_hash(self):
        fs = _FakeFileStorage()
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)

        doc = await svc.generate(tenant_id="tenant-a", inputs=_inputs())

        assert isinstance(doc, BOLDocument)
        assert doc.tenant_id == "tenant-a"
        assert doc.pod_id == "pod-0001"
        assert doc.order_id == "order-1"
        assert doc.status == "generated"
        assert doc.bol_id.startswith("bol-tenant-a-")
        # Hash is a 64-char hex sha256 over the uploaded PDF bytes.
        assert re.fullmatch(r"[0-9a-f]{64}", doc.hash)
        uploaded = fs.put_calls[0]["content_bytes"]
        assert doc.hash == hashlib.sha256(uploaded).hexdigest()

    @pytest.mark.asyncio
    async def test_generate_uploads_to_file_storage_with_bol_category(self):
        fs = _FakeFileStorage()
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)

        doc = await svc.generate(tenant_id="tenant-a", inputs=_inputs(), actor="dispatcher-1")

        # Req 4.3.3 — upload via FileStorageService under category "bol" with PDF MIME.
        assert len(fs.put_calls) == 1
        call = fs.put_calls[0]
        assert call["tenant_id"] == "tenant-a"
        assert call["category"] == "bol"
        assert call["content_type"] == "application/pdf"
        assert call["actor"] == "dispatcher-1"
        assert call["content_bytes"].startswith(b"%PDF-")
        # The file_ref returned by the FS ends up on the persisted record.
        assert doc.file_ref == "tenants/tenant-a/bol/2025/01/15/fake-ref.pdf"

    @pytest.mark.asyncio
    async def test_generate_persists_record_to_bill_of_lading_index(self):
        fs = _FakeFileStorage()
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)

        doc = await svc.generate(tenant_id="tenant-a", inputs=_inputs())

        # Req 4.3.3 — record persisted to bill_of_lading with hash.
        assert len(es.calls) == 1
        persisted = es.calls[0]
        assert persisted["index"] == BILL_OF_LADING_INDEX
        assert persisted["doc_id"] == doc.bol_id
        body = persisted["document"]
        assert body["bol_id"] == doc.bol_id
        assert body["tenant_id"] == "tenant-a"
        assert body["pod_id"] == "pod-0001"
        assert body["order_id"] == "order-1"
        assert body["file_ref"] == doc.file_ref
        assert body["hash"] == doc.hash
        assert body["status"] == "generated"
        assert "fields" in body and body["fields"]["bol_number"] == doc.fields.bol_number
        assert body["generated_at"] == doc.generated_at.isoformat()

    @pytest.mark.asyncio
    async def test_generate_preserves_input_fields_in_bol_fields(self):
        """Req 4.3.1/4.3.2 — BOLDocument fields equal the input POD/context values."""
        fs = _FakeFileStorage()
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)

        inputs = _inputs()
        doc = await svc.generate(tenant_id="tenant-a", inputs=inputs)
        fields = doc.fields

        assert fields.product_name == "Diesel #2"
        assert fields.fuel_grade == "DIESEL_2"
        assert fields.gross_gallons == pytest.approx(742.5)
        assert fields.origin_depot_name == "Springfield Depot"
        assert fields.origin_depot_address == "100 Depot Rd, Springfield, IL"
        assert fields.destination == "Keep-Full Residential — 42 Oak St"
        assert fields.driver_name == "Alex Driver"
        assert fields.driver_cdl == "CDL-98765"
        assert fields.truck_id == "TRUCK-42"
        assert fields.compartments == ["c1", "c2"]
        assert fields.loaded_at == "2025-01-15T08:00:00+00:00"
        assert fields.departed_at == "2025-01-15T09:15:00+00:00"
        assert fields.arrived_at == "2025-01-15T14:10:00+00:00"
        assert fields.delivered_at == "2025-01-15T14:30:00+00:00"
        assert fields.shipper_signature == "tenants/tenant-a/signature/2025/01/15/ship.png"
        assert fields.recipient_signature == "tenants/tenant-a/signature/2025/01/15/abc.png"
        # BOL number is tenant-scoped and references the POD id tail.
        assert fields.bol_number.startswith("BOL-tenant-a-")
        assert fields.bol_number.endswith("-pod-0001") or fields.bol_number.endswith("pod-0001")

    @pytest.mark.asyncio
    async def test_generate_uses_caller_bol_number_when_provided(self):
        fs = _FakeFileStorage()
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)

        doc = await svc.generate(
            tenant_id="tenant-a",
            inputs=_inputs(),
            bol_number="BOL-CUSTOM-001",
        )
        assert doc.fields.bol_number == "BOL-CUSTOM-001"

    @pytest.mark.asyncio
    async def test_generate_rejects_mismatched_tenant(self):
        fs = _FakeFileStorage()
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)

        with pytest.raises(ValueError, match="tenant_id"):
            await svc.generate(tenant_id="tenant-b", inputs=_inputs())
        # Neither upload nor persist should have fired on a tenant mismatch.
        assert fs.put_calls == []
        assert es.calls == []

    @pytest.mark.asyncio
    async def test_generate_requires_non_empty_tenant(self):
        fs = _FakeFileStorage()
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)

        with pytest.raises(ValueError, match="tenant_id"):
            await svc.generate(tenant_id="", inputs=_inputs(tenant_id=""))

    @pytest.mark.asyncio
    async def test_generate_requires_delivered_gallons(self):
        fs = _FakeFileStorage()
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)
        pod = {
            "pod_id": "pod-0001",
            "delivered_at": "2025-01-15T14:30:00+00:00",
        }
        with pytest.raises(ValueError, match="delivered_gallons"):
            await svc.generate(tenant_id="tenant-a", inputs=_inputs(pod=pod))
        assert fs.put_calls == []
        assert es.calls == []

    @pytest.mark.asyncio
    async def test_generate_requires_pod_id(self):
        fs = _FakeFileStorage()
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)
        pod = {
            "delivered_gallons": 100.0,
            "delivered_at": "2025-01-15T14:30:00+00:00",
        }
        with pytest.raises(ValueError, match="pod_id"):
            await svc.generate(tenant_id="tenant-a", inputs=_inputs(pod=pod))

    @pytest.mark.asyncio
    async def test_file_storage_failure_aborts_before_persist(self):
        fs = MagicMock()
        fs.put.side_effect = PermissionError("cross_tenant_file_ref")
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)

        with pytest.raises(PermissionError):
            await svc.generate(tenant_id="tenant-a", inputs=_inputs())
        assert es.calls == []


class TestBOLServicePDFContents:
    """Assertions over the rendered PDF bytes."""

    @pytest.mark.asyncio
    async def test_pdf_opens_and_contains_expected_fields(self):
        from pypdf import PdfReader

        fs = _FakeFileStorage()
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)

        doc = await svc.generate(tenant_id="tenant-a", inputs=_inputs())
        pdf_bytes = fs.put_calls[0]["content_bytes"]

        reader = PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) == 1
        text = reader.pages[0].extract_text() or ""

        # Req 4.3.2 — PDF body contains each enumerated field.
        assert "BILL OF LADING" in text
        assert "Acme Fuel Co." in text
        assert doc.fields.bol_number in text
        assert "Diesel #2" in text
        assert "DIESEL_2" in text
        assert "742.50" in text  # gross gallons formatted to 2dp
        assert "Springfield Depot" in text
        assert "100 Depot Rd" in text
        assert "Keep-Full Residential" in text
        assert "Alex Driver" in text
        assert "CDL-98765" in text
        assert "TRUCK-42" in text
        assert "c1, c2" in text
        assert "2025-01-15T08:00" in text
        assert "2025-01-15T14:30" in text
        assert "Shipper Signature" in text
        assert "Recipient Signature" in text


class TestBOLServiceRenderSequence:
    """Assert the reportlab canvas invocation order."""

    @pytest.mark.asyncio
    async def test_canvas_render_sequence(self, monkeypatch):
        """Intercept the reportlab canvas to record the call sequence.

        The rendered PDF bytes are generated by a real canvas (so upload and
        hashing still use a valid PDF), but we spy on the module-level
        ``Canvas`` factory to verify the drawString labels appear in the
        expected order.
        """
        from reportlab.pdfgen import canvas as rl_canvas_module

        recorded_strings: List[str] = []
        real_canvas_cls = rl_canvas_module.Canvas

        class _SpyCanvas(real_canvas_cls):  # type: ignore[misc]
            def drawString(self, x, y, text):  # noqa: N802 - reportlab API
                recorded_strings.append(str(text))
                return super().drawString(x, y, text)

        monkeypatch.setattr(rl_canvas_module, "Canvas", _SpyCanvas)

        fs = _FakeFileStorage()
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)
        await svc.generate(tenant_id="tenant-a", inputs=_inputs())

        # Labels are drawn in a fixed order. Assert the critical subsequence.
        label_order = [
            "Product:",
            "Fuel Grade:",
            "Gross Gallons:",
            "Origin Depot:",
            "Origin Address:",
            "Destination:",
            "Driver:",
            "CDL:",
            "Truck:",
            "Compartments:",
            "Loaded At:",
            "Departed At:",
            "Arrived At:",
            "Delivered At:",
            "Shipper Signature",
            "Recipient Signature",
        ]
        indices: List[int] = []
        search_from = 0
        for label in label_order:
            for i in range(search_from, len(recorded_strings)):
                if recorded_strings[i] == label or recorded_strings[i].startswith(label):
                    indices.append(i)
                    search_from = i + 1
                    break
            else:
                pytest.fail(
                    f"label '{label}' missing or out of order; "
                    f"recorded={recorded_strings}"
                )
        # Strictly increasing confirms correct ordering.
        assert indices == sorted(indices)


class TestBOLServiceLogo:
    """Tenant logo rendering path."""

    @pytest.mark.asyncio
    async def test_logo_bytes_rendered_without_blocking(self):
        # A 1x1 PNG — valid enough for ImageReader.
        logo_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xff\xff"
            b"\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00"
            b"IEND\xaeB`\x82"
        )
        fs = _FakeFileStorage()
        es = _FakeES()
        svc = BOLService(file_storage=fs, es_service=es)

        doc = await svc.generate(
            tenant_id="tenant-a",
            inputs=_inputs(tenant_logo_bytes=logo_png),
        )
        # The PDF is still valid and the upload + persist still fired.
        assert fs.put_calls[0]["content_bytes"].startswith(b"%PDF-")
        assert len(es.calls) == 1
        assert doc.hash == hashlib.sha256(fs.put_calls[0]["content_bytes"]).hexdigest()
