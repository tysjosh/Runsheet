"""
Unit tests for compatibility adapters on singleton WS manager modules.

Each adapter module exposes:
- ``bind_container(container)`` — wire the ServiceContainer
- ``get_*()`` — return the manager, delegating to the container if bound

Tests verify:
- Without container bound, ``get_*()`` returns the legacy singleton instance.
- With container bound, ``get_*()`` returns ``container.<service>`` instance.
- Adapter returns the same object as direct container access (``is`` identity).

Requirements: 2.7, Correctness Property P4
"""
import sys
import pytest
from unittest.mock import MagicMock

from bootstrap.container import ServiceContainer

# ---------------------------------------------------------------------------
# Ensure ops.services.ops_metrics is importable even without prometheus_client
# ---------------------------------------------------------------------------
if "prometheus_client" not in sys.modules:
    sys.modules["prometheus_client"] = MagicMock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_module_globals(mod, singleton_name: str, container_name: str = "_container"):
    """Reset a singleton module's global state between tests."""
    setattr(mod, singleton_name, None)
    setattr(mod, container_name, None)


# ---------------------------------------------------------------------------
# ConnectionManager (fleet) adapter
# ---------------------------------------------------------------------------


class TestConnectionManagerAdapter:
    """Tests for websocket.connection_manager compatibility adapter."""

    def setup_method(self):
        import websocket.connection_manager as mod
        self.mod = mod
        _reset_module_globals(mod, "_connection_manager")

    def teardown_method(self):
        _reset_module_globals(self.mod, "_connection_manager")

    def test_legacy_singleton_without_container(self):
        """Without container bound, get_connection_manager() returns legacy singleton."""
        from websocket.connection_manager import ConnectionManager

        mgr = self.mod.get_connection_manager()
        assert isinstance(mgr, ConnectionManager)

        # Calling again returns the same instance (singleton)
        mgr2 = self.mod.get_connection_manager()
        assert mgr is mgr2

    def test_delegates_to_container_when_bound(self):
        """With container bound, get_connection_manager() returns container.fleet_ws_manager."""
        container = ServiceContainer()
        mock_mgr = MagicMock(name="fleet_ws_manager")
        container.fleet_ws_manager = mock_mgr

        self.mod.bind_container(container)

        result = self.mod.get_connection_manager()
        assert result is mock_mgr

    def test_identity_with_container_access(self):
        """Adapter returns the same object as direct container attribute access."""
        container = ServiceContainer()
        mock_mgr = MagicMock(name="fleet_ws_manager")
        container.fleet_ws_manager = mock_mgr

        self.mod.bind_container(container)

        assert self.mod.get_connection_manager() is container.fleet_ws_manager


# ---------------------------------------------------------------------------
# OpsWebSocketManager adapter
# ---------------------------------------------------------------------------


class TestOpsWSManagerAdapter:
    """Tests for ops.websocket.ops_ws compatibility adapter."""

    def setup_method(self):
        import ops.websocket.ops_ws as mod
        self.mod = mod
        _reset_module_globals(mod, "_ops_ws_manager")

    def teardown_method(self):
        _reset_module_globals(self.mod, "_ops_ws_manager")

    def test_legacy_singleton_without_container(self):
        """Without container bound, get_ops_ws_manager() returns legacy singleton."""
        from ops.websocket.ops_ws import OpsWebSocketManager

        mgr = self.mod.get_ops_ws_manager()
        assert isinstance(mgr, OpsWebSocketManager)

        mgr2 = self.mod.get_ops_ws_manager()
        assert mgr is mgr2

    def test_delegates_to_container_when_bound(self):
        """With container bound, get_ops_ws_manager() returns container.ops_ws_manager."""
        container = ServiceContainer()
        mock_mgr = MagicMock(name="ops_ws_manager")
        container.ops_ws_manager = mock_mgr

        self.mod.bind_container(container)

        result = self.mod.get_ops_ws_manager()
        assert result is mock_mgr

    def test_identity_with_container_access(self):
        """Adapter returns the same object as direct container attribute access."""
        container = ServiceContainer()
        mock_mgr = MagicMock(name="ops_ws_manager")
        container.ops_ws_manager = mock_mgr

        self.mod.bind_container(container)

        assert self.mod.get_ops_ws_manager() is container.ops_ws_manager


# ---------------------------------------------------------------------------
# SchedulingWebSocketManager adapter
# ---------------------------------------------------------------------------


class TestSchedulingWSManagerAdapter:
    """Tests for scheduling.websocket.scheduling_ws compatibility adapter."""

    def setup_method(self):
        import scheduling.websocket.scheduling_ws as mod
        self.mod = mod
        _reset_module_globals(mod, "_scheduling_ws_manager")

    def teardown_method(self):
        _reset_module_globals(self.mod, "_scheduling_ws_manager")

    def test_legacy_singleton_without_container(self):
        """Without container bound, get_scheduling_ws_manager() returns legacy singleton."""
        from scheduling.websocket.scheduling_ws import SchedulingWebSocketManager

        mgr = self.mod.get_scheduling_ws_manager()
        assert isinstance(mgr, SchedulingWebSocketManager)

        mgr2 = self.mod.get_scheduling_ws_manager()
        assert mgr is mgr2

    def test_delegates_to_container_when_bound(self):
        """With container bound, get_scheduling_ws_manager() returns container.scheduling_ws_manager."""
        container = ServiceContainer()
        mock_mgr = MagicMock(name="scheduling_ws_manager")
        container.scheduling_ws_manager = mock_mgr

        self.mod.bind_container(container)

        result = self.mod.get_scheduling_ws_manager()
        assert result is mock_mgr

    def test_identity_with_container_access(self):
        """Adapter returns the same object as direct container attribute access."""
        container = ServiceContainer()
        mock_mgr = MagicMock(name="scheduling_ws_manager")
        container.scheduling_ws_manager = mock_mgr

        self.mod.bind_container(container)

        assert self.mod.get_scheduling_ws_manager() is container.scheduling_ws_manager


# ---------------------------------------------------------------------------
# AgentActivityWSManager adapter
# ---------------------------------------------------------------------------


class TestAgentWSManagerAdapter:
    """Tests for Agents.agent_ws_manager compatibility adapter."""

    def setup_method(self):
        import Agents.agent_ws_manager as mod
        self.mod = mod
        _reset_module_globals(mod, "_agent_ws_manager")

    def teardown_method(self):
        _reset_module_globals(self.mod, "_agent_ws_manager")

    def test_legacy_singleton_without_container(self):
        """Without container bound, get_agent_ws_manager() returns legacy singleton."""
        from Agents.agent_ws_manager import AgentActivityWSManager

        mgr = self.mod.get_agent_ws_manager()
        assert isinstance(mgr, AgentActivityWSManager)

        mgr2 = self.mod.get_agent_ws_manager()
        assert mgr is mgr2

    def test_delegates_to_container_when_bound(self):
        """With container bound, get_agent_ws_manager() returns container.agent_ws_manager."""
        container = ServiceContainer()
        mock_mgr = MagicMock(name="agent_ws_manager")
        container.agent_ws_manager = mock_mgr

        self.mod.bind_container(container)

        result = self.mod.get_agent_ws_manager()
        assert result is mock_mgr

    def test_identity_with_container_access(self):
        """Adapter returns the same object as direct container attribute access."""
        container = ServiceContainer()
        mock_mgr = MagicMock(name="agent_ws_manager")
        container.agent_ws_manager = mock_mgr

        self.mod.bind_container(container)

        assert self.mod.get_agent_ws_manager() is container.agent_ws_manager
