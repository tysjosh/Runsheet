"""
Ops Intelligence Specialist Agent.

Handles order tracking, driver management, operational metrics, ops reports,
and ops mutations. Wraps a Strands Agent instance with ops-specific system prompt
and tool set.

Validates:
- Requirement 7.4: Ops_Intelligence_Agent with tools limited to ops search, drivers,
  order events, ops metrics, and ops report/mutation tools
- Requirement 7.9: Each Specialist_Agent has its own Strands Agent instance with
  domain-specific system prompt and tool set
- Design §9 — AI tools: new order/driver tools + legacy deprecation
"""

import logging
from strands import Agent
from strands.models.litellm import LiteLLMModel

from Agents.tools import (
    get_ops_metrics,
    # Ops report tools
    generate_sla_report,
    generate_failure_report,
    generate_rider_productivity_report as generate_driver_productivity_report,
)
from Agents.tools.order_tools import (
    search_orders,
    search_drivers,
    get_order_events,
    get_orders_metrics,
)
from Agents.tools._tenant_context import set_current_tenant

logger = logging.getLogger(__name__)


class OpsIntelligenceAgent:
    """Specialist agent for operations intelligence.

    Tracks fuel orders, manages drivers, provides operational metrics and reports,
    and handles ops mutations such as driver reassignment and order escalation.
    """

    TOOLS = [
        # New order/driver tools (preferred)
        search_orders,
        search_drivers,
        get_order_events,
        get_orders_metrics,
        get_ops_metrics,
        # Ops report tools
        generate_sla_report,
        generate_failure_report,
        generate_driver_productivity_report,
    ]

    SYSTEM_PROMPT = (
        "You are an Operations Intelligence Specialist for a fuel distribution platform. "
        "Your role is to track fuel delivery orders, manage drivers, provide operational "
        "metrics and reports, and handle ops mutations.\n\n"
        "**Order Statuses:** placed, confirmed, scheduled, dispatched, in_transit, "
        "delivered, failed, cancelled, on_hold\n"
        "**Driver Statuses:** active, inactive, on_break, off_duty\n"
        "**Call Types:** will_call, auto_fill, keep_full, one_off\n"
        "**Intake Channels:** voice, web_portal, dispatcher, csv, edi, api_partner, legacy\n\n"
        "**Your Tools:**\n"
        "- `search_orders(status, customer_id, driver_id, call_type, product_code, "
        "start_date, end_date, intake_channel)` - Search fuel orders by various filters\n"
        "- `search_drivers(status, availability, min_active_orders, max_active_orders, "
        "hazmat_endorsement)` - Search drivers by status, availability, and qualifications\n"
        "- `get_order_events(order_id)` - Get full event timeline for a fuel order\n"
        "- `get_orders_metrics(metric_type, bucket, start_date, end_date, intake_channel)` "
        "- Get aggregated operational metrics (orders, drivers, sla, failures)\n"
        "- `get_ops_metrics(metric_type, bucket, start_date, end_date, tenant_id)` "
        "- Get aggregated operational metrics\n"
        "- `generate_sla_report(start_date, end_date, tenant_id)` - Generate SLA "
        "violations report\n"
        "- `generate_failure_report(start_date, end_date, tenant_id, intake_channel=None)` - Generate "
        "failure root-cause analysis report. Filter by intake_channel to compare failure rates across channels.\n"
        "- `generate_driver_productivity_report(start_date, end_date, tenant_id)` "
        "- Generate driver productivity report\n\n"
        "**Guidelines:**\n"
        "- Always announce what you are searching for before using tools\n"
        "- Highlight SLA breaches and at-risk orders\n"
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

        Binds the tenant id from ``context`` to the tool ContextVar before
        dispatching the Strands agent so every ES-reading tool runs
        tenant-scoped.

        Args:
            task: The natural language task to process.
            context: Optional context dict (e.g. tenant_id, session_id).

        Returns:
            The agent's response as a string.
        """
        prompt = task
        tenant_id = (context or {}).get("tenant_id")
        if context:
            ctx_parts = []
            if tenant_id:
                ctx_parts.append(f"Tenant: {tenant_id}")
            if ctx_parts:
                prompt = f"[Context: {', '.join(ctx_parts)}]\n{task}"

        with set_current_tenant(tenant_id):
            result = await self.agent.invoke_async(prompt)
        return str(result)
