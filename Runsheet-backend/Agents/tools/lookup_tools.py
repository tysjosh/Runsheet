"""
Lookup and specific data retrieval tools.

Validates:
- Requirement 5.5: WHEN an AI tool is invoked, THE Telemetry_Service SHALL log
  the tool name, input parameters, execution duration, and success/failure status
"""

import logging
import time
from strands import tool
from services.elasticsearch_service import elasticsearch_service

logger = logging.getLogger(__name__)


def _get_telemetry_service():
    """Get the telemetry service instance."""
    try:
        from telemetry.service import get_telemetry_service
        return get_telemetry_service()
    except ImportError:
        return None


def _log_tool_invocation(tool_name: str, input_params: dict, start_time: float, 
                         success: bool, error: str = None):
    """Helper to log tool invocations with telemetry service."""
    duration_ms = (time.time() - start_time) * 1000
    telemetry = _get_telemetry_service()
    if telemetry:
        telemetry.log_tool_invocation(
            tool_name=tool_name,
            input_params=input_params,
            duration_ms=duration_ms,
            success=success,
            error=error
        )
        # Record metrics
        telemetry.record_metric(
            name="tool_invocation_duration_ms",
            value=duration_ms,
            tags={"tool_name": tool_name, "success": str(success).lower()}
        )
        telemetry.record_metric(
            name="tool_invocation_count",
            value=1,
            tags={"tool_name": tool_name, "success": str(success).lower()}
        )


@tool
async def find_truck_by_id(truck_identifier: str) -> str:
    """
    Find specific truck by ID or plate number.
    
    Args:
        truck_identifier: Truck ID or plate number (e.g., "GI-58A", "MO-84A")
    
    Returns:
        Detailed truck information including location, status, cargo
    """
    start_time = time.time()
    success = False
    error_msg = None
    
    try:
        logger.info(f"🚛 Finding truck: {truck_identifier}")
        trucks = await elasticsearch_service.get_all_documents("trucks")
        
        # Find truck by ID or plate number
        truck = None
        for t in trucks:
            if (t.get('truck_id', '').lower() == truck_identifier.lower() or 
                t.get('plate_number', '').lower() == truck_identifier.lower()):
                truck = t
                break
        
        if not truck:
            success = True
            return f"Truck not found: {truck_identifier}"
        
        status_emoji = "🟢" if truck.get('status') == 'on_time' else "🔴" if truck.get('status') == 'delayed' else "🟡"
        
        response = f"🚛 **Truck {truck.get('plate_number')}** {status_emoji}\n\n"
        response += f"• **Driver**: {truck.get('driver_name')}\n"
        response += f"• **Status**: {truck.get('status')}\n"
        response += f"• **Location**: {truck.get('current_location', {}).get('name', 'Unknown')}\n"
        response += f"• **Destination**: {truck.get('destination', {}).get('name', 'Unknown')}\n"
        response += f"• **ETA**: {truck.get('estimated_arrival', 'Unknown')}\n"
        
        if truck.get('cargo'):
            cargo = truck.get('cargo')
            response += f"\n**Cargo:**\n"
            response += f"• Type: {cargo.get('type')}\n"
            response += f"• Weight: {cargo.get('weight')} kg\n"
            response += f"• Priority: {cargo.get('priority')}\n"
            response += f"• Description: {cargo.get('description')}\n"
        
        success = True
        return response
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error finding truck: {e}")
        return f"Error finding truck: {str(e)}"
    finally:
        _log_tool_invocation("find_truck_by_id", {"truck_identifier": truck_identifier}, start_time, success, error_msg)

@tool
async def get_all_locations() -> str:
    """
    Get all locations (depots, warehouses, stations) in the system.
    
    Returns:
        List of all locations organized by type
    """
    start_time = time.time()
    success = False
    error_msg = None
    
    try:
        logger.info("📍 Getting all locations")
        locations = await elasticsearch_service.get_all_documents("locations")
        
        response = f"📍 **All Locations** ({len(locations)} total)\n\n"
        
        # Group by type
        by_type = {}
        for loc in locations:
            loc_type = loc.get('type', 'unknown')
            if loc_type not in by_type:
                by_type[loc_type] = []
            by_type[loc_type].append(loc)
        
        for loc_type, locs in by_type.items():
            type_emoji = {"depot": "🏭", "warehouse": "🏢", "station": "🚉", "port": "⚓"}.get(loc_type, "📍")
            response += f"**{type_emoji} {loc_type.title()}s:**\n"
            for loc in locs:
                response += f"• {loc.get('name')} ({loc.get('region')})\n"
            response += "\n"
        
        success = True
        return response
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error getting locations: {e}")
        return f"Error getting locations: {str(e)}"
    finally:
        _log_tool_invocation("get_all_locations", {}, start_time, success, error_msg)