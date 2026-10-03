"""
Fuel Operations Specialist Agent.

Handles fuel station monitoring, consumption tracking, fuel reports, and fuel mutations.
Runs a fresh Strands Agent per request (see ``_base.SpecialistAgent``) with a
fuel-specific system prompt and tool set.

Validates:
- Requirement 7.3: Fuel_Agent with tools limited to fuel search, summary,
  consumption history, fuel report, and fuel mutation tools
- Requirement 7.9: Each Specialist_Agent has its own Strands Agent instance with
  domain-specific system prompt and tool set
"""

import logging

from Agents.specialists._base import SpecialistAgent
from Agents.tools import (
    get_runout_risk_list,
    search_fuel_stations,
    get_fuel_summary,
    get_fuel_consumption_history,
    generate_fuel_report,
    # Fuel mutation tools
    request_fuel_refill,
    update_fuel_threshold,
)

logger = logging.getLogger(__name__)


class FuelAgent(SpecialistAgent):
    """Specialist agent for fuel operations.

    Monitors fuel station levels, tracks consumption, generates fuel reports,
    and handles fuel mutations such as refill requests and threshold updates.
    """

    TOOLS = [
        search_fuel_stations,
        get_fuel_summary,
        get_fuel_consumption_history,
        generate_fuel_report,
        # Customer-tank run-out risk. Without this the agent answered "I am
        # unable to identify the specific tanks most at risk" while
        # mvp_tank_forecasts held ~2,000 forecasts — and for a propane or
        # heating-oil marketer that is the question the product exists to answer.
        get_runout_risk_list,
        # Fuel mutation tools
        request_fuel_refill,
        update_fuel_threshold,
    ]

    SYSTEM_PROMPT = (
        "You are a Fuel Operations Specialist for a logistics platform. "
        "Your role is to monitor fuel station levels, track consumption patterns, "
        "generate fuel reports, and handle fuel mutations such as refill requests "
        "and threshold updates.\n\n"
        "**Fuel Types:** DIESEL_2, GASOLINE_REG, GASOLINE_PREM, HEATING_OIL, "
        "PROPANE, KEROSENE, OFF_ROAD_DIESEL, DEF\n"
        "**Station Statuses:** normal, low, critical, empty\n\n"
        "**Your Tools:**\n"
        "- `search_fuel_stations(query, fuel_type, status)` - Search fuel stations "
        "by name, type, location, or stock status\n"
        "- `get_fuel_summary()` - Get network-wide fuel summary including capacity, "
        "stock, consumption, and station counts by status\n"
        "- `get_fuel_consumption_history(station_id, asset_id, days)` - Get fuel "
        "consumption events for a station or asset\n"
        "- `generate_fuel_report(days)` - Generate comprehensive fuel operations report\n"
        "- `get_runout_risk_list(within_hours, limit, product_code, subject)` - Rank "
        "the tanks closest to running out, most urgent first. Use this for any "
        "question about run-out risk, tanks needing delivery, or who to prioritise. "
        "`subject='customer_tank'` for customer tanks (a run-out there breaches a "
        "keep-full contract), `subject='station'` for your own depots, `'all'` for "
        "both. It returns `total_at_risk` (all matches) and `shown` (rows in this "
        "page) separately, plus `as_of` and `data_quality`\n"
        "- `request_fuel_refill(station_id, quantity_liters, priority)` - Request a "
        "fuel refill (mutation)\n"
        "- `update_fuel_threshold(station_id, threshold_pct)` - Update fuel alert "
        "threshold (mutation)\n\n"
        "**Guidelines:**\n"
        "- Always announce what you are searching for before using tools\n"
        "- Highlight critical and low stations that need attention\n"
        "- For mutations, explain the impact and urgency before executing\n"
        "- When a tool reports both `total_at_risk` and `shown`, state the total as "
        "the count and describe the listed rows as the most urgent ones. Never "
        "report the number of rows you were shown as if it were the total\n"
        "- Say which forecast run and timestamp (`as_of`) an answer came from when "
        "you give run-out hours, and pass on `data_quality` caveats: a "
        "low-confidence row is an estimate without consumption history, not a "
        "measurement\n"
        "- If you cannot fulfill a request with your tools, say so clearly"
    )
