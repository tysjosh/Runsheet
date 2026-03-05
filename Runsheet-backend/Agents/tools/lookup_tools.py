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
    Find a specific asset by ID, name, or identifier. Works for all asset types
    including vehicles (trucks), vessels (boats), equipment (cranes), and containers.

    Args:
        truck_identifier: Asset identifier to search for. Can be a truck_id, plate_number,
            asset_id, vessel_name, container_number, or equipment_model
            (e.g., "GI-58A", "MO-84A", "crane 7", "CONT-123", "Sea Falcon")

    Returns:
        Detailed asset information including location, status, type, and type-specific details
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        logger.info(f"🔍 Finding asset: {truck_identifier}")
        assets = await elasticsearch_service.get_all_documents("trucks")

        identifier_lower = truck_identifier.lower()

        # Search across all identifier fields for any asset type
        asset = None
        for a in assets:
            if (a.get('truck_id', '').lower() == identifier_lower or
                a.get('plate_number', '').lower() == identifier_lower or
                a.get('asset_id', '').lower() == identifier_lower or
                a.get('vessel_name', '').lower() == identifier_lower or
                a.get('container_number', '').lower() == identifier_lower or
                a.get('equipment_model', '').lower() == identifier_lower or
                a.get('asset_name', '').lower() == identifier_lower):
                asset = a
                break

        if not asset:
            success = True
            return f"Asset not found: {truck_identifier}"

        asset_type = asset.get('asset_type', 'vehicle')
        asset_subtype = asset.get('asset_subtype', 'truck')

        # Choose emoji and display name based on asset type
        type_emojis = {
            'vehicle': '🚛',
            'vessel': '🚢',
            'equipment': '🏗️',
            'container': '📦',
        }
        emoji = type_emojis.get(asset_type, '🚛')

        display_name = (asset.get('asset_name') or asset.get('plate_number') or
                        asset.get('vessel_name') or asset.get('equipment_model') or
                        asset.get('container_number') or asset.get('truck_id') or 'Unknown')

        status_emoji = "🟢" if asset.get('status') == 'on_time' else "🔴" if asset.get('status') == 'delayed' else "🟡"

        response = f"{emoji} **{display_name}** [{asset_type}/{asset_subtype}] {status_emoji}\n\n"
        response += f"• **Type**: {asset_type} / {asset_subtype}\n"
        response += f"• **Status**: {asset.get('status')}\n"
        response += f"• **Location**: {asset.get('current_location', {}).get('name', 'Unknown')}\n"
        response += f"• **Destination**: {asset.get('destination', {}).get('name', 'Unknown')}\n"
        response += f"• **ETA**: {asset.get('estimated_arrival', 'Unknown')}\n"

        # Vehicle-specific details
        if asset_type == 'vehicle':
            if asset.get('driver_name'):
                response += f"• **Driver**: {asset.get('driver_name')}\n"
            if asset.get('plate_number'):
                response += f"• **Plate Number**: {asset.get('plate_number')}\n"
            if asset.get('cargo'):
                cargo = asset.get('cargo')
                response += f"\n**Cargo:**\n"
                response += f"• Type: {cargo.get('type')}\n"
                response += f"• Weight: {cargo.get('weight')} kg\n"
                response += f"• Priority: {cargo.get('priority')}\n"
                response += f"• Description: {cargo.get('description')}\n"

        # Vessel-specific details
        elif asset_type == 'vessel':
            if asset.get('vessel_name'):
                response += f"• **Vessel Name**: {asset.get('vessel_name')}\n"
            if asset.get('imo_number'):
                response += f"• **IMO Number**: {asset.get('imo_number')}\n"
            if asset.get('port_of_registry'):
                response += f"• **Port of Registry**: {asset.get('port_of_registry')}\n"
            if asset.get('draft_meters') is not None:
                response += f"• **Draft**: {asset.get('draft_meters')} m\n"
            if asset.get('vessel_capacity_tonnes') is not None:
                response += f"• **Capacity**: {asset.get('vessel_capacity_tonnes')} tonnes\n"

        # Equipment-specific details
        elif asset_type == 'equipment':
            if asset.get('equipment_model'):
                response += f"• **Model**: {asset.get('equipment_model')}\n"
            if asset.get('lifting_capacity_tonnes') is not None:
                response += f"• **Lifting Capacity**: {asset.get('lifting_capacity_tonnes')} tonnes\n"
            if asset.get('operational_radius_meters') is not None:
                response += f"• **Operational Radius**: {asset.get('operational_radius_meters')} m\n"

        # Container-specific details
        elif asset_type == 'container':
            if asset.get('container_number'):
                response += f"• **Container Number**: {asset.get('container_number')}\n"
            if asset.get('container_size'):
                response += f"• **Size**: {asset.get('container_size')}\n"
            if asset.get('seal_number'):
                response += f"• **Seal Number**: {asset.get('seal_number')}\n"
            if asset.get('contents_description'):
                response += f"• **Contents**: {asset.get('contents_description')}\n"
            if asset.get('weight_tonnes') is not None:
                response += f"• **Weight**: {asset.get('weight_tonnes')} tonnes\n"

        success = True
        return response
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error finding asset: {e}")
        return f"Error finding asset: {str(e)}"
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