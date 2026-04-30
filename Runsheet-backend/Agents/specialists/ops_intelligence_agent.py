"""
Ops Intelligence Specialist Agent.

Handles shipment tracking, rider management, operational metrics, ops reports,
and ops mutations. Wraps a Strands Agent instance with ops-specific system prompt
and tool set.

Validates:
- Requirement 7.4: Ops_Intelligence_Agent with tools limited to ops search, riders,
  shipment events, ops metrics, and ops report/mutation tools
- Requirement 7.9: Each Specialist_Agent has its own Strands Agent instance with
  domain-specific system prompt and tool set
"""

import logging
from strands import Agent
from strands.models.litellm import LiteLLMModel

from Agents.tools import (
    search_shipments,
    search_riders,
    get_shipment_events,
    get_ops_metrics,
    # Ops report tools
    generate_sla_report,
    generate_failure_report,
    generate_rider_productivity_report,
    # Ops mutation tools
    reassign_rider,
    escalate_shipment,
)

logger = logging.getLogger(__name__)


class OpsIntelligenceAgent:
    """Specialist agent for operations intelligence.

    Tracks shipments, manages riders, provides operational metrics and reports,
    and handles ops mutations such as rider reassignment and shipment escalation.
    """

    TOOLS = [
        search_shipments,
        search_riders,
        get_shipment_events,
        get_ops_metrics,
        # Ops report tools
        generate_sla_report,
        generate_failure_report,
        generate_rider_productivity_report,
        # Ops mutation tools
        reassign_rider,
        escalate_shipment,
    ]

    SYSTEM_PROMPT = (
        "You are an Operations Intelligence Specialist for a logistics platform. "
        "Your role is to track shipments, manage riders, provide operational metrics "
        "and reports, and handle ops mutations such as rider reassignment and "
        "shipment escalation.\n\n"
        "**Shipment Statuses:** pending, assigned, picked_up, in_transit, delivered, "
        "failed, cancelled\n"
        "**Rider Statuses:** active, inactive, on_break\n\n"
        "**Your Tools:**\n"
        "- `search_shipments(tenant_id, status, rider_id, start_date, end_date, query)` "
        "- Search shipments by various filters\n"
        "- `search_riders(tenant_id, status, availability)` - Search riders by status "
        "and availability\n"
        "- `get_shipment_events(shipment_id, tenant_id)` - Get full event timeline "
        "for a shipment\n"
        "- `get_ops_metrics(metric_type, bucket, start_date, end_date, tenant_id)` "
        "- Get aggregated operational metrics\n"
        "- `generate_sla_report(start_date, end_date, tenant_id)` - Generate SLA "
        "violations report\n"
        "- `generate_failure_report(start_date, end_date, tenant_id)` - Generate "
        "failure root-cause analysis report\n"
        "- `generate_rider_productivity_report(start_date, end_date, tenant_id)` "
        "- Generate rider productivity report\n"
        "- `reassign_rider(shipment_id, new_rider_id, reason)` - Reassign a shipment "
        "to a different rider (mutation)\n"
        "- `escalate_shipment(shipment_id, priority, reason)` - Escalate shipment "
        "priority (mutation)\n\n"
        "**Guidelines:**\n"
        "- Always announce what you are searching for before using tools\n"
        "- Highlight SLA breaches and at-risk shipments\n"
        "- For mutations, explain the impact and risk level before executing\n"
        "- If you cannot fulfill a request with your tools, say so clearly"
    )

    def __init__(self, model: LiteLLMModel):
        """Initialize the Ops Intelligence Agent with a shared model.

        Args:
            model: The LiteLLM model instance (shared across specialists).
        """
        self.agent = Agent(
            model=model,
            system_prompt=self.SYSTEM_PROMPT,
            tools=self.TOOLS,
        )
        logger.info(
            "✅ OpsIntelligenceAgent initialized with %d tools", len(self.TOOLS)
        )

    async def handle(self, task: str, context: dict = None) -> str:
        """Process an ops intelligence subtask.

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
