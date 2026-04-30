"""
Scheduling Operations Specialist Agent.

Handles job scheduling, dispatch, asset assignment, and scheduling mutations.
Wraps a Strands Agent instance with scheduling-specific system prompt and tool set.

Validates:
- Requirement 7.2: Scheduling_Agent with tools limited to scheduling search, details,
  available assets, summary, dispatch report, and scheduling mutation tools
- Requirement 7.9: Each Specialist_Agent has its own Strands Agent instance with
  domain-specific system prompt and tool set
"""

import logging
from strands import Agent
from strands.models.litellm import LiteLLMModel

from Agents.tools import (
    search_jobs,
    get_job_details,
    find_available_assets,
    get_scheduling_summary,
    generate_dispatch_report,
    # Scheduling mutation tools
    assign_asset_to_job,
    update_job_status,
    cancel_job,
    create_job,
)

logger = logging.getLogger(__name__)


class SchedulingAgent:
    """Specialist agent for scheduling and dispatch operations.

    Manages logistics jobs, dispatching, asset availability, and scheduling
    mutations such as creating jobs, updating status, and cancellations.
    """

    TOOLS = [
        search_jobs,
        get_job_details,
        find_available_assets,
        get_scheduling_summary,
        generate_dispatch_report,
        # Scheduling mutation tools
        assign_asset_to_job,
        update_job_status,
        cancel_job,
        create_job,
    ]

    SYSTEM_PROMPT = (
        "You are a Scheduling & Dispatch Specialist for a logistics platform. "
        "Your role is to manage logistics jobs, track scheduling status, find available "
        "assets, generate dispatch reports, and handle scheduling mutations.\n\n"
        "**Job Types:** cargo_transport, passenger_transport, vessel_movement, "
        "airport_transfer, crane_booking\n"
        "**Job Statuses:** scheduled, assigned, in_progress, completed, cancelled, failed\n\n"
        "**Your Tools:**\n"
        "- `search_jobs(job_type, status, asset, origin, destination, start_date, end_date)` "
        "- Search jobs by various filters\n"
        "- `get_job_details(job_id)` - Get full details of a job including event history\n"
        "- `find_available_assets(asset_type, start_time_range, end_time_range)` "
        "- Find assets not assigned to active jobs\n"
        "- `get_scheduling_summary()` - Get summary of active, delayed, and upcoming jobs\n"
        "- `generate_dispatch_report(days)` - Generate dispatch report with completion rates\n"
        "- `assign_asset_to_job(job_id, asset_id)` - Assign an asset to a job (mutation)\n"
        "- `update_job_status(job_id, new_status, reason)` - Update job status (mutation)\n"
        "- `cancel_job(job_id, reason)` - Cancel a job (mutation)\n"
        "- `create_job(job_type, origin, destination, scheduled_time, ...)` - Create a new job (mutation)\n\n"
        "**Guidelines:**\n"
        "- Always announce what you are searching for before using tools\n"
        "- Validate status transitions before updating job status\n"
        "- For mutations, explain the impact and risk level before executing\n"
        "- If you cannot fulfill a request with your tools, say so clearly"
    )

    def __init__(self, model: LiteLLMModel):
        """Initialize the Scheduling Agent with a shared model.

        Args:
            model: The LiteLLM model instance (shared across specialists).
        """
        self.agent = Agent(
            model=model,
            system_prompt=self.SYSTEM_PROMPT,
            tools=self.TOOLS,
        )
        logger.info("✅ SchedulingAgent initialized with %d tools", len(self.TOOLS))

    async def handle(self, task: str, context: dict = None) -> str:
        """Process a scheduling-related subtask.

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
