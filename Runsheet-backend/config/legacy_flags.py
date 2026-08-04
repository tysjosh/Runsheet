"""
Resolver for the ``legacy_ng_delivery`` feature flag.

Single source of truth for whether the pre-pivot Nigerian last-mile delivery
surface is served. The flag itself lives on :class:`config.settings.Settings`
as ``legacy_ng_delivery_enabled`` (env ``LEGACY_NG_DELIVERY_ENABLED``,
default ``False``) — this module only resolves it.

Why a resolver instead of reading Settings inline:

* ``get_settings()`` caches the Settings singleton for the process lifetime,
  so an operator (or a test) flipping ``LEGACY_NG_DELIVERY_ENABLED`` would
  otherwise need a cache reload. Reading the environment variable first makes
  the flag effective immediately and keeps the gate trivially testable.
* Every gated call site shares one helper, so there is exactly one flag —
  no second flag system alongside the per-tenant
  :class:`ops.services.feature_flags.FeatureFlagService`.

Gating posture: fail-closed. Anything unparseable resolves to disabled.

Audit reference: product-owner-audit-2026-05-08 recommendation #1.
"""

from __future__ import annotations

import os

LEGACY_NG_DELIVERY_ENV_VAR = "LEGACY_NG_DELIVERY_ENABLED"

#: Error code returned by gated endpoints when the surface is off. Raise it via
#: ``errors.exceptions.legacy_ng_delivery_disabled()`` so the response goes
#: through the structured ``ErrorResponse`` envelope.
LEGACY_NG_DELIVERY_DISABLED_CODE = "LEGACY_NG_DELIVERY_DISABLED"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_legacy_ng_delivery_enabled() -> bool:
    """Return ``True`` when the legacy NG last-mile surface should be served.

    Resolution order:

    1. ``LEGACY_NG_DELIVERY_ENABLED`` environment variable, when set.
    2. ``Settings.legacy_ng_delivery_enabled``.
    3. ``False`` (fail-closed) if settings cannot be loaded.
    """
    raw = os.environ.get(LEGACY_NG_DELIVERY_ENV_VAR)
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in _TRUTHY

    try:
        from config.settings import get_settings

        return bool(getattr(get_settings(), "legacy_ng_delivery_enabled", False))
    except Exception:  # noqa: BLE001 — fail closed on any config error
        return False
