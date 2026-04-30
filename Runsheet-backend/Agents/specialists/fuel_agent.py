"""
Fuel Operations Specialist Agent.

Handles fuel station monitoring, consumption tracking, fuel reports, and fuel mutations.
Wraps a Strands Agent instance with fuel-specific system prompt and tool set.

Validates:
- Requirement 7.3: Fuel_Agent with tools limited to fuel search, summary,
  consumption history, fuel report, and fuel mutation tools
- Requirement 7.9: Each Specialist_Agent has its own Strands Agent instance with
  domain-specific system prompt and tool set
"""

import logging
from strands import Agent
from strands.models.litellm import LiteLLMModel

from Agents.tools import (
    search_fuel_stations,
    get_fuel_summary,
    get_fuel_consumption_history,
    generate_fuel_report,
    # Fuel mutation tools
    request_fuel_refill,
    update_fuel_threshold,
)

logger = logging.getLogger(__name__)


class FuelAgent:
    """Specialist agent for fuel operations.

    Monitors fuel station levels, tracks consumption, generates fuel reports,
    and handles fuel mutations such as refill requests and threshold updates.
    """

    TOOLS = [
        search_fuel_stations,
        get_fuel_summary,
        get_fuel_consumption_history,
        generate_fuel_report,
        # Fuel mutation tools
        request_fuel_refill,
        update_fuel_threshold,
    ]

    SYSTEM_PROMPT = (
        "You are a Fuel Operations Specialist for a logistics platform. "
        "Your role is to monitor fuel station levels, track consumption patterns, "
        "generate fuel reports, and handle fuel mutations such as refill requests "
        "and threshold updates.\n\n"
        "**Fuel Types:** AGO (diesel), PMS (petrol), ATK (aviation), LPG\n"
        "**Station Statuses:** normal, low, critical, empty\n\n"
        "**Your Tools:**\n"
        "- `search_fuel_stations(query, fuel_type, status)` - Search fuel stations "
        "by name, type, location, or stock status\n"
        "- `get_fuel_summary()` - Get network-wide fuel summary including capacity, "
        "stock, consumption, and station counts by status\n"
        "- `get_fuel_consumption_history(station_id, asset_id, days)` - Get fuel "
        "consumption events for a station or asset\n"
        "- `generate_fuel_report(days)` - Generate comprehensive fuel operations report\n"
        "- `request_fuel_refill(station_id, quantity_liters, priority)` - Request a "
        "fuel refill (mutation)\n"
        "- `update_fuel_threshold(station_id, threshold_pct)` - Update fuel alert "
        "threshold (mutation)\n\n"
        "**Guidelines:**\n"
        "- Always announce what you are searching for before using tools\n"
        "- Highlight critical and low stations that need attention\n"
        "- For mutations, explain the impact and urgency before executing\n"
        "- If you cannot fulfill a request with your tools, say so clearly"
    )

    def __init__(self, model: LiteLLMModel):
        """Initialize the Fuel Agent with a shared model.

        Args:
            model: The LiteLLM model instance (shared across specialists).
        """
        self.agent = Agent(
            model=model,
            system_prompt=self.SYSTEM_PROMPT,
            tools=self.TOOLS,
        )
        logger.info("✅ FuelAgent initialized with %d tools", len(self.TOOLS))

    async def handle(self, task: str, context: dict = None) -> str:
        """Process a fuel-related subtask.

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
