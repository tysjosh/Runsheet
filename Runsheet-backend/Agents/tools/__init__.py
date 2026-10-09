"""
Agent tools package.

Exports all tools available to the AI agent system. Ops search now uses
order_tools for fuel order and driver operations.

``search_orders`` resolves to :mod:`Agents.tools.order_tools` — the one that
queries the live ``fuel_orders_current`` index and reports a true total.

There used to be a second implementation in :mod:`Agents.tools.search_tools`
exported under the same name, and it could not answer an order question
correctly: it searched an index called ``orders`` that does not exist, capped at
5 hits, returned ``len(page)`` phrased as a count, and rendered
``customer``/``value``/``items``/``priority`` against a document whose fields are
``customer_name``/``gallons_requested``/``product_code``/``status``. Two tools of
the same name reaching different data is how one reply managed to say "There are
3 unassigned orders" and "There are 912 unassigned orders" in a single answer.

``mainagent``'s system prompt documents the filter signature
(``search_orders(status=..., customer_id=...)``), which is order_tools' — so the
export below also removes a mismatch between what the model is told and what it
was handed.
"""

from .search_tools import (
    search_fleet_data,
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
    generate_driver_productivity_report,
)

from .order_tools import (
    search_orders,
    # Retained alias: existing call sites and the ops specialist referred to this
    # name while the package export pointed at the other implementation.
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

from .tank_forecast_tools import (
    get_runout_risk_list,
)

from .commerce_read_tools import (
    get_customer_delivery_eligibility,
    configure_commerce_read_tools,
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
    # Search tools. ``search_orders`` is deliberately not here — it lives in the
    # order/driver block below, because it comes from order_tools now.
    search_fleet_data,
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

    # Order/driver tools (fuel order pipeline).
    #
    # Listed as ``search_orders``, not via the ``search_fuel_orders`` alias:
    # ALL_TOOLS is handed to the model, and the same function appearing twice
    # under two names is exactly the ambiguity being removed here.
    search_orders,
    search_drivers,
    get_order_events,
    get_orders_metrics,

    # Fuel tools
    search_fuel_stations,
    get_fuel_summary,
    get_fuel_consumption_history,
    generate_fuel_report,

    # Tank run-out risk — the core planning question for propane / heating oil
    get_runout_risk_list,

    # Credit / hold / AR context for the dispatch decision. Read-only by
    # design: the ERP is the book of record and no commerce mutation tool is
    # exposed to the model.
    get_customer_delivery_eligibility,

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
