"""
Unit tests for cross-domain agent integration bootstrap wiring.

Verifies that bootstrap/agents.py correctly:
- Registers truck_fuel_monitor, job_sla_monitor, job_priority_engine with AgentScheduler
- Wires delay_response_agent._signal_bus to the signal_bus instance
- Includes new agents in the correct shutdown layer positions

Requirements: 1.1, 3.1, 4.1, 5.7
"""
import ast
import inspect
import textwrap

import pytest


# ---------------------------------------------------------------------------
# Helpers — parse bootstrap source once
# ---------------------------------------------------------------------------

def _get_bootstrap_source() -> str:
    """Return the raw source of bootstrap/agents.py."""
    import bootstrap.agents as mod
    return inspect.getsource(mod)


def _get_function_source(func_name: str) -> str:
    """Return the source of a specific function in bootstrap/agents.py."""
    import bootstrap.agents as mod
    func = getattr(mod, func_name)
    return inspect.getsource(func)


# ---------------------------------------------------------------------------
# Tests: Agent registration with AgentScheduler (Req 1.1, 3.1, 4.1)
# ---------------------------------------------------------------------------


class TestCrossDomainAgentRegistration:
    """Verify that cross-domain agents are registered with the AgentScheduler."""

    def test_truck_fuel_monitor_registered(self):
        """bootstrap registers truck_fuel_monitor with scheduler."""
        source = _get_function_source("initialize")
        assert "scheduler.register(truck_fuel_monitor" in source, (
            "truck_fuel_monitor should be registered with scheduler"
        )

    def test_job_sla_monitor_registered(self):
        """bootstrap registers job_sla_monitor with scheduler."""
        source = _get_function_source("initialize")
        assert "scheduler.register(job_sla_monitor" in source, (
            "job_sla_monitor should be registered with scheduler"
        )

    def test_job_priority_engine_registered(self):
        """bootstrap registers job_priority_engine with scheduler."""
        source = _get_function_source("initialize")
        assert "scheduler.register(job_priority_engine" in source, (
            "job_priority_engine should be registered with scheduler"
        )

    def test_all_cross_domain_agents_use_on_failure_policy(self):
        """All cross-domain agents use RestartPolicy.ON_FAILURE."""
        source = _get_function_source("initialize")
        for agent_var in ["truck_fuel_monitor", "job_sla_monitor", "job_priority_engine"]:
            # Find the register call for this agent
            register_pattern = f"scheduler.register({agent_var}"
            assert register_pattern in source, (
                f"{agent_var} should be registered with scheduler"
            )
            # Find the line containing the register call
            for line in source.splitlines():
                if register_pattern in line:
                    assert "ON_FAILURE" in line, (
                        f"{agent_var} should use RestartPolicy.ON_FAILURE"
                    )
                    break

    def test_truck_fuel_monitor_instantiated_with_signal_bus(self):
        """TruckFuelMonitor is instantiated with signal_bus parameter."""
        source = _get_function_source("initialize")
        # Find the TruckFuelMonitor constructor call block
        assert "TruckFuelMonitor(" in source
        # The signal_bus=signal_bus should appear in the constructor args
        # Find the block between TruckFuelMonitor( and the closing )
        tfm_idx = source.index("TruckFuelMonitor(")
        block = source[tfm_idx:source.index(")", tfm_idx + 200) + 1]
        assert "signal_bus=signal_bus" in block, (
            "TruckFuelMonitor should be instantiated with signal_bus=signal_bus"
        )

    def test_job_sla_monitor_instantiated_with_signal_bus(self):
        """JobSLAMonitor is instantiated with signal_bus parameter."""
        source = _get_function_source("initialize")
        assert "JobSLAMonitor(" in source
        jsm_idx = source.index("JobSLAMonitor(")
        block = source[jsm_idx:source.index(")", jsm_idx + 200) + 1]
        assert "signal_bus=signal_bus" in block, (
            "JobSLAMonitor should be instantiated with signal_bus=signal_bus"
        )

    def test_cross_domain_agents_stored_on_app_state(self):
        """Cross-domain agent references are stored on app.state."""
        source = _get_function_source("initialize")
        assert "app.state.cross_domain_agents" in source, (
            "Cross-domain agents should be stored on app.state"
        )
        # Verify all expected keys are present
        for key in ["truck_fuel_monitor", "job_sla_monitor", "job_priority_engine"]:
            assert f'"{key}"' in source, (
                f'"{key}" should be in app.state.cross_domain_agents'
            )

    def test_imports_cross_domain_classes(self):
        """bootstrap imports TruckFuelMonitor, JobSLAMonitor, JobPriorityEngine."""
        source = _get_function_source("initialize")
        assert "from Agents.autonomous.truck_fuel_monitor import TruckFuelMonitor" in source
        assert "from Agents.autonomous.job_sla_monitor import JobSLAMonitor" in source
        assert "from Agents.overlay.job_priority_engine import JobPriorityEngine" in source


# ---------------------------------------------------------------------------
# Tests: delay_response_agent SignalBus wiring (Req 5.7)
# ---------------------------------------------------------------------------


class TestDelayResponseAgentSignalBusWiring:
    """Verify that delay_response_agent._signal_bus is set to signal_bus."""

    def test_signal_bus_explicitly_wired(self):
        """delay_response_agent._signal_bus is explicitly set to signal_bus."""
        source = _get_function_source("initialize")
        assert "delay_response_agent._signal_bus = signal_bus" in source, (
            "delay_response_agent._signal_bus should be explicitly set to signal_bus"
        )

    def test_signal_bus_wired_after_signal_bus_creation(self):
        """delay_response_agent._signal_bus assignment occurs after SignalBus creation."""
        source = _get_function_source("initialize")
        signal_bus_creation_idx = source.index("signal_bus = SignalBus(")
        wiring_idx = source.index("delay_response_agent._signal_bus = signal_bus")
        assert wiring_idx > signal_bus_creation_idx, (
            "SignalBus wiring should occur after SignalBus is created"
        )

    def test_layer0_agents_wired_to_signal_bus_via_loop(self):
        """All Layer 0 agents are wired to signal_bus via the loop."""
        source = _get_function_source("initialize")
        # The bootstrap has a loop that sets _signal_bus on all L0 agents
        assert "agent._signal_bus = signal_bus" in source, (
            "Layer 0 agents should be wired to signal_bus via loop"
        )


# ---------------------------------------------------------------------------
# Tests: Shutdown order (Req 1.1, 3.1, 4.1)
# ---------------------------------------------------------------------------


class TestShutdownOrder:
    """Verify that shutdown includes new agents in correct layer positions."""

    def _get_shutdown_lists(self):
        """Extract the shutdown layer lists from the shutdown function source."""
        source = _get_function_source("shutdown")
        # Parse the source to find the list assignments
        lists = {}
        for var_name in ["mvp_agents", "l2_agents", "l1_agents", "l0_agents"]:
            # Find the assignment line
            start_marker = f"{var_name} = ["
            if start_marker in source:
                start = source.index(start_marker)
                end = source.index("]", start) + 1
                list_str = source[start + len(var_name) + 3:end]
                # Parse the list literal safely
                items = ast.literal_eval(list_str)
                lists[var_name] = items
        return lists

    def test_job_priority_engine_in_l1_agents(self):
        """job_priority_engine is in the L1 agents shutdown list."""
        lists = self._get_shutdown_lists()
        assert "job_priority_engine" in lists["l1_agents"], (
            "job_priority_engine should be in L1 agents for shutdown"
        )

    def test_truck_fuel_monitor_in_l0_agents(self):
        """truck_fuel_monitor is in the L0 agents shutdown list."""
        lists = self._get_shutdown_lists()
        assert "truck_fuel_monitor" in lists["l0_agents"], (
            "truck_fuel_monitor should be in L0 agents for shutdown"
        )

    def test_job_sla_monitor_in_l0_agents(self):
        """job_sla_monitor is in the L0 agents shutdown list."""
        lists = self._get_shutdown_lists()
        assert "job_sla_monitor" in lists["l0_agents"], (
            "job_sla_monitor should be in L0 agents for shutdown"
        )

    def test_l1_shutdown_before_l0(self):
        """L1 agents are stopped before L0 agents in shutdown order."""
        source = _get_function_source("shutdown")
        # The shutdown iterates layers in order: MVP → L2 → L1 → L0
        # Verify L1 appears before L0 in the iteration list
        l1_marker = '("L1", l1_agents)'
        l0_marker = '("L0", l0_agents)'
        assert l1_marker in source, "L1 layer should be in shutdown iteration"
        assert l0_marker in source, "L0 layer should be in shutdown iteration"
        l1_pos = source.index(l1_marker)
        l0_pos = source.index(l0_marker)
        assert l1_pos < l0_pos, (
            "L1 agents should be stopped before L0 agents"
        )

    def test_new_l0_agents_before_existing_l0_agents(self):
        """truck_fuel_monitor and job_sla_monitor appear before existing L0 agents."""
        lists = self._get_shutdown_lists()
        l0 = lists["l0_agents"]
        tfm_idx = l0.index("truck_fuel_monitor")
        jsm_idx = l0.index("job_sla_monitor")
        dra_idx = l0.index("delay_response_agent")
        fma_idx = l0.index("fuel_management_agent")
        sga_idx = l0.index("sla_guardian_agent")
        # New agents should come before existing ones
        assert tfm_idx < dra_idx, (
            "truck_fuel_monitor should be before delay_response_agent in L0 shutdown"
        )
        assert jsm_idx < fma_idx, (
            "job_sla_monitor should be before fuel_management_agent in L0 shutdown"
        )

    def test_shutdown_order_is_mvp_l2_l1_l0(self):
        """Shutdown processes layers in order: MVP → L2 → L1 → L0."""
        source = _get_function_source("shutdown")
        # Find the for loop that iterates over layers
        mvp_pos = source.index('"MVP"')
        l2_pos = source.index('"L2"')
        l1_pos = source.index('"L1"')
        l0_pos = source.index('"L0"')
        assert mvp_pos < l2_pos < l1_pos < l0_pos, (
            "Shutdown order should be MVP → L2 → L1 → L0"
        )

    def test_all_shutdown_layers_have_expected_agents(self):
        """Each shutdown layer contains the expected set of agents."""
        lists = self._get_shutdown_lists()
        # L1 should contain job_priority_engine plus existing overlay agents
        assert "job_priority_engine" in lists["l1_agents"]
        assert "dispatch_optimizer" in lists["l1_agents"]
        assert "exception_commander" in lists["l1_agents"]
        assert "revenue_guard" in lists["l1_agents"]
        assert "customer_promise" in lists["l1_agents"]
        # L0 should contain new + existing autonomous agents
        assert "truck_fuel_monitor" in lists["l0_agents"]
        assert "job_sla_monitor" in lists["l0_agents"]
        assert "delay_response_agent" in lists["l0_agents"]
        assert "fuel_management_agent" in lists["l0_agents"]
        assert "sla_guardian_agent" in lists["l0_agents"]
