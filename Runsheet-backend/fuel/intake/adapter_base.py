"""
Intake adapter base — Protocol, registry, context, result, and error types.

This module defines the contract that every intake channel adapter must
implement to produce canonical FuelOrder documents. The registry keys
adapters by ``(channel_type, schema_version)`` tuples so multiple versions
of the same channel can coexist during schema migrations.

Validates: Requirements 2.3.1, 2.3.2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple

from fuel.intake_channel_models import IntakeChannel


# ---------------------------------------------------------------------------
# AdapterError
# ---------------------------------------------------------------------------


class AdapterError(Exception):
    """Raised by adapters or the registry when intake cannot proceed.

    Attributes:
        error_type: A machine-readable error classification string
                    (e.g. ``"unknown_schema_version"``,
                    ``"adapter_output_invalid"``).
    """

    def __init__(self, error_type: str, message: str = "") -> None:
        self.error_type = error_type
        super().__init__(message or error_type)


# ---------------------------------------------------------------------------
# IntakeContext
# ---------------------------------------------------------------------------


@dataclass
class IntakeContext:
    """Per-request context passed to every adapter's ``transform`` call.

    Carries tenant identity, the resolved intake channel configuration,
    tracing identifiers, and (for dispatcher/JWT paths) the acting user.
    """

    tenant_id: str
    channel: IntakeChannel
    trace_id: str
    request_id: str
    actor_user_id: Optional[str] = None


# ---------------------------------------------------------------------------
# IntakeResult
# ---------------------------------------------------------------------------


@dataclass
class IntakeResult:
    """Output of an adapter's ``transform`` method.

    Attributes:
        order_doc: A dict representing the FuelOrder fields the adapter
                   is responsible for. The pipeline stamps platform-owned
                   fields (``order_id``, ``tenant_id``, ``status``,
                   timestamps) after receiving this.
        event_docs: A list of dicts representing Order_Events to append
                    (typically a single ``order_placed`` event). Defaults
                    to an empty list when the adapter does not emit events.
    """

    order_doc: Dict[str, Any]
    event_docs: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# IntakeAdapter Protocol
# ---------------------------------------------------------------------------


class IntakeAdapter(Protocol):
    """Transform an upstream payload into a FuelOrder + event docs.

    Adapters MAY NOT set ``order_id``, ``tenant_id``, or ``status`` —
    those are assigned by the ``OrderIntakePipeline``. Adapters MUST
    populate ``intake_channel``, ``intake_channel_id``,
    ``intake_metadata``, and ``source_schema_version`` from the
    context + payload.
    """

    channel_type: str

    def transform(
        self, payload: Dict[str, Any], context: IntakeContext
    ) -> IntakeResult:
        """Convert a raw upstream payload into a canonical order document.

        Args:
            payload: The raw JSON body from the upstream channel.
            context: Per-request context with tenant, channel, and trace info.

        Returns:
            An IntakeResult containing the order_doc and optional event_docs.

        Raises:
            AdapterError: When the payload cannot be transformed (e.g.
                missing required fields, unmappable structure).
        """
        ...


# ---------------------------------------------------------------------------
# IntakeAdapterRegistry
# ---------------------------------------------------------------------------


class IntakeAdapterRegistry:
    """Registry of intake adapters keyed by (channel_type, schema_version).

    Multiple adapter versions for the same channel_type may be registered
    and dispatched by the payload's ``schema_version``, matching the
    existing ``AdapterTransformer`` pattern from ops-intelligence-layer.
    """

    def __init__(self) -> None:
        self._adapters: Dict[Tuple[str, str], IntakeAdapter] = {}

    def register(
        self,
        adapter: IntakeAdapter,
        *,
        channel_type: str,
        schema_version: str,
    ) -> None:
        """Register an adapter for a (channel_type, schema_version) pair.

        Args:
            adapter: The adapter instance implementing IntakeAdapter.
            channel_type: The channel type this adapter handles
                          (e.g. ``"dispatcher"``, ``"csv"``).
            schema_version: The schema version this adapter handles
                            (e.g. ``"1.0"``).
        """
        self._adapters[(channel_type, schema_version)] = adapter

    def get(
        self, channel_type: str, schema_version: str = "1.0"
    ) -> IntakeAdapter:
        """Retrieve the adapter for a (channel_type, schema_version) pair.

        Args:
            channel_type: The channel type to look up.
            schema_version: The schema version to look up. Defaults to "1.0".

        Returns:
            The registered IntakeAdapter instance.

        Raises:
            AdapterError: With ``error_type="unknown_schema_version"`` when
                no adapter is registered for the given combination.
        """
        adapter = self._adapters.get((channel_type, schema_version))
        if adapter is None:
            raise AdapterError(
                error_type="unknown_schema_version",
                message=(
                    f"No adapter registered for channel_type={channel_type!r}, "
                    f"schema_version={schema_version!r}"
                ),
            )
        return adapter


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "AdapterError",
    "IntakeAdapter",
    "IntakeAdapterRegistry",
    "IntakeContext",
    "IntakeResult",
]
