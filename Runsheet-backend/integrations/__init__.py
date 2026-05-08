"""
Integrations package for the Runsheet fuel-ops backend.

Capability 5 of the fuel-ops hardening spec introduces a pluggable
integration framework (QuickBooks Online, Veeder-Root, Geotab, Stripe,
rack-price providers, …) that all share a common :class:`IntegrationConnector`
ABC and a common persistence model for configured instances and sync-run
history.

Sub-modules:

* :mod:`integrations.connector_base` — the :class:`IntegrationConnector`
  ABC, the :class:`SyncRun` and :class:`IntegrationInstance` Pydantic
  models, and the :class:`IntegrationInstanceRepository` that owns CRUD
  against the ``integration_instances`` ES index.
* Per-provider adapter modules (``quickbooks_online``, ``veeder_root``,
  ``geotab``, ``stripe_connector``) will land in later tasks (9.4 – 9.9)
  and import from this package.
* :mod:`integrations.rack_price_provider_base` — ``RackPriceProvider``
  ABC with the OPIS B2B adapter and the S3-backed CSV fallback for
  Capability 8 sourcing (Task 7.3 / Requirements 8.2.1, 8.2.2, 8.2.4).
* :mod:`integrations.rack_price_sync` — :class:`RackPriceSyncService`
  that persists fetched rack prices to the ``rack_prices`` ES index
  and falls back to the most-recent cached price within 24 hours when
  the upstream provider fails or times out (Task 7.4 / Requirements
  8.2.3 and 8.2.5).
* :mod:`integrations.integration_scheduler` — :class:`IntegrationScheduler`
  that drives per-instance cron-scheduled ``sync_pull`` / ``sync_push``
  calls with exponential-backoff retry and exhaustion alerting on the
  SignalBus (Task 9.2 / Requirements 5.1.5, 5.1.6).
"""
