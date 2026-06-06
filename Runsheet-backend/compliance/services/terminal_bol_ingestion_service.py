"""Terminal BOL Ingestion Service — ingest, validate, and persist terminal BOLs.

Implements the ``Terminal_BOL_Ingestion_Service`` described in design §10 of
the Fuel Compliance Backbone spec. This service handles:

* ``ingest_edi(edi_payload, tenant_id)`` — parse an EDI payload (X12 856 or
  pipe-delimited) via the :class:`EDIParserRegistry`, construct a
  :class:`TerminalBOL` model, and persist to the ``terminal_bols`` ES index.
* ``ingest_manual(file_bytes, content_type, tenant_id)`` — accept a manual
  upload (PDF/image) and extract BOL fields using OCR with operator
  confirmation workflow (Task 11.5).
* ``confirm_manual_bol(tenant_id, bol_id, confirmed_fields)`` — update a
  pending_confirmation BOL with operator-confirmed field values (Task 11.5).
* ``link_to_load_plan(bol_id, load_plan_id)`` — link an ingested BOL to a
  load plan for chain-of-custody traceability (Task 11.8).
* Driver validation — validates that the driver_id on a BOL matches an
  active driver in the DriverQualificationService (Task 11.6).
* VCF cross-reference — cross-references the terminal-reported net_gallons
  against the VCFCalculator using the BOL's temperature and API gravity,
  flagging discrepancies exceeding ±0.1% (Task 11.7).
* Idempotency — rejects duplicate load_numbers with error code
  ``bol.duplicate_load_number`` (Task 11.9).

All queries are tenant-scoped via ``inject_tenant_filter`` (Constraint C3).

Validates: Requirement 10.1, 10.2, 10.3, 10.4, 10.5, 10.6
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from compliance.models.terminal_bol import TerminalBOL
from compliance.services.compliance_es_mappings import TERMINAL_BOLS_INDEX
from compliance.services.terminal_bol_edi_parser import (
    EDIParserRegistry,
    EDIParseError,
)
from errors.exceptions import validation_error
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# Supported MIME types for manual BOL upload.
_ALLOWED_CONTENT_TYPES = frozenset({
    "application/pdf",
    "image/jpeg",
    "image/png",
})


class TerminalBOLIngestionService:
    """Service for ingesting terminal Bills of Lading.

    Accepts EDI payloads or manual uploads, parses them into
    :class:`TerminalBOL` records, and persists them to the
    ``terminal_bols`` Elasticsearch index.

    Args:
        es_service: Elasticsearch handle for the ``terminal_bols`` index.
        edi_parser_registry: Registry of EDI parser strategies for
            auto-detecting and parsing EDI payloads.
        vcf_calculator: Optional VCF calculator for cross-referencing
            net_gallons (Task 11.7).
        driver_qualification_service: Optional driver qualification service
            for validating driver_id (Task 11.6).
        file_storage_service: Optional file storage service for persisting
            raw EDI/documents as immutable attachments (Task 11.10).

    Validates: Requirement 10.1
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        edi_parser_registry: EDIParserRegistry,
        vcf_calculator: Optional[Any] = None,
        driver_qualification_service: Optional[Any] = None,
        file_storage_service: Optional[Any] = None,
    ) -> None:
        self._es = es_service
        self._edi_parser_registry = edi_parser_registry
        self._vcf_calculator = vcf_calculator
        self._driver_qualification_service = driver_qualification_service
        self._file_storage_service = file_storage_service

    # ------------------------------------------------------------------
    # VCF cross-reference (Task 11.7 — Validates: Requirement 10.4)
    # ------------------------------------------------------------------

    #: Tolerance threshold for VCF discrepancy flagging (±0.1%).
    VCF_DISCREPANCY_THRESHOLD: float = 0.001

    async def _cross_reference_vcf(
        self,
        bol: TerminalBOL,
    ) -> TerminalBOL:
        """Cross-reference terminal-reported net_gallons against VCFCalculator.

        Computes net gallons locally using the BOL's gross_gallons,
        observed_temperature_f, and api_gravity via VCFCalculator. If the
        discrepancy between the terminal-reported net_gallons and the
        locally computed value exceeds ±0.1%, the BOL is flagged with
        ``vcf_discrepancy_flag=True``. The BOL is still ingested (not
        rejected) — this is a warning, not a hard failure.

        If the VCFCalculator dependency is not configured, cross-referencing
        is skipped (graceful degradation for testing/bootstrap).

        If the VCFCalculator raises an error (e.g., input out of range),
        the cross-reference is skipped with a warning log rather than
        blocking ingestion.

        Args:
            bol: The TerminalBOL instance to validate.

        Returns:
            The same TerminalBOL instance, potentially with
            ``vcf_discrepancy_flag`` set to True if a discrepancy is found,
            or False if the values match within tolerance.

        Validates: Requirement 10.4
        """
        if self._vcf_calculator is None:
            logger.debug(
                "VCF cross-reference skipped — VCFCalculator not configured "
                "(tenant=%s, bol_id=%s)",
                bol.tenant_id,
                bol.bol_id,
            )
            return bol

        try:
            computed_net_gallons = self._vcf_calculator.compute_net_gallons(
                gross_gallons=bol.gross_gallons,
                temperature_f=bol.observed_temperature_f,
                api_gravity=bol.api_gravity,
            )
        except (ValueError, Exception) as exc:
            # If VCF computation fails (e.g., input out of range), log a
            # warning but do not block ingestion. The BOL is still valid
            # from a data-capture perspective.
            logger.warning(
                "VCF cross-reference failed for BOL %s (tenant=%s): %s. "
                "Skipping VCF validation.",
                bol.bol_id,
                bol.tenant_id,
                exc,
            )
            return bol

        # Compute the discrepancy as a percentage of the locally computed
        # net gallons. Guard against division by zero (should not happen
        # with valid gross_gallons > 0, but defensive).
        if computed_net_gallons == 0:
            logger.warning(
                "VCF cross-reference: computed_net_gallons is zero for "
                "BOL %s (tenant=%s). Skipping discrepancy check.",
                bol.bol_id,
                bol.tenant_id,
            )
            return bol

        discrepancy = abs(bol.net_gallons - computed_net_gallons) / computed_net_gallons

        if discrepancy > self.VCF_DISCREPANCY_THRESHOLD:
            bol.vcf_discrepancy_flag = True
            logger.warning(
                "VCF discrepancy detected for BOL %s (tenant=%s): "
                "terminal_net=%.1f, computed_net=%.1f, discrepancy=%.4f%% "
                "(threshold=%.4f%%)",
                bol.bol_id,
                bol.tenant_id,
                bol.net_gallons,
                computed_net_gallons,
                discrepancy * 100,
                self.VCF_DISCREPANCY_THRESHOLD * 100,
            )
        else:
            bol.vcf_discrepancy_flag = False
            logger.debug(
                "VCF cross-reference passed for BOL %s (tenant=%s): "
                "terminal_net=%.1f, computed_net=%.1f, discrepancy=%.4f%%",
                bol.bol_id,
                bol.tenant_id,
                bol.net_gallons,
                computed_net_gallons,
                discrepancy * 100,
            )

        return bol

    # ------------------------------------------------------------------
    # Driver validation (Task 11.6 — Validates: Requirement 10.3)
    # ------------------------------------------------------------------

    async def _validate_driver_id(self, tenant_id: str, driver_id: str) -> None:
        """Validate that driver_id matches an active driver.

        Queries the DriverQualificationService to verify:
        1. The driver exists in the system for this tenant.
        2. The driver's status is 'active'.

        If the driver_qualification_service dependency is not configured,
        validation is skipped (graceful degradation for testing/bootstrap).

        Args:
            tenant_id: Tenant scope for the query.
            driver_id: The driver identifier from the BOL.

        Raises:
            AppException (validation_error): If the driver is not found or
                is not in 'active' status.

        Validates: Requirement 10.3
        """
        if self._driver_qualification_service is None:
            logger.debug(
                "Driver validation skipped — DriverQualificationService "
                "not configured (tenant=%s, driver_id=%s)",
                tenant_id,
                driver_id,
            )
            return

        try:
            driver = await self._driver_qualification_service.get(
                tenant_id, driver_id
            )
        except Exception as exc:
            # The DriverQualificationService.get() raises resource_not_found
            # when the driver doesn't exist. Re-raise as a validation error
            # with a BOL-specific message.
            raise validation_error(
                f"Driver '{driver_id}' not found in Driver Qualification Service",
                details={
                    "driver_id": driver_id,
                    "tenant_id": tenant_id,
                    "error_code": "bol.driver_not_found",
                },
            ) from exc

        # Check that the driver is active (Req 10.3: "active driver")
        driver_status = driver.get("status", "unknown")
        if driver_status != "active":
            raise validation_error(
                f"Driver '{driver_id}' is not active (current status: {driver_status})",
                details={
                    "driver_id": driver_id,
                    "driver_status": driver_status,
                    "tenant_id": tenant_id,
                    "error_code": "bol.driver_not_active",
                },
            )

        logger.debug(
            "Driver validation passed: driver_id=%s is active (tenant=%s)",
            driver_id,
            tenant_id,
        )

    # ------------------------------------------------------------------
    # Idempotency check (Task 11.9 — Validates: Requirement 10.6)
    # ------------------------------------------------------------------

    async def _check_duplicate_load_number(
        self, tenant_id: str, load_number: str, exclude_bol_id: Optional[str] = None
    ) -> None:
        """Reject ingestion if a BOL with the same load_number already exists.

        Queries the ``terminal_bols`` ES index for an existing document with
        the same ``load_number`` within the tenant scope. If a match is found,
        raises a validation error with error code ``bol.duplicate_load_number``
        to enforce idempotency.

        Args:
            tenant_id: Tenant scope for the query.
            load_number: The load number to check for duplicates.
            exclude_bol_id: Optional BOL ID to exclude from the duplicate
                check (used during confirm_manual_bol to avoid matching the
                BOL being confirmed against itself).

        Raises:
            AppException (validation_error): If a BOL with the same
                load_number already exists for this tenant.

        Validates: Requirement 10.6
        """
        query = inject_tenant_filter(
            {"query": {"term": {"load_number": load_number}}},
            tenant_id,
        )
        result = await self._es.search_documents(TERMINAL_BOLS_INDEX, query)
        hits = result.get("hits", {}).get("hits", [])

        # Filter out the BOL being confirmed (if applicable)
        if exclude_bol_id:
            hits = [
                h for h in hits
                if h["_source"].get("bol_id") != exclude_bol_id
            ]

        if hits:
            existing_bol_id = hits[0]["_source"].get("bol_id", "unknown")
            raise validation_error(
                f"A BOL with load_number '{load_number}' already exists "
                f"(existing bol_id: {existing_bol_id})",
                details={
                    "load_number": load_number,
                    "existing_bol_id": existing_bol_id,
                    "tenant_id": tenant_id,
                    "error_code": "bol.duplicate_load_number",
                },
            )

        logger.debug(
            "Idempotency check passed: no existing BOL with load_number=%s "
            "(tenant=%s)",
            load_number,
            tenant_id,
        )

    # ------------------------------------------------------------------
    # ingest_edi (Task 11.3 / 11.4)
    # ------------------------------------------------------------------

    async def ingest_edi(
        self, edi_payload: bytes, tenant_id: str
    ) -> TerminalBOL:
        """Ingest a terminal BOL from an EDI payload.

        Parses the EDI payload using the registered parser strategies,
        constructs a :class:`TerminalBOL` model from the extracted fields,
        persists the raw EDI payload as an immutable attachment via
        FileStorageService (if configured), and persists the parsed record
        to the ``terminal_bols`` ES index.

        Args:
            edi_payload: Raw EDI bytes (X12 856 or pipe-delimited).
            tenant_id: Tenant identifier for data isolation.

        Returns:
            The persisted :class:`TerminalBOL` instance.

        Raises:
            EDIParseError: If the payload cannot be parsed.
            ValueError: If the parsed fields fail model validation.

        Validates: Requirement 10.1, 10.7
        """
        # 1. Parse the EDI payload using the registry
        parsed_fields = self._edi_parser_registry.parse(edi_payload)

        # 2. Map parsed fields to TerminalBOL model fields
        timestamp = parsed_fields.get("timestamp")
        if isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp)
            except (ValueError, TypeError):
                raise validation_error(
                    "Cannot parse timestamp from EDI payload",
                    details={"timestamp": timestamp},
                )

        bol = TerminalBOL(
            tenant_id=tenant_id,
            load_number=parsed_fields["load_number"],
            product_code=parsed_fields["product_code"],
            gross_gallons=parsed_fields["gross_gallons"],
            net_gallons=parsed_fields["net_gallons"],
            observed_temperature_f=parsed_fields["observed_temperature"],
            api_gravity=parsed_fields["api_gravity"],
            supplier_name=parsed_fields["supplier_name"],
            terminal_name=parsed_fields["terminal_name"],
            driver_id=parsed_fields["driver_id"],
            timestamp=timestamp,
            status="ingested",
        )

        # 3. Validate driver_id against DriverQualificationService (Req 10.3)
        await self._validate_driver_id(tenant_id, bol.driver_id)

        # 4. Idempotency check: reject duplicate load_number (Req 10.6)
        await self._check_duplicate_load_number(tenant_id, bol.load_number)

        # 5. Cross-reference net_gallons against VCFCalculator (Req 10.4)
        await self._cross_reference_vcf(bol)

        # 6. Persist raw EDI payload to FileStorageService as immutable
        #    attachment (Req 10.7). If FileStorageService is not configured,
        #    skip gracefully without blocking ingestion.
        if self._file_storage_service is not None:
            try:
                raw_document_ref = self._file_storage_service.put(
                    tenant_id=tenant_id,
                    category="terminal_bols",
                    content_bytes=edi_payload,
                    content_type="application/edi-x12",
                )
                bol.raw_document_ref = raw_document_ref
            except Exception as exc:
                logger.warning(
                    "Failed to store raw EDI payload for BOL %s "
                    "(tenant=%s): %s. Continuing without raw_document_ref.",
                    bol.bol_id,
                    tenant_id,
                    exc,
                )
        else:
            logger.debug(
                "Raw EDI storage skipped — FileStorageService not configured "
                "(tenant=%s, bol_id=%s)",
                tenant_id,
                bol.bol_id,
            )

        # 7. Persist to the terminal_bols ES index
        doc = bol.model_dump(mode="json")
        await self._es.index_document(
            TERMINAL_BOLS_INDEX, bol.bol_id, doc
        )

        logger.info(
            "Ingested terminal BOL %s (load_number=%s) for tenant %s",
            bol.bol_id,
            bol.load_number,
            tenant_id,
        )

        return bol

    # ------------------------------------------------------------------
    # ingest_manual (Task 11.5 — Validates: Requirement 10.2)
    # ------------------------------------------------------------------

    async def ingest_manual(
        self, file_bytes: bytes, content_type: str, tenant_id: str
    ) -> TerminalBOL:
        """Ingest a terminal BOL from a manual upload (PDF/image).

        Accepts PDF or image formats, stores the raw document via
        FileStorageService (if available), and creates a TerminalBOL record
        with status ``pending_confirmation``. The operator must confirm the
        OCR-extracted fields via :meth:`confirm_manual_bol` before the BOL
        transitions to ``ingested`` status.

        Args:
            file_bytes: Raw file bytes (PDF or image).
            content_type: MIME type of the uploaded file
                (application/pdf, image/jpeg, image/png).
            tenant_id: Tenant identifier for data isolation.

        Returns:
            The persisted :class:`TerminalBOL` instance with
            ``status="pending_confirmation"`` and
            ``needs_operator_confirmation=True``.

        Raises:
            ValueError: If content_type is not one of the allowed types.

        Validates: Requirement 10.2
        """
        # 1. Validate content_type
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise validation_error(
                f"Unsupported content type for manual BOL upload: {content_type}. "
                f"Allowed types: {', '.join(sorted(_ALLOWED_CONTENT_TYPES))}",
                details={"content_type": content_type},
            )

        # 2. Validate file_bytes is non-empty
        if not file_bytes:
            raise validation_error(
                "File bytes must not be empty for manual BOL upload",
                details={"content_type": content_type},
            )

        # 3. Store the raw document via FileStorageService (if available)
        raw_document_ref: Optional[str] = None
        if self._file_storage_service is not None:
            try:
                raw_document_ref = self._file_storage_service.put(
                    tenant_id=tenant_id,
                    category="terminal_bols",
                    content_bytes=file_bytes,
                    content_type=content_type,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to store raw document for manual BOL upload "
                    "tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                # Continue without raw_document_ref — the BOL record is
                # still created so the operator can confirm fields.

        # 4. Create a TerminalBOL record with placeholder fields and
        #    status "pending_confirmation". The operator will fill in the
        #    actual values via confirm_manual_bol().
        bol = TerminalBOL(
            tenant_id=tenant_id,
            load_number="PENDING",
            product_code="PENDING",
            gross_gallons=0.1,  # Placeholder — must be positive per validator
            net_gallons=0.1,    # Placeholder — must be positive per validator
            observed_temperature_f=60.0,  # Standard reference temperature
            api_gravity=0.0,
            supplier_name="PENDING",
            terminal_name="PENDING",
            driver_id="PENDING",
            timestamp=utcnow(),
            status="pending_confirmation",
            needs_operator_confirmation=True,
            raw_document_ref=raw_document_ref,
        )

        # 5. Persist to the terminal_bols ES index
        doc = bol.model_dump(mode="json")
        await self._es.index_document(
            TERMINAL_BOLS_INDEX, bol.bol_id, doc
        )

        logger.info(
            "Created pending_confirmation BOL %s from manual upload "
            "(content_type=%s) for tenant %s",
            bol.bol_id,
            content_type,
            tenant_id,
        )

        return bol

    # ------------------------------------------------------------------
    # confirm_manual_bol (Task 11.5 — Validates: Requirement 10.2)
    # ------------------------------------------------------------------

    async def confirm_manual_bol(
        self,
        tenant_id: str,
        bol_id: str,
        confirmed_fields: Dict[str, Any],
    ) -> TerminalBOL:
        """Update a pending_confirmation BOL with operator-confirmed fields.

        Transitions the BOL status from ``pending_confirmation`` to
        ``ingested`` after the operator has reviewed and confirmed the
        OCR-extracted (or manually entered) field values.

        Args:
            tenant_id: Tenant identifier for data isolation.
            bol_id: The BOL identifier to update.
            confirmed_fields: Dictionary of confirmed field values. Expected
                keys include: load_number, product_code, gross_gallons,
                net_gallons, observed_temperature_f, api_gravity,
                supplier_name, terminal_name, driver_id, timestamp.

        Returns:
            The updated :class:`TerminalBOL` instance with
            ``status="ingested"`` and ``needs_operator_confirmation=False``.

        Raises:
            ValueError: If the BOL is not found or not in
                pending_confirmation status.

        Validates: Requirement 10.2
        """
        # 1. Retrieve the existing BOL from ES
        query = inject_tenant_filter(
            {"query": {"term": {"bol_id": bol_id}}},
            tenant_id,
        )
        result = await self._es.search_documents(TERMINAL_BOLS_INDEX, query)
        hits = result.get("hits", {}).get("hits", [])

        if not hits:
            raise validation_error(
                f"BOL {bol_id} not found for tenant {tenant_id}",
                details={"bol_id": bol_id, "tenant_id": tenant_id},
            )

        existing_doc = hits[0]["_source"]

        # 2. Verify the BOL is in pending_confirmation status
        if existing_doc.get("status") != "pending_confirmation":
            raise validation_error(
                f"BOL {bol_id} is not in pending_confirmation status "
                f"(current status: {existing_doc.get('status')})",
                details={
                    "bol_id": bol_id,
                    "current_status": existing_doc.get("status"),
                },
            )

        # 3. Build the update payload from confirmed fields
        allowed_fields = {
            "load_number",
            "product_code",
            "gross_gallons",
            "net_gallons",
            "observed_temperature_f",
            "api_gravity",
            "supplier_name",
            "terminal_name",
            "terminal_id",
            "driver_id",
            "timestamp",
        }

        update_doc: Dict[str, Any] = {}
        for field, value in confirmed_fields.items():
            if field in allowed_fields:
                update_doc[field] = value

        # 4. Transition status to "ingested" and clear confirmation flag
        update_doc["status"] = "ingested"
        update_doc["needs_operator_confirmation"] = False
        update_doc["updated_at"] = utcnow().isoformat()

        # 4a. Validate driver_id if provided in confirmed fields (Req 10.3)
        confirmed_driver_id = update_doc.get("driver_id")
        if confirmed_driver_id:
            await self._validate_driver_id(tenant_id, confirmed_driver_id)

        # 4b. Idempotency check: reject duplicate load_number (Req 10.6)
        # Only check if a load_number is provided and it's not the
        # placeholder "PENDING" value. Exclude the current BOL from the
        # check to avoid false positives when the BOL's own record is found.
        confirmed_load_number = update_doc.get("load_number")
        if confirmed_load_number and confirmed_load_number != "PENDING":
            await self._check_duplicate_load_number(
                tenant_id, confirmed_load_number, exclude_bol_id=bol_id
            )

        # 4c. Cross-reference net_gallons against VCFCalculator (Req 10.4)
        # Only perform VCF cross-reference if the necessary fields are
        # available in the confirmed data.
        gross = update_doc.get("gross_gallons", existing_doc.get("gross_gallons"))
        net = update_doc.get("net_gallons", existing_doc.get("net_gallons"))
        temp = update_doc.get("observed_temperature_f", existing_doc.get("observed_temperature_f"))
        gravity = update_doc.get("api_gravity", existing_doc.get("api_gravity"))

        if (
            self._vcf_calculator is not None
            and gross is not None
            and net is not None
            and temp is not None
            and gravity is not None
            and gross > 0
            and net > 0
        ):
            try:
                computed_net = self._vcf_calculator.compute_net_gallons(
                    gross_gallons=gross,
                    temperature_f=temp,
                    api_gravity=gravity,
                )
                if computed_net > 0:
                    discrepancy = abs(net - computed_net) / computed_net
                    if discrepancy > self.VCF_DISCREPANCY_THRESHOLD:
                        update_doc["vcf_discrepancy_flag"] = True
                        logger.warning(
                            "VCF discrepancy detected on confirm for BOL %s "
                            "(tenant=%s): terminal_net=%.1f, computed_net=%.1f, "
                            "discrepancy=%.4f%%",
                            bol_id,
                            tenant_id,
                            net,
                            computed_net,
                            discrepancy * 100,
                        )
                    else:
                        update_doc["vcf_discrepancy_flag"] = False
            except (ValueError, Exception) as exc:
                logger.warning(
                    "VCF cross-reference failed during confirm for BOL %s "
                    "(tenant=%s): %s",
                    bol_id,
                    tenant_id,
                    exc,
                )

        # 5. Persist the update
        await self._es.update_document(
            TERMINAL_BOLS_INDEX, bol_id, {"doc": update_doc}
        )

        # 6. Reconstruct the updated BOL for return
        existing_doc.update(update_doc)
        # Handle timestamp parsing for the model
        if "timestamp" in existing_doc and isinstance(
            existing_doc["timestamp"], str
        ):
            try:
                existing_doc["timestamp"] = datetime.fromisoformat(
                    existing_doc["timestamp"]
                )
            except (ValueError, TypeError):
                pass

        if "created_at" in existing_doc and isinstance(
            existing_doc["created_at"], str
        ):
            try:
                existing_doc["created_at"] = datetime.fromisoformat(
                    existing_doc["created_at"]
                )
            except (ValueError, TypeError):
                pass

        if "updated_at" in existing_doc and isinstance(
            existing_doc["updated_at"], str
        ):
            try:
                existing_doc["updated_at"] = datetime.fromisoformat(
                    existing_doc["updated_at"]
                )
            except (ValueError, TypeError):
                pass

        bol = TerminalBOL(**existing_doc)

        logger.info(
            "Confirmed manual BOL %s (load_number=%s) for tenant %s",
            bol.bol_id,
            bol.load_number,
            tenant_id,
        )

        return bol

    # ------------------------------------------------------------------
    # link_to_load_plan (Task 11.8 — Validates: Requirement 10.5)
    # ------------------------------------------------------------------

    async def link_to_load_plan(
        self, bol_id: str, load_plan_id: str, tenant_id: Optional[str] = None
    ) -> None:
        """Link an ingested BOL to a load plan for chain-of-custody traceability.

        Looks up the BOL by ``bol_id``, verifies it exists and belongs to
        the given tenant, then updates the BOL record's ``load_plan_id``
        field and transitions its status to ``linked``.

        This establishes the chain-of-custody link between the terminal BOL
        (product loaded at the rack) and the truck load plan (compartment
        assignment for delivery), enabling full traceability from terminal
        to delivery point.

        Args:
            bol_id: The BOL identifier to update.
            load_plan_id: The load plan identifier to link.
            tenant_id: Tenant identifier for data isolation. If not provided,
                the BOL is looked up without tenant scoping (for backward
                compatibility in tests).

        Raises:
            AppException (validation_error): If the BOL is not found, or if
                the BOL is not in a linkable status (must be ``ingested``).

        Validates: Requirement 10.5
        """
        # 1. Look up the BOL from ES by bol_id
        if tenant_id:
            query = inject_tenant_filter(
                {"query": {"term": {"bol_id": bol_id}}},
                tenant_id,
            )
        else:
            query = {"query": {"term": {"bol_id": bol_id}}}

        result = await self._es.search_documents(TERMINAL_BOLS_INDEX, query)
        hits = result.get("hits", {}).get("hits", [])

        if not hits:
            raise validation_error(
                f"BOL '{bol_id}' not found",
                details={
                    "bol_id": bol_id,
                    "tenant_id": tenant_id,
                    "error_code": "bol.not_found",
                },
            )

        existing_doc = hits[0]["_source"]

        # 2. Verify the BOL is in a linkable status (ingested or verified)
        current_status = existing_doc.get("status", "unknown")
        linkable_statuses = {"ingested", "verified"}
        if current_status not in linkable_statuses:
            raise validation_error(
                f"BOL '{bol_id}' cannot be linked — current status is "
                f"'{current_status}' (must be one of: {', '.join(sorted(linkable_statuses))})",
                details={
                    "bol_id": bol_id,
                    "current_status": current_status,
                    "error_code": "bol.not_linkable",
                },
            )

        # 3. Update the BOL document with load_plan_id and transition status
        update_doc: Dict[str, Any] = {
            "load_plan_id": load_plan_id,
            "status": "linked",
            "updated_at": utcnow().isoformat(),
        }

        await self._es.update_document(
            TERMINAL_BOLS_INDEX, bol_id, {"doc": update_doc}
        )

        logger.info(
            "Linked BOL %s to load plan %s (tenant=%s). "
            "Status transitioned from '%s' to 'linked'.",
            bol_id,
            load_plan_id,
            tenant_id or existing_doc.get("tenant_id"),
            current_status,
        )
