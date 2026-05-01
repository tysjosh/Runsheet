"""
AI agent tools for fuel monitoring queries and reports.

All tools are read-only and tenant-scoped. They query the fuel_stations
and fuel_events Elasticsearch indices to provide fuel insights through
natural language interactions.

Validates:
- Requirement 7.1: search_fuel_stations tool
- Requirement 7.2: get_fuel_summary tool
- Requirement 7.3: get_fuel_consumption_history tool
- Requirement 7.4: generate_fuel_report tool
- Requirement 7.5: Tenant scoping enforcement
- Requirement 7.6: Read-only mode
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from strands import tool
from services.elasticsearch_service import elasticsearch_service
from .logging_wrapper import get_telemetry_service

logger = logging.getLogger(__name__)

DEFAULT_TENANT_ID = "dev-tenant"


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
async def search_fuel_stations(query: str, fuel_type: str = None, status: str = None,
                                tenant_id: str = DEFAULT_TENANT_ID) -> str:
    """
    Search fuel stations by name, type, location, or stock status.

    Args:
        query: Natural language search query (e.g., "Industrial Area", "diesel stations",
               "low stock stations near Nairobi")
        fuel_type: Optional fuel type filter. One of: "AGO", "PMS", "ATK", "LPG"
        status: Optional stock status filter. One of: "normal", "low", "critical", "empty"
        tenant_id: Tenant identifier for data scoping

    Returns:
        Formatted text listing matching fuel stations with stock levels and status
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        logger.info(
            f"⛽ Searching fuel stations for: {query}"
            + (f" (fuel_type={fuel_type})" if fuel_type else "")
            + (f" (status={status})" if status else "")
        )

        # Build bool query with tenant scoping
        must_clauses = [
            {
                "multi_match": {
                    "query": query,
                    "fields": ["name", "location_name", "station_id"],
                    "type": "best_fields"
                }
            }
        ]

        filter_clauses = [
            {"term": {"tenant_id": tenant_id}}
        ]

        if fuel_type:
            filter_clauses.append({"term": {"fuel_type": fuel_type}})
        if status:
            filter_clauses.append({"term": {"status": status}})

        es_query = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    "filter": filter_clauses
                }
            }
        }

        response = await elasticsearch_service.search_documents("fuel_stations", es_query, 10)
        results = [hit["_source"] for hit in response["hits"]["hits"]]

        if not results:
            success = True
            filter_msg = ""
            if fuel_type:
                filter_msg += f" with fuel_type='{fuel_type}'"
            if status:
                filter_msg += f" with status='{status}'"
            return f"No fuel stations found for query: '{query}'{filter_msg}"

        response_text = f"⛽ Found {len(results)} fuel station(s) matching '{query}':\n\n"
        for station in results:
            capacity = station.get("capacity_liters", 0)
            stock = station.get("current_stock_liters", 0)
            pct = (stock / capacity * 100) if capacity > 0 else 0
            status_emoji = {"normal": "🟢", "low": "🟡", "critical": "🔴", "empty": "⚫"}.get(
                station.get("status", ""), "⚪"
            )

            response_text += f"{status_emoji} **{station.get('name', 'Unknown')}** ({station.get('station_id', '')})\n"
            response_text += f"  Fuel Type: {station.get('fuel_type', 'N/A')}\n"
            response_text += f"  Stock: {stock:,.0f} / {capacity:,.0f} L ({pct:.1f}%)\n"
            response_text += f"  Status: {station.get('status', 'N/A')}\n"
            response_text += f"  Daily Consumption: {station.get('daily_consumption_rate', 0):,.1f} L/day\n"
            response_text += f"  Days Until Empty: {station.get('days_until_empty', 0):.1f}\n"
            if station.get("location_name"):
                response_text += f"  Location: {station.get('location_name')}\n"
            response_text += "\n"

        success = True
        return response_text
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error searching fuel stations: {e}")
        return f"Error searching fuel stations: {str(e)}"
    finally:
        _log_tool_invocation(
            "search_fuel_stations",
            {"query": query, "fuel_type": fuel_type, "status": status, "tenant_id": tenant_id},
            start_time, success, error_msg
        )


@tool
async def get_fuel_summary(tenant_id: str = DEFAULT_TENANT_ID) -> str:
    """
    Get network-wide fuel summary including total stock, capacity, alerts,
    and days until empty across all stations.

    Args:
        tenant_id: Tenant identifier for data scoping

    Returns:
        Formatted text with network-wide fuel summary metrics
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        logger.info("⛽ Fetching network-wide fuel summary")

        es_query = {
            "size": 0,
            "query": {
                "term": {"tenant_id": tenant_id}
            },
            "aggs": {
                "total_capacity": {"sum": {"field": "capacity_liters"}},
                "total_stock": {"sum": {"field": "current_stock_liters"}},
                "total_daily_consumption": {"sum": {"field": "daily_consumption_rate"}},
                "avg_days_until_empty": {"avg": {"field": "days_until_empty"}},
                "by_status": {
                    "terms": {"field": "status"}
                }
            }
        }

        response = await elasticsearch_service.search_documents("fuel_stations", es_query, 0)

        total_stations = response["hits"]["total"]["value"]
        aggs = response.get("aggregations", {})

        total_capacity = aggs.get("total_capacity", {}).get("value", 0)
        total_stock = aggs.get("total_stock", {}).get("value", 0)
        total_daily = aggs.get("total_daily_consumption", {}).get("value", 0)
        avg_days = aggs.get("avg_days_until_empty", {}).get("value", 0)

        # Parse status buckets
        status_counts = {"normal": 0, "low": 0, "critical": 0, "empty": 0}
        for bucket in aggs.get("by_status", {}).get("buckets", []):
            key = bucket.get("key", "")
            if key in status_counts:
                status_counts[key] = bucket.get("doc_count", 0)

        active_alerts = status_counts["low"] + status_counts["critical"] + status_counts["empty"]
        overall_pct = (total_stock / total_capacity * 100) if total_capacity > 0 else 0

        response_text = "⛽ **Fuel Network Summary**\n\n"
        response_text += f"Total Stations: {total_stations}\n"
        response_text += f"Total Capacity: {total_capacity:,.0f} L\n"
        response_text += f"Total Current Stock: {total_stock:,.0f} L ({overall_pct:.1f}%)\n"
        response_text += f"Total Daily Consumption: {total_daily:,.1f} L/day\n"
        response_text += f"Average Days Until Empty: {avg_days:.1f}\n\n"
        response_text += "**Station Status Breakdown:**\n"
        response_text += f"  🟢 Normal: {status_counts['normal']}\n"
        response_text += f"  🟡 Low: {status_counts['low']}\n"
        response_text += f"  🔴 Critical: {status_counts['critical']}\n"
        response_text += f"  ⚫ Empty: {status_counts['empty']}\n"
        response_text += f"\nActive Alerts: {active_alerts}\n"

        success = True
        return response_text
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error fetching fuel summary: {e}")
        return f"Error fetching fuel summary: {str(e)}"
    finally:
        _log_tool_invocation(
            "get_fuel_summary",
            {"tenant_id": tenant_id},
            start_time, success, error_msg
        )


@tool
async def get_fuel_consumption_history(station_id: str = None, asset_id: str = None,
                                        days: int = 7,
                                        tenant_id: str = DEFAULT_TENANT_ID) -> str:
    """
    Get fuel consumption events for a specific station or asset over a date range.

    Args:
        station_id: Optional station ID to filter consumption events
        asset_id: Optional asset ID (truck/vehicle) to filter consumption events
        days: Number of days to look back (default: 7)
        tenant_id: Tenant identifier for data scoping

    Returns:
        Formatted text listing consumption events with quantities and timestamps
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        logger.info(
            f"⛽ Fetching fuel consumption history"
            + (f" for station={station_id}" if station_id else "")
            + (f" for asset={asset_id}" if asset_id else "")
            + f" over last {days} days"
        )

        now = datetime.now(timezone.utc)
        start_date = now - timedelta(days=days)

        filter_clauses = [
            {"term": {"tenant_id": tenant_id}},
            {"term": {"event_type": "consumption"}},
            {"range": {"event_timestamp": {"gte": start_date.isoformat(), "lte": now.isoformat()}}}
        ]

        if station_id:
            filter_clauses.append({"term": {"station_id": station_id}})
        if asset_id:
            filter_clauses.append({"term": {"asset_id": asset_id}})

        es_query = {
            "query": {
                "bool": {
                    "filter": filter_clauses
                }
            },
            "sort": [{"event_timestamp": {"order": "desc"}}]
        }

        response = await elasticsearch_service.search_documents("fuel_events", es_query, 50)
        results = [hit["_source"] for hit in response["hits"]["hits"]]

        if not results:
            success = True
            filter_msg = ""
            if station_id:
                filter_msg += f" station={station_id}"
            if asset_id:
                filter_msg += f" asset={asset_id}"
            return f"No consumption events found for the last {days} days{filter_msg}"

        total_consumed = sum(e.get("quantity_liters", 0) for e in results)

        response_text = f"⛽ **Fuel Consumption History** (last {days} days)\n"
        response_text += f"Total Events: {len(results)} | Total Consumed: {total_consumed:,.1f} L\n\n"

        for event in results:
            ts = event.get("event_timestamp", "N/A")
            if isinstance(ts, str) and len(ts) > 16:
                ts = ts[:16].replace("T", " ")

            response_text += f"• **{event.get('quantity_liters', 0):,.1f} L** — {event.get('fuel_type', 'N/A')}\n"
            response_text += f"  Station: {event.get('station_id', 'N/A')}\n"
            response_text += f"  Asset: {event.get('asset_id', 'N/A')}\n"
            response_text += f"  Operator: {event.get('operator_id', 'N/A')}\n"
            response_text += f"  Time: {ts}\n"
            if event.get("odometer_reading"):
                response_text += f"  Odometer: {event.get('odometer_reading'):,.0f} km\n"
            response_text += "\n"

        success = True
        return response_text
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error fetching fuel consumption history: {e}")
        return f"Error fetching fuel consumption history: {str(e)}"
    finally:
        _log_tool_invocation(
            "get_fuel_consumption_history",
            {"station_id": station_id, "asset_id": asset_id, "days": days, "tenant_id": tenant_id},
            start_time, success, error_msg
        )


@tool
async def generate_fuel_report(days: int = 7, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    """
    Generate a markdown fuel operations report covering stock levels,
    consumption trends, alert history, and refill recommendations.

    Args:
        days: Number of days to cover in the report (default: 7)
        tenant_id: Tenant identifier for data scoping

    Returns:
        Markdown-formatted fuel operations report
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        logger.info(f"⛽ Generating fuel report for last {days} days")

        now = datetime.now(timezone.utc)
        start_date = now - timedelta(days=days)

        # --- 1. Fetch network summary via aggregation ---
        summary_query = {
            "size": 0,
            "query": {"term": {"tenant_id": tenant_id}},
            "aggs": {
                "total_capacity": {"sum": {"field": "capacity_liters"}},
                "total_stock": {"sum": {"field": "current_stock_liters"}},
                "total_daily_consumption": {"sum": {"field": "daily_consumption_rate"}},
                "avg_days_until_empty": {"avg": {"field": "days_until_empty"}},
                "by_status": {"terms": {"field": "status"}},
                "by_fuel_type": {
                    "terms": {"field": "fuel_type"},
                    "aggs": {
                        "stock": {"sum": {"field": "current_stock_liters"}},
                        "capacity": {"sum": {"field": "capacity_liters"}}
                    }
                }
            }
        }
        summary_resp = await elasticsearch_service.search_documents("fuel_stations", summary_query, 0)
        total_stations = summary_resp["hits"]["total"]["value"]
        aggs = summary_resp.get("aggregations", {})

        total_capacity = aggs.get("total_capacity", {}).get("value", 0)
        total_stock = aggs.get("total_stock", {}).get("value", 0)
        total_daily = aggs.get("total_daily_consumption", {}).get("value", 0)
        avg_days = aggs.get("avg_days_until_empty", {}).get("value", 0)

        status_counts = {"normal": 0, "low": 0, "critical": 0, "empty": 0}
        for bucket in aggs.get("by_status", {}).get("buckets", []):
            key = bucket.get("key", "")
            if key in status_counts:
                status_counts[key] = bucket.get("doc_count", 0)

        # --- 2. Fetch stations with alerts (status != normal) ---
        alerts_query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"status": ["low", "critical", "empty"]}}
                    ]
                }
            },
            "sort": [{"days_until_empty": {"order": "asc"}}]
        }
        alerts_resp = await elasticsearch_service.search_documents("fuel_stations", alerts_query, 20)
        alert_stations = [hit["_source"] for hit in alerts_resp["hits"]["hits"]]

        # --- 3. Fetch recent consumption events ---
        consumption_query = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"event_type": "consumption"}},
                        {"range": {"event_timestamp": {"gte": start_date.isoformat(), "lte": now.isoformat()}}}
                    ]
                }
            },
            "aggs": {
                "total_consumed": {"sum": {"field": "quantity_liters"}},
                "by_fuel_type": {
                    "terms": {"field": "fuel_type"},
                    "aggs": {
                        "consumed": {"sum": {"field": "quantity_liters"}}
                    }
                },
                "daily_trend": {
                    "date_histogram": {
                        "field": "event_timestamp",
                        "calendar_interval": "day"
                    },
                    "aggs": {
                        "consumed": {"sum": {"field": "quantity_liters"}}
                    }
                }
            }
        }
        consumption_resp = await elasticsearch_service.search_documents("fuel_events", consumption_query, 0)
        cons_aggs = consumption_resp.get("aggregations", {})
        total_consumed = cons_aggs.get("total_consumed", {}).get("value", 0)

        # --- Build the report ---
        overall_pct = (total_stock / total_capacity * 100) if total_capacity > 0 else 0
        report_date = now.strftime("%Y-%m-%d %H:%M UTC")

        report = f"# ⛽ Fuel Operations Report\n"
        report += f"**Period:** Last {days} days | **Generated:** {report_date}\n\n"

        # Network overview
        report += "## Network Overview\n\n"
        report += f"| Metric | Value |\n"
        report += f"|--------|-------|\n"
        report += f"| Total Stations | {total_stations} |\n"
        report += f"| Total Capacity | {total_capacity:,.0f} L |\n"
        report += f"| Current Stock | {total_stock:,.0f} L ({overall_pct:.1f}%) |\n"
        report += f"| Daily Consumption Rate | {total_daily:,.1f} L/day |\n"
        report += f"| Avg Days Until Empty | {avg_days:.1f} |\n\n"

        # Status breakdown
        report += "## Station Status\n\n"
        report += f"- 🟢 Normal: {status_counts['normal']}\n"
        report += f"- 🟡 Low: {status_counts['low']}\n"
        report += f"- 🔴 Critical: {status_counts['critical']}\n"
        report += f"- ⚫ Empty: {status_counts['empty']}\n\n"

        # Stock by fuel type
        report += "## Stock by Fuel Type\n\n"
        report += "| Fuel Type | Stock (L) | Capacity (L) | % Full |\n"
        report += "|-----------|-----------|--------------|--------|\n"
        for bucket in aggs.get("by_fuel_type", {}).get("buckets", []):
            ft = bucket.get("key", "N/A")
            ft_stock = bucket.get("stock", {}).get("value", 0)
            ft_cap = bucket.get("capacity", {}).get("value", 0)
            ft_pct = (ft_stock / ft_cap * 100) if ft_cap > 0 else 0
            report += f"| {ft} | {ft_stock:,.0f} | {ft_cap:,.0f} | {ft_pct:.1f}% |\n"
        report += "\n"

        # Consumption trends
        report += f"## Consumption Trends (Last {days} Days)\n\n"
        report += f"**Total Consumed:** {total_consumed:,.1f} L\n\n"

        # By fuel type
        report += "| Fuel Type | Consumed (L) |\n"
        report += "|-----------|-------------|\n"
        for bucket in cons_aggs.get("by_fuel_type", {}).get("buckets", []):
            ft = bucket.get("key", "N/A")
            consumed = bucket.get("consumed", {}).get("value", 0)
            report += f"| {ft} | {consumed:,.1f} |\n"
        report += "\n"

        # Daily trend
        daily_buckets = cons_aggs.get("daily_trend", {}).get("buckets", [])
        if daily_buckets:
            report += "**Daily Consumption:**\n\n"
            report += "| Date | Consumed (L) |\n"
            report += "|------|-------------|\n"
            for bucket in daily_buckets:
                day_str = bucket.get("key_as_string", "")
                if isinstance(day_str, str) and len(day_str) > 10:
                    day_str = day_str[:10]
                consumed = bucket.get("consumed", {}).get("value", 0)
                report += f"| {day_str} | {consumed:,.1f} |\n"
            report += "\n"

        # Alerts section
        report += "## Active Alerts\n\n"
        if alert_stations:
            for station in alert_stations:
                capacity = station.get("capacity_liters", 0)
                stock = station.get("current_stock_liters", 0)
                pct = (stock / capacity * 100) if capacity > 0 else 0
                status_emoji = {"low": "🟡", "critical": "🔴", "empty": "⚫"}.get(
                    station.get("status", ""), "⚪"
                )
                report += f"{status_emoji} **{station.get('name', 'Unknown')}** ({station.get('station_id', '')})\n"
                report += f"  - Fuel: {station.get('fuel_type', 'N/A')} | Stock: {stock:,.0f} / {capacity:,.0f} L ({pct:.1f}%)\n"
                report += f"  - Days Until Empty: {station.get('days_until_empty', 0):.1f}\n\n"
        else:
            report += "No active alerts. All stations are at normal stock levels.\n\n"

        # Recommendations
        report += "## Recommendations\n\n"
        recommendations = []

        if status_counts["critical"] > 0 or status_counts["empty"] > 0:
            recommendations.append(
                f"🚨 **Urgent:** {status_counts['critical'] + status_counts['empty']} station(s) at critical/empty levels. Schedule immediate refill."
            )

        if status_counts["low"] > 0:
            recommendations.append(
                f"⚠️ **Plan Refills:** {status_counts['low']} station(s) at low stock. Coordinate deliveries within the next few days."
            )

        if avg_days and avg_days < 7:
            recommendations.append(
                f"📉 **Low Network Reserve:** Average days until empty is {avg_days:.1f}. Consider increasing refill frequency."
            )

        if total_daily > 0 and total_stock > 0:
            network_days = total_stock / total_daily
            if network_days < 14:
                recommendations.append(
                    f"📊 **Network Runway:** At current consumption, total network stock covers ~{network_days:.0f} days. Review procurement schedule."
                )

        if not recommendations:
            recommendations.append("✅ All fuel operations are running normally. No immediate action required.")

        for rec in recommendations:
            report += f"- {rec}\n"

        success = True
        return report
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error generating fuel report: {e}")
        return f"Error generating fuel report: {str(e)}"
    finally:
        _log_tool_invocation(
            "generate_fuel_report",
            {"days": days, "tenant_id": tenant_id},
            start_time, success, error_msg
        )
