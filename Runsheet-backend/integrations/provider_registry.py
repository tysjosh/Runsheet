"""
Integration provider bootstrap (Task 9.10 / Req 5.6.2, 5.6.6).

Single place responsible for wiring every built-in connector module
into the shared :mod:`integrations.provider_catalog` at application
start-up time. The per-connector modules intentionally do NOT
auto-register on import — they expose :func:`register_catalog_entry`
helpers instead so unit tests can import them without mutating the
global registry. That leaves ONE job for this module: call each
helper, in a stable order, at bootstrap.

Usage:

    from integrations.provider_registry import register_all_providers

    def bootstrap() -> None:
        # ... wire vault, ES, scheduler ...
        register_all_providers()
        # ... mount routers ...

Design points:

* **Call once at startup.** Bootstrap should invoke
  :func:`register_all_providers` exactly once during application
  start-up, before the integrations router starts serving
  ``GET /api/integrations/providers``. Calling it more than once is
  safe — :func:`integrations.provider_catalog.register_provider`
  replaces the entry for an already-registered ``provider_name``
  atomically — but the expected production posture is a single call.

* **Deterministic order.** The Marketplace UI sorts the response by
  insertion order; registering in a fixed order (QBO → tank monitors →
  Geotab → Stripe) keeps the UI stable across deploys and keeps
  integration tests predictable.

* **No side-effects at import time.** This module only defines the
  bootstrap helper; nothing is registered until the function runs.
  Importing this module (e.g. during test collection) MUST NOT
  mutate the shared registry.

* **Marketplace-level feature-flag gating.** Each registered
  :class:`ProviderCatalogEntry` exposes an
  :meth:`effective_feature_flag_key` that defaults to
  ``overlay.integration.{provider_name}`` per Requirement 5.6.6.
  Connector-specific flags (e.g. ``overlay.qbo_invoice_push``,
  ``overlay.stripe_autocharge``) gate per-behaviour execution inside
  the connector and are NOT surfaced via the Marketplace visibility
  key.

Validates: Requirements 5.6.2, 5.6.6.
"""
from __future__ import annotations

import logging
from typing import Callable, List, Sequence, Tuple

from integrations.provider_catalog import ProviderCatalogEntry

logger = logging.getLogger(__name__)


#: Deterministic registration order. Each entry is
#: ``(provider_name, register_callable)`` — the callable is the
#: per-connector ``register_catalog_entry`` helper, accessed by
#: attribute lookup so unit tests that monkey-patch individual
#: connectors see the patched function. ``provider_name`` is included
#: for clearer log output and to make the order explicit at the
#: call-site without having to unwrap the connector module.
_DefaultRegistration = Tuple[str, Callable[[], ProviderCatalogEntry]]


def _default_registrations() -> List[_DefaultRegistration]:
    """Return the ordered list of built-in connector registrations.

    Imports are performed inside the function so a test that only
    needs :mod:`integrations.provider_registry` (e.g. to patch the
    order list) does not transitively import every connector module
    at collection time. Production bootstrap calls
    :func:`register_all_providers` which evaluates this list eagerly.
    """

    from integrations import (
        additional_tank_monitors,
        geotab,
        quickbooks_online,
        stripe_connector,
        veeder_root,
    )

    return [
        ("quickbooks_online", quickbooks_online.register_catalog_entry),
        ("veeder_root", veeder_root.register_catalog_entry),
        ("otodata", additional_tank_monitors.register_otodata_catalog_entry),
        ("silverlink", additional_tank_monitors.register_silverlink_catalog_entry),
        ("gasboy", additional_tank_monitors.register_gasboy_catalog_entry),
        (
            "franklin_fueling",
            additional_tank_monitors.register_franklin_fueling_catalog_entry,
        ),
        ("geotab", geotab.register_catalog_entry),
        ("stripe", stripe_connector.register_catalog_entry),
    ]


def register_all_providers(
    registrations: Sequence[_DefaultRegistration] | None = None,
) -> List[ProviderCatalogEntry]:
    """Register every built-in connector with the shared provider catalog.

    Bootstrap should call this ONCE at application start-up, before
    the integrations router begins serving
    ``GET /api/integrations/providers``. Calling it more than once is
    idempotent because :func:`integrations.provider_catalog.register_provider`
    replaces an already-registered entry atomically — the resulting
    registry size stays at the number of built-in connectors
    regardless of how many times this runs.

    Args:
        registrations: Optional override for the default
            registration tuple list. Unit tests pass a smaller list
            to exercise ordering / idempotency behaviour without
            touching every connector module. Production callers omit
            this argument and the default QBO → tank monitors → Geotab
            → Stripe order is used.

    Returns:
        The list of :class:`ProviderCatalogEntry` objects that were
        registered, in registration order. Consumers rarely need the
        return value — the shared registry is the authoritative
        source — but returning it keeps the helper composable with
        logging / test assertions.
    """

    pending = list(registrations) if registrations is not None else _default_registrations()
    registered: List[ProviderCatalogEntry] = []
    for provider_name, register in pending:
        entry = register()
        if not isinstance(entry, ProviderCatalogEntry):
            raise TypeError(
                "register_catalog_entry for "
                f"{provider_name!r} returned {type(entry).__name__}, "
                "expected ProviderCatalogEntry"
            )
        if entry.provider_name != provider_name:
            # Keep the logged name and the catalog name in lockstep so
            # the bootstrap log line matches what operators see in the
            # Marketplace UI. A mismatch is a developer error in the
            # connector module, not a runtime concern.
            raise ValueError(
                "provider_name mismatch: expected "
                f"{provider_name!r}, got {entry.provider_name!r}"
            )
        registered.append(entry)
    logger.info(
        "integrations.bootstrap: registered %d providers in order: %s",
        len(registered),
        [entry.provider_name for entry in registered],
    )
    return registered


__all__ = ["register_all_providers"]
