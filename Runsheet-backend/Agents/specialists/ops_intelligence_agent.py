"""
Ops Intelligence Specialist Agent.

Handles order tracking, driver management, operational metrics, ops reports,
and ops mutations. Runs a fresh Strands Agent per request (see
``_base.SpecialistAgent``) with an ops-specific system prompt and tool set.

Validates:
- Requirement 7.4: Ops_Intelligence_Agent with tools limited to ops search, drivers,
  order events, ops metrics, and ops report/mutation tools
- Requirement 7.9: Each Specialist_Agent has its own Strands Agent instance with
  domain-specific system prompt and tool set
- Design §9 — AI tools: new order/driver tools + legacy deprecation
"""

import logging

from Agents.specialists._base import SpecialistAgent
from Agents.tools import (
    get_ops_metrics,
    # Ops report tools
    generate_sla_report,
    generate_failure_report,
    generate_driver_productivity_report,
    # Commerce read-only context for the dispatch decision
    get_customer_delivery_eligibility,
)
from Agents.tools.order_tools import (
    search_orders,
    search_drivers,
    get_order_events,
    get_orders_metrics,
)

logger = logging.getLogger(__name__)


class OpsIntelligenceAgent(SpecialistAgent):
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
        # Credit context. Without it the agent answered "I cannot identify
        # accounts over their credit limit. My tools do not have access to
        # credit limit information" while accounts_current held the limits.
        # Read-only on purpose: the ERP owns the ledger.
        get_customer_delivery_eligibility,
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
        "- `get_ops_metrics(metric_type, bucket, start_date, end_date)` "
        "- Get aggregated operational metrics\n"
        "- `get_customer_delivery_eligibility(customer_id, account_id, "
        "order_total_cents, limit)` - Whether a customer can take a delivery on "
        "credit: verdict, credit state, limit, open balance and AR aging in one "
        "answer. Use it for any question about credit limits, credit holds, "
        "accounts over their limit, or whether to send a truck. Call it with no id "
        "to list the accounts currently blocked. It is READ-ONLY — it cannot change "
        "a limit, release a hold or touch an invoice, and you must not claim "
        "otherwise\n"
        "- `generate_sla_report(start_date, end_date)` - Generate SLA "
        "violations report\n"
        "- `generate_failure_report(start_date, end_date, intake_channel=None)` - Generate "
        "failure root-cause analysis report. Filter by intake_channel to compare failure rates across channels.\n"
        "- `generate_driver_productivity_report(start_date, end_date)` "
        "- Generate driver productivity report\n\n"
        "**Guidelines:**\n"
        "- Always announce what you are searching for before using tools\n"
        "- Highlight SLA breaches and at-risk orders\n"
        "- On credit answers, always pass on `credit_holds_enforced`. If it is "
        "false, a hold does not actually stop an order on this deployment and the "
        "verdict is advice rather than a block\n"
        "- Report `total_blocked_candidates` and `shown` as the different numbers "
        "they are, and never present the rows you were shown as the total\n"
        "- Never offer to raise a credit limit, release a hold, or adjust an "
        "invoice. Those live in the customer's accounting system, not here. Point "
        "the user at their controller instead\n"
        "- If you cannot fulfill a request with your tools, say so clearly"
    )
