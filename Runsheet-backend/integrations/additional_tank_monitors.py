"""
Catalog entries for non-Veeder tank-monitor integrations.

These providers use the same IntegrationInstance, credential-vault, scheduler,
and tank-mapping surfaces as Veeder-Root. The concrete polling adapters can be
enabled behind the advertised feature flags per tenant, but the Marketplace
must already expose the supported ATG/telemetry systems so operators can create
instances and attach credentials without a code deployment.
"""
from __future__ import annotations

from integrations.provider_catalog import ProviderCatalogEntry, register_provider


def _register_tank_monitor(
    *,
    provider_name: str,
    description: str,
    required_credential_fields: list[str],
    doc_url: str,
) -> ProviderCatalogEntry:
    return register_provider(
        ProviderCatalogEntry(
            provider_name=provider_name,
            category="tank_monitor",
            description=description,
            required_credential_fields=required_credential_fields,
            doc_url=doc_url,
            auth_mode="api_key",
        )
    )


def register_otodata_catalog_entry() -> ProviderCatalogEntry:
    """Register Otodata propane tank telemetry."""

    return _register_tank_monitor(
        provider_name="otodata",
        description=(
            "Pull propane tank-level telemetry from Otodata monitors, including "
            "battery and signal-health fields when supplied by the tenant account."
        ),
        required_credential_fields=["api_token", "account_id", "endpoint_url"],
        doc_url="https://www.otodatatankmonitors.com/",
    )


def register_silverlink_catalog_entry() -> ProviderCatalogEntry:
    """Register SilverLink tank monitor telemetry."""

    return _register_tank_monitor(
        provider_name="silverlink",
        description=(
            "Pull tank inventory snapshots from SilverLink ATG/telemetry feeds "
            "through a tenant-scoped API endpoint."
        ),
        required_credential_fields=["api_token", "endpoint_url"],
        doc_url="https://www.silverlinktechnologies.com/",
    )


def register_gasboy_catalog_entry() -> ProviderCatalogEntry:
    """Register Gasboy site-controller fuel telemetry."""

    return _register_tank_monitor(
        provider_name="gasboy",
        description=(
            "Pull site-controller fuel inventory readings from Gasboy-connected "
            "locations using the tenant's configured API endpoint."
        ),
        required_credential_fields=["api_token", "endpoint_url"],
        doc_url="https://www.gasboy.com/",
    )


def register_franklin_fueling_catalog_entry() -> ProviderCatalogEntry:
    """Register Franklin Fueling Systems ATG telemetry."""

    return _register_tank_monitor(
        provider_name="franklin_fueling",
        description=(
            "Pull tank-gauge inventory readings from Franklin Fueling Systems "
            "ATG integrations."
        ),
        required_credential_fields=["api_token", "endpoint_url"],
        doc_url="https://www.franklinfueling.com/",
    )


__all__ = [
    "register_franklin_fueling_catalog_entry",
    "register_gasboy_catalog_entry",
    "register_otodata_catalog_entry",
    "register_silverlink_catalog_entry",
]
