"""
Agent tools package.

Exports all tools available to the AI agent system. Legacy ops tools
(search_shipments, search_riders, reassign_rider, escalate_shipment)
have been removed — use order_tools for fuel order operations.
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
    get_ops_metrics,
)

from .ops_report_tools import (
    generate_sla_report,
    generate_failure_report,
    generate_rider_productivity_report as generate_driver_productivity_report,
)

from .order_tools import (
    search_orders as search_fuel_orders,
    search_drivers,
    get_order_events,
    get_orders_metrics,
)

from .fuel_tools import (
    search_fuel_stations,
    get_fuel_summary,
    get_fuel_consumption_history,
    generate_fuel_report
)

from .scheduling_tools import (
    search_jobs,
    get_job_details,
    find_available_assets,
    get_scheduling_summary,
    generate_dispatch_report
)

from .mutation_tools import (
    assign_asset_to_job,
    update_job_status,
    cancel_job,
    create_job,
    request_fuel_refill,
    update_fuel_threshold,
    configure_mutation_tools,
)

# All available tools for the main agent fallback path
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

    # Ops metrics
    get_ops_metrics,

    # Ops report tools
    generate_sla_report,
    generate_failure_report,
    generate_driver_productivity_report,

    # Order/driver tools (fuel order pipeline)
    search_fuel_orders,
    search_drivers,
    get_order_events,
    get_orders_metrics,

    # Fuel tools
    search_fuel_stations,
    get_fuel_summary,
    get_fuel_consumption_history,
    generate_fuel_report,

    # Scheduling tools
    search_jobs,
    get_job_details,
    find_available_assets,
    get_scheduling_summary,
    generate_dispatch_report,

    # Mutation tools - scheduling
    assign_asset_to_job,
    update_job_status,
    cancel_job,
    create_job,

    # Mutation tools - fuel
    request_fuel_refill,
    update_fuel_threshold,
]
