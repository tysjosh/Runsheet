"""
Unit tests for the ServiceContainer class.

Requirements: 2.1, 2.2, 2.3, 2.4
"""
import pytest
from unittest.mock import MagicMock

from bootstrap.container import ServiceContainer


class TestServiceContainer:
    """Tests for ServiceContainer registration, retrieval, and error handling."""

    def test_register_and_retrieve_via_attribute(self):
        """Register a service and retrieve it via attribute access."""
        container = ServiceContainer()
        mock_service = MagicMock(name="es_service")

        container.es_service = mock_service

        assert container.es_service is mock_service

    def test_register_and_retrieve_via_get(self):
        """Register a service and retrieve it via the get() method."""
        container = ServiceContainer()
        mock_service = MagicMock(name="fuel_service")

        container.fuel_service = mock_service

        result = container.get("fuel_service")
        assert result is mock_service

    def test_get_raises_key_error_for_unregistered(self):
        """get() raises KeyError with descriptive message for unregistered service."""
        container = ServiceContainer()
        container.es_service = MagicMock()

        with pytest.raises(KeyError, match="not_registered"):
            container.get("not_registered")

    def test_get_key_error_lists_available_services(self):
        """KeyError message includes list of available services."""
        container = ServiceContainer()
        container.alpha = MagicMock()
        container.beta = MagicMock()

        with pytest.raises(KeyError) as exc_info:
            container.get("missing")

        error_msg = str(exc_info.value)
        assert "alpha" in error_msg
        assert "beta" in error_msg

    def test_attribute_access_raises_attribute_error_for_unregistered(self):
        """Attribute access raises AttributeError for unregistered service."""
        container = ServiceContainer()

        with pytest.raises(AttributeError, match="no_such_service"):
            _ = container.no_such_service

    def test_has_returns_true_for_registered(self):
        """has() returns True for a registered service."""
        container = ServiceContainer()
        container.settings = MagicMock()

        assert container.has("settings") is True

    def test_has_returns_false_for_unregistered(self):
        """has() returns False for an unregistered service."""
        container = ServiceContainer()

        assert container.has("nonexistent") is False

    def test_registered_services_returns_sorted_list(self):
        """registered_services returns a sorted list of registered service names."""
        container = ServiceContainer()
        container.zeta = MagicMock()
        container.alpha = MagicMock()
        container.middle = MagicMock()

        result = container.registered_services
        assert result == ["alpha", "middle", "zeta"]

    def test_registered_services_empty_initially(self):
        """registered_services returns empty list for a fresh container."""
        container = ServiceContainer()

        assert container.registered_services == []

    def test_mock_injection_returns_mock(self):
        """Register a mock/stub and verify it is returned — supports test injection."""
        container = ServiceContainer()
        mock_es = MagicMock(name="mock_elasticsearch")
        mock_es.search.return_value = {"hits": []}

        container.es_service = mock_es

        retrieved = container.es_service
        assert retrieved is mock_es
        assert retrieved.search() == {"hits": []}

    def test_private_attributes_bypass_registry(self):
        """Private attributes (starting with _) bypass the registry."""
        container = ServiceContainer()
        container._custom_private = "private_value"

        # Should not appear in registered services
        assert container.has("_custom_private") is False
        assert "_custom_private" not in container.registered_services

        # But should be accessible as a normal attribute
        assert container._custom_private == "private_value"

    def test_overwrite_service(self):
        """Overwriting a service replaces the previous value."""
        container = ServiceContainer()
        first = MagicMock(name="first")
        second = MagicMock(name="second")

        container.my_service = first
        assert container.my_service is first

        container.my_service = second
        assert container.my_service is second
