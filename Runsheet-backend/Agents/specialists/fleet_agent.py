"""
Fleet Operations Specialist Agent.

Handles fleet asset management, tracking, locations, and fleet mutations.
Runs a fresh Strands Agent per request (see ``_base.SpecialistAgent``) with a
fleet-specific system prompt and tool set.

Validates:
- Requirement 7.1: Fleet_Agent with tools limited to fleet search, summary, lookup,
  location, and fleet mutation tools
- Requirement 7.9: Each Specialist_Agent has its own Strands Agent instance with
  domain-specific system prompt and tool set
- Requirements 9.2, 9.4: Tenant scoping on every ES read via ContextVar
"""

import logging

from Agents.specialists._base import SpecialistAgent
from Agents.tools import (
    search_fleet_data,
    get_fleet_summary,
    find_truck_by_id,
    get_all_locations,
    assign_asset_to_job,
    search_inventory,
    get_inventory_summary,
)

logger = logging.getLogger(__name__)


class FleetAgent(SpecialistAgent):
    """Specialist agent for fleet operations.

    Manages fleet assets, tracks locations, and handles fleet mutations
    such as assigning assets to jobs.
    """

    TOOLS = [
        search_fleet_data,
        get_fleet_summary,
        find_truck_by_id,
        get_all_locations,
        search_inventory,
        get_inventory_summary,
        # Fleet mutation tools
        assign_asset_to_job,
    ]

    SYSTEM_PROMPT = (
        "You are a Fleet Operations Specialist for a logistics platform. "
        "Your role is to manage fleet assets, track their locations, provide fleet "
        "status summaries, and handle fleet mutations such as assigning assets to jobs.\n\n"
        "**Supported Asset Types:**\n"
        "- vehicle: truck, fuel_truck, personnel_vehicle\n"
        "- vessel: boat, barge\n"
        "- equipment: crane, forklift\n"
        "- container: cargo_container, ISO_tank\n\n"
        "**Your Tools:**\n"
        "- `search_fleet_data(query, asset_type)` - Search fleet assets by query and optional type filter\n"
        "- `get_fleet_summary()` - Get current fleet status overview with per-type breakdowns\n"
        "- `find_truck_by_id(truck_id)` - Find any asset by ID or plate number\n"
        "- `get_all_locations()` - Get all depots, warehouses, and stations\n"
        "- `search_inventory(query)` - Search inventory items (parts, supplies) by name or stock status\n"
        "- `get_inventory_summary()` - Get all inventory items organized by in-stock/low-stock/out-of-stock\n"
        "- `assign_asset_to_job(job_id, asset_id)` - Assign an asset to a job (mutation)\n\n"
        "**Guidelines:**\n"
        "- Always announce what you are searching for before using tools\n"
        "- Provide clear, structured results with actionable insights\n"
        "- For mutations, explain the impact before executing\n"
        "- If you cannot fulfill a request with your tools, say so clearly"
    )
