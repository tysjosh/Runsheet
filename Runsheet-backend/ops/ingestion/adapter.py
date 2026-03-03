"""
Adapter Transformer for Dinee payload normalization with schema versioning.

Converts Dinee webhook payloads into normalized Elasticsearch documents
conforming to the strict index mappings for shipments_current, shipment_events,
and riders_current. Maintains a registry of schema version handlers for
concurrent version support during migration periods.

Requirements: 2.1-2.10
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebhookPayload model (used by receiver and adapter)
# ---------------------------------------------------------------------------


class WebhookPayload(BaseModel):
    """Incoming Dinee webhook payload structure."""

    event_id: str = Field(..., description="Unique event identifier")
    event_type: str = Field(
        ...,
        description="Event type (shipment_created, rider_assigned, etc.)",
    )
    schema_version: str = Field(..., description='Semver e.g. "1.0"')
    tenant_id: str = Field(..., description="Tenant identifier")
    timestamp: str = Field(..., description="ISO 8601 event timestamp")
    data: dict = Field(default_factory=dict, description="Event-specific payload")


# ---------------------------------------------------------------------------
# Transform result
# ---------------------------------------------------------------------------


@dataclass
class TransformResult:
    """
    Output of a schema handler transformation.

    Each field maps to a target Elasticsearch index:
    - shipment_current_doc: upsert into shipments_current
    - rider_current_doc: upsert into riders_current
    - event_doc: append into shipment_events
    """

    event_doc: dict = field(default_factory=dict)
    shipment_current_doc: Optional[dict] = None
    rider_current_doc: Optional[dict] = None


# ---------------------------------------------------------------------------
# Schema handler ABC
# ---------------------------------------------------------------------------


class SchemaHandler(ABC):
    """
    Abstract base class for versioned schema handlers.

    Each handler knows how to transform a Dinee payload of a specific
    schema_version into normalized Elasticsearch documents.
    """

    @abstractmethod
    def transform(self, payload: dict, request_id: str) -> TransformResult:
        """
        Transform a Dinee payload into ES documents.

        Args:
            payload: The raw ``data`` dict from the webhook payload.
            request_id: Originating request/trace identifier.

        Returns:
            TransformResult with documents for each target index.
        """
        ...


# ---------------------------------------------------------------------------
# Allowed fields per target index (derived from strict ES mappings)
# ---------------------------------------------------------------------------

SHIPMENTS_CURRENT_FIELDS: set[str] = {
    "shipment_id",
    "status",
    "tenant_id",
    "rider_id",
    "failure_reason",
    "source_schema_version",
    "trace_id",
    "created_at",
    "updated_at",
    "estimated_delivery",
    "last_event_timestamp",
    "ingested_at",
    "current_location",
    "origin",
    "destination",
}

SHIPMENT_EVENTS_FIELDS: set[str] = {
    "event_id",
    "shipment_id",
    "event_type",
    "tenant_id",
    "source_schema_version",
    "trace_id",
    "event_timestamp",
    "ingested_at",
    "event_payload",
    "location",
}

RIDERS_CURRENT_FIELDS: set[str] = {
    "rider_id",
    "rider_name",
    "status",
    "tenant_id",
    "availability",
    "source_schema_version",
    "trace_id",
    "last_seen",
    "last_event_timestamp",
    "ingested_at",
    "current_location",
    "active_shipment_count",
    "completed_today",
}


# ---------------------------------------------------------------------------
# Adapter Transformer
# ---------------------------------------------------------------------------


class AdapterTransformer:
    """
    Versioned adapter that converts Dinee webhook payloads into normalized
    Elasticsearch documents.

    Maintains a registry of :class:`SchemaHandler` instances keyed by
    schema version string.  The ``transform`` method selects the appropriate
    handler, enriches the output with tracing/ingestion metadata, and
    validates documents against the target index schemas.

    Requirements: 2.1-2.10
    """

    def __init__(self) -> None:
        self._handlers: dict[str, SchemaHandler] = {}
        self._deprecated_versions: set[str] = set()

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------

    def register_handler(
        self,
        version: str,
        handler: SchemaHandler,
        deprecated: bool = False,
    ) -> None:
        """
        Register a schema version handler.

        Args:
            version: Semantic version string (e.g. ``"1.0"``).
            handler: Handler instance implementing :class:`SchemaHandler`.
            deprecated: If ``True``, payloads using this version will be
                processed but a WARN log will be emitted.

        Validates: Req 2.9
        """
        self._handlers[version] = handler
        if deprecated:
            self._deprecated_versions.add(version)
        logger.info(
            "Registered schema handler for version %s%s",
            version,
            " (deprecated)" if deprecated else "",
        )

    def is_version_supported(self, version: str) -> bool:
        """Check if a schema version has a registered handler."""
        return version in self._handlers

    def is_version_deprecated(self, version: str) -> bool:
        """Check if a schema version is deprecated."""
        return version in self._deprecated_versions

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------

    def transform(
        self,
        payload: WebhookPayload,
        request_id: str,
    ) -> TransformResult:
        """
        Transform a webhook payload using the appropriate version handler.

        Steps:
        1. Look up handler by ``payload.schema_version``.
        2. Warn if the version is deprecated (Req 2.10).
        3. Delegate to handler.
        4. Enrich output docs with ``ingested_at``, ``trace_id``,
           ``source_schema_version`` (Req 2.6, 2.8).
        5. Validate output docs against target index schemas (Req 2.5).
        6. Log warnings for unmappable fields (Req 2.4).

        Args:
            payload: Parsed webhook payload.
            request_id: Originating request identifier (becomes ``trace_id``).

        Returns:
            TransformResult with enriched, validated documents.

        Raises:
            ValueError: If the schema version is not supported.
        """
        version = payload.schema_version

        if version not in self._handlers:
            raise ValueError(
                f"Unsupported schema version: {version}"
            )

        # Req 2.10 – warn on deprecated versions
        if version in self._deprecated_versions:
            logger.warning(
                "Processing payload with deprecated schema version %s "
                "(event_id=%s, tenant_id=%s)",
                version,
                payload.event_id,
                payload.tenant_id,
            )

        handler = self._handlers[version]
        # Pass the full payload as a dict so handlers can access top-level
        # fields (event_type, tenant_id, event_id, timestamp) alongside data.
        full_payload = payload.model_dump()
        result = handler.transform(full_payload, request_id)

        # Enrich & validate each output document
        now = datetime.now(timezone.utc).isoformat()

        if result.shipment_current_doc is not None:
            result.shipment_current_doc = self._enrich_and_validate(
                doc=result.shipment_current_doc,
                allowed_fields=SHIPMENTS_CURRENT_FIELDS,
                index_name="shipments_current",
                ingested_at=now,
                trace_id=request_id,
                source_schema_version=version,
                event_id=payload.event_id,
            )

        if result.rider_current_doc is not None:
            result.rider_current_doc = self._enrich_and_validate(
                doc=result.rider_current_doc,
                allowed_fields=RIDERS_CURRENT_FIELDS,
                index_name="riders_current",
                ingested_at=now,
                trace_id=request_id,
                source_schema_version=version,
                event_id=payload.event_id,
            )

        result.event_doc = self._enrich_and_validate(
            doc=result.event_doc,
            allowed_fields=SHIPMENT_EVENTS_FIELDS,
            index_name="shipment_events",
            ingested_at=now,
            trace_id=request_id,
            source_schema_version=version,
            event_id=payload.event_id,
        )

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _enrich_and_validate(
        doc: dict,
        allowed_fields: set[str],
        index_name: str,
        *,
        ingested_at: str,
        trace_id: str,
        source_schema_version: str,
        event_id: str,
    ) -> dict:
        """
        Enrich a document with tracing metadata and strip unmapped fields.

        - Adds ``ingested_at``, ``trace_id``, ``source_schema_version``
          (Req 2.6, 2.8).
        - Removes fields not in the target index mapping and logs a
          warning for each (Req 2.4, 2.5).

        Returns:
            A new dict containing only allowed fields plus enrichment.
        """
        # Enrichment (Req 2.6, 2.8)
        doc["ingested_at"] = ingested_at
        doc["trace_id"] = trace_id
        doc["source_schema_version"] = source_schema_version

        # Validate against target schema – strip unmapped fields (Req 2.4, 2.5)
        validated: dict = {}
        for key, value in doc.items():
            if key in allowed_fields:
                validated[key] = value
            else:
                logger.warning(
                    "Unmappable field '%s' in output for index '%s' "
                    "(event_id=%s) – field omitted",
                    key,
                    index_name,
                    event_id,
                )

        return validated
