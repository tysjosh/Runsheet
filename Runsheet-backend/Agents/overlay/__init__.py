"""
Overlay agents package.

Layered agent overlay architecture composing five agents on top of the
existing Layer 0 autonomous agents (DelayResponseAgent, FuelManagementAgent,
SLAGuardianAgent).  The overlay introduces a Signal Bus for inter-agent
communication, standardized data contracts, and a shared OverlayAgentBase
class that handles signal subscription, decision-cycle scheduling,
shadow/active mode toggling, and proposal routing.

Layer 1 — Decision Overlays:
    DispatchOptimizer   – global reassignment and reroute optimisation
    ExceptionCommander  – incident triage and ranked response plans
    RevenueGuard        – margin-leakage detection and policy proposals
    CustomerPromise     – proactive ETA trust management

Layer 2 — Meta-Control:
    LearningPolicyAgent – continuous threshold and policy tuning
"""

from Agents.overlay.data_contracts import (
    InterventionProposal,
    OutcomeRecord,
    PolicyChangeProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.overlay.signal_bus import SignalBus
from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.dispatch_optimizer import DispatchOptimizer
from Agents.overlay.exception_commander import ExceptionCommander
from Agents.overlay.revenue_guard import RevenueGuard
from Agents.overlay.customer_promise import CustomerPromise
from Agents.overlay.learning_policy_agent import LearningPolicyAgent
from Agents.overlay.outcome_tracker import OutcomeTracker
from Agents.overlay.overlay_es_mappings import setup_overlay_indices

__all__ = [
    # Data contracts
    "Severity",
    "RiskClass",
    "RiskSignal",
    "InterventionProposal",
    "OutcomeRecord",
    "PolicyChangeProposal",
    # Signal Bus
    "SignalBus",
    # Base class
    "OverlayAgentBase",
    # Layer 1 agents
    "DispatchOptimizer",
    "ExceptionCommander",
    "RevenueGuard",
    "CustomerPromise",
    # Layer 2 agent
    "LearningPolicyAgent",
    # Outcome tracking
    "OutcomeTracker",
    # ES mappings
    "setup_overlay_indices",
]
