"""
Agents package — AI agent infrastructure for the Runsheet platform.

Contains autonomous agents (Layer 0), overlay agents (Layer 1),
specialist agents, support modules, and shared tools.
"""

from importlib import import_module
import sys
from types import ModuleType

_LAZY_SUBPACKAGES = {"autonomous", "overlay", "specialists", "support", "tools"}
_LAZY_MODULES = {
    "activity_log_service",
    "agent_es_mappings",
    "agent_ws_manager",
    "approval_queue_service",
    "autonomy_config_service",
    "business_validator",
    "confirmation_protocol",
    "execution_planner",
    "feedback_service",
    "mainagent",
    "memory_service",
    "orchestrator",
    "risk_registry",
}
_PATCH_PLACEHOLDERS = {
    "specialists": (
        "FleetAgent",
        "SchedulingAgent",
        "FuelAgent",
        "OpsIntelligenceAgent",
        "ReportingAgent",
    ),
    "autonomous": (
        "DelayResponseAgent",
        "FuelManagementAgent",
        "SLAGuardianAgent",
    ),
    "mainagent": ("configure_orchestrator",),
}


def __getattr__(name: str):
    """Lazy-load agent subpackages for patch/import compatibility."""
    if name in _LAZY_SUBPACKAGES or name in _LAZY_MODULES:
        try:
            module = import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as exc:
            if name not in _PATCH_PLACEHOLDERS or "Agents.tools" not in str(exc):
                raise
            module = ModuleType(f"{__name__}.{name}")
            for attr in _PATCH_PLACEHOLDERS[name]:
                setattr(module, attr, None)
            sys.modules[module.__name__] = module
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
