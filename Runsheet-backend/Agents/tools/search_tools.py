"""
Search tools for the logistics agent.

Validates:
- Requirement 5.5: WHEN an AI tool is invoked, THE Telemetry_Service SHALL log
  the tool name, input parameters, execution duration, and success/failure status
"""

import logging
import time
from strands import tool
from services.elasticsearch_service import elasticsearch_service
from .logging_wrapper import get_telemetry_service

logger = logging.getLogger(__name__)


def _log_tool_invocation(tool_name: str, input_params: dict, start_time: float, 
                         success: bool, error: str = None):
    """Helper to log tool invocations with telemetry service."""
    duration_ms = (time.time() - start_time) * 1000
    telemetry = get_telemetry_service()
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
async def search_fleet_data(query: str, asset_type: str = None) -> str:
    """
    Search fleet and asset data using natural language. Supports all asset types
    including vehicles, vessels, equipment, and containers.

    Args:
        query: Natural language search query (e.g., "trucks carrying perishables",
               "delayed vehicles", "search for all vessels", "find idle equipment",
               "containers in transit", "show me all boats")
        asset_type: Optional asset type filter. One of: "vehicle", "vessel",
                    "equipment", "container". When provided, results are limited
                    to the specified asset type.

    Returns:
        Search results from fleet database
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        logger.info(f"🔍 Searching fleet data for: {query}" + (f" (asset_type={asset_type})" if asset_type else ""))

        # Build the base multi_match query
        must_clause = {
            "multi_match": {
                "query": query,
                "fields": ["cargo.description", "driver_name", "status", "asset_name", "vessel_name", "equipment_model", "container_number"],
                "type": "best_fields"
            }
        }

        # When asset_type is provided, wrap in a bool query with a term filter
        if asset_type:
            es_query = {
                "query": {
                    "bool": {
                        "must": [must_clause],
                        "filter": [
                            {"term": {"asset_type": asset_type}}
                        ]
                    }
                }
            }
        else:
            es_query = {
                "query": must_clause
            }

        response = await elasticsearch_service.search_documents("trucks", es_query, 5)
        results = [hit["_source"] for hit in response["hits"]["hits"]]

        if not results:
            success = True
            filter_msg = f" with asset_type='{asset_type}'" if asset_type else ""
            return f"No fleet data found for query: '{query}'{filter_msg}"

        type_label = asset_type if asset_type else "assets"
        response_text = f"🚛 Found {len(results)} {type_label} matching '{query}':\n\n"
        for asset in results:
            # Use asset_name or plate_number as the display name
            display_name = asset.get('asset_name') or asset.get('plate_number') or asset.get('vessel_name') or asset.get('equipment_model') or asset.get('container_number') or 'Unknown'
            asset_type_label = asset.get('asset_type', 'vehicle')
            asset_subtype_label = asset.get('asset_subtype', 'truck')

            response_text += f"• **{display_name}** [{asset_type_label}/{asset_subtype_label}] - {asset.get('driver_name', 'N/A')}\n"
            response_text += f"  Status: {asset.get('status')}\n"
            if asset.get('cargo'):
                response_text += f"  Cargo: {asset.get('cargo', {}).get('description', 'N/A')}\n"
            response_text += f"  Location: {asset.get('current_location', {}).get('name', 'Unknown')}\n\n"

        success = True
        return response_text
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error searching fleet data: {e}")
        return f"Error searching fleet data: {str(e)}"
    finally:
        _log_tool_invocation("search_fleet_data", {"query": query, "asset_type": asset_type}, start_time, success, error_msg)


@tool
async def search_orders(query: str) -> str:
    """
    Search order data using natural language.
    
    Args:
        query: Natural language search query (e.g., "network equipment orders", "high priority deliveries")
    
    Returns:
        Search results from orders database
    """
    start_time = time.time()
    success = False
    error_msg = None
    
    try:
        logger.info(f"🔍 Searching orders for: {query}")
        results = await elasticsearch_service.semantic_search("orders", query, ["items", "customer"], 5)
        
        if not results:
            success = True
            return f"No orders found for query: '{query}'"
        
        response = f"📦 Found {len(results)} orders matching '{query}':\n\n"
        for order in results:
            response += f"• **{order.get('order_id')}** - {order.get('customer')}\n"
            response += f"  Status: {order.get('status')}\n"
            response += f"  Value: ${order.get('value', 0):,.2f}\n"
            response += f"  Items: {order.get('items', 'N/A')}\n"
            response += f"  Priority: {order.get('priority', 'N/A')}\n\n"
        
        success = True
        return response
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error searching orders: {e}")
        return f"Error searching orders: {str(e)}"
    finally:
        _log_tool_invocation("search_orders", {"query": query}, start_time, success, error_msg)

@tool
async def search_support_tickets(query: str) -> str:
    """
    Search support tickets using natural language.
    
    Args:
        query: Natural language search query (e.g., "delivery delays", "damaged goods")
    
    Returns:
        Search results from support tickets database
    """
    start_time = time.time()
    success = False
    error_msg = None
    
    try:
        logger.info(f"🔍 Searching support tickets for: {query}")
        
        # First try semantic search
        try:
            results = await elasticsearch_service.semantic_search("support_tickets", query, ["issue", "description"], 5)
        except Exception as search_error:
            logger.warning(f"Semantic search failed, trying get_all_documents: {search_error}")
            # Fallback to get all and filter
            all_tickets = await elasticsearch_service.get_all_documents("support_tickets")
            if query.lower() in ["all", "all support tickets", "support tickets"]:
                results = all_tickets
            else:
                results = [ticket for ticket in all_tickets if 
                          query.lower() in ticket.get('issue', '').lower() or 
                          query.lower() in ticket.get('description', '').lower() or
                          query.lower() in ticket.get('ticket_id', '').lower()]
        
        if not results:
            success = True
            return f"No support tickets found for query: '{query}'"
        
        response = f"🎫 Found {len(results)} support tickets matching '{query}':\n\n"
        for ticket in results:
            response += f"• **{ticket.get('ticket_id')}** - {ticket.get('customer')}\n"
            response += f"  Issue: {ticket.get('issue')}\n"
            response += f"  Priority: {ticket.get('priority')}\n"
            response += f"  Status: {ticket.get('status')}\n"
            response += f"  Description: {ticket.get('description', 'N/A')[:100]}...\n\n"
        
        success = True
        return response
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error searching support tickets: {e}")
        return f"Error searching support tickets: {str(e)}"
    finally:
        _log_tool_invocation("search_support_tickets", {"query": query}, start_time, success, error_msg)

@tool
async def search_inventory(query: str) -> str:
    """
    Search inventory items using semantic search.
    
    Args:
        query: Natural language query (e.g., "diesel fuel", "brake parts", "low stock items")
    
    Returns:
        Matching inventory items with stock levels and locations
    """
    start_time = time.time()
    success = False
    error_msg = None
    
    try:
        logger.info(f"📦 Searching inventory for: {query}")
        
        # First try semantic search
        try:
            results = await elasticsearch_service.semantic_search("inventory", query, ["name"], 10)
        except Exception as search_error:
            logger.warning(f"Semantic search failed, trying get_all_documents: {search_error}")
            # Fallback to get all and filter
            all_items = await elasticsearch_service.get_all_documents("inventory")
            results = [item for item in all_items if query.lower() in item.get('name', '').lower()]
        
        if not results:
            success = True
            return f"No inventory items found for: '{query}'"
        
        response = f"📦 Found {len(results)} inventory items:\n\n"
        for item in results:
            status_emoji = "🟢" if item.get('status') == 'in_stock' else "🟡" if item.get('status') == 'low_stock' else "🔴"
            response += f"{status_emoji} **{item.get('name')}**\n"
            response += f"  • Quantity: {item.get('quantity')} {item.get('unit')}\n"
            response += f"  • Location: {item.get('location')}\n"
            response += f"  • Status: {item.get('status')}\n\n"
        
        success = True
        return response
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error searching inventory: {e}")
        return f"Error searching inventory: {str(e)}"
    finally:
        _log_tool_invocation("search_inventory", {"query": query}, start_time, success, error_msg)