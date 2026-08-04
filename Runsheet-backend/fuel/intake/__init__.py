"""
fuel.intake — Intake adapter framework for the Order Intake Pipeline.

This package defines the adapter contract that every intake channel
(voice, dispatcher, CSV, API partner) must implement to produce canonical
FuelOrder documents.

Public API:
    - IntakeContext: dataclass carrying per-request context for adapters
    - IntakeResult: dataclass carrying the adapter's output
    - IntakeAdapter: Protocol that every channel adapter implements
    - IntakeAdapterRegistry: registry keyed by (channel_type, schema_version)
    - AdapterError: exception raised by adapters or the registry
    - DispatcherIntakeAdapter: adapter for dispatcher keyboard channel
    - CsvIntakeAdapter: adapter for CSV bulk-upload channel
    - ApiPartnerGenericAdapter: reference adapter for API partner channels
    - VoiceIntakeAdapter: adapter for the Dinee voice channel
      (in ``voice_intake_adapter``; not re-exported here, mirroring how it is
      imported directly at bootstrap)

``LegacyDineeShipmentAdapter`` was removed with the ``POST /webhooks/dinee``
route it served. It was already unreachable: it registered under
``channel_type="legacy"``, while the only writer of the ``dinee-legacy``
channel record set ``channel_type="api_partner"``, so adapter resolution never
selected it.
"""

from fuel.intake.adapter_base import (
    AdapterError,
    IntakeAdapter,
    IntakeAdapterRegistry,
    IntakeContext,
    IntakeResult,
)
from fuel.intake.api_partner_adapter import ApiPartnerGenericAdapter
from fuel.intake.csv_adapter import CsvIntakeAdapter
from fuel.intake.dispatcher_adapter import DispatcherIntakeAdapter

__all__ = [
    "AdapterError",
    "ApiPartnerGenericAdapter",
    "CsvIntakeAdapter",
    "DispatcherIntakeAdapter",
    "IntakeAdapter",
    "IntakeAdapterRegistry",
    "IntakeContext",
    "IntakeResult",
]
