"""
Inline endpoints extracted from main.py to keep it under 200 lines.

Contains: chat, demo, upload, and location endpoints plus CSV helpers.
"""
import csv
import io
import json
import logging
import os
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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
# Per-route overrides are declared in middleware/auth_policy.POLICY_EXCEPTIONS
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
    from Agents.mainagent import LogisticsAgent
    agent = LogisticsAgent()
    async def generate_response():
        try:
            async for event in agent.chat_streaming(
                request.message,
                request.mode,
                session_id=request.session_id,
                tenant_id=tenant.tenant_id,
            ):
                if isinstance(event, dict):
                    if "error" in event:
                        yield f"data: {json.dumps({'error': event['error']})}\n\n"
                    elif "data" in event:
                        text = event["data"]
                        if text:
                            yield f"data: {json.dumps({'type': 'text', 'content': text})}\n\n"
                    elif "current_tool_use" in event:
                        tool_info = event["current_tool_use"]
                        yield f"data: {json.dumps({'type': 'tool', 'tool_name': tool_info.get('name', ''), 'tool_input': tool_info.get('input', {})})}\n\n"
                    elif "current_tool_result" in event:
                        tool_result = event["current_tool_result"]
                        yield f"data: {json.dumps({'type': 'tool_result', 'tool_name': tool_result.get('name', ''), 'tool_output': tool_result.get('output', '')})}\n\n"
                    elif event.get('event') == 'messageStop' or 'result' in event:
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        break
        except Exception as e:
            logger.error("Error in chat streaming: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    return StreamingResponse(generate_response(), media_type="text/plain",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "Content-Type": "text/plain; charset=utf-8"})

@router.post("/api/chat/fallback")
async def chat_fallback_endpoint(
    request: ChatRequest,
    http_request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    from Agents.mainagent import LogisticsAgent
    agent = LogisticsAgent()
    response = await agent.chat_fallback(
        request.message,
        request.mode,
        session_id=request.session_id,
        tenant_id=tenant.tenant_id,
    )
    return {"response": response, "mode": request.mode, "session_id": request.session_id, "timestamp": utcnow().isoformat()}

@router.post("/api/chat/clear")
async def clear_chat_endpoint(
    request: ClearChatRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    # ``clear_memory`` itself is tenant-agnostic (it wipes the per-session
    # in-memory agent state), but we still require an authenticated tenant
    # context on this endpoint so unauthenticated callers can't clear
    # another tenant's session by ID alone.
    from Agents.mainagent import LogisticsAgent
    LogisticsAgent().clear_memory(session_id=request.session_id)
    return {"message": "Chat memory cleared successfully", "session_id": request.session_id}


# ---------------------------------------------------------------------------
# Demo endpoints
# ---------------------------------------------------------------------------
#
# These endpoints wipe + reseed the shared indices with synthetic demo
# data. They are admin-only and refuse to run in production — a stray
# call would otherwise nuke every tenant's fleet, inventory, and
# analytics history in one POST. The seeded docs themselves are
# tenant-stamped with ``tenant_id="demo"`` by the ``DataSeeder`` so
# real tenant queries (which always filter on a specific tenant id)
# cannot resolve them.

@router.post("/api/demo/reset")
async def reset_demo(
    tenant: TenantContext = Depends(get_tenant_context),
):
    from config.settings import Environment, get_settings as _get_settings
    from errors.exceptions import forbidden
    from services.data_seeder import data_seeder

    _settings = _get_settings()
    if _settings.environment == Environment.PRODUCTION:
        raise forbidden(
            message="Demo reset is not available in production",
            details={"environment": _settings.environment.value},
        )
    if "admin" not in (tenant.roles or []):
        raise forbidden(
            message="Demo reset requires the admin role",
            details={"required_role": "admin"},
        )
    await data_seeder.clear_all_data()
    await data_seeder.seed_baseline_data(operational_time="09:00")
    return {"success": True, "message": "Demo reset to baseline morning operations",
            "timestamp": utcnow().isoformat(), "state": "morning_baseline"}

@router.get("/api/demo/status")
async def get_demo_status(
    tenant: TenantContext = Depends(get_tenant_context),
):
    # Demo status reads from the shared ``trucks`` index but only returns
    # the synthetic batch label + row count, neither of which is tenant-
    # sensitive. We still require an authenticated tenant so an anonymous
    # caller cannot learn whether demo data has been loaded.
    from services.data_seeder import data_seeder
    trucks = await data_seeder.es_service.get_all_documents("trucks")
    current_state = "unknown"
    if trucks:
        batch_id = trucks[0].get("batch_id", "morning_baseline")
        for period in ("afternoon", "evening", "night"):
            if period in batch_id.lower():
                current_state = period; break
        else:
            current_state = "morning_baseline"
    return {"success": True, "current_state": current_state, "total_trucks": len(trucks),
            "timestamp": utcnow().isoformat()}


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
        raise HTTPException(status_code=400, detail=f"Invalid data_type '{data_type}'. Must be one of: {sorted(VALID_DATA_TYPES)}")
    from services.data_seeder import data_seeder
    content = await file.read()
    documents = [d for d in (convert_csv_row_to_document(row, data_type, tenant.tenant_id)
                             for row in csv.DictReader(io.StringIO(content.decode("utf-8")))) if d]
    if not documents:
        raise HTTPException(status_code=400, detail="No valid data found in CSV")
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
        raise HTTPException(status_code=400, detail="No data generated from sheets")
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

@router.post("/api/locations/webhook")
async def location_webhook(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    from ingestion.service import LocationUpdate
    body = await request.json()
    update = LocationUpdate(**body)
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
    raise HTTPException(status_code=500, detail=result.message)

@router.post("/api/locations/batch")
async def batch_location_updates(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    from ingestion.service import BatchLocationUpdate
    body = await request.json()
    batch = BatchLocationUpdate(**body)
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
