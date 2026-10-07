"""
Inline endpoints extracted from main.py to keep it under 200 lines.

Contains: chat, upload, and location endpoints plus CSV helpers.
"""
import csv
import io
import json
import logging
import os
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from errors.codes import ErrorCode
from errors.exceptions import (
    AppException,
    internal_error,
    resource_not_found,
    validation_error,
)
from ops.middleware.tenant_guard import (
    TenantContext,
    get_tenant_context,
    inject_tenant_filter,
)
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

router = APIRouter()

# Auth policy declaration for this router (Req 5.2)
# Default: JWT_REQUIRED for chat/upload endpoints; PUBLIC for health
# Public-route exceptions are enumerated in
# middleware.auth_enforcement.PUBLIC_ROUTE_ALLOWLIST
ROUTER_AUTH_POLICY = "jwt_required"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10000)
    mode: str = Field(default="chat", pattern=r"^(chat|command|analysis)$")
    session_id: Optional[str] = Field(default=None, max_length=128)

class ClearChatRequest(BaseModel):
    session_id: Optional[str] = Field(default=None, max_length=128)

VALID_DATA_TYPES = {"trucks", "fleet", "orders", "inventory", "support_tickets", "support"}

class TemporalUploadRequest(BaseModel):
    data_type: str = Field(..., min_length=1, max_length=50)
    batch_id: str = Field(..., min_length=1, max_length=128)
    operational_time: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    sheets_url: str = None

class SelectiveUploadRequest(BaseModel):
    batch_id: str = Field(..., min_length=1, max_length=128)
    operational_time: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    data_types: list[str] = Field(..., min_length=1)


def _container(request: Request):
    return request.app.state.container


# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------

@router.post("/api/chat")
async def chat_endpoint(
    request: ChatRequest,
    http_request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    from Agents.llm_errors import (
        AI_SERVICE_UNAVAILABLE,
        ChatEvent,
        done_event,
        error_event,
        safe_message_for,
        tool_result_event,
    )
    from Agents.mainagent import LogisticsAgent
    from middleware.request_id import get_request_id

    # Captured here: the request-id ContextVar is not guaranteed to be set
    # while the StreamingResponse body is iterated.
    request_id = get_request_id() or None
    agent = LogisticsAgent()

    def _sse(payload: dict) -> str:
        return f"data: {json.dumps(payload, default=str)}\n\n"

    async def generate_response():
        done_sent = False
        seen_tool_uses: set = set()
        try:
            async for event in agent.chat_streaming(
                request.message,
                request.mode,
                session_id=request.session_id,
                tenant_id=tenant.tenant_id,
                request_id=request_id,
                user_id=tenant.user_id,
            ):
                if isinstance(event, ChatEvent):
                    # Already normalized (orchestrator path and legacy
                    # status/error events): forward verbatim.
                    # Not a ``break`` on done: draining lets chat_streaming
                    # finish (it records telemetry after the last event).
                    if event.get("type") == "done":
                        if done_sent:
                            continue
                        done_sent = True
                    yield _sse(event)
                elif isinstance(event, dict):
                    # Raw Strands events from the legacy direct-agent path.
                    if "error" in event:
                        logger.error("Raw error event in chat stream (request_id=%s)", request_id)
                        yield _sse(error_event(
                            AI_SERVICE_UNAVAILABLE,
                            safe_message_for(AI_SERVICE_UNAVAILABLE),
                            request_id,
                        ))
                    elif "data" in event:
                        text = event["data"]
                        if text:
                            yield _sse({'type': 'text', 'content': text})
                    elif "current_tool_use" in event:
                        # Strands repeats current_tool_use for every streamed
                        # input delta; emit one tool event per tool use, as
                        # the specialists' stream() does (OI-39).
                        tool_info = event["current_tool_use"] or {}
                        use_key = tool_info.get('toolUseId') or tool_info.get('name', '')
                        if use_key in seen_tool_uses:
                            continue
                        seen_tool_uses.add(use_key)
                        yield _sse({'type': 'tool', 'tool_name': tool_info.get('name', ''), 'tool_input': tool_info.get('input', {})})
                    elif "current_tool_result" in event:
                        # Name and status only: tool output can carry
                        # str(exc) from a failing tool (F3).
                        tool_result = event["current_tool_result"]
                        yield _sse(tool_result_event(
                            tool_result.get('name', ''),
                            tool_result.get('status', 'success'),
                        ))
                    elif event.get('event') == 'messageStop' or 'result' in event:
                        if not done_sent:
                            yield _sse(done_event())
                            done_sent = True
        except Exception:
            # Full detail stays in the server log; the client gets a code, a
            # safe message and the request id to quote (F3).
            logger.exception("Error in chat streaming (request_id=%s)", request_id)
            yield _sse(error_event(
                AI_SERVICE_UNAVAILABLE,
                safe_message_for(AI_SERVICE_UNAVAILABLE),
                request_id,
            ))
        if not done_sent:
            yield _sse(done_event())
    # Server-Sent Events, flushed per event: ``X-Accel-Buffering: no`` stops
    # nginx-style proxies from holding the stream until it ends (F6).
    return StreamingResponse(generate_response(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.post("/api/chat/fallback")
async def chat_fallback_endpoint(
    request: ChatRequest,
    http_request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    from Agents.llm_errors import AI_RATE_LIMITED, AgentServiceError
    from Agents.mainagent import LogisticsAgent
    from errors.exceptions import ai_rate_limited, ai_service_unavailable

    agent = LogisticsAgent()
    try:
        response = await agent.chat_fallback(
            request.message,
            request.mode,
            session_id=request.session_id,
            tenant_id=tenant.tenant_id,
            user_id=tenant.user_id,
        )
    except AgentServiceError as err:
        # An AI failure is an error response, not a 200 whose text is the
        # provider's error (F3). Only the safe message leaves the server.
        if err.code == AI_RATE_LIMITED:
            raise ai_rate_limited(err.safe_message, err.retry_after_seconds) from err
        details = (
            {"retry_after_seconds": err.retry_after_seconds}
            if err.retry_after_seconds is not None
            else None
        )
        raise ai_service_unavailable(err.safe_message, details) from err
    return {"response": response, "mode": request.mode, "session_id": request.session_id, "timestamp": utcnow().isoformat()}

@router.post("/api/chat/clear")
async def clear_chat_endpoint(
    request: ClearChatRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    # The clear is scoped to (caller's verified tenant, user, session_id) and
    # awaited, so it removes exactly the history the caller's next turn would
    # load and never another tenant's entry under the same session id (F1).
    # Specialists keep no history between requests, so the session store
    # entries (the legacy key and its ``orch:`` transcript, OI-17) are the
    # caller's whole history.
    #
    # A False result means the store delete failed (it is True when there was
    # nothing to clear). Reporting success then would be wrong: the history is
    # still stored and the next turn reloads it. Fail with 503 so the client
    # can retry.
    from Agents.mainagent import LogisticsAgent
    from errors.exceptions import session_store_unavailable

    cleared = await LogisticsAgent().clear_memory(
        session_id=request.session_id,
        tenant_id=tenant.tenant_id,
        user_id=tenant.user_id,
    )
    if not cleared:
        raise session_store_unavailable(
            "Chat memory could not be cleared; retry the request",
            details={"session_id": request.session_id},
        )
    return {"message": "Chat memory cleared successfully", "session_id": request.session_id}


# ---------------------------------------------------------------------------
# Upload endpoints
# ---------------------------------------------------------------------------

@router.post("/api/upload/csv")
async def upload_csv_temporal(
    file: UploadFile = File(...),
    data_type: str = Form(...),
    batch_id: str = Form(...),
    operational_time: str = Form(...),
    tenant: TenantContext = Depends(get_tenant_context),
):
    if data_type not in VALID_DATA_TYPES:
        raise validation_error(
            message=f"Invalid data_type '{data_type}'. Must be one of: {sorted(VALID_DATA_TYPES)}",
            details={"data_type": data_type, "valid_types": sorted(VALID_DATA_TYPES)},
        )
    from services.data_seeder import data_seeder
    content = await file.read()
    documents = [d for d in (convert_csv_row_to_document(row, data_type, tenant.tenant_id)
                             for row in csv.DictReader(io.StringIO(content.decode("utf-8")))) if d]
    if not documents:
        raise validation_error(
            message="No valid data found in CSV",
            details={"data_type": data_type},
        )
    await data_seeder.upsert_batch_data(
        data_type=data_type,
        documents=documents,
        batch_id=batch_id,
        operational_time=operational_time,
        tenant_id=tenant.tenant_id,
    )
    return {"data": {"recordCount": len(documents), "batch_id": batch_id, "operational_time": operational_time},
            "success": True, "message": f"Successfully uploaded {len(documents)} {data_type} records",
            "timestamp": utcnow().isoformat()}

@router.post("/api/upload/batch")
async def upload_batch_temporal(
    request: TemporalUploadRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    from services.data_seeder import data_seeder
    total_records, results = 0, {}
    for dt in ["fleet", "orders", "inventory", "support"]:
        docs = generate_demo_sheets_data(dt, request.batch_id, tenant.tenant_id)
        if docs:
            await data_seeder.upsert_batch_data(
                data_type=dt,
                documents=docs,
                batch_id=request.batch_id,
                operational_time=request.operational_time,
                tenant_id=tenant.tenant_id,
            )
            total_records += len(docs); results[dt] = len(docs)
    return {"data": {"recordCount": total_records, "batch_id": request.batch_id,
                     "operational_time": request.operational_time, "breakdown": results},
            "success": True, "message": f"Successfully uploaded complete operational snapshot with {total_records} total records",
            "timestamp": utcnow().isoformat()}

@router.post("/api/upload/selective")
async def upload_selective_temporal(
    request: SelectiveUploadRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    from services.data_seeder import data_seeder
    total_records, results = 0, {}
    for dt in request.data_types:
        docs = generate_demo_sheets_data(dt, request.batch_id, tenant.tenant_id)
        if docs:
            await data_seeder.upsert_batch_data(
                data_type=dt,
                documents=docs,
                batch_id=request.batch_id,
                operational_time=request.operational_time,
                tenant_id=tenant.tenant_id,
            )
            total_records += len(docs); results[dt] = len(docs)
    return {"data": {"recordCount": total_records, "batch_id": request.batch_id,
                     "operational_time": request.operational_time, "breakdown": results},
            "success": True, "message": f"Successfully uploaded {len(request.data_types)} data types with {total_records} total records",
            "timestamp": utcnow().isoformat()}

@router.post("/api/upload/sheets")
async def upload_sheets_temporal(
    request: TemporalUploadRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    from services.data_seeder import data_seeder
    documents = generate_demo_sheets_data(request.data_type, request.batch_id, tenant.tenant_id)
    if not documents:
        raise validation_error(
            message="No data generated from sheets",
            details={"data_type": request.data_type},
        )
    await data_seeder.upsert_batch_data(
        data_type=request.data_type,
        documents=documents,
        batch_id=request.batch_id,
        operational_time=request.operational_time,
        tenant_id=tenant.tenant_id,
    )
    return {"data": {"recordCount": len(documents), "batch_id": request.batch_id,
                     "operational_time": request.operational_time},
            "success": True, "message": f"Successfully uploaded {len(documents)} {request.data_type} records from sheets",
            "timestamp": utcnow().isoformat()}


# ---------------------------------------------------------------------------
# Location endpoints
# ---------------------------------------------------------------------------

def _invalid_location_payload(details: Optional[dict] = None) -> AppException:
    return AppException(
        error_code=ErrorCode.VALIDATION_ERROR,
        message="Invalid location payload",
        status_code=422,
        details=details,
    )


async def _parse_location_body(request: Request, model):
    """Parse the JSON body into ``model``; any bad input is a 422.

    A non-JSON body, a non-object body, or a pydantic validation failure
    (missing fields, out-of-range coordinates, an empty batch) raises a
    ``VALIDATION_ERROR`` instead of escaping as a 500. Field errors are
    reported without echoing the submitted input.
    """
    try:
        body = await request.json()
    except ValueError:  # json.JSONDecodeError / UnicodeDecodeError
        raise _invalid_location_payload()
    if not isinstance(body, dict):
        raise _invalid_location_payload()
    try:
        return model(**body)
    except ValidationError as exc:
        raise _invalid_location_payload(
            {"errors": exc.errors(include_url=False, include_input=False, include_context=False)}
        )


@router.post("/api/locations/webhook")
async def location_webhook(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    from ingestion.service import LocationUpdate
    update = await _parse_location_body(request, LocationUpdate)
    # Stamp the authenticated tenant on the update so the ingestion
    # service writes tenant-scoped docs to both ``trucks`` and
    # ``locations``, and verify the referenced truck belongs to the
    # caller's tenant. ``tenant_id`` supplied by the caller is ignored —
    # the JWT-derived tenant always wins.
    update.tenant_id = tenant.tenant_id
    c = _container(request)
    result = await c.data_ingestion_service.process_location_update(update)
    if result.success:
        return {"success": True, "truck_id": result.truck_id, "message": result.message,
                "timestamp": utcnow().isoformat()}
    raise internal_error(
        message=result.message,
        details={"truck_id": result.truck_id},
    )

@router.post("/api/locations/batch")
async def batch_location_updates(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    from ingestion.service import BatchLocationUpdate
    batch = await _parse_location_body(request, BatchLocationUpdate)
    # Stamp the authenticated tenant on every update so the ingestion
    # service writes tenant-scoped docs and per-truck ownership checks
    # run against the correct tenant.
    for item in batch.updates:
        item.tenant_id = tenant.tenant_id
    c = _container(request)
    result = await c.data_ingestion_service.process_batch_updates(batch.updates)
    return {"success": True, "total": result.total, "successful": result.successful,
            "failed": result.failed,
            "results": [{"truck_id": r.truck_id, "success": r.success, "message": r.message} for r in result.results],
            "timestamp": utcnow().isoformat()}


# ---------------------------------------------------------------------------
# CSV / demo data helpers
# ---------------------------------------------------------------------------

def convert_csv_row_to_document(row: dict, data_type: str, tenant_id: Optional[str] = None) -> dict:
    """Convert CSV row to Elasticsearch document format.

    When ``tenant_id`` is provided, every returned document is stamped with
    it so downstream ``upsert_batch_data`` / ``bulk_index_documents`` calls
    write tenant-scoped records. Rows that fail conversion (e.g. missing
    geocoding for a fleet row) return ``None`` so the caller can filter
    them out instead of silently falling back to a hard-coded default
    location that would pollute every tenant's data.
    """
    def _loc(name, lat=None, lon=None):
        path = os.path.join("demo-data", "locations.csv")
        m = {}
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        m[r["name"]] = {"id": r["location_id"], "name": r["name"], "type": r["type"],
                                        "coordinates": {"lat": float(r["lat"]), "lon": float(r["lon"])}, "address": r["address"]}
        except Exception as loc_err:
            logger.debug("Failed to read locations CSV: %s", loc_err)
        if name in m: return m[name]
        if lat is not None and lon is not None:
            return {"id": name.lower().replace(" ", "-").replace(",", ""), "name": name, "type": "location",
                    "coordinates": {"lat": lat, "lon": lon}, "address": name}
        # No known location and no coordinates supplied — return None so the
        # caller drops the row instead of inheriting a hard-coded default
        # fallback (which previously leaked a shared synthetic location into
        # every tenant's CSV uploads).
        return None
    try:
        doc = None
        if data_type in ("trucks", "fleet"):
            lat = float(row.get("lat", 0)) if row.get("lat") else None
            lon = float(row.get("lon", 0)) if row.get("lon") else None
            current_location = _loc(row.get("current_location", row.get("location", "Houston Terminal")), lat, lon)
            destination = _loc(row.get("destination", "Dallas Depot"))
            if current_location is None or destination is None:
                # Skip the row rather than fabricate a station.
                return None
            doc = {"truck_id": row.get("truck_id"), "plate_number": row.get("plate_number", row.get("truck_id")),
                    "driver_id": f"driver-{row.get('truck_id', 'unknown')}", "driver_name": row.get("driver_name", row.get("driver")),
                    "status": row.get("status", "on_time"),
                    "current_location": current_location,
                    "destination": destination,
                    "route": {"id": "route", "distance": 500.0, "estimated_duration": 300, "actual_duration": None},
                    "estimated_arrival": row.get("estimated_arrival", row.get("eta")),
                    "last_update": utcnow().isoformat(),
                    "cargo": {"type": row.get("cargo_type", row.get("cargo", "General Cargo")), "weight": 10000.0,
                              "volume": 30.0, "description": row.get("cargo_description", row.get("description", "Standard cargo")),
                              "priority": "medium"}}
        elif data_type == "orders":
            doc = {"order_id": row.get("order_id"), "customer": row.get("customer"), "status": row.get("status", "pending"),
                    "value": float(row.get("value", 0)) if row.get("value") else 0,
                    "items": row.get("items", row.get("description")), "region": row.get("region"),
                    "priority": row.get("priority", "medium"), "truck_id": row.get("truck_id")}
        elif data_type == "inventory":
            doc = {"item_id": row.get("item_id"), "name": row.get("name", row.get("item_name")),
                    "category": row.get("category"), "quantity": int(row.get("quantity", 0)) if row.get("quantity") else 0,
                    "unit": row.get("unit"), "location": row.get("location"), "status": row.get("status", "in_stock")}
        elif data_type in ("support_tickets", "support"):
            doc = {"ticket_id": row.get("ticket_id"), "customer": row.get("customer"), "issue": row.get("issue"),
                    "description": row.get("description"), "priority": row.get("priority", "medium"),
                    "status": row.get("status", "open")}

        if doc is not None and tenant_id:
            doc["tenant_id"] = tenant_id
        return doc
    except Exception as conv_err:
        logger.warning("Failed to convert CSV row to %s document: %s", data_type, conv_err)
    return None


def generate_demo_sheets_data(data_type: str, batch_id: str, tenant_id: Optional[str] = None) -> list:
    """Generate demo data by reading from CSV files.

    Every produced document is stamped with ``tenant_id`` when provided so
    the uploaded batch is tenant-scoped end-to-end.
    """
    time_period = "morning"
    for p in ("afternoon", "evening", "night"):
        if p in batch_id.lower():
            time_period = p; break
    csv_type = {"trucks": "fleet", "fleet": "fleet", "orders": "orders", "inventory": "inventory",
                "support_tickets": "support", "support": "support"}.get(data_type, data_type)
    path = os.path.join("demo-data", f"{time_period}_{csv_type}.csv")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [d for d in (convert_csv_row_to_document(row, data_type, tenant_id) for row in csv.DictReader(f)) if d]
    except Exception as read_err:
        logger.warning("Failed to read demo CSV %s: %s", path, read_err)
        return []
