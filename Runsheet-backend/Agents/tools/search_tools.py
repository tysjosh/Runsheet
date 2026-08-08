"""
Search tools for the logistics agent.

Validates:
- Requirement 5.5: WHEN an AI tool is invoked, THE Telemetry_Service SHALL log
  the tool name, input parameters, execution duration, and success/failure status
- Requirements 9.2, 9.4: Enforce tenant scoping on every ES read
"""

import logging
import time
from strands import tool
from services.elasticsearch_service import elasticsearch_service
from ops.middleware.tenant_guard import inject_tenant_filter
from .logging_wrapper import get_telemetry_service
from ._tenant_context import get_current_tenant

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

    The search is scoped to the current tenant (bound by the orchestrator
    when the chat request is received) so cross-tenant assets never leak.

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
    tenant_id = get_current_tenant()

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
            inner_es_query = {
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
            inner_es_query = {
                "query": must_clause
            }

        es_query = inject_tenant_filter(inner_es_query, tenant_id)

        response = await elasticsearch_service.search_documents("trucks", es_query, 5)
        results = [hit["_source"] for hit in response["hits"]["hits"]]

        if not results:
            success = True
            filter_msg = f" with asset_type='{asset_type}'" if asset_type else ""
            return f"No fleet data found for query: '{query}'{filter_msg}"

        type_label = asset_type if asset_type else "assets"
        # "Showing", not "Found": this is a capped page, and phrasing a page
        # length as a total is what let the agent report a count it had never
        # measured. A true total would need semantic_search to return one.
        response_text = f"🚛 Showing {len(results)} {type_label} matching '{query}':\n\n"
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
        logger.exception("Error searching fleet data")
        return f"Error searching fleet data: {str(e)}"
    finally:
        _log_tool_invocation("search_fleet_data", {"query": query, "asset_type": asset_type}, start_time, success, error_msg)


@tool
# ``search_orders`` used to live here and has been removed rather than repaired.
#
# It searched an index named ``orders``, which does not exist — live orders are in
# ``fuel_orders_current``. It also capped at 5 hits and returned ``len(page)``
# phrased as a count, so it could not report a true total even against a healthy
# index, and it rendered ``customer`` / ``value`` / ``items`` / ``priority``
# against a document whose fields are ``customer_name`` / ``gallons_requested`` /
# ``product_code`` / ``status``.
#
# ``Agents.tools.order_tools.search_orders`` already queries the live index with
# real filters and a true total, and is what ``mainagent``'s system prompt
# documents. Keeping a second tool of the same name reaching different data is
# what let one reply report two different counts for the same question.


@tool
async def search_support_tickets(query: str) -> str:
    """
    Search support tickets using natural language.

    The search is scoped to the current tenant so cross-tenant tickets never leak.

    Args:
        query: Natural language search query (e.g., "delivery delays", "damaged goods")
    
    Returns:
        Search results from support tickets database
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()

    try:
        logger.info(f"🔍 Searching support tickets for: {query}")
        
        # First try semantic search
        try:
            results = await elasticsearch_service.semantic_search(tenant_id, "support_tickets", query, ["issue", "description"], 5)
        except Exception:
            logger.warning(
                "Semantic search failed, trying tenant-scoped fallback",
                exc_info=True,
            )
            # Fallback: run a tenant-scoped match_all and filter in Python so we
            # still avoid leaking data from other tenants if semantic search is
            # unavailable (missing index / circuit open).
            fallback_query = inject_tenant_filter({"query": {"match_all": {}}}, tenant_id)
            fallback_resp = await elasticsearch_service.search_documents(
                "support_tickets", fallback_query, size=100
            )
            all_tickets = [hit["_source"] for hit in fallback_resp.get("hits", {}).get("hits", [])]
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
        
        response = f"🎫 Showing {len(results)} support tickets matching '{query}':\n\n"
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
        logger.exception("Error searching support tickets")
        return f"Error searching support tickets: {str(e)}"
    finally:
        _log_tool_invocation("search_support_tickets", {"query": query}, start_time, success, error_msg)

@tool
async def search_inventory(query: str) -> str:
    """
    Search inventory items using semantic search.

    The search is scoped to the current tenant so cross-tenant inventory never leaks.

    Args:
        query: Natural language query (e.g., "diesel fuel", "brake parts", "low stock items")
    
    Returns:
        Matching inventory items with stock levels and locations
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()

    try:
        logger.info(f"📦 Searching inventory for: {query}")
        
        # First try semantic search
        try:
            results = await elasticsearch_service.semantic_search(tenant_id, "inventory", query, ["name"], 10)
        except Exception:
            logger.warning(
                "Semantic search failed, trying tenant-scoped fallback",
                exc_info=True,
            )
            # Fallback: run a tenant-scoped match_all and filter in Python so
            # results are still tenant-isolated when semantic search is down.
            fallback_query = inject_tenant_filter({"query": {"match_all": {}}}, tenant_id)
            fallback_resp = await elasticsearch_service.search_documents(
                "inventory", fallback_query, size=200
            )
            all_items = [hit["_source"] for hit in fallback_resp.get("hits", {}).get("hits", [])]
            results = [item for item in all_items if query.lower() in item.get('name', '').lower()]
        
        if not results:
            success = True
            return f"No inventory items found for: '{query}'"
        
        response = f"📦 Showing {len(results)} inventory items:\n\n"
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
        logger.exception("Error searching inventory")
        return f"Error searching inventory: {str(e)}"
    finally:
        _log_tool_invocation("search_inventory", {"query": query}, start_time, success, error_msg)
