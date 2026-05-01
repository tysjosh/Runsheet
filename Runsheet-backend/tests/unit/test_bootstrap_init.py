"""
Unit tests for bootstrap/__init__.py — initialize_all() and shutdown_all().

Requirements: 1.4, 1.5
"""
import asyncio
import logging
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
    return app


class TestInitializeAll:
    """Tests for initialize_all()."""

    @pytest.mark.asyncio
    async def test_calls_all_modules_in_order(self, mock_app, container):
        """Verify all bootstrap modules are called in dependency order."""
        call_order = []

        async def _make_init(name):
            async def init(app, cont):
                call_order.append(name)
            return init

        patches = {}
        for mod_name in _BOOT_ORDER:
            mock_mod = MagicMock()
            init_fn = AsyncMock(side_effect=lambda a, c, n=mod_name: call_order.append(n))
            mock_mod.initialize = init_fn
            patches[f"bootstrap.{mod_name}"] = mock_mod

        with patch("importlib.import_module") as mock_import:
            def _import(name, package=None):
                mod_name = name.lstrip(".")
                return patches[f"bootstrap.{mod_name}"]
            mock_import.side_effect = _import

            await initialize_all(mock_app, container)

        assert call_order == list(_BOOT_ORDER)

    @pytest.mark.asyncio
    async def test_fail_open_continues_on_error(self, mock_app, container):
        """If one module raises, remaining modules still initialize."""
        call_order = []

        patches = {}
        for mod_name in _BOOT_ORDER:
            mock_mod = MagicMock()
            if mod_name == "ops":
                mock_mod.initialize = AsyncMock(side_effect=RuntimeError("ops boom"))
            else:
                mock_mod.initialize = AsyncMock(
                    side_effect=lambda a, c, n=mod_name: call_order.append(n)
                )
            patches[f"bootstrap.{mod_name}"] = mock_mod

        with patch("importlib.import_module") as mock_import:
            def _import(name, package=None):
                mod_name = name.lstrip(".")
                return patches[f"bootstrap.{mod_name}"]
            mock_import.side_effect = _import

            await initialize_all(mock_app, container)

        # ops should NOT be in call_order (it raised), but all others should
        expected = [m for m in _BOOT_ORDER if m != "ops"]
        assert call_order == expected


class TestShutdownAll:
    """Tests for shutdown_all()."""

    @pytest.mark.asyncio
    async def test_calls_shutdown_in_reverse_order(self, mock_app, container):
        """Verify shutdown is called in reverse dependency order."""
        call_order = []

        patches = {}
        for mod_name in _BOOT_ORDER:
            mock_mod = MagicMock()
            mock_mod.shutdown = AsyncMock(
                side_effect=lambda a, c, n=mod_name: call_order.append(n)
            )
            patches[f"bootstrap.{mod_name}"] = mock_mod

        with patch("importlib.import_module") as mock_import:
            def _import(name, package=None):
                mod_name = name.lstrip(".")
                return patches[f"bootstrap.{mod_name}"]
            mock_import.side_effect = _import

            await shutdown_all(mock_app, container)

        assert call_order == list(reversed(_BOOT_ORDER))

    @pytest.mark.asyncio
    async def test_skips_modules_without_shutdown(self, mock_app, container):
        """Modules without a shutdown function are skipped."""
        call_order = []

        patches = {}
        for mod_name in _BOOT_ORDER:
            mock_mod = MagicMock(spec=[])  # no attributes
            if mod_name in ("core", "agents"):
                mock_mod.shutdown = AsyncMock(
                    side_effect=lambda a, c, n=mod_name: call_order.append(n)
                )
            patches[f"bootstrap.{mod_name}"] = mock_mod

        with patch("importlib.import_module") as mock_import:
            def _import(name, package=None):
                mod_name = name.lstrip(".")
                return patches[f"bootstrap.{mod_name}"]
            mock_import.side_effect = _import

            await shutdown_all(mock_app, container)

        # Only core and agents have shutdown
        assert "core" in call_order
        assert "agents" in call_order
        assert len(call_order) == 2

    @pytest.mark.asyncio
    async def test_shutdown_continues_on_error(self, mock_app, container):
        """If one module's shutdown raises, remaining modules still shut down."""
        call_order = []

        patches = {}
        for mod_name in _BOOT_ORDER:
            mock_mod = MagicMock()
            if mod_name == "scheduling":
                mock_mod.shutdown = AsyncMock(side_effect=RuntimeError("sched boom"))
            else:
                mock_mod.shutdown = AsyncMock(
                    side_effect=lambda a, c, n=mod_name: call_order.append(n)
                )
            patches[f"bootstrap.{mod_name}"] = mock_mod

        with patch("importlib.import_module") as mock_import:
            def _import(name, package=None):
                mod_name = name.lstrip(".")
                return patches[f"bootstrap.{mod_name}"]
            mock_import.side_effect = _import

            await shutdown_all(mock_app, container)

        expected = [m for m in reversed(_BOOT_ORDER) if m != "scheduling"]
        assert call_order == expected
