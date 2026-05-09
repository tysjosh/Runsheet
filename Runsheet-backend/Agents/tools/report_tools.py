"""
Report generation tools for comprehensive analysis.

Validates:
- Requirement 5.5: WHEN an AI tool is invoked, THE Telemetry_Service SHALL log
  the tool name, input parameters, execution duration, and success/failure status
- Requirements 9.2, 9.4: Enforce tenant scoping on every ES read
"""

import logging
import time
from datetime import datetime
from strands import tool
from services.elasticsearch_service import elasticsearch_service
from ops.middleware.tenant_guard import inject_tenant_filter
from ._tenant_context import get_current_tenant

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
async def generate_operations_report() -> str:
    """
    Generate a comprehensive operations report combining fleet, inventory, and support data.

    The report is scoped to the current tenant so cross-tenant data never leaks.

    Returns:
        Structured operations report with current status and recommendations
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()

    try:
        logger.info("📋 Generating operations report")
        
        # Gather data from multiple sources (all tenant-scoped)
        fleet_query = inject_tenant_filter({"query": {"match_all": {}}}, tenant_id)
        fleet_resp = await elasticsearch_service.search_documents("trucks", fleet_query, size=1000)
        fleet_data = [hit["_source"] for hit in fleet_resp.get("hits", {}).get("hits", [])]

        inv_query = inject_tenant_filter({"query": {"match_all": {}}}, tenant_id)
        inv_resp = await elasticsearch_service.search_documents("inventory", inv_query, size=1000)
        inventory_data = [hit["_source"] for hit in inv_resp.get("hits", {}).get("hits", [])]

        tix_query = inject_tenant_filter({"query": {"match_all": {}}}, tenant_id)
        tix_resp = await elasticsearch_service.search_documents("support_tickets", tix_query, size=1000)
        tickets_data = [hit["_source"] for hit in tix_resp.get("hits", {}).get("hits", [])]
        
        # Calculate metrics
        total_trucks = len(fleet_data)
        on_time_trucks = len([t for t in fleet_data if t.get('status') == 'on_time'])
        delayed_trucks = len([t for t in fleet_data if t.get('status') == 'delayed'])
        
        low_stock_items = len([i for i in inventory_data if i.get('status') == 'low_stock'])
        out_of_stock_items = len([i for i in inventory_data if i.get('status') == 'out_of_stock'])
        
        urgent_tickets = len([t for t in tickets_data if t.get('priority') == 'urgent'])
        open_tickets = len([t for t in tickets_data if t.get('status') == 'open'])
        
        on_time_pct = (on_time_trucks / total_trucks * 100) if total_trucks > 0 else 0
        delayed_pct = (delayed_trucks / total_trucks * 100) if total_trucks > 0 else 0
        
        report = f"""# 📋 Operations Report
*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*

## 🚛 Fleet Status
- **Total Trucks**: {total_trucks}
- **On Time**: {on_time_trucks} ({on_time_pct:.1f}%)
- **Delayed**: {delayed_trucks} ({delayed_pct:.1f}%)

## 📦 Inventory Status
- **Total Items**: {len(inventory_data)}
- **Low Stock Alerts**: {low_stock_items}
- **Out of Stock**: {out_of_stock_items}

## 🎫 Support Status
- **Open Tickets**: {open_tickets}
- **Urgent Issues**: {urgent_tickets}

## 🎯 Key Recommendations
"""
        
        # Add recommendations based on data
        if delayed_trucks > total_trucks * 0.3:
            report += f"- ⚠️ **High delay rate** ({delayed_trucks} trucks delayed) - investigate route optimization\n"
        
        if out_of_stock_items > 0:
            report += f"- 🚨 **Critical**: {out_of_stock_items} items out of stock - immediate restocking needed\n"
        
        if urgent_tickets > 0:
            report += f"- 🔥 **Urgent**: {urgent_tickets} urgent tickets require immediate attention\n"
        
        if low_stock_items > 2:
            report += f"- 📦 **Inventory**: {low_stock_items} items running low - schedule restocking\n"
        
        success = True
        return report
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error generating operations report: {e}")
        return f"Error generating operations report: {str(e)}"
    finally:
        _log_tool_invocation("generate_operations_report", {}, start_time, success, error_msg)

@tool
async def generate_performance_report() -> str:
    """
    Generate a performance analysis report with metrics and trends.

    The report is scoped to the current tenant so cross-tenant analytics never leak.

    Returns:
        Detailed performance report with analytics and insights
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()

    try:
        logger.info("📊 Generating performance report")
        
        # Get analytics data
        metrics = await elasticsearch_service.get_current_metrics(tenant_id)
        routes = await elasticsearch_service.get_route_performance_data(tenant_id)
        delays = await elasticsearch_service.get_delay_causes_data(tenant_id)
        regions = await elasticsearch_service.get_regional_performance_data(tenant_id)
        
        report = f"""# 📊 Performance Analysis Report
*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*

## 🎯 Key Performance Indicators
"""
        
        for key, metric in metrics.items():
            trend_emoji = "📈" if metric.get("trend") == "up" else "📉"
            report += f"- **{metric.get('title')}**: {metric.get('value')} {trend_emoji} ({metric.get('change')})\n"
        
        report += f"""
## 🛣️ Route Performance
"""
        for route in sorted(routes, key=lambda x: x.get('performance', 0), reverse=True):
            performance = route.get('performance', 0)
            status_emoji = "🟢" if performance >= 90 else "🟡" if performance >= 80 else "🔴"
            report += f"- {status_emoji} **{route.get('name')}**: {performance}%\n"
        
        report += f"""
## ⏰ Delay Analysis
"""
        for cause in sorted(delays, key=lambda x: x.get('percentage', 0), reverse=True):
            report += f"- **{cause.get('name')}**: {cause.get('percentage')}%\n"
        
        report += f"""
## 🌍 Regional Performance
"""
        for region in sorted(regions, key=lambda x: x.get('onTimePercentage', 0), reverse=True):
            performance = region.get('onTimePercentage', 0)
            status_emoji = "🟢" if performance >= 90 else "🟡" if performance >= 80 else "🔴"
            report += f"- {status_emoji} **{region.get('name')}**: {performance}% on-time\n"
        
        # Add insights
        report += "\n## 💡 Key Insights\n"
        
        if routes:
            best_route = max(routes, key=lambda x: x.get('performance', 0))
            worst_route = min(routes, key=lambda x: x.get('performance', 0))
            report += f"- 🏆 **Best performing route**: {best_route.get('name')} ({best_route.get('performance')}%)\n"
            report += f"- 🎯 **Needs improvement**: {worst_route.get('name')} ({worst_route.get('performance')}%)\n"
        else:
            report += "- ℹ️ No route performance data available yet\n"
        
        if delays:
            main_delay = max(delays, key=lambda x: x.get('percentage', 0))
            report += f"- ⚠️ **Main delay cause**: {main_delay.get('name')} ({main_delay.get('percentage')}%)\n"
        else:
            report += "- ℹ️ No delay data recorded\n"
        
        if not regions:
            report += "- ℹ️ No regional performance data available yet\n"
        
        success = True
        return report
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error generating performance report: {e}")
        return f"Error generating performance report: {str(e)}"
    finally:
        _log_tool_invocation("generate_performance_report", {}, start_time, success, error_msg)

@tool
async def generate_incident_analysis(issue_description: str = "") -> str:
    """
    Generate an incident analysis report by examining related data across systems.

    The analysis is scoped to the current tenant so cross-tenant data never leaks.

    Args:
        issue_description: Description of the incident to analyze
    
    Returns:
        Comprehensive incident analysis with related data and recommendations
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()

    try:
        logger.info(f"🔍 Generating incident analysis for: {issue_description}")
        
        report = f"""# 🔍 Incident Analysis Report
*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*
*Issue*: {issue_description or 'General system analysis'}

"""
        
        # Get related support tickets (tenant-scoped)
        if issue_description:
            tickets = await elasticsearch_service.semantic_search(
                tenant_id, "support_tickets", issue_description, ["issue", "description"], 5
            )
        else:
            tix_query = inject_tenant_filter({"query": {"match_all": {}}}, tenant_id)
            tix_resp = await elasticsearch_service.search_documents("support_tickets", tix_query, size=1000)
            tickets = [hit["_source"] for hit in tix_resp.get("hits", {}).get("hits", [])]
            tickets = [t for t in tickets if t.get('status') in ['open', 'in_progress']][:5]
        
        if tickets:
            report += "## 🎫 Related Support Tickets\n"
            for ticket in tickets:
                priority_emoji = "🚨" if ticket.get('priority') == 'urgent' else "🔴" if ticket.get('priority') == 'high' else "🟡"
                report += f"- {priority_emoji} **{ticket.get('ticket_id')}**: {ticket.get('issue')} ({ticket.get('status')})\n"
        
        # Check for delayed trucks (tenant-scoped)
        trucks_query = inject_tenant_filter({"query": {"match_all": {}}}, tenant_id)
        trucks_resp = await elasticsearch_service.search_documents("trucks", trucks_query, size=1000)
        trucks = [hit["_source"] for hit in trucks_resp.get("hits", {}).get("hits", [])]
        delayed_trucks = [t for t in trucks if t.get('status') == 'delayed']
        
        if delayed_trucks:
            report += f"\n## 🚛 Affected Fleet ({len(delayed_trucks)} delayed trucks)\n"
            for truck in delayed_trucks[:5]:
                report += f"- **{truck.get('plate_number')}** - {truck.get('driver_name')} (ETA: {truck.get('estimated_arrival', 'Unknown')})\n"
        
        # Check inventory issues (tenant-scoped)
        inv_query = inject_tenant_filter({"query": {"match_all": {}}}, tenant_id)
        inv_resp = await elasticsearch_service.search_documents("inventory", inv_query, size=1000)
        inventory = [hit["_source"] for hit in inv_resp.get("hits", {}).get("hits", [])]
        critical_items = [i for i in inventory if i.get('status') in ['low_stock', 'out_of_stock']]
        
        if critical_items:
            report += f"\n## 📦 Inventory Issues ({len(critical_items)} items)\n"
            for item in critical_items:
                status_emoji = "🔴" if item.get('status') == 'out_of_stock' else "🟡"
                report += f"- {status_emoji} **{item.get('name')}**: {item.get('quantity')} {item.get('unit')} at {item.get('location')}\n"
        
        report += f"""
## 🎯 Recommended Actions
- Review and prioritize urgent support tickets
- Investigate root causes of delays
- Ensure critical inventory is restocked
- Monitor affected routes for improvements
"""
        
        success = True
        return report
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error generating incident analysis: {e}")
        return f"Error generating incident analysis: {str(e)}"
    finally:
        _log_tool_invocation("generate_incident_analysis", {"issue_description": issue_description}, start_time, success, error_msg)
