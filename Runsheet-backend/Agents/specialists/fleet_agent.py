"""
Fleet Operations Specialist Agent.

Handles fleet asset management, tracking, locations, and fleet mutations.
Wraps a Strands Agent instance with fleet-specific system prompt and tool set.

Validates:
- Requirement 7.1: Fleet_Agent with tools limited to fleet search, summary, lookup,
  location, and fleet mutation tools
- Requirement 7.9: Each Specialist_Agent has its own Strands Agent instance with
  domain-specific system prompt and tool set
"""

import logging
from strands import Agent
from strands.models.litellm import LiteLLMModel

from Agents.tools import (
    search_fleet_data,
    get_fleet_summary,
    find_truck_by_id,
    get_all_locations,
    assign_asset_to_job,
)

logger = logging.getLogger(__name__)


class FleetAgent:
    """Specialist agent for fleet operations.

    Manages fleet assets, tracks locations, and handles fleet mutations
    such as assigning assets to jobs.
    """

    TOOLS = [
        search_fleet_data,
        get_fleet_summary,
        find_truck_by_id,
        get_all_locations,
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
        "- `assign_asset_to_job(job_id, asset_id)` - Assign an asset to a job (mutation)\n\n"
        "**Guidelines:**\n"
        "- Always announce what you are searching for before using tools\n"
        "- Provide clear, structured results with actionable insights\n"
        "- For mutations, explain the impact before executing\n"
        "- If you cannot fulfill a request with your tools, say so clearly"
    )

    def __init__(self, model: LiteLLMModel):
        """Initialize the Fleet Agent with a shared model.

        Args:
            model: The LiteLLM model instance (shared across specialists).
        """
        self.agent = Agent(
            model=model,
            system_prompt=self.SYSTEM_PROMPT,
            tools=self.TOOLS,
        )
        logger.info("✅ FleetAgent initialized with %d tools", len(self.TOOLS))

    async def handle(self, task: str, context: dict = None) -> str:
        """Process a fleet-related subtask.

        Args:
            task: The natural language task to process.
            context: Optional context dict (e.g. tenant_id, session_id).

        Returns:
            The agent's response as a string.
        """
        prompt = task
        if context:
            ctx_parts = []
            if context.get("tenant_id"):
                ctx_parts.append(f"Tenant: {context['tenant_id']}")
            if ctx_parts:
                prompt = f"[Context: {', '.join(ctx_parts)}]\n{task}"

        result = await self.agent.invoke_async(prompt)
        return str(result)
