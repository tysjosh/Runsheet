"""
Integration test for the full bootstrap sequence.

Tests that initialize_all() completes with mocked external services,
all expected services are registered, and fail-open behavior works.

Requirements: 1.4, 1.5, Correctness Property P3
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bootstrap import initialize_all, shutdown_all, _BOOT_ORDER
from bootstrap.container import ServiceContainer


@pytest.fixture
def container():
    return ServiceContainer()


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.state = MagicMock()
    app.add_middleware = MagicMock()
    return app


class TestFullBootstrapSequence:
    """Integration tests for the complete bootstrap sequence."""

    @pytest.mark.asyncio
    async def test_fail_open_one_module_raises(self, mock_app, container):
        """Patch one module to raise; verify others still complete.

        Validates: Requirement 1.5, Correctness Property P3
        """
        call_order = []

        patches = {}
        for mod_name in _BOOT_ORDER:
            mock_mod = MagicMock()
            if mod_name == "fuel":
                mock_mod.initialize = AsyncMock(side_effect=RuntimeError("fuel boom"))
            else:
                mock_mod.initialize = AsyncMock(
                    side_effect=lambda a, c, n=mod_name: call_order.append(n)
                )
            patches[mod_name] = mock_mod

        with patch("importlib.import_module") as mock_import:
            def _import(name, package=None):
                mod_name = name.lstrip(".")
                return patches[mod_name]
            mock_import.side_effect = _import

            # Should not raise
            await initialize_all(mock_app, container)

        # fuel should NOT be in call_order, all others should
        expected = [m for m in _BOOT_ORDER if m != "fuel"]
        assert call_order == expected

    @pytest.mark.asyncio
    async def test_fail_open_all_modules_raise(self, mock_app, container):
        """If ALL modules raise, initialize_all still completes without error."""
        patches = {}
        for mod_name in _BOOT_ORDER:
            mock_mod = MagicMock()
            mock_mod.initialize = AsyncMock(side_effect=RuntimeError(f"{mod_name} boom"))
            patches[mod_name] = mock_mod

        with patch("importlib.import_module") as mock_import:
            def _import(name, package=None):
                mod_name = name.lstrip(".")
                return patches[mod_name]
            mock_import.side_effect = _import

            # Should not raise even if all modules fail
            await initialize_all(mock_app, container)

    @pytest.mark.asyncio
    async def test_boot_order_is_correct(self):
        """Verify the boot order matches the design spec."""
        assert _BOOT_ORDER == ["core", "middleware", "ops", "fuel", "scheduling", "notifications", "agents"]

    @pytest.mark.asyncio
    async def test_shutdown_reverse_order(self, mock_app, container):
        """Verify shutdown calls modules in reverse order."""
        call_order = []

        patches = {}
        for mod_name in _BOOT_ORDER:
            mock_mod = MagicMock()
            mock_mod.shutdown = AsyncMock(
                side_effect=lambda a, c, n=mod_name: call_order.append(n)
            )
            patches[mod_name] = mock_mod

        with patch("importlib.import_module") as mock_import:
            def _import(name, package=None):
                mod_name = name.lstrip(".")
                return patches[mod_name]
            mock_import.side_effect = _import

            await shutdown_all(mock_app, container)

        assert call_order == list(reversed(_BOOT_ORDER))
