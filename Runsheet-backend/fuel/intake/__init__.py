"""
fuel.intake — Intake adapter framework for the Order Intake Pipeline.

This package defines the adapter contract that every intake channel
(voice, web portal, dispatcher, CSV, EDI, API partner, legacy) must
implement to produce canonical FuelOrder documents.

Public API:
    - IntakeContext: dataclass carrying per-request context for adapters
    - IntakeResult: dataclass carrying the adapter's output
    - IntakeAdapter: Protocol that every channel adapter implements
    - IntakeAdapterRegistry: registry keyed by (channel_type, schema_version)
    - AdapterError: exception raised by adapters or the registry
    - DispatcherIntakeAdapter: adapter for dispatcher keyboard channel
    - CsvIntakeAdapter: adapter for CSV bulk-upload channel
    - LegacyDineeShipmentAdapter: adapter for legacy Dinee shipment channel
    - ApiPartnerGenericAdapter: reference adapter for API partner channels
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
from fuel.intake.legacy_dinee_adapter import LegacyDineeShipmentAdapter

__all__ = [
    "AdapterError",
    "ApiPartnerGenericAdapter",
    "CsvIntakeAdapter",
    "DispatcherIntakeAdapter",
    "IntakeAdapter",
    "IntakeAdapterRegistry",
    "IntakeContext",
    "IntakeResult",
    "LegacyDineeShipmentAdapter",
]
