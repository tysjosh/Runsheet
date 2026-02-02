from fastapi import FastAPI, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, ValidationError
from typing import Optional
from contextlib import asynccontextmanager
import json
import logging
import asyncio
import csv
import io
import time
from datetime import datetime, timedelta
from Agents.mainagent import LogisticsAgent
from data_endpoints import router as data_router
from services.data_seeder import data_seeder
from services.elasticsearch_service import elasticsearch_service
from config.settings import get_settings
from errors.handlers import register_exception_handlers
from errors.exceptions import AppException, validation_error
from middleware.request_id import RequestIDMiddleware
from middleware.rate_limiter import limiter, setup_rate_limiting
from middleware.security_headers import setup_security_headers
from health.service import HealthCheckService
from telemetry.service import get_telemetry_service, initialize_telemetry
from ingestion.service import DataIngestionService, LocationUpdate, BatchLocationUpdate
from websocket.connection_manager import ConnectionManager, get_connection_manager

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load settings from centralized configuration
settings = get_settings()

# Initialize telemetry service for structured logging and metrics
# Validates: Requirement 5.1, 5.4, 5.7
telemetry_service = initialize_telemetry(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    try:
        logger.info("🚀 Starting Runsheet Logistics API...")
        logger.info("🌅 Seeding Elasticsearch with baseline morning data...")
        await data_seeder.seed_baseline_data(operational_time="09:00")
        logger.info("✅ Baseline data seeding completed! Ready for temporal demo.")
    except Exception as e:
        logger.error(f"❌ Failed to seed Elasticsearch data: {e}")
        # Don't fail startup, just log the error
    
    yield  # Application runs here
    
    # Shutdown (if needed)
    logger.info("👋 Shutting down Runsheet Logistics API...")


# Initialize FastAPI app with lifespan handler
app = FastAPI(title="Runsheet Logistics API", version="1.0.0", lifespan=lifespan)

# Register exception handlers for structured error responses
register_exception_handlers(app)

# Add CORS middleware using configured origins only (no wildcards)
# Validates: Requirement 14.4 - CORS restrictions allowing only configured frontend domains
# In production, only configured origins are allowed. Wildcards are not permitted.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,  # Only configured origins, no wildcards
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],  # Explicit methods, no wildcard
    allow_headers=[
        "Accept",
        "Accept-Language",
        "Content-Language",
        "Content-Type",
        "Authorization",
        "X-Request-ID",
        "X-Requested-With",
    ],  # Explicit headers, no wildcard
    expose_headers=[
        "X-Request-ID",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
    ],  # Headers exposed to the client
    max_age=600,  # Cache preflight requests for 10 minutes
)

# Add request ID middleware for request correlation
# This must be added after CORS middleware so it runs for all requests
app.add_middleware(RequestIDMiddleware)

# Set up rate limiting for API security
# Validates: Requirement 14.1 (100 req/min for API) and 14.2 (10 req/min for AI chat)
setup_rate_limiting(
    app,
    api_rate_limit=settings.rate_limit_requests_per_minute,
    ai_rate_limit=settings.rate_limit_ai_requests_per_minute
)

# Set up security headers middleware
# Validates: Requirement 14.5 (X-Content-Type-Options, X-Frame-Options, Content-Security-Policy)
setup_security_headers(app)

# Initialize the logistics agent
logistics_agent = LogisticsAgent()

# Initialize the health check service with Elasticsearch
# Session store is optional and can be added later when implemented
health_check_service = HealthCheckService(
    es_service=elasticsearch_service,
    session_store=None,  # Will be added when session store is implemented
    check_timeout=5.0  # 5 second timeout as per Requirement 4.4
)

# Initialize the data ingestion service for GPS location updates
# Validates: Requirement 6.1, 6.2, 6.3, 6.6
data_ingestion_service = DataIngestionService(
    es_service=elasticsearch_service,
    telemetry=telemetry_service
)

# Initialize the WebSocket connection manager for real-time fleet updates
# Validates: Requirement 6.7 - WebSocket connections for pushing real-time updates
fleet_connection_manager = get_connection_manager()

# Configure the data ingestion service with the WebSocket connection manager
# This enables automatic broadcasting of location updates to connected clients
data_ingestion_service.set_connection_manager(fleet_connection_manager)

# Include data endpoints
app.include_router(data_router)

class ChatRequest(BaseModel):
    message: str
    mode: str = "chat"  # "chat" or "agent"
    session_id: Optional[str] = None  # Optional session ID for conversation persistence

class ClearChatRequest(BaseModel):
    session_id: Optional[str] = None  # Optional session ID to clear from store

class TemporalUploadRequest(BaseModel):
    data_type: str
    batch_id: str
    operational_time: str
    sheets_url: str = None

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "Runsheet Logistics API is running"}

@app.post("/api/chat")
@limiter.limit(f"{settings.rate_limit_ai_requests_per_minute}/minute")
async def chat_endpoint(request: ChatRequest, http_request: Request):
    """
    Streaming chat endpoint for the logistics AI assistant.
    
    Supports optional session_id for conversation persistence across
    multiple backend instances (stateless operation).
    
    Rate limited to 10 requests per minute per IP address.
    
    Validates:
    - Requirement 8.2: Load conversation history from Session_Store using session identifier
    - Requirement 8.3: Persist updated conversation history to Session_Store
    - Requirement 8.6: Gracefully degrade when Session_Store is unavailable
    - Requirement 14.2: Rate limiting of 10 requests per minute per IP for AI chat endpoints
    """
    try:
        logger.info(f"🔴 BACKEND: Chat request received - Mode: {request.mode}, Session: {request.session_id}, Message: {request.message[:100]}...")
        
        async def generate_response():
            logger.info(f"🟠 BACKEND: Starting generate_response for message: {request.message[:50]}...")
            try:
                async for event in logistics_agent.chat_streaming(
                    request.message, 
                    request.mode,
                    session_id=request.session_id
                ):
                    # Handle streaming events according to Strands documentation
                    if isinstance(event, dict):
                        if "error" in event:
                            yield f"data: {json.dumps({'error': event['error']})}\n\n"
                        elif "data" in event:
                            # This is the actual streaming text data
                            text = event["data"]
                            if text:
                                yield f"data: {json.dumps({'type': 'text', 'content': text})}\n\n"
                        elif "current_tool_use" in event:
                            # Tool is being invoked
                            tool_info = event["current_tool_use"]
                            yield f"data: {json.dumps({'type': 'tool', 'tool_name': tool_info.get('name', ''), 'tool_input': tool_info.get('input', {})})}\n\n"
                        elif "current_tool_result" in event:
                            # Tool result received
                            tool_result = event["current_tool_result"]
                            yield f"data: {json.dumps({'type': 'tool_result', 'tool_name': tool_result.get('name', ''), 'tool_output': tool_result.get('output', '')})}\n\n"
                        elif event.get('event') == 'messageStop' or 'result' in event:
                            # Message is complete
                            yield f"data: {json.dumps({'type': 'done'})}\n\n"
                            break
                
            except Exception as e:
                logger.error(f"Error in chat streaming: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        
        return StreamingResponse(
            generate_response(),
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "text/plain; charset=utf-8"
            }
        )
        
    except Exception as e:
        logger.error(f"Error in chat endpoint: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/chat/fallback")
@limiter.limit(f"{settings.rate_limit_ai_requests_per_minute}/minute")
async def chat_fallback_endpoint(request: ChatRequest, http_request: Request):
    """
    Non-streaming chat fallback endpoint.
    
    Supports optional session_id for conversation persistence across
    multiple backend instances (stateless operation).
    
    Rate limited to 10 requests per minute per IP address.
    
    Validates:
    - Requirement 8.2: Load conversation history from Session_Store using session identifier
    - Requirement 8.3: Persist updated conversation history to Session_Store
    - Requirement 8.6: Gracefully degrade when Session_Store is unavailable
    - Requirement 14.2: Rate limiting of 10 requests per minute per IP for AI chat endpoints
    """
    try:
        logger.info(f"🔄 BACKEND: Fallback chat request - Mode: {request.mode}, Session: {request.session_id}, Message: {request.message[:50]}...")
        
        response = await logistics_agent.chat_fallback(
            request.message, 
            request.mode,
            session_id=request.session_id
        )
        
        return {
            "response": response,
            "mode": request.mode,
            "session_id": request.session_id,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error in fallback chat endpoint: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/chat/clear")
async def clear_chat_endpoint(request: ClearChatRequest):
    """
    Clear the chat history/memory.
    
    If a session_id is provided, also clears the persisted session
    from the session store.
    
    Validates:
    - Requirement 8.6: Gracefully degrade when Session_Store is unavailable
    """
    try:
        logistics_agent.clear_memory(session_id=request.session_id)
        return {
            "message": "Chat memory cleared successfully",
            "session_id": request.session_id
        }
    except Exception as e:
        logger.error(f"Error clearing chat: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/demo/reset")
async def reset_demo():
    """Reset demo to baseline morning state"""
    try:
        logger.info("Demo reset requested - clearing and reseeding data...")
        
        # Clear all existing data
        await data_seeder.clear_all_data()
        
        # Reseed with baseline morning data
        await data_seeder.seed_baseline_data(operational_time="09:00")
        
        return {
            "success": True,
            "message": "Demo reset to baseline morning operations",
            "timestamp": datetime.now().isoformat(),
            "state": "morning_baseline"
        }
        
    except Exception as e:
        logger.error(f"Demo reset failed: {e}")
        raise HTTPException(status_code=500, detail=f"Demo reset failed: {str(e)}")

@app.get("/api/demo/status")
async def get_demo_status():
    """Get current demo state"""
    try:
        # Check what data exists to determine current state
        trucks = await data_seeder.es_service.get_all_documents("trucks")
        
        # Analyze data to determine current time period
        current_state = "unknown"
        if trucks:
            # Check batch_id or operational_time to determine state
            sample_truck = trucks[0]
            batch_id = sample_truck.get("batch_id", "morning_baseline")
            
            if "afternoon" in batch_id.lower():
                current_state = "afternoon"
            elif "evening" in batch_id.lower():
                current_state = "evening"
            elif "night" in batch_id.lower():
                current_state = "night"
            else:
                current_state = "morning_baseline"
        
        return {
            "success": True,
            "current_state": current_state,
            "total_trucks": len(trucks),
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Failed to get demo status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/upload/csv")
async def upload_csv_temporal(
    file: UploadFile = File(...),
    data_type: str = Form(...),
    batch_id: str = Form(...),
    operational_time: str = Form(...)
):
    """
    Upload CSV file with temporal metadata for demo.
    
    Validates:
    - Requirement 5.7: THE Backend_Service SHALL implement audit logging for
      compliance-sensitive operations including data uploads
    """
    start_time = time.time()
    
    try:
        logger.info(f"📊 CSV Upload: {data_type} batch {batch_id} at {operational_time}")
        
        # Read CSV content
        content = await file.read()
        csv_content = content.decode('utf-8')
        
        # Parse CSV data
        documents = []
        csv_reader = csv.DictReader(io.StringIO(csv_content))
        
        for row in csv_reader:
            # Convert CSV row to document format based on data type
            doc = convert_csv_row_to_document(row, data_type)
            if doc:
                documents.append(doc)
        
        if not documents:
            raise HTTPException(status_code=400, detail="No valid data found in CSV")
        
        # Upsert the data with temporal metadata
        result = await data_seeder.upsert_batch_data(
            data_type=data_type,
            documents=documents,
            batch_id=batch_id,
            operational_time=operational_time
        )
        
        # Log audit event for data upload (Requirement 5.7)
        telemetry = get_telemetry_service()
        if telemetry:
            duration_ms = (time.time() - start_time) * 1000
            telemetry.log_audit_event(
                event_type="data_upload",
                user_id=None,  # User ID would come from auth when implemented
                resource_type=data_type,
                resource_id=batch_id,
                action="create",
                details={
                    "file_name": file.filename,
                    "record_count": len(documents),
                    "operational_time": operational_time,
                    "duration_ms": duration_ms
                }
            )
            # Record upload metrics
            telemetry.record_metric(
                name="data_upload_duration_ms",
                value=duration_ms,
                tags={"data_type": data_type, "upload_type": "csv"}
            )
            telemetry.record_metric(
                name="data_upload_record_count",
                value=len(documents),
                tags={"data_type": data_type, "upload_type": "csv"}
            )
        
        return {
            "data": {
                "recordCount": len(documents),
                "batch_id": batch_id,
                "operational_time": operational_time
            },
            "success": True,
            "message": f"Successfully uploaded {len(documents)} {data_type} records",
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"CSV upload error: {e}")
        # Log failed audit event
        telemetry = get_telemetry_service()
        if telemetry:
            telemetry.log_audit_event(
                event_type="data_upload",
                user_id=None,
                resource_type=data_type,
                resource_id=batch_id,
                action="create_failed",
                details={
                    "file_name": file.filename if file else "unknown",
                    "error": str(e)
                }
            )
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/upload/batch")
async def upload_batch_temporal(request: TemporalUploadRequest):
    """
    Upload all data types for a complete operational snapshot.
    
    Validates:
    - Requirement 5.7: THE Backend_Service SHALL implement audit logging for
      compliance-sensitive operations including data uploads
    """
    start_time = time.time()
    
    try:
        logger.info(f"📊 Batch Upload: All data types for {request.batch_id} at {request.operational_time}")
        
        data_types = ["fleet", "orders", "inventory", "support"]
        total_records = 0
        results = {}
        
        for data_type in data_types:
            documents = generate_demo_sheets_data(data_type, request.batch_id)
            if documents:
                result = await data_seeder.upsert_batch_data(
                    data_type=data_type,
                    documents=documents,
                    batch_id=request.batch_id,
                    operational_time=request.operational_time
                )
                total_records += len(documents)
                results[data_type] = len(documents)
        
        # Log audit event for batch upload (Requirement 5.7)
        telemetry = get_telemetry_service()
        if telemetry:
            duration_ms = (time.time() - start_time) * 1000
            telemetry.log_audit_event(
                event_type="data_upload",
                user_id=None,
                resource_type="batch",
                resource_id=request.batch_id,
                action="create",
                details={
                    "data_types": data_types,
                    "record_counts": results,
                    "total_records": total_records,
                    "operational_time": request.operational_time,
                    "duration_ms": duration_ms
                }
            )
            telemetry.record_metric(
                name="data_upload_duration_ms",
                value=duration_ms,
                tags={"data_type": "batch", "upload_type": "batch"}
            )
            telemetry.record_metric(
                name="data_upload_record_count",
                value=total_records,
                tags={"data_type": "batch", "upload_type": "batch"}
            )
        
        return {
            "data": {
                "recordCount": total_records,
                "batch_id": request.batch_id,
                "operational_time": request.operational_time,
                "breakdown": results
            },
            "success": True,
            "message": f"Successfully uploaded complete operational snapshot with {total_records} total records",
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Batch upload error: {e}")
        telemetry = get_telemetry_service()
        if telemetry:
            telemetry.log_audit_event(
                event_type="data_upload",
                user_id=None,
                resource_type="batch",
                resource_id=request.batch_id,
                action="create_failed",
                details={"error": str(e)}
            )
        raise HTTPException(status_code=500, detail=str(e))

class SelectiveUploadRequest(BaseModel):
    batch_id: str
    operational_time: str
    data_types: list[str]  # Selected data types to upload

@app.post("/api/upload/selective")
async def upload_selective_temporal(request: SelectiveUploadRequest):
    """
    Upload selected data types for a customized operational update.
    
    Validates:
    - Requirement 5.7: THE Backend_Service SHALL implement audit logging for
      compliance-sensitive operations including data uploads
    """
    start_time = time.time()
    
    try:
        logger.info(f"📊 Selective Upload: {request.data_types} for {request.batch_id} at {request.operational_time}")
        
        total_records = 0
        results = {}
        
        for data_type in request.data_types:
            documents = generate_demo_sheets_data(data_type, request.batch_id)
            if documents:
                result = await data_seeder.upsert_batch_data(
                    data_type=data_type,
                    documents=documents,
                    batch_id=request.batch_id,
                    operational_time=request.operational_time
                )
                total_records += len(documents)
                results[data_type] = len(documents)
        
        # Log audit event for selective upload (Requirement 5.7)
        telemetry = get_telemetry_service()
        if telemetry:
            duration_ms = (time.time() - start_time) * 1000
            telemetry.log_audit_event(
                event_type="data_upload",
                user_id=None,
                resource_type="selective",
                resource_id=request.batch_id,
                action="create",
                details={
                    "data_types": request.data_types,
                    "record_counts": results,
                    "total_records": total_records,
                    "operational_time": request.operational_time,
                    "duration_ms": duration_ms
                }
            )
            telemetry.record_metric(
                name="data_upload_duration_ms",
                value=duration_ms,
                tags={"data_type": "selective", "upload_type": "selective"}
            )
            telemetry.record_metric(
                name="data_upload_record_count",
                value=total_records,
                tags={"data_type": "selective", "upload_type": "selective"}
            )
        
        return {
            "data": {
                "recordCount": total_records,
                "batch_id": request.batch_id,
                "operational_time": request.operational_time,
                "breakdown": results
            },
            "success": True,
            "message": f"Successfully uploaded {len(request.data_types)} data types with {total_records} total records",
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Selective upload error: {e}")
        telemetry = get_telemetry_service()
        if telemetry:
            telemetry.log_audit_event(
                event_type="data_upload",
                user_id=None,
                resource_type="selective",
                resource_id=request.batch_id,
                action="create_failed",
                details={"error": str(e), "data_types": request.data_types}
            )
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/upload/sheets")
async def upload_sheets_temporal(request: TemporalUploadRequest):
    """
    Upload from Google Sheets with temporal metadata for demo.
    
    Validates:
    - Requirement 5.7: THE Backend_Service SHALL implement audit logging for
      compliance-sensitive operations including data uploads
    """
    start_time = time.time()
    
    try:
        logger.info(f"📊 Sheets Upload: {request.data_type} batch {request.batch_id} at {request.operational_time}")
        
        # For demo purposes, we'll simulate Google Sheets data
        # In production, you'd fetch from the actual Google Sheets API
        documents = generate_demo_sheets_data(request.data_type, request.batch_id)
        
        if not documents:
            raise HTTPException(status_code=400, detail="No data generated from sheets")
        
        # Upsert the data with temporal metadata
        result = await data_seeder.upsert_batch_data(
            data_type=request.data_type,
            documents=documents,
            batch_id=request.batch_id,
            operational_time=request.operational_time
        )
        
        # Log audit event for sheets upload (Requirement 5.7)
        telemetry = get_telemetry_service()
        if telemetry:
            duration_ms = (time.time() - start_time) * 1000
            telemetry.log_audit_event(
                event_type="data_upload",
                user_id=None,
                resource_type=request.data_type,
                resource_id=request.batch_id,
                action="create",
                details={
                    "source": "google_sheets",
                    "sheets_url": request.sheets_url,
                    "record_count": len(documents),
                    "operational_time": request.operational_time,
                    "duration_ms": duration_ms
                }
            )
            telemetry.record_metric(
                name="data_upload_duration_ms",
                value=duration_ms,
                tags={"data_type": request.data_type, "upload_type": "sheets"}
            )
            telemetry.record_metric(
                name="data_upload_record_count",
                value=len(documents),
                tags={"data_type": request.data_type, "upload_type": "sheets"}
            )
        
        return {
            "data": {
                "recordCount": len(documents),
                "batch_id": request.batch_id,
                "operational_time": request.operational_time
            },
            "success": True,
            "message": f"Successfully uploaded {len(documents)} {request.data_type} records from sheets",
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Sheets upload error: {e}")
        telemetry = get_telemetry_service()
        if telemetry:
            telemetry.log_audit_event(
                event_type="data_upload",
                user_id=None,
                resource_type=request.data_type,
                resource_id=request.batch_id,
                action="create_failed",
                details={"source": "google_sheets", "error": str(e)}
            )
        raise HTTPException(status_code=500, detail=str(e))

def convert_csv_row_to_document(row: dict, data_type: str) -> dict:
    """Convert CSV row to Elasticsearch document format"""
    
    def create_location_object(location_name: str, lat: float = None, lon: float = None):
        """Create a proper location object by reading from locations CSV"""
        import os
        
        # Load locations from CSV file
        locations_path = os.path.join("demo-data", "locations.csv")
        location_map = {}
        
        try:
            if os.path.exists(locations_path):
                with open(locations_path, 'r', encoding='utf-8') as file:
                    locations_reader = csv.DictReader(file)
                    for loc_row in locations_reader:
                        location_map[loc_row['name']] = {
                            "id": loc_row['location_id'],
                            "name": loc_row['name'],
                            "type": loc_row['type'],
                            "coordinates": {"lat": float(loc_row['lat']), "lon": float(loc_row['lon'])},
                            "address": loc_row['address']
                        }
        except Exception as e:
            logger.error(f"Error loading locations CSV: {e}")
        
        # Try to find exact match first
        if location_name in location_map:
            return location_map[location_name]
        
        # If custom coordinates provided, create dynamic location
        if lat is not None and lon is not None:
            return {
                "id": location_name.lower().replace(" ", "-").replace(",", ""),
                "name": location_name,
                "type": "location",
                "coordinates": {"lat": lat, "lon": lon},
                "address": f"{location_name}, Kenya"
            }
        
        # Default fallback to Nairobi if no match
        return {
            "id": "nairobi-station",
            "name": "Nairobi Station",
            "type": "station", 
            "coordinates": {"lat": -1.2921, "lon": 36.8219},
            "address": "Nairobi, Kenya"
        }
    
    try:
        if data_type == "trucks" or data_type == "fleet":
            # Get coordinates if available
            lat = float(row.get("lat", 0)) if row.get("lat") else None
            lon = float(row.get("lon", 0)) if row.get("lon") else None
            
            current_location_name = row.get("current_location", row.get("location", "Nairobi Station"))
            destination_name = row.get("destination", "Mombasa Port")
            
            return {
                "truck_id": row.get("truck_id"),
                "plate_number": row.get("plate_number", row.get("truck_id")),
                "driver_id": f"driver-{row.get('truck_id', 'unknown')}",
                "driver_name": row.get("driver_name", row.get("driver")),
                "status": row.get("status", "on_time"),
                "current_location": create_location_object(current_location_name, lat, lon),
                "destination": create_location_object(destination_name),
                "route": {
                    "id": f"{current_location_name.lower().replace(' ', '-')}-{destination_name.lower().replace(' ', '-')}",
                    "distance": 500.0,  # Default distance
                    "estimated_duration": 300,  # Default 5 hours
                    "actual_duration": None
                },
                "estimated_arrival": row.get("estimated_arrival", row.get("eta")),
                "last_update": datetime.now().isoformat() + "Z",
                "cargo": {
                    "type": row.get("cargo_type", row.get("cargo", "General Cargo")),
                    "weight": 10000.0,  # Default weight
                    "volume": 30.0,     # Default volume
                    "description": row.get("cargo_description", row.get("description", "Standard cargo")),
                    "priority": "medium"
                }
            }
        
        elif data_type == "orders":
            return {
                "order_id": row.get("order_id"),
                "customer": row.get("customer"),
                "status": row.get("status", "pending"),
                "value": float(row.get("value", 0)) if row.get("value") else 0,
                "items": row.get("items", row.get("description")),
                "region": row.get("region"),
                "priority": row.get("priority", "medium"),
                "truck_id": row.get("truck_id")
            }
        
        elif data_type == "inventory":
            return {
                "item_id": row.get("item_id"),
                "name": row.get("name", row.get("item_name")),
                "category": row.get("category"),
                "quantity": int(row.get("quantity", 0)) if row.get("quantity") else 0,
                "unit": row.get("unit"),
                "location": row.get("location"),
                "status": row.get("status", "in_stock")
            }
        
        elif data_type == "support_tickets" or data_type == "support":
            return {
                "ticket_id": row.get("ticket_id"),
                "customer": row.get("customer"),
                "issue": row.get("issue"),
                "description": row.get("description"),
                "priority": row.get("priority", "medium"),
                "status": row.get("status", "open")
            }
        
        return None
    except Exception as e:
        logger.error(f"Error converting CSV row: {e}")
        return None

def generate_demo_sheets_data(data_type: str, batch_id: str) -> list:
    """Generate demo data by reading from CSV files"""
    import os
    
    # Determine time period from batch_id
    time_period = "morning"  # default
    if "afternoon" in batch_id.lower():
        time_period = "afternoon"
    elif "evening" in batch_id.lower():
        time_period = "evening"
    elif "night" in batch_id.lower():
        time_period = "night"
    
    # Map data types to CSV file names
    data_type_mapping = {
        "trucks": "fleet",
        "fleet": "fleet",
        "orders": "orders", 
        "inventory": "inventory",
        "support_tickets": "support",
        "support": "support"
    }
    
    csv_data_type = data_type_mapping.get(data_type, data_type)
    csv_filename = f"{time_period}_{csv_data_type}.csv"
    csv_path = os.path.join("demo-data", csv_filename)
    
    # Check if CSV file exists
    if not os.path.exists(csv_path):
        logger.warning(f"CSV file not found: {csv_path}")
        return []
    
    try:
        # Read CSV and convert to documents
        documents = []
        with open(csv_path, 'r', encoding='utf-8') as file:
            csv_reader = csv.DictReader(file)
            for row in csv_reader:
                doc = convert_csv_row_to_document(row, data_type)
                if doc:
                    documents.append(doc)
        
        logger.info(f"Loaded {len(documents)} records from {csv_filename}")
        return documents
        
    except Exception as e:
        logger.error(f"Error reading CSV file {csv_path}: {e}")
        return []
    
    # If no specific time period matched, return empty list
    logger.warning(f"⚠️ No demo data generated for data_type={data_type}, batch_id={batch_id}")
    return []

@app.get("/api/health")
async def health_check():
    """
    Health check endpoint for monitoring
    """
    return {
        "status": "healthy",
        "service": "Runsheet Logistics API",
        "agent": "LogisticsAgent",
        "version": "1.0.0"
    }


# =============================================================================
# GPS Location Webhook Endpoint
# =============================================================================
# This endpoint receives real-time GPS location updates from IoT devices.
#
# Validates:
# - Requirement 6.1: Expose a webhook endpoint for receiving GPS location updates
# - Requirement 6.2: Validate payload schema and reject malformed requests with 400
# - Requirement 6.6: Reject updates for non-existent truck_ids
# =============================================================================


@app.post("/api/locations/webhook")
async def location_webhook(update: LocationUpdate):
    """
    Webhook endpoint for receiving GPS location updates from IoT devices.
    
    This endpoint receives location updates from GPS/IoT devices and processes
    them through the DataIngestionService. It validates the payload schema,
    verifies the truck exists, and stores the update in Elasticsearch.
    
    Validates:
    - Requirement 6.1: THE Data_Ingestion_Service SHALL expose a webhook endpoint
      for receiving GPS location updates from IoT devices
    - Requirement 6.2: WHEN a location update is received, THE Data_Ingestion_Service
      SHALL validate the payload schema and reject malformed requests with a 400 status
    - Requirement 6.6: IF a location update references a non-existent truck_id,
      THEN THE Data_Ingestion_Service SHALL log a warning and reject the update
    
    Args:
        update: LocationUpdate model containing GPS coordinates and metadata
        
    Returns:
        dict: Success response with truck_id and timestamp
        
    Raises:
        HTTPException: 400 for validation errors, 404 for non-existent truck
    """
    try:
        logger.info(
            f"📍 Location webhook received for truck {update.truck_id}",
            extra={"extra_data": {
                "truck_id": update.truck_id,
                "latitude": update.latitude,
                "longitude": update.longitude
            }}
        )
        
        # Process the location update through the ingestion service
        result = await data_ingestion_service.process_location_update(update)
        
        if result.success:
            return {
                "success": True,
                "truck_id": result.truck_id,
                "message": result.message,
                "timestamp": datetime.now().isoformat()
            }
        else:
            # If processing failed but didn't raise an exception
            raise HTTPException(
                status_code=500,
                detail=result.message
            )
            
    except AppException as e:
        # Re-raise AppExceptions - they will be handled by the exception handlers
        raise
    except ValidationError as e:
        # Handle Pydantic validation errors
        logger.warning(
            f"Location webhook validation failed: {e}",
            extra={"extra_data": {"errors": e.errors()}}
        )
        raise validation_error(
            message="Invalid location update payload",
            details={"validation_errors": e.errors()}
        )
    except Exception as e:
        logger.error(f"Location webhook error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Batch Location Updates Endpoint
# =============================================================================
# This endpoint receives batch GPS location updates for efficiency when
# multiple trucks report simultaneously.
#
# Validates:
# - Requirement 6.5: Support batch location updates for efficiency
# =============================================================================


@app.post("/api/locations/batch")
async def batch_location_updates(batch: BatchLocationUpdate):
    """
    Batch endpoint for receiving multiple GPS location updates efficiently.
    
    This endpoint receives multiple location updates in a single request,
    processes them through the DataIngestionService, and returns aggregate
    success/failure counts along with individual results.
    
    Validates:
    - Requirement 6.5: THE Data_Ingestion_Service SHALL support batch location
      updates for efficiency when multiple trucks report simultaneously
    
    Args:
        batch: BatchLocationUpdate model containing a list of location updates
        
    Returns:
        dict: Batch processing result with success/failure counts
            - total: Total number of updates in the batch
            - successful: Number of successfully processed updates
            - failed: Number of failed updates
            - results: Individual results for each update
        
    Raises:
        HTTPException: 400 for validation errors
    """
    try:
        logger.info(
            f"📍 Batch location update received with {len(batch.updates)} updates",
            extra={"extra_data": {
                "batch_size": len(batch.updates),
                "truck_ids": [u.truck_id for u in batch.updates[:10]]  # Log first 10 truck IDs
            }}
        )
        
        # Process the batch through the ingestion service
        result = await data_ingestion_service.process_batch_updates(batch.updates)
        
        return {
            "success": True,
            "total": result.total,
            "successful": result.successful,
            "failed": result.failed,
            "results": [
                {
                    "truck_id": r.truck_id,
                    "success": r.success,
                    "message": r.message
                }
                for r in result.results
            ],
            "timestamp": datetime.now().isoformat()
        }
            
    except ValidationError as e:
        # Handle Pydantic validation errors
        logger.warning(
            f"Batch location update validation failed: {e}",
            extra={"extra_data": {"errors": e.errors()}}
        )
        raise validation_error(
            message="Invalid batch location update payload",
            details={"validation_errors": e.errors()}
        )
    except Exception as e:
        logger.error(f"Batch location update error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# WebSocket Endpoint for Real-time Fleet Updates
# =============================================================================
# This endpoint provides WebSocket connections for pushing real-time
# location updates to connected frontend clients.
#
# Validates:
# - Requirement 6.7: THE Backend_Service SHALL implement WebSocket connections
#   for pushing real-time updates to connected Frontend_Application clients
# =============================================================================


@app.websocket("/api/fleet/live")
async def fleet_live_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for real-time fleet location updates.
    
    This endpoint accepts WebSocket connections from frontend clients and
    broadcasts location updates in real-time as they are received from
    IoT/GPS devices. Clients receive updates for all trucks in the fleet.
    
    Validates:
    - Requirement 6.7: THE Backend_Service SHALL implement WebSocket connections
      for pushing real-time updates to connected Frontend_Application clients
    
    Message Types Sent to Clients:
    - connection: Initial connection confirmation
        {
            "type": "connection",
            "status": "connected",
            "message": "Connected to fleet live updates",
            "timestamp": "2024-01-01T00:00:00.000Z"
        }
    
    - location_update: Real-time truck location update
        {
            "type": "location_update",
            "data": {
                "truck_id": "TRK-001",
                "coordinates": {"lat": -1.2921, "lon": 36.8219},
                "timestamp": "2024-01-01T00:00:00.000Z",
                "speed_kmh": 65.5,
                "heading": 180.0
            },
            "timestamp": "2024-01-01T00:00:00.000Z"
        }
    
    - batch_location_update: Multiple location updates in one message
        {
            "type": "batch_location_update",
            "data": {
                "updates": [...],
                "count": 5
            },
            "timestamp": "2024-01-01T00:00:00.000Z"
        }
    
    - heartbeat: Keep-alive message
        {
            "type": "heartbeat",
            "timestamp": "2024-01-01T00:00:00.000Z"
        }
    
    Args:
        websocket: The WebSocket connection from the client
    """
    await fleet_connection_manager.connect(websocket)
    
    try:
        logger.info(
            f"🔌 WebSocket client connected to /api/fleet/live",
            extra={"extra_data": {
                "client_host": websocket.client.host if websocket.client else "unknown",
                "total_connections": fleet_connection_manager.get_connection_count()
            }}
        )
        
        # Keep the connection alive and handle incoming messages
        while True:
            try:
                # Wait for messages from the client
                # Clients can send ping/pong or subscription messages
                data = await websocket.receive_text()
                
                # Parse the message
                try:
                    message = json.loads(data)
                    message_type = message.get("type", "unknown")
                    
                    if message_type == "ping":
                        # Respond to ping with pong
                        await websocket.send_json({
                            "type": "pong",
                            "timestamp": datetime.utcnow().isoformat() + "Z"
                        })
                    elif message_type == "subscribe":
                        # Client wants to subscribe to specific trucks (future enhancement)
                        # For now, all clients receive all updates
                        await websocket.send_json({
                            "type": "subscribed",
                            "message": "Subscribed to all fleet updates",
                            "timestamp": datetime.utcnow().isoformat() + "Z"
                        })
                    else:
                        # Echo unknown message types for debugging
                        logger.debug(
                            f"Received unknown WebSocket message type: {message_type}",
                            extra={"extra_data": {"message": message}}
                        )
                        
                except json.JSONDecodeError:
                    # Non-JSON message, log and ignore
                    logger.debug(f"Received non-JSON WebSocket message: {data[:100]}")
                    
            except WebSocketDisconnect:
                # Client disconnected normally
                break
            except Exception as e:
                # Log error but keep connection alive if possible
                logger.warning(
                    f"Error processing WebSocket message: {e}",
                    extra={"extra_data": {"error": str(e)}}
                )
                
    except WebSocketDisconnect:
        # Client disconnected
        pass
    except Exception as e:
        logger.error(
            f"WebSocket error: {e}",
            extra={"extra_data": {"error": str(e)}}
        )
    finally:
        # Clean up the connection
        await fleet_connection_manager.disconnect(websocket)
        logger.info(
            f"🔌 WebSocket client disconnected from /api/fleet/live",
            extra={"extra_data": {
                "total_connections": fleet_connection_manager.get_connection_count()
            }}
        )


# =============================================================================
# Health Check Endpoints
# =============================================================================
# These endpoints provide comprehensive health monitoring for load balancers
# and monitoring systems to accurately determine service availability.
#
# Validates:
# - Requirement 4.1: /health endpoint returns 200 OK when service is accepting requests
# - Requirement 4.2: /health/ready endpoint verifies Elasticsearch connectivity
# - Requirement 4.3: /health/live endpoint returns 200 OK if process is running
# - Requirement 4.6: Include failure reason in response body when dependency fails
# =============================================================================


@app.get("/health")
async def health_basic():
    """
    Basic health check endpoint.
    
    Returns 200 OK when the service is accepting requests.
    This is a simple check that doesn't verify external dependencies.
    
    Validates:
    - Requirement 4.1: THE Backend_Service SHALL expose a `/health` endpoint
      that returns 200 OK when the service is accepting requests
    
    Returns:
        dict: Basic health status with service information
    """
    result = await health_check_service.check_health()
    return {
        "status": result["status"],
        "service": "Runsheet Logistics API",
        "version": "1.0.0",
        "timestamp": result["timestamp"]
    }


@app.get("/health/ready")
async def health_ready():
    """
    Readiness check endpoint with dependency verification.
    
    Verifies connectivity to all dependencies (Elasticsearch, session store)
    and returns 503 if any critical dependency is unavailable.
    
    Validates:
    - Requirement 4.2: THE Backend_Service SHALL expose a `/health/ready` endpoint
      that verifies connectivity to Elasticsearch and returns 503 if any
      dependency is unavailable
    - Requirement 4.4: Check Elasticsearch connectivity with a timeout of 5 seconds
    - Requirement 4.5: Include response time metrics for each dependency
    - Requirement 4.6: WHEN a dependency check fails, THE Health_Check_Service
      SHALL include the failure reason in the response body
    
    Returns:
        JSONResponse: Health status with dependency details
        - 200 OK: All dependencies healthy
        - 503 Service Unavailable: One or more critical dependencies unhealthy
    """
    health_status = await health_check_service.check_readiness()
    response_data = {
        "status": health_status.status,
        "service": "Runsheet Logistics API",
        "version": "1.0.0",
        "timestamp": health_status.timestamp.isoformat() + "Z",
        "dependencies": [dep.to_dict() for dep in health_status.dependencies]
    }
    
    # Return 503 if service is unhealthy (critical dependencies failed)
    if health_status.status == "unhealthy":
        # Include failure reasons in response body (Requirement 4.6)
        failed_deps = [
            dep for dep in health_status.dependencies if not dep.healthy
        ]
        response_data["failure_reasons"] = [
            {
                "dependency": dep.name,
                "error": dep.error
            }
            for dep in failed_deps
        ]
        return JSONResponse(
            status_code=503,
            content=response_data
        )
    
    # Return 200 for healthy or degraded status
    return response_data


@app.get("/health/live")
async def health_live():
    """
    Liveness check endpoint.
    
    Returns 200 OK if the process is running, regardless of dependency status.
    This is used by orchestration systems (like Kubernetes) to determine if
    the process should be restarted.
    
    Validates:
    - Requirement 4.3: THE Backend_Service SHALL expose a `/health/live` endpoint
      that returns 200 OK if the process is running, regardless of dependency status
    
    Returns:
        dict: Simple liveness status indicating the process is alive
    """
    result = await health_check_service.check_liveness()
    return {
        "status": result["status"],
        "service": "Runsheet Logistics API",
        "version": "1.0.0",
        "timestamp": result["timestamp"]
    }

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
