"""
Reporting Specialist Agent.

Handles all report generation across domains: operations, performance, incidents,
SLA, failures, rider productivity, fuel, and dispatch reports.
Wraps a Strands Agent instance with reporting-specific system prompt and tool set.

Validates:
- Requirement 7.5: Reporting_Agent with tools limited to all report generation tools
  across domains and cross-domain analytics queries
- Requirement 7.9: Each Specialist_Agent has its own Strands Agent instance with
  domain-specific system prompt and tool set
"""

import logging
from strands import Agent
from strands.models.litellm import LiteLLMModel

from Agents.tools import (
    # General report tools
    generate_operations_report,
    generate_performance_report,
    generate_incident_analysis,
    # Ops report tools
    generate_sla_report,
    generate_failure_report,
    generate_rider_productivity_report,
    # Fuel report tools
    generate_fuel_report,
    # Scheduling report tools
    generate_dispatch_report,
)

logger = logging.getLogger(__name__)


class ReportingAgent:
    """Specialist agent for cross-domain reporting.

    Generates reports across all domains: operations, performance, incidents,
    SLA compliance, failure analysis, rider productivity, fuel, and dispatch.
    """

    TOOLS = [
        # General report tools
        generate_operations_report,
        generate_performance_report,
        generate_incident_analysis,
        # Ops report tools
        generate_sla_report,
        generate_failure_report,
        generate_rider_productivity_report,
        # Fuel report tools
        generate_fuel_report,
        # Scheduling report tools
        generate_dispatch_report,
    ]

    SYSTEM_PROMPT = (
        "You are a Reporting & Analytics Specialist for a logistics platform. "
        "Your role is to generate comprehensive reports across all operational domains "
        "including fleet operations, scheduling, fuel, and ops intelligence.\n\n"
        "**Your Tools:**\n"
        "- `generate_operations_report()` - Generate comprehensive operations status report\n"
        "- `generate_performance_report()` - Generate detailed performance analysis report\n"
        "- `generate_incident_analysis(issue)` - Analyze incidents across multiple data sources\n"
        "- `generate_sla_report(start_date, end_date, tenant_id)` - Generate SLA violations report\n"
        "- `generate_failure_report(start_date, end_date, tenant_id)` - Generate failure "
        "root-cause analysis report\n"
        "- `generate_rider_productivity_report(start_date, end_date, tenant_id)` - Generate "
        "rider productivity report\n"
        "- `generate_fuel_report(days)` - Generate comprehensive fuel operations report\n"
        "- `generate_dispatch_report(days)` - Generate dispatch report with completion rates\n\n"
        "**Guidelines:**\n"
        "- Always announce which report you are generating before using tools\n"
        "- When asked for a general overview, combine multiple reports for a comprehensive view\n"
        "- Present findings in structured markdown with clear sections\n"
        "- Highlight key metrics, trends, and actionable recommendations\n"
        "- If you cannot fulfill a request with your tools, say so clearly"
    )

    def __init__(self, model: LiteLLMModel):
        """Initialize the Reporting Agent with a shared model.

        Args:
            model: The LiteLLM model instance (shared across specialists).
        """
        self.agent = Agent(
            model=model,
            system_prompt=self.SYSTEM_PROMPT,
            tools=self.TOOLS,
        )
        logger.info("✅ ReportingAgent initialized with %d tools", len(self.TOOLS))

    async def handle(self, task: str, context: dict = None) -> str:
        """Process a reporting subtask.

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
