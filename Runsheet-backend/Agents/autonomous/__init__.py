"""
Autonomous agents package.

Background monitoring agents that run as asyncio tasks, polling data
sources and taking corrective actions without human prompting.
"""
from Agents.autonomous.base_agent import AutonomousAgentBase
from Agents.autonomous.delay_response_agent import DelayResponseAgent
from Agents.autonomous.fuel_management_agent import FuelManagementAgent
from Agents.autonomous.sla_guardian_agent import SLAGuardianAgent
from Agents.autonomous.fuel_calculations import (
    FuelPriority,
    calculate_refill_quantity,
    calculate_refill_priority,
)

__all__ = [
    "AutonomousAgentBase",
    "DelayResponseAgent",
    "FuelManagementAgent",
    "SLAGuardianAgent",
    "FuelPriority",
    "calculate_refill_quantity",
    "calculate_refill_priority",
]
