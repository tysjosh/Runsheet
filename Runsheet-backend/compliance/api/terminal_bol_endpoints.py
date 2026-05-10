"""Terminal BOL Ingestion REST endpoints for the Fuel Compliance Backbone.

Exposes endpoints for ingesting terminal Bills of Lading via EDI or manual
upload, confirming manual BOLs, linking BOLs to load plans, and listing
BOLs under the ``/api/compliance/terminal-bols`` prefix (design §10,
"REST API Endpoints (New)").

Endpoints:

* ``POST /api/compliance/terminal-bols`` — EDI ingestion endpoint
  (Req 10.1).
* ``POST /api/compliance/terminal-bols/upload`` — Manual upload endpoint
  (Req 10.2).
* ``POST /api/compliance/terminal-bols/{bol_id}/confirm`` — Confirm
  operator-reviewed fields on a pending manual BOL (Req 10.2).
* ``POST /api/compliance/terminal-bols/{bol_id}/link`` — Link a BOL to
  a load plan (Req 10.5).
* ``GET  /api/compliance/terminal-bols`` — List BOLs with pagination and
  optional filters.

Wiring pattern mirrors ``compliance/api/driver_endpoints.py``:

1. A module-level ``_bol_service`` is populated by
   :func:`configure_terminal_bol_api` at application startup (see
   ``bootstrap/compliance.py``).
2. Each handler extracts the tenant from :func:`get_tenant_context` so
   all queries are tenant-scoped (Constraint C3).
3. ``AppException`` errors raised by the service layer are propagated
   to the global exception handler registered in ``main.py``.

Validates: Requirements 10.1, 10.2, 10.5
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from pydantic import BaseModel, ConfigDict, Field

from compliance.services.compliance_es_mappings import TERMINAL_BOLS_INDEX
from compliance.services.terminal_bol_ingestion_service import (
    TerminalBOLIngestionService,
)
from errors.exceptions import AppException
from ops.middleware.tenant_guard import (
    TenantContext,
    get_tenant_context,
    inject_tenant_filter,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_terminal_bol_api()
# ---------------------------------------------------------------------------

_bol_service: Optional[TerminalBOLIngestionService] = None
_es_service: Optional[Any] = None

router = APIRouter(
    prefix="/api/compliance/terminal-bols", tags=["compliance-terminal-bols"]
)


def configure_terminal_bol_api(
    *,
    bol_service: TerminalBOLIngestionService,
    es_service: Any,
) -> None:
    """Wire the TerminalBOLIngestionService into this module.

    Called once during application startup (``bootstrap/compliance.py``)
    so that per-request handlers can delegate to the service without
    taking a hard import dependency on the container.

    Args:
        bol_service: The application-scoped TerminalBOLIngestionService
            instance.
        es_service: The application-scoped Elasticsearch service for
            direct queries (listing BOLs).
    """
    global _bol_service, _es_service
    _bol_service = bol_service
    _es_service = es_service


def _get_bol_service() -> TerminalBOLIngestionService:
    """Return the configured TerminalBOLIngestionService or raise."""
    if _bol_service is None:
        raise RuntimeError(
            "Terminal BOL API not configured. "
            "Call configure_terminal_bol_api() during startup."
        )
    return _bol_service


def _get_es_service() -> Any:
    """Return the configured Elasticsearch service or raise."""
    if _es_service is None:
        raise RuntimeError(
            "Terminal BOL API not configured. "
            "Call configure_terminal_bol_api() during startup."
        )
    return _es_service


def _get_request_id(request: Request) -> str:
    """Extract the RequestIDMiddleware-assigned id, with a safe default."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class BOLConfirmRequest(BaseModel):
    """Body for ``POST /api/compliance/terminal-bols/{bol_id}/confirm``.

    Operator-confirmed fields for a pending manual BOL (Req 10.2).
    All fields are optional — only provided fields are applied.
    """

    model_config = ConfigDict(extra="forbid")

    load_number: Optional[str] = Field(
        default=None,
        description="Unique load identifier assigned by the terminal.",
    )
    product_code: Optional[str] = Field(
        default=None,
        description="Fuel product code (e.g., UNL87, ULSD, PROP).",
    )
    gross_gallons: Optional[float] = Field(
        default=None,
        gt=0,
        description="Gross gallons loaded at observed temperature.",
    )
    net_gallons: Optional[float] = Field(
        default=None,
        gt=0,
        description="Net gallons corrected to 60°F via VCF.",
    )
    observed_temperature_f: Optional[float] = Field(
        default=None,
        description="Observed temperature at loading in degrees Fahrenheit.",
    )
    api_gravity: Optional[float] = Field(
        default=None,
        description="API gravity of the product at loading.",
    )
    supplier_name: Optional[str] = Field(
        default=None,
        description="Name of the fuel supplier at the terminal.",
    )
    terminal_name: Optional[str] = Field(
        default=None,
        description="Name of the loading terminal (rack).",
    )
    driver_id: Optional[str] = Field(
        default=None,
        description="Identifier of the driver who loaded the product.",
    )
    timestamp: Optional[datetime] = Field(
        default=None,
        description="Timestamp when the BOL was issued at the terminal.",
    )


class BOLLinkRequest(BaseModel):
    """Body for ``POST /api/compliance/terminal-bols/{bol_id}/link``.

    Links a BOL to a load plan for chain-of-custody traceability (Req 10.5).
    """

    model_config = ConfigDict(extra="forbid")

    load_plan_id: str = Field(
        ...,
        description="The load plan identifier to link to this BOL.",
    )


# ---------------------------------------------------------------------------
# POST /api/compliance/terminal-bols (EDI ingestion)
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def ingest_edi(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Ingest a terminal BOL from a raw EDI payload.

    Accepts the raw EDI payload as bytes in the request body. The service
    auto-detects the EDI format (X12 856 or pipe-delimited) and parses
    all required fields.

    Returns the created :class:`TerminalBOL` record with status
    ``ingested``.

    Validates: Requirement 10.1
    """
    svc = _get_bol_service()

    # Read raw bytes from the request body (EDI payloads are not JSON)
    edi_payload = await request.body()

    if not edi_payload:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "terminal_bols.empty_payload",
                "message": "EDI payload must not be empty.",
            },
        )

    try:
        bol = await svc.ingest_edi(edi_payload, tenant.tenant_id)
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "terminal_bols.invalid_edi",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "terminal_bols.ingest_edi: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "terminal_bols.ingest_edi_failed",
                "message": "Failed to ingest EDI payload.",
            },
        )

    logger.info(
        "terminal_bols.ingest_edi: tenant=%s bol=%s load_number=%s",
        tenant.tenant_id,
        bol.bol_id,
        bol.load_number,
    )

    return {
        "data": bol.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/compliance/terminal-bols/upload (manual upload)
# ---------------------------------------------------------------------------


@router.post("/upload", status_code=201)
async def upload_manual_bol(
    request: Request,
    file: UploadFile = File(..., description="PDF or image file of the BOL."),
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Upload a terminal BOL manually (PDF or image).

    Accepts a file upload (PDF, JPEG, or PNG). The service stores the
    raw document and creates a BOL record with status
    ``pending_confirmation``. The operator must confirm the extracted
    fields via the ``/confirm`` endpoint before the BOL transitions to
    ``ingested`` status.

    Validates: Requirement 10.2
    """
    svc = _get_bol_service()

    # Determine content type from the uploaded file
    content_type = file.content_type or "application/octet-stream"

    # Read file bytes
    file_bytes = await file.read()

    if not file_bytes:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "terminal_bols.empty_file",
                "message": "Uploaded file must not be empty.",
            },
        )

    try:
        bol = await svc.ingest_manual(file_bytes, content_type, tenant.tenant_id)
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "terminal_bols.invalid_upload",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "terminal_bols.upload: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "terminal_bols.upload_failed",
                "message": "Failed to process manual BOL upload.",
            },
        )

    logger.info(
        "terminal_bols.upload: tenant=%s bol=%s content_type=%s",
        tenant.tenant_id,
        bol.bol_id,
        content_type,
    )

    return {
        "data": bol.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/compliance/terminal-bols/{bol_id}/confirm
# ---------------------------------------------------------------------------


@router.post("/{bol_id}/confirm")
async def confirm_manual_bol(
    request: Request,
    bol_id: str,
    body: BOLConfirmRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Confirm operator-reviewed fields on a pending manual BOL.

    Accepts the confirmed field values and transitions the BOL from
    ``pending_confirmation`` to ``ingested`` status. Only fields
    provided in the request body are applied.

    Validates: Requirement 10.2
    """
    svc = _get_bol_service()

    # Build confirmed_fields dict from non-None body fields
    confirmed_fields: Dict[str, Any] = {}
    for field_name, value in body.model_dump(exclude_none=True).items():
        confirmed_fields[field_name] = value

    # Convert datetime to ISO string for ES storage if present
    if "timestamp" in confirmed_fields and isinstance(
        confirmed_fields["timestamp"], datetime
    ):
        confirmed_fields["timestamp"] = confirmed_fields[
            "timestamp"
        ].isoformat()

    try:
        bol = await svc.confirm_manual_bol(
            tenant.tenant_id, bol_id, confirmed_fields
        )
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "terminal_bols.invalid_confirm",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "terminal_bols.confirm: unexpected error for tenant=%s bol=%s: %s",
            tenant.tenant_id,
            bol_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "terminal_bols.confirm_failed",
                "message": "Failed to confirm manual BOL.",
            },
        )

    logger.info(
        "terminal_bols.confirm: tenant=%s bol=%s load_number=%s",
        tenant.tenant_id,
        bol.bol_id,
        bol.load_number,
    )

    return {
        "data": bol.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/compliance/terminal-bols/{bol_id}/link
# ---------------------------------------------------------------------------


@router.post("/{bol_id}/link")
async def link_bol_to_load_plan(
    request: Request,
    bol_id: str,
    body: BOLLinkRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Link a BOL to a load plan for chain-of-custody traceability.

    Transitions the BOL status from ``ingested`` to ``linked`` and
    records the load_plan_id association.

    Validates: Requirement 10.5
    """
    svc = _get_bol_service()

    try:
        await svc.link_to_load_plan(
            bol_id, body.load_plan_id, tenant_id=tenant.tenant_id
        )
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "terminal_bols.invalid_link",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "terminal_bols.link: unexpected error for tenant=%s bol=%s: %s",
            tenant.tenant_id,
            bol_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "terminal_bols.link_failed",
                "message": "Failed to link BOL to load plan.",
            },
        )

    logger.info(
        "terminal_bols.link: tenant=%s bol=%s load_plan=%s",
        tenant.tenant_id,
        bol_id,
        body.load_plan_id,
    )

    return {
        "data": {
            "bol_id": bol_id,
            "load_plan_id": body.load_plan_id,
            "status": "linked",
        },
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/terminal-bols
# ---------------------------------------------------------------------------


@router.get("")
async def list_terminal_bols(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    status: Optional[str] = Query(
        default=None,
        description=(
            "Filter by BOL status: ingested, linked, verified, or "
            "pending_confirmation."
        ),
    ),
    driver_id: Optional[str] = Query(
        default=None,
        description="Filter by driver ID.",
    ),
    product_code: Optional[str] = Query(
        default=None,
        description="Filter by product code.",
    ),
    load_number: Optional[str] = Query(
        default=None,
        description="Filter by load number (exact match).",
    ),
    cursor: Optional[str] = Query(
        default=None,
        description=(
            "Cursor for keyset pagination — the bol_id of the last "
            "item on the previous page."
        ),
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Page size (max 200).",
    ),
) -> Dict[str, Any]:
    """List terminal BOLs for the tenant with pagination and optional filters.

    Supports filtering by status, driver_id, product_code, and
    load_number. Results are ordered by created_at descending (most
    recent first).
    """
    es = _get_es_service()

    filters: List[Dict[str, Any]] = []
    if status is not None:
        filters.append({"term": {"status": status.strip()}})
    if driver_id is not None:
        filters.append({"term": {"driver_id": driver_id.strip()}})
    if product_code is not None:
        filters.append({"term": {"product_code": product_code.strip()}})
    if load_number is not None:
        filters.append({"term": {"load_number": load_number.strip()}})

    # Keyset pagination: if cursor is provided, only return BOLs with
    # bol_id lexicographically after the cursor.
    if cursor is not None:
        filters.append({"range": {"bol_id": {"gt": cursor.strip()}}})

    base_query: Dict[str, Any] = {
        "query": (
            {"bool": {"filter": filters}} if filters else {"match_all": {}}
        ),
        "sort": [{"created_at": {"order": "desc"}}],
        "size": limit,
    }
    query = inject_tenant_filter(base_query, tenant.tenant_id)

    try:
        response = await es.search_documents(
            TERMINAL_BOLS_INDEX, query, size=limit
        )
    except Exception as exc:
        logger.error(
            "terminal_bols.list: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "terminal_bols.list_failed",
                "message": "Failed to list terminal BOLs.",
            },
        )

    hits = ((response or {}).get("hits") or {}).get("hits") or []
    items: List[Dict[str, Any]] = []
    for hit in hits:
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict):
            continue
        items.append(source)

    # Determine next_cursor from the last item's bol_id
    next_cursor: Optional[str] = None
    if items and len(items) == limit:
        next_cursor = items[-1].get("bol_id")

    return {
        "data": items,
        "next_cursor": next_cursor,
        "limit": limit,
        "count": len(items),
        "request_id": _get_request_id(request),
    }


__all__ = [
    "configure_terminal_bol_api",
    "router",
    "BOLConfirmRequest",
    "BOLLinkRequest",
]
