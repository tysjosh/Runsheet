"""
Scheduling Operations Specialist Agent.

Handles job scheduling, dispatch, asset assignment, and scheduling mutations.
Runs a fresh Strands Agent per request (see ``_base.SpecialistAgent``) with a
scheduling-specific system prompt and tool set.

Validates:
- Requirement 7.2: Scheduling_Agent with tools limited to scheduling search, details,
  available assets, summary, dispatch report, and scheduling mutation tools
- Requirement 7.9: Each Specialist_Agent has its own Strands Agent instance with
  domain-specific system prompt and tool set
"""

import logging

from Agents.specialists._base import SpecialistAgent
from Agents.tools.scheduling_tools import JOB_STATUS_VALUES, JOB_TYPE_VALUES
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


class SchedulingAgent(SpecialistAgent):
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
        f"**Job Types:** {', '.join(JOB_TYPE_VALUES)}\n"
        f"**Job Statuses:** {', '.join(JOB_STATUS_VALUES)}\n\n"
        "**Your Tools:**\n"
        "- `search_jobs(job_type, status, asset, origin, destination, start_date, end_date)` "
        "- Search jobs by various filters\n"
        "- `get_job_details(job_id)` - Get full details of a job including event history\n"
        "- `find_available_assets(asset_type, start_time_range, end_time_range)` "
        "- Find assets not assigned to active jobs\n"
        "- `get_scheduling_summary()` - Get summary of active, delayed, and upcoming jobs\n"
        "- `generate_dispatch_report(days, tenant_id, intake_channel=None)` - Generate dispatch report with completion rates. "
        "Filter by intake_channel (voice, web_portal, dispatcher, csv, edi, api_partner, legacy).\n"
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
