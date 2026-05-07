"""
BOL Service — Bill-of-Lading PDF generation, upload, and persistence.

Implements the BOL generator required by Capability 4 (POD + Reconciliation).
Given a finalized POD record and its surrounding context (order, depot, driver,
truck, tenant), the service:

    1. Renders a single-page BOL PDF with ``reportlab`` containing the tenant
       logo, a tenant-unique BOL number, the product name and fuel_grade, the
       gross gallons delivered, the origin Depot name and address, the
       destination (customer_tank or station), driver name + CDL number, the
       truck ID and compartments used, timestamps (loaded_at / departed_at /
       arrived_at / delivered_at), and shipper + recipient signatures.
    2. Uploads the PDF bytes via :class:`services.file_storage_service.FileStorageService`
       under category ``"bol"`` and captures the returned file_ref.
    3. Computes a SHA-256 hash over the raw PDF bytes (tamper-evident reference
       that the reconciliation pipeline and the Hash_Chain verifier can cross-
       check against the stored artifact).
    4. Persists a ``BOLDocument`` record to the ``bill_of_lading`` ES index
       with tenant_id, bol_id, pod_id, file_ref, generated_at, hash, status,
       and the human-readable ``fields`` payload so the document can be
       reconstructed without re-parsing the PDF.

All IO is tenant-scoped: the injected ``FileStorageService`` prefixes S3 keys
with ``tenants/{tenant_id}/bol/...`` and the persisted ES doc carries
``tenant_id``. The ES persister never reads cross-tenant state.

The render-first-then-upload ordering is deliberate — if rendering fails the
caller can log a failure without creating an S3 object, and if S3 upload fails
we never persist a BOL record (the caller marks the BOL ``pending_regeneration``
at the POD layer, Requirement 4.3.5).

Validates:
    * Requirement 4.3.1 — BOL_Service.generate returns a BOLDocument with the
      listed fields.
    * Requirement 4.3.2 — PDF content includes the enumerated fields.
    * Requirement 4.3.3 — PDF uploaded via File_Storage_Service with category
      "bol" and record persisted to the ``bill_of_lading`` ES index with hash.
"""
from __future__ import annotations

import hashlib
import io
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

from fuel.services.fuel_ops_es_mappings import BILL_OF_LADING_INDEX

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class BOLFields(BaseModel):
    """Human-readable BOL payload persisted alongside the PDF file_ref.

    These fields mirror the PDF body 1:1 so downstream consumers (audit UIs,
    reconciliation, legal discovery) do not need to re-parse the PDF.
    """

    bol_number: str
    product_name: str
    fuel_grade: Optional[str] = None
    gross_gallons: float
    origin_depot_name: str
    origin_depot_address: str
    destination: str
    driver_name: str
    driver_cdl: Optional[str] = None
    truck_id: str
    compartments: List[str] = Field(default_factory=list)
    loaded_at: Optional[str] = None
    departed_at: Optional[str] = None
    arrived_at: Optional[str] = None
    delivered_at: str
    shipper_signature: Optional[str] = None
    recipient_signature: Optional[str] = None


class BOLDocument(BaseModel):
    """Persisted BOL record written to the ``bill_of_lading`` ES index.

    ``status`` defaults to ``generated``; the POD finalizer flips it to
    ``pending_regeneration`` if this service raises before persistence
    (Req 4.3.5 — BOL failure must not block POD persistence).
    """

    bol_id: str
    tenant_id: str
    pod_id: str
    order_id: Optional[str] = None
    file_ref: str
    hash: str
    status: str = "generated"
    fields: BOLFields
    generated_at: datetime


@dataclass(frozen=True)
class BOLRenderInputs:
    """Plain container for the data needed to render a BOL PDF.

    Kept separate from the Pydantic :class:`BOLFields` so the caller can pass
    nested objects (POD, order, depot, driver, truck, tenant) without having
    to flatten them first; :meth:`BOLService.generate` does the flattening.
    """

    tenant_id: str
    tenant_name: str
    tenant_logo_bytes: Optional[bytes]
    pod: Mapping[str, Any]
    order: Mapping[str, Any]
    depot: Mapping[str, Any]
    driver: Mapping[str, Any]
    truck: Mapping[str, Any]
    destination: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class BOLService:
    """Render, upload, and persist a Bill of Lading for a finalized POD.

    Args:
        file_storage: :class:`FileStorageService` used to upload the rendered
            PDF under the tenant-scoped ``bol`` category.
        es_service: ElasticsearchService-compatible instance used to persist
            the BOL record to ``bill_of_lading``.

    The service is intentionally lightweight: it does not cache, does not
    retry, and does not reach outside the two collaborators above. Retries
    and caching are handled at the caller (Req 4.3.5).
    """

    #: ES index for persisted BOL records.
    INDEX = BILL_OF_LADING_INDEX

    def __init__(self, file_storage: Any, es_service: Any) -> None:
        if file_storage is None:
            raise ValueError("file_storage must be provided")
        if es_service is None:
            raise ValueError("es_service must be provided")
        self._fs = file_storage
        self._es = es_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate(
        self,
        tenant_id: str,
        inputs: BOLRenderInputs,
        *,
        bol_number: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> BOLDocument:
        """Render + upload + persist a BOL for the given POD context.

        Args:
            tenant_id: Owning tenant id. Must match ``inputs.tenant_id``.
            inputs: Collected POD/order/depot/driver/truck data.
            bol_number: Optional pre-assigned BOL number. Generated when
                absent using a millisecond-precision, tenant-scoped format.
            actor: Optional actor id forwarded to the FileStorageService
                audit log.

        Returns:
            The persisted :class:`BOLDocument`.

        Raises:
            ValueError: If ``tenant_id`` is blank or inputs are missing a
                required field (pod_id, delivered_at, delivered_gallons, etc).
            PermissionError: Propagated from FileStorageService when inputs
                reference a cross-tenant file_ref.
        """
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if inputs.tenant_id != tenant_id:
            raise ValueError("inputs.tenant_id must match tenant_id")

        # Flatten POD + surrounding context into the persisted field set.
        bol_fields = self._build_fields(inputs, bol_number=bol_number)

        # Render the PDF first so we never leave a stranded S3 object on a
        # render failure.
        pdf_bytes = self._render_pdf(inputs, bol_fields)

        file_ref = self._fs.put(
            tenant_id=tenant_id,
            category="bol",
            content_bytes=pdf_bytes,
            content_type="application/pdf",
            actor=actor,
        )

        bol_id = self._build_bol_id(tenant_id)
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        generated_at = _utcnow()

        doc = BOLDocument(
            bol_id=bol_id,
            tenant_id=tenant_id,
            pod_id=str(inputs.pod.get("pod_id") or ""),
            order_id=(str(inputs.order.get("order_id"))
                      if inputs.order.get("order_id") is not None else None),
            file_ref=file_ref,
            hash=digest,
            status="generated",
            fields=bol_fields,
            generated_at=generated_at,
        )

        await self._persist(doc)
        logger.info(
            "BOL generated tenant_id=%s pod_id=%s bol_id=%s file_ref=%s",
            tenant_id, doc.pod_id, bol_id, file_ref,
        )
        return doc

    # ------------------------------------------------------------------
    # Field assembly
    # ------------------------------------------------------------------

    def _build_fields(
        self,
        inputs: BOLRenderInputs,
        *,
        bol_number: Optional[str],
    ) -> BOLFields:
        pod = inputs.pod or {}
        order = inputs.order or {}
        depot = inputs.depot or {}
        driver = inputs.driver or {}
        truck = inputs.truck or {}
        destination = inputs.destination or {}

        pod_id = pod.get("pod_id")
        if not pod_id:
            raise ValueError("pod.pod_id is required")

        delivered_gallons = pod.get("delivered_gallons")
        if delivered_gallons is None:
            # Accept legacy "gross_gallons" on POD for forward-compat.
            delivered_gallons = pod.get("gross_gallons")
        if delivered_gallons is None:
            raise ValueError("pod.delivered_gallons is required")

        delivered_at = pod.get("delivered_at") or pod.get("timestamp")
        if not delivered_at:
            raise ValueError("pod.delivered_at is required")

        # Product + fuel_grade
        product_name = (
            order.get("product_name")
            or order.get("product_code")
            or pod.get("product_name")
            or pod.get("product_code")
            or "UNKNOWN"
        )
        fuel_grade = order.get("fuel_grade") or pod.get("fuel_grade")

        # Origin depot
        depot_name = depot.get("name") or depot.get("depot_name") or "UNKNOWN_DEPOT"
        depot_address = depot.get("address") or ""

        # Destination description — collapse a customer_tank or station to a
        # single human-readable string.
        destination_desc = (
            destination.get("name")
            or destination.get("customer_tank_id")
            or destination.get("station_id")
            or destination.get("destination_id")
            or order.get("destination_name")
            or "UNKNOWN_DESTINATION"
        )

        # Driver
        driver_name = (
            driver.get("name")
            or driver.get("driver_name")
            or pod.get("driver_name")
            or "UNKNOWN_DRIVER"
        )
        driver_cdl = driver.get("cdl") or driver.get("cdl_number")

        # Truck
        truck_id = (
            truck.get("truck_id")
            or truck.get("id")
            or order.get("truck_id")
            or "UNKNOWN_TRUCK"
        )
        compartments = list(
            truck.get("compartments_used")
            or truck.get("compartments")
            or order.get("compartments_used")
            or []
        )
        compartments = [str(c) for c in compartments]

        return BOLFields(
            bol_number=bol_number or self._build_bol_number(inputs.tenant_id, pod_id),
            product_name=str(product_name),
            fuel_grade=str(fuel_grade) if fuel_grade is not None else None,
            gross_gallons=float(delivered_gallons),
            origin_depot_name=str(depot_name),
            origin_depot_address=str(depot_address),
            destination=str(destination_desc),
            driver_name=str(driver_name),
            driver_cdl=str(driver_cdl) if driver_cdl is not None else None,
            truck_id=str(truck_id),
            compartments=compartments,
            loaded_at=_maybe_iso(order.get("loaded_at") or pod.get("loaded_at")),
            departed_at=_maybe_iso(order.get("departed_at") or pod.get("departed_at")),
            arrived_at=_maybe_iso(order.get("arrived_at") or pod.get("arrived_at")),
            delivered_at=_maybe_iso(delivered_at) or str(delivered_at),
            shipper_signature=pod.get("shipper_signature_ref") or pod.get("shipper_signature"),
            recipient_signature=(
                pod.get("recipient_signature_ref")
                or pod.get("signature_ref")
                or pod.get("signature_url")
            ),
        )

    # ------------------------------------------------------------------
    # PDF rendering (reportlab)
    # ------------------------------------------------------------------

    def _render_pdf(self, inputs: BOLRenderInputs, fields: BOLFields) -> bytes:
        """Render a single-page PDF and return the raw bytes.

        Uses the low-level ``reportlab`` canvas API so tests can assert the
        call sequence directly (see ``test_bol_service.test_canvas_render_sequence``).
        """
        # Imported lazily so modules that don't need BOL rendering can avoid
        # the reportlab import cost.
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas as rl_canvas

        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=letter)
        page_width, page_height = letter

        # --- Tenant logo (optional) ---
        if inputs.tenant_logo_bytes:
            try:
                logo = ImageReader(io.BytesIO(inputs.tenant_logo_bytes))
                c.drawImage(
                    logo,
                    x=40,
                    y=page_height - 100,
                    width=80,
                    height=60,
                    preserveAspectRatio=True,
                    mask="auto",
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("BOL logo render failed: %s", exc)

        # --- Header ---
        c.setFont("Helvetica-Bold", 16)
        c.drawString(140, page_height - 60, "BILL OF LADING")
        c.setFont("Helvetica", 10)
        c.drawString(140, page_height - 78, f"Tenant: {inputs.tenant_name}")
        c.drawString(140, page_height - 92, f"BOL Number: {fields.bol_number}")

        # --- Body: key/value lines. The order of drawString calls is the
        #     contract asserted by ``test_canvas_render_sequence``.
        y = page_height - 140
        line_gap = 16

        def line(label: str, value: Any) -> None:
            nonlocal y
            c.setFont("Helvetica-Bold", 10)
            c.drawString(40, y, f"{label}:")
            c.setFont("Helvetica", 10)
            c.drawString(180, y, str(value) if value is not None else "")
            y -= line_gap

        line("Product", fields.product_name)
        line("Fuel Grade", fields.fuel_grade or "")
        line("Gross Gallons", f"{fields.gross_gallons:.2f}")
        line("Origin Depot", fields.origin_depot_name)
        line("Origin Address", fields.origin_depot_address)
        line("Destination", fields.destination)
        line("Driver", fields.driver_name)
        line("CDL", fields.driver_cdl or "")
        line("Truck", fields.truck_id)
        line("Compartments", ", ".join(fields.compartments))
        line("Loaded At", fields.loaded_at or "")
        line("Departed At", fields.departed_at or "")
        line("Arrived At", fields.arrived_at or "")
        line("Delivered At", fields.delivered_at)

        # --- Signatures block ---
        y -= line_gap
        c.setFont("Helvetica-Bold", 11)
        c.drawString(40, y, "Shipper Signature")
        c.drawString(320, y, "Recipient Signature")
        y -= line_gap
        c.setFont("Helvetica", 9)
        c.drawString(40, y, fields.shipper_signature or "[on file]")
        c.drawString(320, y, fields.recipient_signature or "[on file]")

        c.showPage()
        c.save()
        return buf.getvalue()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist(self, doc: BOLDocument) -> None:
        """Write the BOL record to the ``bill_of_lading`` ES index."""
        payload: Dict[str, Any] = {
            "bol_id": doc.bol_id,
            "tenant_id": doc.tenant_id,
            "pod_id": doc.pod_id,
            "order_id": doc.order_id,
            "file_ref": doc.file_ref,
            "hash": doc.hash,
            "status": doc.status,
            "fields": doc.fields.model_dump(),
            "generated_at": doc.generated_at.isoformat(),
            "created_at": doc.generated_at.isoformat(),
            "updated_at": doc.generated_at.isoformat(),
        }
        await self._es.index_document(self.INDEX, doc.bol_id, payload)

    # ------------------------------------------------------------------
    # Identifiers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_bol_id(tenant_id: str) -> str:
        # Unique identifier for the ES doc. Includes tenant_id so operators
        # can scan by prefix.
        return f"bol-{tenant_id}-{uuid.uuid4()}"

    @staticmethod
    def _build_bol_number(tenant_id: str, pod_id: str) -> str:
        """Generate a tenant-unique, human-readable BOL number.

        Format: ``BOL-{tenant_slug}-{yyyyMMddHHMMSS}-{short_pod}``. Uses UTC
        to keep the ordering monotonic across tenants.
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        slug = tenant_id.replace("/", "_")[:16]
        short_pod = str(pod_id)[-8:] if pod_id else format(int(time.time_ns()) % 0xFFFFFFFF, "08x")
        return f"BOL-{slug}-{ts}-{short_pod}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _maybe_iso(value: Any) -> Optional[str]:
    """Return ``value`` serialized as an ISO-8601 string when possible.

    Accepts ``datetime``, ``date``, or strings. Strings are returned verbatim
    so callers that already pass an ISO string don't incur re-parse errors.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # pragma: no cover - defensive
            return str(value)
    return str(value)
