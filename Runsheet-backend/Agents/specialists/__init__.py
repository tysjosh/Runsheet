"""
Specialist Agents package.

Each specialist runs a Strands Agent with a domain-specific system prompt
and tool set, sharing the same Gemini model configuration.

Validates:
- Requirement 7.1: FleetAgent
- Requirement 7.2: SchedulingAgent
- Requirement 7.3: FuelAgent
- Requirement 7.4: OpsIntelligenceAgent
- Requirement 7.5: ReportingAgent
- Requirement 7.9: Each specialist builds its own Strands Agent per request,
  so conversation history never crosses requests, tenants or sessions (F1)
"""

from ._base import SpecialistAgent
from .fleet_agent import FleetAgent
from .scheduling_agent import SchedulingAgent
from .fuel_agent import FuelAgent
from .ops_intelligence_agent import OpsIntelligenceAgent
from .reporting_agent import ReportingAgent

__all__ = [
    "SpecialistAgent",
    "FleetAgent",
    "SchedulingAgent",
    "FuelAgent",
    "OpsIntelligenceAgent",
    "ReportingAgent",
]
