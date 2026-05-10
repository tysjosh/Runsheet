"""Dyed Diesel Enforcer — IRS 637M certificate validation and dyed-fuel controls.

Implements the ``Dyed_Diesel_Enforcer`` described in design §6 of the
Fuel Compliance Backbone spec. This service enforces that dyed (off-road)
diesel is sold only to customers with valid IRS 637M exemption certificates,
prevents loading dyed fuel into clear-designated compartments, and confirms
tax exemption on invoices.

Integration points:
- Order intake pipeline → ``validate_order()``
- Compartment loading agent → ``validate_load_plan()``
- Invoice finalization → ``validate_invoice()``

All queries are tenant-scoped via ``inject_tenant_filter`` (Constraint C3).

Validates: Requirement 6.1
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from compliance.services.compliance_es_mappings import (
    DYED_DIESEL_AUDIT_LOG_INDEX,
    TAX_EXEMPTIONS_INDEX,
)
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

# Type-only import for SignalBus to avoid circular dependency
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Agents.overlay.signal_bus import SignalBus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Product codes that identify dyed (off-road) diesel
DYED_DIESEL_PRODUCT_CODES = frozenset({
    "OFF_ROAD_DIESEL",
    "DYED_DIESEL",
    "DYED_ULSD",
    "OFF_ROAD_ULSD",
})

# The IRS 637 letter suffix for dyed-diesel blender/buyer exemption
IRS_637M_EXEMPTION_TYPE = "637M"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ValidationResult(BaseModel):
    """Result of a dyed-diesel validation check.

    Attributes:
        valid: Whether the validation passed.
        error_code: Machine-readable error code when validation fails.
        message: Human-readable explanation when validation fails.
    """

    model_config = ConfigDict(extra="forbid")

    valid: bool
    error_code: Optional[str] = None
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DyedDieselEnforcer:
    """Service layer for dyed-diesel compliance enforcement.

    Validates that customers ordering dyed diesel have a valid IRS 637M
    exemption certificate, that compartments are dyed-compatible, and
    that invoices correctly exclude road-use excise tax.

    Args:
        es_service: Elasticsearch handle for querying exemption indices.
        signal_bus: Optional SignalBus for publishing sales team alerts
            when dyed-diesel orders are rejected (Req 6.2).

    Validates: Requirement 6.1, 6.2
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        signal_bus: Optional["SignalBus"] = None,
    ) -> None:
        self._es = es_service
        self._signal_bus = signal_bus

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def is_dyed_diesel(product_code: str) -> bool:
        """Return True if the product code represents dyed (off-road) diesel."""
        return product_code.upper() in DYED_DIESEL_PRODUCT_CODES

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        """Parse a date from an ISO string or return it if already a date."""
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except (ValueError, TypeError):
                return None
        return None

    # ------------------------------------------------------------------
    # Sales team notification (Req 6.2)
    # ------------------------------------------------------------------

    async def _notify_sales_team_rejection(
        self,
        tenant_id: str,
        customer_id: str,
        product_code: str,
    ) -> None:
        """Publish a RiskSignal to notify the sales team of a dyed-diesel rejection.

        When a customer orders dyed diesel without a valid IRS 637M
        certificate, this method emits a signal so the sales team can
        follow up (e.g., request the customer upload a renewed cert).

        If no signal_bus is configured, the notification is logged but
        not published (graceful degradation).

        Validates: Requirement 6.2
        """
        if self._signal_bus is None:
            logger.info(
                "DyedDieselEnforcer: no signal_bus configured — "
                "skipping sales team notification for customer %s (tenant %s)",
                customer_id,
                tenant_id,
            )
            return

        try:
            from Agents.overlay.data_contracts import RiskSignal, Severity

            signal = RiskSignal(
                source_agent="dyed_diesel_enforcer",
                entity_id=customer_id,
                entity_type="customer",
                severity=Severity.MEDIUM,
                confidence=1.0,
                ttl_seconds=86400,  # 24 hours
                tenant_id=tenant_id,
                context={
                    "event": "dyed_diesel_order_rejected",
                    "reason": "no_valid_637m_certificate",
                    "product_code": product_code,
                    "customer_id": customer_id,
                    "action_required": "sales_team_followup",
                    "message": (
                        f"Customer '{customer_id}' attempted to order "
                        f"dyed diesel ({product_code}) without a valid "
                        f"IRS 637M exemption certificate. Sales team "
                        f"should contact customer to obtain renewed certificate."
                    ),
                },
            )
            await self._signal_bus.publish(signal)
            logger.info(
                "DyedDieselEnforcer: sales team notified of rejection "
                "for customer %s (tenant %s)",
                customer_id,
                tenant_id,
            )
        except Exception as exc:
            # Non-critical — log and continue; the order rejection
            # itself is the primary enforcement action.
            logger.error(
                "DyedDieselEnforcer: failed to notify sales team for "
                "customer %s (tenant %s): %s",
                customer_id,
                tenant_id,
                exc,
            )

    # ------------------------------------------------------------------
    # log_dyed_sale (Task 9.6 — Req 6.7)
    # ------------------------------------------------------------------

    async def log_dyed_sale(
        self,
        tenant_id: str,
        customer_id: str,
        certificate_id: str,
        certificate_expiry: str,
        gallons: float,
        invoice_id: str,
        product_code: str,
    ) -> None:
        """Persist a dyed-diesel sale to the audit log for IRS audit readiness.

        Writes a record to the ``dyed_diesel_audit_log`` ES index containing
        all fields required by IRS audit: customer_id, certificate_id,
        certificate_expiry, gallons, and invoice_id. A timestamp is included
        for temporal ordering.

        This method is non-blocking: failures are logged but do not raise,
        ensuring that a transient ES issue does not block the sale itself.

        Args:
            tenant_id: Tenant scope for the document.
            customer_id: The customer making the dyed-diesel purchase.
            certificate_id: The IRS 637M certificate number on file.
            certificate_expiry: ISO date string of certificate expiry.
            gallons: Number of gallons sold.
            invoice_id: The invoice associated with this sale.
            product_code: The dyed-diesel product code.

        Validates: Requirement 6.7
        """
        import uuid

        now = utcnow()
        audit_id = f"dyed_audit_{uuid.uuid4().hex[:12]}"

        doc: Dict[str, Any] = {
            "audit_id": audit_id,
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "certificate_id": certificate_id,
            "certificate_expiry": certificate_expiry,
            "gallons": gallons,
            "invoice_id": invoice_id,
            "product_code": product_code,
            "timestamp": now.isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

        try:
            await self._es.index_document(
                DYED_DIESEL_AUDIT_LOG_INDEX, audit_id, doc
            )
            logger.info(
                "DyedDieselEnforcer: audit log persisted for invoice %s, "
                "customer %s, gallons %.1f (tenant %s)",
                invoice_id,
                customer_id,
                gallons,
                tenant_id,
            )
        except Exception as exc:
            # Non-blocking: log the failure but do not raise.
            # The sale should not be blocked by an audit log write failure.
            logger.error(
                "DyedDieselEnforcer: failed to persist audit log for "
                "invoice %s, customer %s (tenant %s): %s",
                invoice_id,
                customer_id,
                tenant_id,
                exc,
            )

    # ------------------------------------------------------------------
    # check_expiring_certificates (Task 9.5 — Req 6.6)
    # ------------------------------------------------------------------

    async def check_expiring_certificates(
        self,
        tenant_id: str,
        days_ahead: int = 30,
    ) -> List[Dict[str, Any]]:
        """Check for 637M certificates expiring within the given window.

        Returns a list of certificates that will expire within ``days_ahead``
        days. This is intended to be called by a daily cron job so that
        customers can be proactively notified to renew their certificates
        before they expire and their dyed-diesel orders are blocked.

        Once a certificate expires, ``validate_order()`` will automatically
        block future dyed-diesel orders for that customer (Req 6.6) because
        the ES query filters by ``expiry_date >= today``. This method
        provides advance warning so the block can be avoided.

        Args:
            tenant_id: Tenant scope for the query.
            days_ahead: Number of days to look ahead for expiring certs.

        Returns:
            List of certificate documents expiring within the window.

        Validates: Requirement 6.6
        """
        from datetime import timedelta

        today = date.today()
        future_date = today + timedelta(days=days_ahead)

        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"exemption_type": IRS_637M_EXEMPTION_TYPE}},
                        {"term": {"status": "valid"}},
                    ],
                    "filter": [
                        # Certificate expires between today and days_ahead
                        {
                            "range": {
                                "expiry_date": {
                                    "gte": today.isoformat(),
                                    "lte": future_date.isoformat(),
                                }
                            }
                        },
                    ],
                }
            },
            "size": 100,
            "sort": [{"expiry_date": "asc"}],
        }

        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            TAX_EXEMPTIONS_INDEX, query, size=100
        )

        hits = response["hits"]["hits"]
        expiring_certs = [hit["_source"] for hit in hits]

        if expiring_certs:
            logger.warning(
                "DyedDieselEnforcer: %d certificates expiring within %d days "
                "(tenant %s). Affected customers will be blocked from "
                "ordering dyed diesel upon expiry.",
                len(expiring_certs),
                days_ahead,
                tenant_id,
            )

            # Notify sales team for each expiring certificate
            for cert in expiring_certs:
                customer_id = cert.get("customer_id", "unknown")
                expiry = cert.get("expiry_date", "unknown")
                logger.info(
                    "DyedDieselEnforcer: certificate for customer %s "
                    "expires on %s (tenant %s)",
                    customer_id,
                    expiry,
                    tenant_id,
                )

        return expiring_certs

    # ------------------------------------------------------------------
    # validate_order (Task 9.1 / 9.2 — Req 6.1, 6.2)
    # ------------------------------------------------------------------

    async def validate_order(
        self,
        tenant_id: str,
        customer_id: str,
        product_code: str,
    ) -> ValidationResult:
        """Validate that a customer may order dyed diesel.

        If the product is not a dyed-diesel code, the order is
        automatically valid (no exemption check needed).

        For dyed-diesel orders, queries the ``tax_exemptions`` index for
        a valid, non-expired IRS 637M certificate belonging to the
        customer. If no valid certificate is found, the order is
        rejected with error code ``dyed.no_valid_exemption``.

        Args:
            tenant_id: Tenant scope for the query.
            customer_id: The customer placing the order.
            product_code: The product being ordered.

        Returns:
            ValidationResult indicating pass/fail.

        Validates: Requirements 6.1, 6.2
        """
        # Non-dyed products pass without checks
        if not self.is_dyed_diesel(product_code):
            return ValidationResult(valid=True)

        # Query for a valid, non-expired 637M certificate for this customer
        today_iso = date.today().isoformat()

        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"customer_id": customer_id}},
                        {"term": {"exemption_type": IRS_637M_EXEMPTION_TYPE}},
                        {"term": {"status": "valid"}},
                    ],
                    "filter": [
                        # Certificate must not be expired (expiry_date >= today)
                        {"range": {"expiry_date": {"gte": today_iso}}},
                    ],
                }
            },
            "size": 1,
        }

        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            TAX_EXEMPTIONS_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]

        if hits:
            # Valid certificate found
            logger.info(
                "Dyed diesel order validated for customer %s (tenant %s): "
                "certificate %s",
                customer_id,
                tenant_id,
                hits[0]["_source"].get("certificate_number", "unknown"),
            )
            return ValidationResult(valid=True)

        # No valid certificate — reject
        logger.warning(
            "Dyed diesel order rejected for customer %s (tenant %s): "
            "no valid IRS 637M exemption certificate",
            customer_id,
            tenant_id,
        )

        # Notify sales team of the rejection (Req 6.2)
        await self._notify_sales_team_rejection(
            tenant_id=tenant_id,
            customer_id=customer_id,
            product_code=product_code,
        )

        return ValidationResult(
            valid=False,
            error_code="dyed.no_valid_exemption",
            message=(
                f"Customer '{customer_id}' does not have a valid, non-expired "
                f"IRS 637M exemption certificate required for dyed diesel purchases"
            ),
        )

    # ------------------------------------------------------------------
    # validate_load_plan (Task 9.3 — Req 6.3, 6.4) — skeleton
    # ------------------------------------------------------------------

    async def validate_load_plan(
        self,
        tenant_id: str,
        compartment_id: str,
        product_code: str,
    ) -> ValidationResult:
        """Validate that a compartment is dyed-compatible for dyed diesel.

        Checks the compartment configuration to ensure dyed diesel is not
        loaded into a clear-only designated compartment.

        If the product is not dyed diesel, the load plan is automatically
        valid (no compatibility check needed).

        For dyed-diesel products, queries the ``truck_compartments`` index
        for the compartment document and inspects the ``dyed_compatible``
        flag. If the flag is explicitly ``False``, the compartment is
        clear-only and the load plan is rejected with error code
        ``dyed.compartment_incompatible``. If the compartment is not found,
        the load plan is rejected with ``dyed.compartment_not_found``.

        Note: Legacy compartment documents that lack the ``dyed_compatible``
        field are treated as dyed-compatible (default True).

        Args:
            tenant_id: Tenant scope for the query.
            compartment_id: The compartment being loaded.
            product_code: The product being loaded.

        Returns:
            ValidationResult indicating pass/fail.

        Validates: Requirements 6.3, 6.4
        """
        from Agents.support.mvp_es_mappings import TRUCK_COMPARTMENTS_INDEX

        # Non-dyed products pass without checks
        if not self.is_dyed_diesel(product_code):
            return ValidationResult(valid=True)

        # Query the truck_compartments index for this compartment
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"compartment_id": compartment_id}},
                    ],
                }
            },
            "size": 1,
        }

        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            TRUCK_COMPARTMENTS_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]

        if not hits:
            # Compartment not found in the index
            logger.warning(
                "Dyed diesel load plan rejected: compartment %s not found "
                "(tenant %s)",
                compartment_id,
                tenant_id,
            )
            return ValidationResult(
                valid=False,
                error_code="dyed.compartment_not_found",
                message=(
                    f"Compartment '{compartment_id}' not found in "
                    f"truck compartment configuration"
                ),
            )

        compartment_doc = hits[0]["_source"]

        # Check the dyed_compatible flag. Legacy documents without the field
        # are treated as dyed-compatible (default True).
        dyed_compatible = compartment_doc.get("dyed_compatible", True)

        if dyed_compatible:
            logger.info(
                "Dyed diesel load plan validated for compartment %s "
                "(tenant %s)",
                compartment_id,
                tenant_id,
            )
            return ValidationResult(valid=True)

        # Compartment is clear-only — reject
        logger.warning(
            "Dyed diesel load plan rejected: compartment %s is designated "
            "clear-only (tenant %s)",
            compartment_id,
            tenant_id,
        )
        return ValidationResult(
            valid=False,
            error_code="dyed.compartment_incompatible",
            message=(
                f"Compartment '{compartment_id}' is designated as clear-only "
                f"and cannot be loaded with dyed diesel"
            ),
        )

    # ------------------------------------------------------------------
    # validate_invoice (Task 9.4 — Req 6.5) — skeleton
    # ------------------------------------------------------------------

    async def validate_invoice(
        self,
        tenant_id: str,
        invoice_id: str,
    ) -> ValidationResult:
        """Validate that a dyed-diesel invoice excludes road-use excise tax.

        Cross-references the invoice with the Tax_Engine exemption logic
        to confirm that federal and state road-use excise taxes were not
        applied to dyed-diesel line items.

        Steps:
        1. Query the ``invoices_current`` index for the invoice by ID.
        2. Check if any line item has a dyed-diesel product code.
        3. If no dyed-diesel line items, return valid (no check needed).
        4. If dyed-diesel line items exist, inspect the tax_breakdown
           line_items for "federal_excise" or "state_excise" components.
        5. If road-use excise taxes are found → invalid.
        6. If no road-use excise taxes → valid.

        Args:
            tenant_id: Tenant scope for the query.
            invoice_id: The invoice to validate.

        Returns:
            ValidationResult indicating pass/fail.

        Validates: Requirement 6.5
        """
        from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX

        # Step 1: Query the invoices index for this invoice
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"invoice_id": invoice_id}},
                    ],
                }
            },
            "size": 1,
        }

        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]

        if not hits:
            # Invoice not found
            logger.warning(
                "Dyed diesel invoice validation failed: invoice %s not found "
                "(tenant %s)",
                invoice_id,
                tenant_id,
            )
            return ValidationResult(
                valid=False,
                error_code="dyed.invoice_not_found",
                message=(
                    f"Invoice '{invoice_id}' not found in invoices index"
                ),
            )

        invoice_doc = hits[0]["_source"]

        # Step 2: Check if any line item has a dyed-diesel product code
        line_items: List[Dict[str, Any]] = invoice_doc.get("line_items", [])
        has_dyed_diesel = any(
            self.is_dyed_diesel(item.get("product_code", ""))
            for item in line_items
        )

        # Step 3: If no dyed-diesel line items, no check needed
        if not has_dyed_diesel:
            return ValidationResult(valid=True)

        # Step 4: Check tax_breakdown line_items for road-use excise taxes
        # Road-use excise tax component names that should NOT be present
        # on dyed-diesel invoices.
        ROAD_USE_EXCISE_COMPONENTS = frozenset({
            "federal_excise",
            "state_excise",
        })

        tax_breakdown = invoice_doc.get("tax_breakdown")
        if tax_breakdown is not None:
            tax_line_items: List[Dict[str, Any]] = tax_breakdown.get(
                "line_items", []
            )

            # Find any road-use excise tax line items
            offending_items = [
                tli
                for tli in tax_line_items
                if tli.get("tax_component_name", "").lower()
                in ROAD_USE_EXCISE_COMPONENTS
            ]

            if offending_items:
                # Step 5: Road-use excise taxes found — invalid
                offending_names = [
                    tli.get("tax_component_name", "unknown")
                    for tli in offending_items
                ]
                logger.warning(
                    "Dyed diesel invoice validation failed: invoice %s "
                    "(tenant %s) has road-use excise tax line items: %s",
                    invoice_id,
                    tenant_id,
                    offending_names,
                )
                return ValidationResult(
                    valid=False,
                    error_code="dyed.tax_exemption_not_applied",
                    message=(
                        f"Invoice '{invoice_id}' contains road-use excise "
                        f"tax line items ({', '.join(offending_names)}) that "
                        f"should be excluded for dyed diesel deliveries"
                    ),
                )

        # Also check the top-level breakdown fields as a secondary guard:
        # If federal_cents or state_cents > 0 and there's no non-dyed
        # product to account for them, that's a problem. However, if
        # the invoice has mixed products (dyed + clear), the excise
        # taxes may legitimately apply to the clear products. We only
        # flag when ALL line items are dyed diesel.
        if tax_breakdown is not None:
            all_dyed = all(
                self.is_dyed_diesel(item.get("product_code", ""))
                for item in line_items
                if item.get("product_code")
            )
            if all_dyed:
                federal_cents = tax_breakdown.get("federal_cents", 0)
                state_cents = tax_breakdown.get("state_cents", 0)
                if federal_cents > 0 or state_cents > 0:
                    logger.warning(
                        "Dyed diesel invoice validation failed: invoice %s "
                        "(tenant %s) has federal_cents=%d state_cents=%d "
                        "but all line items are dyed diesel",
                        invoice_id,
                        tenant_id,
                        federal_cents,
                        state_cents,
                    )
                    return ValidationResult(
                        valid=False,
                        error_code="dyed.tax_exemption_not_applied",
                        message=(
                            f"Invoice '{invoice_id}' has road-use excise tax "
                            f"(federal={federal_cents}¢, state={state_cents}¢) "
                            f"applied to an all-dyed-diesel invoice"
                        ),
                    )

        # Step 6: No road-use excise taxes found — valid
        logger.info(
            "Dyed diesel invoice validation passed: invoice %s (tenant %s) "
            "correctly excludes road-use excise tax",
            invoice_id,
            tenant_id,
        )
        return ValidationResult(valid=True)
