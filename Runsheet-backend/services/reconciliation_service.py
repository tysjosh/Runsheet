"""
Reconciliation Service — Ordered / Loaded / Delivered / Invoiced gallon
variance tracking for Capability 4 (POD + Reconciliation) of the fuel-ops
hardening spec.

On POD finalization, :class:`ReconciliationService` folds three
record sources into a single :class:`ReconciliationRecord`:

    * ``ordered_gallons``   — from the Order (customer request)
    * ``loaded_gallons``    — from the Loading_Plan (what the truck left the
                              depot with)
    * ``delivered_gallons`` — from the POD (what the meter ticket / driver
                              reported at the stop)

The service computes the two always-available percentage variances:

    variance_load_vs_order_pct      = abs(loaded    - ordered) / ordered * 100
    variance_delivered_vs_loaded_pct = abs(delivered - loaded)  / loaded  * 100

and the optional third variance when an ``invoice`` is already available on
the Order side of the transaction (rare at POD-finalization time — this one
is typically filled in later by the QuickBooks connector):

    variance_invoiced_vs_delivered_pct = abs(invoiced - delivered) / delivered * 100

If any variance exceeds the tenant's configured threshold (Redis key
``variance_alert_pct:{tenant_id}``, platform default ``3.0``), the service
appends the canonical ``variance_exceeds_threshold`` alert flag to
``alert_flags`` so downstream dashboards / notification pipelines can fan out
without re-reading the record.

Persistence targets the ``mvp_reconciliation`` ES index (strict mapping
defined in :mod:`fuel.services.fuel_ops_es_mappings`). Each persisted
document is keyed by the generated ``reconciliation_id`` so the downstream
Capability 4 read endpoint (task 8.8) can look the record up by itself or by
any of the (order_id, plan_id, pod_id) coordinates.

The QuickBooks Online Connector (Phase 9) calls
:meth:`ReconciliationService.update_invoice_fields` when an invoice event
arrives so the reconciliation record carries ``invoiced_gallons`` and
``variance_invoiced_vs_delivered_pct`` within 60 seconds of the QBO event
(Requirement 4.4.5).

Tenant isolation is enforced by:

    * Deriving ``tenant_id`` from the POD input and rejecting records that
      don't carry one.
    * Writing ``tenant_id`` as a top-level keyword on the persisted document
      so the GET endpoint can filter the query by the caller's tenant.
    * Cross-tenant ``update_invoice_fields`` calls are rejected with
      :class:`PermissionError` so a misrouted QBO webhook cannot mutate
      another tenant's reconciliation record.

Validates: Requirements 4.4.1, 4.4.2, 4.4.3, 4.4.5.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from config.settings import get_settings
from fuel.services.fuel_ops_es_mappings import MVP_RECONCILIATION_INDEX

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Platform-wide default threshold (percentage points) above which a variance
#: triggers the ``variance_exceeds_threshold`` alert flag. Overridable per
#: tenant via Redis key ``variance_alert_pct:{tenant_id}``
#: (Requirement 4.4.3).
DEFAULT_VARIANCE_ALERT_PCT: float = 3.0

#: Canonical alert flag appended to ``alert_flags`` when *any* computed
#: variance exceeds the tenant threshold.
VARIANCE_ALERT_FLAG: str = "variance_exceeds_threshold"

#: Redis key pattern for the tenant-configurable variance threshold.
_VARIANCE_THRESHOLD_KEY_PATTERN: str = "variance_alert_pct:{tenant_id}"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ReconciliationRecord(BaseModel):
    """Strict Pydantic model matching the ``mvp_reconciliation`` ES mapping.

    ``invoice_id``, ``invoiced_gallons``, and
    ``variance_invoiced_vs_delivered_pct`` are nullable because the accounting
    integration (QuickBooks Online) typically reports an invoice seconds-to-
    minutes *after* the POD is finalized. The :class:`ReconciliationService`
    writes a partial record on POD finalization and the QBO connector later
    updates it (task 8.8 / Requirement 4.4.5).
    """

    model_config = ConfigDict(extra="forbid")

    reconciliation_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    order_id: str = Field(..., min_length=1)
    plan_id: str = Field(..., min_length=1)
    pod_id: str = Field(..., min_length=1)
    invoice_id: Optional[str] = None
    ordered_gallons: float = Field(..., ge=0.0)
    loaded_gallons: float = Field(..., ge=0.0)
    delivered_gallons: float = Field(..., ge=0.0)
    invoiced_gallons: Optional[float] = Field(default=None, ge=0.0)
    variance_load_vs_order_pct: float = Field(..., ge=0.0)
    variance_delivered_vs_loaded_pct: float = Field(..., ge=0.0)
    variance_invoiced_vs_delivered_pct: Optional[float] = Field(default=None, ge=0.0)
    canonical_invoice_id: Optional[str] = None
    qbo_invoice_id: Optional[str] = None
    external_refs: Optional[Dict[str, Any]] = None
    alert_flags: List[str] = Field(default_factory=list)
    generated_at: datetime


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ReconciliationService:
    """Compute and persist a :class:`ReconciliationRecord` from a POD context.

    The service is intentionally dependency-light: it takes an ES service
    (any object with an async ``index_document(index, id, doc)`` coroutine)
    and an optional Redis client for per-tenant threshold overrides. Neither
    is reached into at construction time, so the service can be instantiated
    in any bootstrap order.

    Args:
        es_service: ``ElasticsearchService``-compatible instance used to
            persist records to ``mvp_reconciliation``.
        redis_client: Optional async Redis client for reading
            ``variance_alert_pct:{tenant_id}``. When ``None`` the platform
            default is used for every tenant.
        default_variance_alert_pct: Platform-wide default threshold applied
            when no tenant override is configured.
    """

    INDEX: str = MVP_RECONCILIATION_INDEX

    def __init__(
        self,
        es_service: Any,
        redis_client: Optional[Any] = None,
        default_variance_alert_pct: float = DEFAULT_VARIANCE_ALERT_PCT,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service is required")
        if default_variance_alert_pct < 0:
            raise ValueError("default_variance_alert_pct must be >= 0")

        self._es = es_service
        self._redis = redis_client
        self._default_threshold = float(default_variance_alert_pct)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compute(
        self,
        pod: Mapping[str, Any],
        order: Mapping[str, Any],
        loading_plan: Mapping[str, Any],
    ) -> ReconciliationRecord:
        """Compute variances, persist the record, return it.

        Args:
            pod: The finalized POD record (mapping-like). Must carry
                ``pod_id``, ``tenant_id``, ``order_id``, and
                ``delivered_gallons``.
            order: The originating order (mapping-like). Must carry
                ``ordered_gallons``. ``order_id`` is cross-checked against
                ``pod.order_id`` when both are present.
            loading_plan: The loading plan the truck left the depot with
                (mapping-like). Must carry ``plan_id`` and
                ``loaded_gallons``.

        Returns:
            The persisted :class:`ReconciliationRecord`.

        Raises:
            ValueError: Required fields are missing / non-numeric / negative,
                or the input tenant_id values mismatch.
        """
        if pod is None:
            raise ValueError("pod is required")
        if order is None:
            raise ValueError("order is required")
        if loading_plan is None:
            raise ValueError("loading_plan is required")

        tenant_id = _require_str(pod, "tenant_id", "pod")
        # If the order / plan carry tenant_id, require them to match. This
        # guards against an upstream bug cross-wiring records across tenants.
        for other, label in ((order, "order"), (loading_plan, "loading_plan")):
            if "tenant_id" in other and other.get("tenant_id") not in (None, "", tenant_id):
                raise ValueError(
                    f"{label}.tenant_id={other.get('tenant_id')!r} "
                    f"does not match pod.tenant_id={tenant_id!r}"
                )

        pod_id = _require_str(pod, "pod_id", "pod")
        order_id = _require_str(order, "order_id", "order", fallback=pod.get("order_id"))
        plan_id = _require_str(
            loading_plan, "plan_id", "loading_plan", fallback=pod.get("plan_id")
        )

        ordered_gallons = _require_non_negative_float(order, "ordered_gallons", "order")
        loaded_gallons = _require_non_negative_float(
            loading_plan, "loaded_gallons", "loading_plan"
        )
        delivered_gallons = _require_non_negative_float(
            pod, "delivered_gallons", "pod"
        )

        # Optional invoice inputs may already be attached to the order (rare
        # at POD-finalization time). When present, compute the third
        # variance so the record is complete in a single write.
        invoiced_gallons = _optional_non_negative_float(order, "invoiced_gallons")
        invoice_id = order.get("invoice_id") or None
        if invoiced_gallons is None and invoice_id:
            # invoice_id without a gallons value is not actionable for the
            # variance formula — downgrade to no-invoice state.
            invoice_id = None

        variance_load_vs_order_pct = _percent_variance(
            numerator=loaded_gallons, denominator=ordered_gallons
        )
        variance_delivered_vs_loaded_pct = _percent_variance(
            numerator=delivered_gallons, denominator=loaded_gallons
        )
        variance_invoiced_vs_delivered_pct: Optional[float] = None
        if invoiced_gallons is not None:
            variance_invoiced_vs_delivered_pct = _percent_variance(
                numerator=invoiced_gallons, denominator=delivered_gallons
            )

        threshold = await self._resolve_threshold(tenant_id)
        alert_flags = _derive_alert_flags(
            threshold=threshold,
            variances=(
                variance_load_vs_order_pct,
                variance_delivered_vs_loaded_pct,
                variance_invoiced_vs_delivered_pct,
            ),
        )

        record = ReconciliationRecord(
            reconciliation_id=f"rec-{tenant_id}-{uuid.uuid4()}",
            tenant_id=tenant_id,
            order_id=order_id,
            plan_id=plan_id,
            pod_id=pod_id,
            invoice_id=invoice_id if isinstance(invoice_id, str) else None,
            ordered_gallons=ordered_gallons,
            loaded_gallons=loaded_gallons,
            delivered_gallons=delivered_gallons,
            invoiced_gallons=invoiced_gallons,
            variance_load_vs_order_pct=variance_load_vs_order_pct,
            variance_delivered_vs_loaded_pct=variance_delivered_vs_loaded_pct,
            variance_invoiced_vs_delivered_pct=variance_invoiced_vs_delivered_pct,
            alert_flags=alert_flags,
            generated_at=_utcnow(),
        )

        await self._persist(record)
        logger.info(
            "Reconciliation persisted id=%s tenant=%s order=%s plan=%s pod=%s "
            "variances=(load/order=%.4f, delivered/loaded=%.4f, invoiced/delivered=%s) "
            "threshold=%.3f flags=%s",
            record.reconciliation_id,
            record.tenant_id,
            record.order_id,
            record.plan_id,
            record.pod_id,
            record.variance_load_vs_order_pct,
            record.variance_delivered_vs_loaded_pct,
            (
                f"{record.variance_invoiced_vs_delivered_pct:.4f}"
                if record.variance_invoiced_vs_delivered_pct is not None
                else "n/a"
            ),
            threshold,
            record.alert_flags,
        )
        return record

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _resolve_threshold(self, tenant_id: str) -> float:
        """Return the tenant's configured threshold or the platform default.

        Reads Redis key ``variance_alert_pct:{tenant_id}``. Malformed or
        out-of-range values log a warning and fall back to the default so a
        bad override never blocks reconciliation of a finalized POD.
        """
        if self._redis is None:
            return self._default_threshold

        key = _VARIANCE_THRESHOLD_KEY_PATTERN.format(tenant_id=tenant_id)
        try:
            raw = await self._redis.get(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Redis lookup for variance_alert_pct failed tenant=%s: %s",
                tenant_id,
                exc,
            )
            return self._default_threshold

        if raw is None:
            return self._default_threshold

        try:
            text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            value = float(text)
        except (TypeError, ValueError, UnicodeDecodeError):
            logger.warning(
                "Malformed variance_alert_pct for tenant=%s value=%r",
                tenant_id,
                raw,
            )
            return self._default_threshold

        if value < 0:
            logger.warning(
                "Negative variance_alert_pct for tenant=%s value=%s — using default",
                tenant_id,
                value,
            )
            return self._default_threshold

        return value

    async def _persist(self, record: ReconciliationRecord) -> None:
        """Index ``record`` into ``mvp_reconciliation``."""
        document = record.model_dump(mode="json")
        # Mirror the ES mapping's ``created_at`` / ``updated_at`` timestamps
        # on the canonical ``generated_at`` so downstream search by date
        # range works uniformly across Capability-4 artifacts.
        document["created_at"] = document["generated_at"]
        document["updated_at"] = document["generated_at"]
        await self._es.index_document(self.INDEX, record.reconciliation_id, document)

    # ------------------------------------------------------------------
    # QuickBooks Online (Phase 9) integration seam (Req 4.4.5, Task 8.8)
    # ------------------------------------------------------------------

    async def update_invoice_fields(
        self,
        *,
        tenant_id: str,
        reconciliation_id: str,
        invoice_id: str,
        invoiced_gallons: float,
        payment_status: Optional[str] = None,
    ) -> ReconciliationRecord:
        """Attach invoice data from QuickBooks Online to an existing record.

        This is the seam the QuickBooks Online Connector (Phase 9,
        :mod:`integrations.quickbooks_online`) calls when an invoice
        event (created / updated / paid) arrives from QBO. The connector
        is responsible for meeting the "within 60 seconds of invoice
        events" SLA mandated by Requirement 4.4.5 — this method only
        carries out the atomic update once the connector has decided
        which :class:`ReconciliationRecord` an invoice maps to.

        Integration contract (binding on the QBO Connector):

            * ``tenant_id`` MUST match the tenant that owns the record.
              Cross-tenant updates are rejected with :class:`PermissionError`
              so a misrouted webhook can never mutate another tenant's
              reconciliation.
            * ``reconciliation_id`` MUST be a known record id already
              persisted by :meth:`compute`. Unknown ids raise
              :class:`LookupError` — the connector should then fall
              back to its standard "create partial record" path rather
              than silently swallowing the update.
            * ``invoiced_gallons`` MUST be a non-negative float (QBO
              invoice line ``Qty`` * unit conversion). Negative / NaN
              inputs raise :class:`ValueError`.
            * ``invoice_id`` MUST be the QBO ``Invoice.Id`` value so the
              reconciliation record can be traced back to the source
              document.
            * ``payment_status`` MAY be supplied when the QBO event is
              a payment update (``paid`` / ``partial`` / ``overdue``).
              When omitted the payment_status is left unchanged so
              separate invoice-created and payment-settled events do
              not clobber each other.

        After the update is applied, the service recomputes
        ``variance_invoiced_vs_delivered_pct`` against the stored
        ``delivered_gallons`` and re-evaluates the
        ``variance_exceeds_threshold`` alert flag against the tenant's
        configured threshold. Both are persisted atomically via
        :meth:`ElasticsearchService.update_document`.

        Returns:
            The updated :class:`ReconciliationRecord` so the connector
            can surface the new variance / alert_flag to the caller
            (e.g. an audit webhook handler).

        Raises:
            ValueError: ``invoiced_gallons`` is negative / non-numeric
                or ``reconciliation_id`` / ``invoice_id`` / ``tenant_id``
                is empty.
            LookupError: No record exists for ``reconciliation_id``.
            PermissionError: The record exists but belongs to a
                different tenant.
        """
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("tenant_id is required and must be a non-empty string")
        if not isinstance(reconciliation_id, str) or not reconciliation_id:
            raise ValueError(
                "reconciliation_id is required and must be a non-empty string"
            )
        if not isinstance(invoice_id, str) or not invoice_id:
            raise ValueError("invoice_id is required and must be a non-empty string")
        try:
            numeric_gallons = float(invoiced_gallons)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invoiced_gallons must be numeric, got {invoiced_gallons!r}"
            ) from exc
        if numeric_gallons < 0 or numeric_gallons != numeric_gallons:  # NaN check
            raise ValueError(
                f"invoiced_gallons must be a finite non-negative float, got {numeric_gallons}"
            )

        # Fetch the record so we can recompute the variance against the
        # stored ``delivered_gallons`` and honour tenant isolation.
        try:
            existing = await self._es.get_document(self.INDEX, reconciliation_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "Reconciliation get_document failed id=%s tenant=%s: %s",
                reconciliation_id,
                tenant_id,
                exc,
            )
            raise

        if not existing:
            raise LookupError(
                f"Reconciliation record {reconciliation_id!r} not found"
            )

        # :meth:`ElasticsearchService.get_document` returns the raw ES
        # hit; unwrap ``_source`` when present.
        source = existing.get("_source") if isinstance(existing, dict) else None
        if source is None and isinstance(existing, dict):
            # Some test doubles return the source document directly.
            source = existing
        if not isinstance(source, dict):
            raise LookupError(
                f"Reconciliation record {reconciliation_id!r} has no source document"
            )

        if source.get("tenant_id") != tenant_id:
            raise PermissionError(
                f"Reconciliation record {reconciliation_id!r} belongs to a "
                f"different tenant"
            )

        delivered_gallons = _require_non_negative_float(
            source, "delivered_gallons", "reconciliation"
        )

        variance = _percent_variance(
            numerator=numeric_gallons, denominator=delivered_gallons
        )
        threshold = await self._resolve_threshold(tenant_id)

        # Recompute alert flags across all three variances (keeping any
        # previously-flagged variance surfaced) so the QBO update can
        # both raise and clear the flag based on the current data.
        variance_load = _optional_non_negative_float(
            source, "variance_load_vs_order_pct"
        )
        variance_delivered = _optional_non_negative_float(
            source, "variance_delivered_vs_loaded_pct"
        )
        alert_flags = _derive_alert_flags(
            threshold=threshold,
            variances=(variance_load, variance_delivered, variance),
        )

        updated_at = _utcnow().isoformat()
        patch: Dict[str, Any] = {
            "invoice_id": invoice_id,
            "invoiced_gallons": numeric_gallons,
            "variance_invoiced_vs_delivered_pct": variance,
            "alert_flags": alert_flags,
            "updated_at": updated_at,
        }

        # ── Step 9.5a: Dual-write path ────────────────────────────────
        #
        # When commerce_reconciliation_dual_write is enabled, write BOTH:
        #   - canonical_invoice_id: the commerce Invoice.invoice_id
        #     (shape inv_<uuid4>) for forward-looking reads
        #   - qbo_invoice_id: the existing free-form QBO Invoice.Id
        #     for backward compatibility
        #
        # The legacy `invoice_id` field continues to be written for
        # existing consumers that haven't migrated to get_invoice_id().
        #
        # This dual-write runs for a one-week soak period. After the soak,
        # enable commerce_reconciliation_prefer_canonical (step 9.5b).
        settings = get_settings()
        if (
            settings.commerce_backbone_enabled
            and settings.commerce_reconciliation_dual_write
        ):
            # The canonical_invoice_id is the same value passed in as
            # invoice_id when the caller is the commerce backbone
            # (CommerceExternalSync). For legacy QBO connector calls,
            # this is the QBO Invoice.Id — both are stored so the
            # read-side can pick the right one based on the prefer flag.
            patch["canonical_invoice_id"] = invoice_id
            patch["qbo_invoice_id"] = invoice_id
            # When the commerce backbone is the caller, it passes the
            # canonical inv_<uuid4> as invoice_id. Store it in both
            # fields. The QBO connector (legacy path) passes the QBO
            # Invoice.Id. The external_refs.qbo field preserves the
            # QBO cross-reference regardless of which caller wrote it.
            if not patch.get("external_refs"):
                patch["external_refs"] = {}
            patch["external_refs"] = {"qbo": invoice_id}

        # ── Step 9.5c TODO ─────────────────────────────────────────────
        # After the second one-week soak (step 9.5b confirmed stable):
        #   1. Remove the dual-write block above
        #   2. Stop writing `qbo_invoice_id` — only write
        #      `canonical_invoice_id`
        #   3. Keep `external_refs.qbo` as a permanent cross-reference
        #   4. Remove the `commerce_reconciliation_dual_write` flag
        #   5. The `invoice_id` field becomes an alias for
        #      `canonical_invoice_id`
        # This step is independently committable and reversible by
        # re-enabling the dual_write flag.
        if payment_status is not None:
            if not isinstance(payment_status, str) or not payment_status.strip():
                raise ValueError(
                    "payment_status must be a non-empty string when supplied"
                )
            patch["payment_status"] = payment_status.strip()

        await self._es.update_document(self.INDEX, reconciliation_id, patch)

        # Re-materialize a :class:`ReconciliationRecord` from the merged
        # state so the connector can echo the post-update record back to
        # its caller without an additional round-trip.
        merged: Dict[str, Any] = dict(source)
        merged.update(patch)
        # ``generated_at`` is the original record's immutable timestamp
        # — keep it as-is for the model. ``created_at`` / ``updated_at``
        # on the persisted document are surfaced separately and are not
        # part of the Pydantic schema.
        merged.pop("created_at", None)
        merged.pop("updated_at", None)
        # ``payment_status`` is persisted but not part of the model —
        # drop it so ``extra="forbid"`` does not trip.
        merged.pop("payment_status", None)
        # Dual-write fields (step 9.5a) are persistence-only — they live
        # in ES for the read-side helper (get_invoice_id) but are not part
        # of the ReconciliationRecord Pydantic schema.
        merged.pop("canonical_invoice_id", None)
        merged.pop("qbo_invoice_id", None)
        merged.pop("external_refs", None)
        try:
            refreshed = ReconciliationRecord(**merged)
        except Exception as exc:
            logger.error(
                "Reconciliation model rehydrate failed after QBO update "
                "id=%s tenant=%s: %s",
                reconciliation_id,
                tenant_id,
                exc,
            )
            raise

        logger.info(
            "Reconciliation QBO invoice update id=%s tenant=%s invoice=%s "
            "invoiced_gallons=%.3f variance=%.4f threshold=%.3f flags=%s "
            "payment_status=%s",
            reconciliation_id,
            tenant_id,
            invoice_id,
            numeric_gallons,
            variance,
            threshold,
            alert_flags,
            patch.get("payment_status"),
        )
        return refreshed

    # ------------------------------------------------------------------
    # Step 9.5b: Read-side flip — get_invoice_id helper
    # ------------------------------------------------------------------

    def get_invoice_id(self, record: Mapping[str, Any]) -> Optional[str]:
        """Return the appropriate invoice_id from a reconciliation record.

        Step 9.5b of the reconciliation migration. This helper abstracts
        the read-side so callers don't need to know which field to read.

        Behavior controlled by ``commerce_reconciliation_prefer_canonical``:

        * When True (post-soak): returns ``canonical_invoice_id`` if
          present, falls back to ``qbo_invoice_id``, then to the legacy
          ``invoice_id`` field.
        * When False (legacy behavior): returns ``qbo_invoice_id`` if
          present, falls back to the legacy ``invoice_id`` field.

        The free-form QBO reference is always available via
        ``external_refs.qbo`` for cross-reference regardless of which
        field is returned as the primary invoice_id.

        This method is independently toggleable via the
        ``commerce_reconciliation_prefer_canonical`` flag and is
        reversible by setting the flag back to False.

        Args:
            record: A reconciliation record (dict-like) as returned by
                ES or as a model_dump() of ReconciliationRecord.

        Returns:
            The invoice_id string, or None if no invoice reference exists.
        """
        settings = get_settings()

        if (
            settings.commerce_backbone_enabled
            and settings.commerce_reconciliation_prefer_canonical
        ):
            # Prefer canonical — this is the post-soak read path
            canonical = record.get("canonical_invoice_id")
            if canonical:
                return canonical
            # Fall back to qbo_invoice_id (dual-write period records)
            qbo = record.get("qbo_invoice_id")
            if qbo:
                return qbo
            # Final fallback: legacy invoice_id field
            return record.get("invoice_id") or None
        else:
            # Legacy behavior — return qbo_invoice_id or invoice_id
            qbo = record.get("qbo_invoice_id")
            if qbo:
                return qbo
            return record.get("invoice_id") or None

    # ------------------------------------------------------------------
    # Stripe (Phase 9) integration seam (Req 5.5.4, Task 9.8)
    # ------------------------------------------------------------------

    async def update_payment_status(
        self,
        *,
        tenant_id: str,
        reconciliation_id: str,
        payment_status: str,
        payment_intent_id: Optional[str] = None,
        invoice_id: Optional[str] = None,
    ) -> ReconciliationRecord:
        """Set ``payment_status`` on an existing reconciliation record.

        This is the seam the Stripe Connector
        (:mod:`integrations.stripe_connector`) calls when a
        ``payment_intent.*`` webhook event arrives from Stripe.
        Unlike :meth:`update_invoice_fields` this method does NOT
        require ``invoiced_gallons`` — a Stripe payment event does
        not carry the line-item quantity, only the payment status.

        Integration contract (binding on the Stripe Connector):

            * ``tenant_id`` MUST match the tenant that owns the record.
              Cross-tenant updates are rejected with
              :class:`PermissionError` so a misrouted webhook can never
              mutate another tenant's reconciliation.
            * ``reconciliation_id`` MUST be a known record id already
              persisted by :meth:`compute`. Unknown ids raise
              :class:`LookupError`.
            * ``payment_status`` MUST be a non-empty string
              (``paid`` / ``failed`` / ``processing`` / ``refunded`` /
              …). Stripe's event taxonomy is mirrored here verbatim.
            * ``payment_intent_id`` is the Stripe ``PaymentIntent.id``
              (optional). When provided it is persisted alongside the
              status so the admin UI can deep-link back to the Stripe
              dashboard for audit.
            * ``invoice_id`` is an optional cross-reference for cases
              where a Stripe invoice (not PaymentIntent) was used.
              When supplied it is persisted on the record too.

        Returns:
            The updated :class:`ReconciliationRecord`.

        Raises:
            ValueError: ``tenant_id`` / ``reconciliation_id`` /
                ``payment_status`` is empty or non-string.
            LookupError: No record exists for ``reconciliation_id``.
            PermissionError: The record exists but belongs to a
                different tenant.
        """

        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError(
                "tenant_id is required and must be a non-empty string"
            )
        if not isinstance(reconciliation_id, str) or not reconciliation_id:
            raise ValueError(
                "reconciliation_id is required and must be a non-empty string"
            )
        if not isinstance(payment_status, str) or not payment_status.strip():
            raise ValueError(
                "payment_status is required and must be a non-empty string"
            )
        if payment_intent_id is not None and (
            not isinstance(payment_intent_id, str) or not payment_intent_id.strip()
        ):
            raise ValueError(
                "payment_intent_id must be a non-empty string when supplied"
            )
        if invoice_id is not None and (
            not isinstance(invoice_id, str) or not invoice_id.strip()
        ):
            raise ValueError(
                "invoice_id must be a non-empty string when supplied"
            )

        try:
            existing = await self._es.get_document(
                self.INDEX, reconciliation_id
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "Reconciliation get_document failed id=%s tenant=%s: %s",
                reconciliation_id,
                tenant_id,
                exc,
            )
            raise

        if not existing:
            raise LookupError(
                f"Reconciliation record {reconciliation_id!r} not found"
            )

        source = existing.get("_source") if isinstance(existing, dict) else None
        if source is None and isinstance(existing, dict):
            source = existing
        if not isinstance(source, dict):
            raise LookupError(
                f"Reconciliation record {reconciliation_id!r} has no source document"
            )

        if source.get("tenant_id") != tenant_id:
            raise PermissionError(
                f"Reconciliation record {reconciliation_id!r} belongs to a "
                f"different tenant"
            )

        patch: Dict[str, Any] = {
            "payment_status": payment_status.strip(),
            "updated_at": _utcnow().isoformat(),
        }
        if payment_intent_id is not None:
            patch["payment_intent_id"] = payment_intent_id.strip()
        if invoice_id is not None:
            patch["invoice_id"] = invoice_id.strip()

        await self._es.update_document(self.INDEX, reconciliation_id, patch)

        merged: Dict[str, Any] = dict(source)
        merged.update(patch)
        # Drop persistence-only surrogates so ReconciliationRecord's
        # ``extra="forbid"`` does not trip on rehydration.
        merged.pop("created_at", None)
        merged.pop("updated_at", None)
        merged.pop("payment_status", None)
        merged.pop("payment_intent_id", None)
        try:
            refreshed = ReconciliationRecord(**merged)
        except Exception as exc:
            logger.error(
                "Reconciliation model rehydrate failed after Stripe "
                "payment update id=%s tenant=%s: %s",
                reconciliation_id,
                tenant_id,
                exc,
            )
            raise

        logger.info(
            "Reconciliation Stripe payment update id=%s tenant=%s "
            "payment_status=%s payment_intent=%s",
            reconciliation_id,
            tenant_id,
            patch["payment_status"],
            patch.get("payment_intent_id"),
        )
        return refreshed


# ---------------------------------------------------------------------------
# Module helpers (kept outside the class so the unit tests can exercise them
# in isolation without spinning up the full service).
# ---------------------------------------------------------------------------


def _percent_variance(*, numerator: float, denominator: float) -> float:
    """Return ``abs(numerator - denominator) / denominator * 100``.

    When ``denominator`` is zero the variance is defined as ``0.0`` when
    ``numerator`` is also zero (no shipment, no loss) and ``100.0`` otherwise
    (a non-zero value against a zero baseline is a 100% deviation by
    convention). This keeps the caller free of ``ZeroDivisionError`` and
    produces an alertable (non-None) number in every well-formed input case.
    """
    if denominator == 0:
        return 0.0 if numerator == 0 else 100.0
    return abs(numerator - denominator) / denominator * 100.0


def _derive_alert_flags(
    *, threshold: float, variances: Iterable[Optional[float]]
) -> List[str]:
    """Return the alert_flag list derived from the computed variances.

    Any numeric variance strictly greater than ``threshold`` appends the
    canonical :data:`VARIANCE_ALERT_FLAG`. ``None`` entries (unavailable
    variances, e.g. invoiced_vs_delivered before the QBO connector runs)
    are ignored.
    """
    for v in variances:
        if v is None:
            continue
        if v > threshold:
            return [VARIANCE_ALERT_FLAG]
    return []


def _require_str(
    mapping: Mapping[str, Any],
    key: str,
    label: str,
    *,
    fallback: Any = None,
) -> str:
    """Return a non-empty string from ``mapping[key]`` or ``fallback``.

    Raises ``ValueError`` with a caller-friendly message identifying the
    missing field. Used for (pod_id, order_id, plan_id, tenant_id).
    """
    value = mapping.get(key)
    if value in (None, ""):
        value = fallback
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{key} is required and must be a non-empty string")
    return value


def _require_non_negative_float(
    mapping: Mapping[str, Any], key: str, label: str
) -> float:
    """Return a non-negative float from ``mapping[key]``.

    Raises ``ValueError`` when the value is missing, non-numeric, or
    negative. Accepts ``int``/``float``/``Decimal``-like numerics.
    """
    value = mapping.get(key)
    if value is None:
        raise ValueError(f"{label}.{key} is required")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}.{key} must be numeric, got {value!r}") from exc
    if numeric < 0:
        raise ValueError(f"{label}.{key} must be >= 0, got {numeric}")
    return numeric


def _optional_non_negative_float(
    mapping: Mapping[str, Any], key: str
) -> Optional[float]:
    """Return a non-negative float from ``mapping[key]`` or ``None``.

    Silently returns ``None`` when the field is absent; returns ``None`` and
    logs a warning when the value is negative/non-numeric so a malformed
    upstream payload does not block POD-time reconciliation.
    """
    if key not in mapping:
        return None
    value = mapping.get(key)
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        logger.warning("Ignoring non-numeric %s=%r on reconciliation input", key, value)
        return None
    if numeric < 0:
        logger.warning("Ignoring negative %s=%s on reconciliation input", key, numeric)
        return None
    return numeric


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "DEFAULT_VARIANCE_ALERT_PCT",
    "MVP_RECONCILIATION_INDEX",
    "ReconciliationRecord",
    "ReconciliationService",
    "VARIANCE_ALERT_FLAG",
]
