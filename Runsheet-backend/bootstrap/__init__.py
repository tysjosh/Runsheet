"""
Bootstrap package for Runsheet backend.

Orchestrates domain-specific initialization in dependency order:
core → middleware → ops → fuel → scheduling → agents

Each module exposes ``async def initialize(app, container)`` and optionally
``async def shutdown(app, container)``.  If a module raises during
initialization the error is logged and remaining modules proceed (fail-open).

Requirements: 1.4, 1.5
"""
import importlib
import logging

from .container import ServiceContainer

logger = logging.getLogger("bootstrap")

# Ordered list of bootstrap modules — dependency order matters.
_BOOT_ORDER = ["core", "middleware", "ops", "fuel", "scheduling", "agents"]


async def initialize_all(app, container: ServiceContainer) -> None:
    """Initialize all bootstrap modules in dependency order.

    Each module's ``initialize(app, container)`` is called sequentially.
    If a module raises, the error is logged and remaining modules proceed.

    Requirements: 1.4, 1.5
    """
    for module_name in _BOOT_ORDER:
        try:
            mod = importlib.import_module(f".{module_name}", package=__name__)
            await mod.initialize(app, container)
            logger.info("Bootstrap module '%s' initialized", module_name)
        except Exception as exc:
            logger.error(
                "Bootstrap module '%s' failed: %s",
                module_name,
                exc,
                exc_info=True,
            )


async def shutdown_all(app, container: ServiceContainer) -> None:
    """Shutdown all bootstrap modules in reverse dependency order.

    Only calls ``shutdown`` if the module defines it.
    """
    for module_name in reversed(_BOOT_ORDER):
        try:
            mod = importlib.import_module(f".{module_name}", package=__name__)
            if hasattr(mod, "shutdown"):
                await mod.shutdown(app, container)
                logger.info("Bootstrap module '%s' shut down", module_name)
        except Exception as exc:
            logger.error(
                "Bootstrap module '%s' shutdown failed: %s",
                module_name,
                exc,
            )
