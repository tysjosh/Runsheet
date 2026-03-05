"""
Agent tools package
"""

from .search_tools import (
    search_fleet_data,
    search_orders,
    search_support_tickets,
    search_inventory
)

from .summary_tools import (
    get_fleet_summary,
    get_inventory_summary,
    get_analytics_overview,
    get_performance_insights
)

from .lookup_tools import (
    find_truck_by_id,
    get_all_locations
)

from .report_tools import (
    generate_operations_report,
    generate_performance_report,
    generate_incident_analysis
)

from .ops_search_tools import (
    search_shipments,
    search_riders,
    get_shipment_events,
    get_ops_metrics
)

from .ops_report_tools import (
    generate_sla_report,
    generate_failure_report,
    generate_rider_productivity_report
)

from .fuel_tools import (
    search_fuel_stations,
    get_fuel_summary,
    get_fuel_consumption_history,
    generate_fuel_report
)

# All available tools
ALL_TOOLS = [
    # Search tools
    search_fleet_data,
    search_orders,
    search_support_tickets,
    search_inventory,
    
    # Summary tools
    get_fleet_summary,
    get_inventory_summary,
    get_analytics_overview,
    get_performance_insights,
    
    # Lookup tools
    find_truck_by_id,
    get_all_locations,
    
    # Report tools
    generate_operations_report,
    generate_performance_report,
    generate_incident_analysis,

    # Ops search tools
    search_shipments,
    search_riders,
    get_shipment_events,
    get_ops_metrics,

    # Ops report tools
    generate_sla_report,
    generate_failure_report,
    generate_rider_productivity_report,

    # Fuel tools
    search_fuel_stations,
    get_fuel_summary,
    get_fuel_consumption_history,
    generate_fuel_report,
]