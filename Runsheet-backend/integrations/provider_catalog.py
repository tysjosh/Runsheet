"""
Integration provider catalog — registry consumed by
``GET /api/integrations/providers``.

Task 9.3 of the fuel-ops-hardening spec introduces the catalog endpoint
that the Integration_Marketplace UI (Req 5.6.2) reads to render the list
of providers a tenant can connect. The actual provider descriptors are
registered by the per-provider adapter modules landed in Tasks 9.4–9.10
(QuickBooks Online, Veeder-Root, Geotab, Stripe, rack-price, …). This
module is intentionally small and side-effect-free so it can be imported
without triggering adapter imports; registration is pull-based (the
adapters import this module and call :func:`register_provider` at import
time) rather than push-based from here.

Design points:

* **No secrets**: A :class:`ProviderCatalogEntry` carries only metadata
  the Marketplace UI needs to render the "Connect" flow — the
  ``required_credential_fields`` list is a schema, not values.
  Requirement 5.1.8 forbids exposing credential values anywhere, and
  that prohibition is enforced here by keeping this module model-only.

* **Stable ordering**: The catalog endpoint returns entries in the
  order they were registered so test assertions remain deterministic.
  Tasks 9.4–9.10 register in a fixed order from
  :mod:`bootstrap.integrations` (or whichever bootstrap module wires
  them) so production behaviour matches test behaviour.

* **Idempotent registration**: Re-registering an existing provider
  replaces the entry atomically so hot-reloads and test fixtures can
  safely re-register without duplicate warnings.

Validates: Requirements 5.1.7, 5.6.2, 5.6.6.
"""
from __future__ import annotations

from threading import RLock
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# Mirror the ``IntegrationCategory`` literal from :mod:`connector_base`
# without importing it so this module stays importable even when
# ``connector_base`` is mocked in a test fixture. The set of allowed
# values is identical and checked at validation time.
_PROVIDER_CATEGORIES = frozenset(
    {
        "accounting",
        "tank_monitor",
        "gps_eld",
        "payment",
        "tms",
        "terminal_pricing",
    }
)


class ProviderCatalogEntry(BaseModel):
    """Metadata describing a third-party integration the platform supports.

    Shape matches the :class:`integration_marketplace` contract spelled
    out in Requirement 5.6.2 verbatim: the Marketplace UI reads
    ``provider_name``, ``category``, ``description``, and the
    ``required_credential_fields`` list to render the "Connect" form for
    each provider, with ``doc_url`` linking out to the provider's setup
    guide. ``feature_flag_key`` is the per-tenant Redis key the
    Marketplace checks before surfacing the provider (Requirement 5.6.6).

    The model is intentionally frozen so adapter modules can hand their
    entry straight to :func:`register_provider` without worrying about
    mutation from the catalog consumer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str = Field(
        ...,
        min_length=1,
        description=(
            "Short snake_case identifier matching the owning "
            "``IntegrationConnector.provider_name`` ClassVar. Used as "
            "the primary key of this catalog."
        ),
    )
    category: str = Field(
        ...,
        min_length=1,
        description=(
            "Coarse grouping used by the Marketplace UI to organize the "
            "provider list. One of accounting, tank_monitor, gps_eld, "
            "payment, tms, terminal_pricing."
        ),
    )
    description: str = Field(
        ...,
        min_length=1,
        description="Short human-readable description surfaced in the card header.",
    )
    required_credential_fields: List[str] = Field(
        default_factory=list,
        description=(
            "Names of the credential fields the provider's connect "
            "flow expects (e.g. ``['client_id', 'client_secret']`` for "
            "OAuth providers, ``['api_token']`` for API-key providers). "
            "This is a schema only — VALUES are never transported "
            "through this model (Req 5.1.8)."
        ),
    )
    doc_url: Optional[str] = Field(
        default=None,
        description=(
            "Optional link to the provider's setup guide. Surfaced "
            "as a 'Learn more' anchor in the Marketplace card."
        ),
    )
    auth_mode: Literal["oauth2", "api_key", "basic", "custom"] = Field(
        default="api_key",
        description=(
            "Hint to the Marketplace UI on which connect flow to "
            "render. ``oauth2`` kicks off an authorization handoff; "
            "``api_key`` / ``basic`` render a form; ``custom`` defers "
            "to the adapter's own UI contribution."
        ),
    )
    feature_flag_key: Optional[str] = Field(
        default=None,
        description=(
            "Per-tenant Redis feature-flag key checked by the "
            "Marketplace before surfacing this provider (Req 5.6.6). "
            "Defaults to ``overlay.integration.{provider_name}`` when "
            "omitted by the caller."
        ),
    )

    @field_validator("category")
    @classmethod
    def _category_must_be_known(cls, value: str) -> str:
        stripped = value.strip()
        if stripped not in _PROVIDER_CATEGORIES:
            raise ValueError(
                f"unknown integration category {value!r}; expected one of "
                f"{sorted(_PROVIDER_CATEGORIES)}"
            )
        return stripped

    @field_validator(
        "provider_name",
        "description",
        mode="before",
    )
    @classmethod
    def _strip_required_strings(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("doc_url", "feature_flag_key", mode="before")
    @classmethod
    def _normalize_optional_strings(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None

    @field_validator("required_credential_fields")
    @classmethod
    def _dedupe_fields_preserving_order(cls, value: List[str]) -> List[str]:
        """Drop empties and duplicates, preserving first-seen order."""
        seen: Dict[str, None] = {}
        for item in value or []:
            if not isinstance(item, str):
                raise ValueError(
                    "required_credential_fields entries must be strings, got "
                    f"{type(item).__name__}"
                )
            stripped = item.strip()
            if not stripped:
                continue
            if stripped not in seen:
                seen[stripped] = None
        return list(seen.keys())

    def effective_feature_flag_key(self) -> str:
        """Return the feature-flag key the Marketplace should check for this provider.

        When the adapter does not supply an override, the default is
        ``overlay.integration.{provider_name}`` as mandated by
        Requirement 5.6.6.
        """
        return self.feature_flag_key or f"overlay.integration.{self.provider_name}"


# ---------------------------------------------------------------------------
# Module-level registry
# ---------------------------------------------------------------------------

# Insertion-ordered registry so ``list_providers`` is deterministic.
# ``RLock`` guards concurrent registration (e.g. parallel adapter imports
# during a test run) without blocking re-entrant calls from the same
# thread during bootstrap.
_REGISTRY: "Dict[str, ProviderCatalogEntry]" = {}
_REGISTRY_LOCK = RLock()


def register_provider(entry: ProviderCatalogEntry) -> ProviderCatalogEntry:
    """Register or replace a provider entry.

    Re-registering an existing ``provider_name`` replaces the entry
    atomically so hot-reload and test fixtures never raise.

    Returns the registered entry so callers can chain on the result if
    needed.
    """

    if not isinstance(entry, ProviderCatalogEntry):
        raise TypeError(
            "register_provider expects a ProviderCatalogEntry, got "
            f"{type(entry).__name__}"
        )
    with _REGISTRY_LOCK:
        _REGISTRY[entry.provider_name] = entry
    return entry


def unregister_provider(provider_name: str) -> bool:
    """Remove a provider entry. Returns ``True`` when an entry was removed.

    Primarily used by tests to restore a clean baseline between cases.
    """

    if not provider_name:
        return False
    with _REGISTRY_LOCK:
        return _REGISTRY.pop(provider_name, None) is not None


def list_providers() -> List[ProviderCatalogEntry]:
    """Return every registered provider entry in registration order."""

    with _REGISTRY_LOCK:
        return list(_REGISTRY.values())


def get_provider(provider_name: str) -> Optional[ProviderCatalogEntry]:
    """Return the catalog entry for ``provider_name`` or ``None`` when absent."""

    if not provider_name:
        return None
    with _REGISTRY_LOCK:
        return _REGISTRY.get(provider_name)


def clear_registry() -> None:
    """Remove every entry. Test-only helper; never call from production code."""

    with _REGISTRY_LOCK:
        _REGISTRY.clear()


__all__ = [
    "ProviderCatalogEntry",
    "clear_registry",
    "get_provider",
    "list_providers",
    "register_provider",
    "unregister_provider",
]
