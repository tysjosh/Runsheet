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

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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
    message: str
    mode: str = "chat"
    session_id: Optional[str] = None

class ClearChatRequest(BaseModel):
    session_id: Optional[str] = None

class TemporalUploadRequest(BaseModel):
    data_type: str
    batch_id: str
    operational_time: str
    sheets_url: str = None

class SelectiveUploadRequest(BaseModel):
    batch_id: str
    operational_time: str
    data_types: list[str]


def _container(request: Request):
    return request.app.state.container


# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------

@router.post("/api/chat")
async def chat_endpoint(request: ChatRequest, http_request: Request):
    from Agents.mainagent import LogisticsAgent
    agent = LogisticsAgent()
    async def generate_response():
        try:
            async for event in agent.chat_streaming(request.message, request.mode, session_id=request.session_id):
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
async def chat_fallback_endpoint(request: ChatRequest, http_request: Request):
    from Agents.mainagent import LogisticsAgent
    agent = LogisticsAgent()
    response = await agent.chat_fallback(request.message, request.mode, session_id=request.session_id)
    return {"response": response, "mode": request.mode, "session_id": request.session_id, "timestamp": datetime.now().isoformat()}

@router.post("/api/chat/clear")
async def clear_chat_endpoint(request: ClearChatRequest):
    from Agents.mainagent import LogisticsAgent
    LogisticsAgent().clear_memory(session_id=request.session_id)
    return {"message": "Chat memory cleared successfully", "session_id": request.session_id}


# ---------------------------------------------------------------------------
# Demo endpoints
# ---------------------------------------------------------------------------

@router.post("/api/demo/reset")
async def reset_demo():
    from services.data_seeder import data_seeder
    await data_seeder.clear_all_data()
    await data_seeder.seed_baseline_data(operational_time="09:00")
    return {"success": True, "message": "Demo reset to baseline morning operations",
            "timestamp": datetime.now().isoformat(), "state": "morning_baseline"}

@router.get("/api/demo/status")
async def get_demo_status():
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
            "timestamp": datetime.now().isoformat()}


# ---------------------------------------------------------------------------
# Upload endpoints
# ---------------------------------------------------------------------------

@router.post("/api/upload/csv")
async def upload_csv_temporal(file: UploadFile = File(...), data_type: str = Form(...),
                              batch_id: str = Form(...), operational_time: str = Form(...)):
    from services.data_seeder import data_seeder
    content = await file.read()
    documents = [d for d in (convert_csv_row_to_document(row, data_type)
                             for row in csv.DictReader(io.StringIO(content.decode("utf-8")))) if d]
    if not documents:
        raise HTTPException(status_code=400, detail="No valid data found in CSV")
    await data_seeder.upsert_batch_data(data_type=data_type, documents=documents,
                                        batch_id=batch_id, operational_time=operational_time)
    return {"data": {"recordCount": len(documents), "batch_id": batch_id, "operational_time": operational_time},
            "success": True, "message": f"Successfully uploaded {len(documents)} {data_type} records",
            "timestamp": datetime.now().isoformat()}

@router.post("/api/upload/batch")
async def upload_batch_temporal(request: TemporalUploadRequest):
    from services.data_seeder import data_seeder
    total_records, results = 0, {}
    for dt in ["fleet", "orders", "inventory", "support"]:
        docs = generate_demo_sheets_data(dt, request.batch_id)
        if docs:
            await data_seeder.upsert_batch_data(data_type=dt, documents=docs,
                                                batch_id=request.batch_id, operational_time=request.operational_time)
            total_records += len(docs); results[dt] = len(docs)
    return {"data": {"recordCount": total_records, "batch_id": request.batch_id,
                     "operational_time": request.operational_time, "breakdown": results},
            "success": True, "message": f"Successfully uploaded complete operational snapshot with {total_records} total records",
            "timestamp": datetime.now().isoformat()}

@router.post("/api/upload/selective")
async def upload_selective_temporal(request: SelectiveUploadRequest):
    from services.data_seeder import data_seeder
    total_records, results = 0, {}
    for dt in request.data_types:
        docs = generate_demo_sheets_data(dt, request.batch_id)
        if docs:
            await data_seeder.upsert_batch_data(data_type=dt, documents=docs,
                                                batch_id=request.batch_id, operational_time=request.operational_time)
            total_records += len(docs); results[dt] = len(docs)
    return {"data": {"recordCount": total_records, "batch_id": request.batch_id,
                     "operational_time": request.operational_time, "breakdown": results},
            "success": True, "message": f"Successfully uploaded {len(request.data_types)} data types with {total_records} total records",
            "timestamp": datetime.now().isoformat()}

@router.post("/api/upload/sheets")
async def upload_sheets_temporal(request: TemporalUploadRequest):
    from services.data_seeder import data_seeder
    documents = generate_demo_sheets_data(request.data_type, request.batch_id)
    if not documents:
        raise HTTPException(status_code=400, detail="No data generated from sheets")
    await data_seeder.upsert_batch_data(data_type=request.data_type, documents=documents,
                                        batch_id=request.batch_id, operational_time=request.operational_time)
    return {"data": {"recordCount": len(documents), "batch_id": request.batch_id,
                     "operational_time": request.operational_time},
            "success": True, "message": f"Successfully uploaded {len(documents)} {request.data_type} records from sheets",
            "timestamp": datetime.now().isoformat()}


# ---------------------------------------------------------------------------
# Location endpoints
# ---------------------------------------------------------------------------

@router.post("/api/locations/webhook")
async def location_webhook(request: Request):
    from ingestion.service import LocationUpdate
    body = await request.json()
    update = LocationUpdate(**body)
    c = _container(request)
    result = await c.data_ingestion_service.process_location_update(update)
    if result.success:
        return {"success": True, "truck_id": result.truck_id, "message": result.message,
                "timestamp": datetime.now().isoformat()}
    raise HTTPException(status_code=500, detail=result.message)

@router.post("/api/locations/batch")
async def batch_location_updates(request: Request):
    from ingestion.service import BatchLocationUpdate
    body = await request.json()
    batch = BatchLocationUpdate(**body)
    c = _container(request)
    result = await c.data_ingestion_service.process_batch_updates(batch.updates)
    return {"success": True, "total": result.total, "successful": result.successful,
            "failed": result.failed,
            "results": [{"truck_id": r.truck_id, "success": r.success, "message": r.message} for r in result.results],
            "timestamp": datetime.now().isoformat()}


# ---------------------------------------------------------------------------
# CSV / demo data helpers
# ---------------------------------------------------------------------------

def convert_csv_row_to_document(row: dict, data_type: str) -> dict:
    """Convert CSV row to Elasticsearch document format."""
    def _loc(name, lat=None, lon=None):
        path = os.path.join("demo-data", "locations.csv")
        m = {}
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        m[r["name"]] = {"id": r["location_id"], "name": r["name"], "type": r["type"],
                                        "coordinates": {"lat": float(r["lat"]), "lon": float(r["lon"])}, "address": r["address"]}
        except Exception:
            pass
        if name in m: return m[name]
        if lat is not None and lon is not None:
            return {"id": name.lower().replace(" ", "-").replace(",", ""), "name": name, "type": "location",
                    "coordinates": {"lat": lat, "lon": lon}, "address": f"{name}, Kenya"}
        return {"id": "nairobi-station", "name": "Nairobi Station", "type": "station",
                "coordinates": {"lat": -1.2921, "lon": 36.8219}, "address": "Nairobi, Kenya"}
    try:
        if data_type in ("trucks", "fleet"):
            lat = float(row.get("lat", 0)) if row.get("lat") else None
            lon = float(row.get("lon", 0)) if row.get("lon") else None
            return {"truck_id": row.get("truck_id"), "plate_number": row.get("plate_number", row.get("truck_id")),
                    "driver_id": f"driver-{row.get('truck_id', 'unknown')}", "driver_name": row.get("driver_name", row.get("driver")),
                    "status": row.get("status", "on_time"),
                    "current_location": _loc(row.get("current_location", row.get("location", "Nairobi Station")), lat, lon),
                    "destination": _loc(row.get("destination", "Mombasa Port")),
                    "route": {"id": "route", "distance": 500.0, "estimated_duration": 300, "actual_duration": None},
                    "estimated_arrival": row.get("estimated_arrival", row.get("eta")),
                    "last_update": datetime.now().isoformat() + "Z",
                    "cargo": {"type": row.get("cargo_type", row.get("cargo", "General Cargo")), "weight": 10000.0,
                              "volume": 30.0, "description": row.get("cargo_description", row.get("description", "Standard cargo")),
                              "priority": "medium"}}
        elif data_type == "orders":
            return {"order_id": row.get("order_id"), "customer": row.get("customer"), "status": row.get("status", "pending"),
                    "value": float(row.get("value", 0)) if row.get("value") else 0,
                    "items": row.get("items", row.get("description")), "region": row.get("region"),
                    "priority": row.get("priority", "medium"), "truck_id": row.get("truck_id")}
        elif data_type == "inventory":
            return {"item_id": row.get("item_id"), "name": row.get("name", row.get("item_name")),
                    "category": row.get("category"), "quantity": int(row.get("quantity", 0)) if row.get("quantity") else 0,
                    "unit": row.get("unit"), "location": row.get("location"), "status": row.get("status", "in_stock")}
        elif data_type in ("support_tickets", "support"):
            return {"ticket_id": row.get("ticket_id"), "customer": row.get("customer"), "issue": row.get("issue"),
                    "description": row.get("description"), "priority": row.get("priority", "medium"),
                    "status": row.get("status", "open")}
    except Exception:
        pass
    return None


def generate_demo_sheets_data(data_type: str, batch_id: str) -> list:
    """Generate demo data by reading from CSV files."""
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
            return [d for d in (convert_csv_row_to_document(row, data_type) for row in csv.DictReader(f)) if d]
    except Exception:
        return []
