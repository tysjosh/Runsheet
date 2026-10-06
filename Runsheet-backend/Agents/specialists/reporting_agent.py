"""
Reporting Specialist Agent.

Handles all report generation across domains: operations, performance, incidents,
SLA, failures, driver productivity, fuel, and dispatch reports.
Runs a fresh Strands Agent per request (see ``_base.SpecialistAgent``) with a
reporting-specific system prompt and tool set.

Validates:
- Requirement 7.5: Reporting_Agent with tools limited to all report generation tools
  across domains and cross-domain analytics queries
- Requirement 7.9: Each Specialist_Agent has its own Strands Agent instance with
  domain-specific system prompt and tool set
"""

import logging

from Agents.specialists._base import SpecialistAgent
from Agents.tools import (
    # General report tools
    generate_operations_report,
    generate_performance_report,
    generate_incident_analysis,
    # Ops report tools
    generate_sla_report,
    generate_failure_report,
    generate_driver_productivity_report,
    # Fuel report tools
    generate_fuel_report,
    # Scheduling report tools
    generate_dispatch_report,
)
from Agents.tools.summary_tools import (
    get_analytics_overview,
    get_performance_insights,
)

logger = logging.getLogger(__name__)


class ReportingAgent(SpecialistAgent):
    """Specialist agent for cross-domain reporting.

    Generates reports across all domains: operations, performance, incidents,
    SLA compliance, failure analysis, driver productivity, fuel, and dispatch.
    """

    TOOLS = [
        # General report tools
        generate_operations_report,
        generate_performance_report,
        generate_incident_analysis,
        # Ops report tools
        generate_sla_report,
        generate_failure_report,
        generate_driver_productivity_report,
        # Fuel report tools
        generate_fuel_report,
        # Scheduling report tools
        generate_dispatch_report,
        # Cross-domain analytics
        get_analytics_overview,
        get_performance_insights,
    ]

    SYSTEM_PROMPT = (
        "You are a Reporting & Analytics Specialist for a logistics platform. "
        "Your role is to generate comprehensive reports across all operational domains "
        "including fleet operations, scheduling, fuel, and ops intelligence.\n\n"
        "**Your Tools:**\n"
        "- `generate_operations_report()` - Generate comprehensive operations status report\n"
        "- `generate_performance_report()` - Generate detailed performance analysis report\n"
        "- `generate_incident_analysis(issue)` - Analyze incidents across multiple data sources\n"
        "- `generate_sla_report(start_date, end_date)` - Generate SLA violations report\n"
        "- `generate_failure_report(start_date, end_date, intake_channel=None)` - Generate failure "
        "root-cause analysis report. Filter by intake_channel to compare failure rates across channels.\n"
        "- `generate_driver_productivity_report(start_date, end_date)` - Generate "
        "driver productivity report\n"
        "- `generate_fuel_report(days)` - Generate comprehensive fuel operations report\n"
        "- `generate_dispatch_report(days, intake_channel=None)` - Generate dispatch report with completion rates. "
        "Filter by intake_channel (voice, web_portal, dispatcher, csv, edi, api_partner, legacy).\n"
        "- `get_analytics_overview()` - Get current KPIs, top routes, and main delay causes\n"
        "- `get_performance_insights()` - Get best/worst route and regional performance with "
        "improvement recommendations\n\n"
        "**Guidelines:**\n"
        "- Always announce which report you are generating before using tools\n"
        "- When asked for a general overview, combine multiple reports for a comprehensive view\n"
        "- Present findings in structured markdown with clear sections\n"
        "- Highlight key metrics, trends, and actionable recommendations\n"
        "- If you cannot fulfill a request with your tools, say so clearly"
    )
