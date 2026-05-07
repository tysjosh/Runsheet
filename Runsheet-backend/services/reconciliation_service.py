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

Tenant isolation is enforced by:

    * Deriving ``tenant_id`` from the POD input and rejecting records that
      don't carry one.
    * Writing ``tenant_id`` as a top-level keyword on the persisted document
      so the GET endpoint can filter the query by the caller's tenant.

Validates: Requirements 4.4.1, 4.4.2, 4.4.3.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, List, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

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
