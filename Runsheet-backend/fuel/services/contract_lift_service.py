"""
Contract_Lift Service — monthly rolling lift counter for Supplier_Contracts.

Capability 8 / Requirement 8.3.4 of the fuel-ops hardening spec says:

    THE Platform SHALL track lifted gallons per contract per month in a
    rolling counter and SHALL surface warnings in the admin UI when the
    current month's lifts trend below the contract's
    ``minimum_lift_gallons_per_month``.

Task 7.6 pins the storage to a Redis key of the shape
``contract_lift:{tenant_id}:{contract_id}:{YYYY-MM}`` and wires the
increment into the Loading_Plan commit path.

This module implements the :class:`ContractLiftService` that owns the
counter semantics so both the Loading_Plan commit path (write) and the
Supplier_Contract admin endpoint (read) share a single, well-tested
implementation. It is intentionally lightweight:

* **Incrementing** — uses Redis ``INCRBYFLOAT`` so concurrent commits can
  bump the same counter without a lost update. The first increment of a
  month automatically creates the key and stamps a TTL equal to 62 days
  (long enough to cover the prior month's UI summary view, short enough
  that abandoned keys don't accumulate).
* **Reading** — returns a :class:`ContractLiftSummary` with the raw
  ``gallons_lifted_this_month`` plus a ``percent_of_minimum`` projection
  when the contract carries a ``minimum_lift_gallons_per_month`` and a
  ``below_minimum`` boolean flag the admin UI can consume directly.
* **Tenant-scoped** — every method takes ``tenant_id`` and the key
  pattern prefixes it so cross-tenant key collisions are impossible.
* **Redis-optional** — when ``redis_client`` is ``None`` (e.g. in tests
  without a Redis fixture) every read returns zero and every write is a
  no-op, matching the fallback semantics used by other tenant-config
  services (``TenantSettingsService``, ``TenantInventoryConfigService``).

The service never raises on a transient Redis error — it logs a warning
and degrades gracefully. Losing a counter bump is strictly preferable to
failing the Loading_Plan commit that triggered it, because
``mvp_load_plans`` is the authoritative source and the counter is a
derived aggregate.

Validates: Requirements 8.3.4.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Redis key pattern for the monthly rolling-lift counter. Matches the
#: shape mandated by Task 7.6 verbatim so the ops team can inspect the
#: key directly.
CONTRACT_LIFT_KEY_PATTERN: str = "contract_lift:{tenant_id}:{contract_id}:{yyyy_mm}"

#: TTL for a monthly counter. Sixty-two days comfortably covers the
#: tail end of the preceding month (so the admin UI can render a "last
#: month" summary without a second query) while still reclaiming
#: abandoned keys from inactive contracts.
CONTRACT_LIFT_TTL_SECONDS: int = 62 * 24 * 60 * 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def month_bucket(moment: Optional[datetime] = None) -> str:
    """Return the ``YYYY-MM`` bucket for ``moment``.

    Defaults to the current UTC time when ``moment`` is ``None``. Naive
    datetimes are treated as UTC so callers that build a datetime from a
    business-facing source (``datetime.utcnow()`` in older code paths)
    still land in the expected bucket.
    """

    if moment is None:
        moment = datetime.now(timezone.utc)
    elif moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}"


def _build_key(tenant_id: str, contract_id: str, yyyy_mm: str) -> str:
    """Return the canonical Redis key for a (tenant, contract, month) triple."""

    if not tenant_id or not isinstance(tenant_id, str):
        raise ValueError("tenant_id must be a non-empty string")
    if not contract_id or not isinstance(contract_id, str):
        raise ValueError("contract_id must be a non-empty string")
    if not yyyy_mm or not isinstance(yyyy_mm, str):
        raise ValueError("yyyy_mm must be a non-empty string")
    return CONTRACT_LIFT_KEY_PATTERN.format(
        tenant_id=tenant_id,
        contract_id=contract_id,
        yyyy_mm=yyyy_mm,
    )


# ---------------------------------------------------------------------------
# Summary payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractLiftSummary:
    """A read-only projection of a contract's current-month lift counter.

    ``gallons_lifted_this_month`` is the raw Redis value (zero when the
    key has never been written). ``minimum_lift_gallons_per_month`` is
    copied from the Supplier_Contract so the caller can render a single
    row without a second ES read. ``percent_of_minimum`` is ``None`` when
    the contract has no minimum configured, else a float in ``[0.0, ∞)``
    so the admin UI can render both "below" (< 100) and "over" (>= 100)
    states without a second calculation. ``below_minimum`` is ``True``
    only when the contract carries a positive minimum and the counter
    has not yet reached it.
    """

    tenant_id: str
    contract_id: str
    yyyy_mm: str
    gallons_lifted_this_month: float
    minimum_lift_gallons_per_month: Optional[float]
    percent_of_minimum: Optional[float]
    below_minimum: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "contract_id": self.contract_id,
            "yyyy_mm": self.yyyy_mm,
            "gallons_lifted_this_month": self.gallons_lifted_this_month,
            "minimum_lift_gallons_per_month": self.minimum_lift_gallons_per_month,
            "percent_of_minimum": self.percent_of_minimum,
            "below_minimum": self.below_minimum,
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ContractLiftService:
    """Read / write the per-contract monthly rolling-lift counter.

    Args:
        redis_client: Optional async Redis client. When ``None`` the
            service treats every read as zero and every write as a
            no-op so unit tests and local bootstrap without Redis still
            succeed.
        ttl_seconds: Override the default monthly-bucket TTL. Exposed
            primarily for tests; production bootstrap should keep the
            default ``CONTRACT_LIFT_TTL_SECONDS``.
    """

    def __init__(
        self,
        redis_client: Optional[Any] = None,
        *,
        ttl_seconds: int = CONTRACT_LIFT_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._redis = redis_client
        self._ttl_seconds = int(ttl_seconds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def record_lift(
        self,
        tenant_id: str,
        contract_id: str,
        gallons: float,
        *,
        moment: Optional[datetime] = None,
    ) -> float:
        """Increment the rolling counter by ``gallons`` for the given month.

        Zero or negative ``gallons`` are treated as no-ops (negative
        values could only come from a data bug upstream; we refuse to
        silently decrement).

        Returns the post-increment counter value. When Redis is
        unreachable or the client is ``None``, returns ``0.0`` and logs
        a warning — never raises, so a transient Redis outage cannot
        block a Loading_Plan commit.

        Validates: Requirement 8.3.4.
        """

        if gallons is None:
            return 0.0
        try:
            gallons_float = float(gallons)
        except (TypeError, ValueError):
            logger.warning(
                "ContractLiftService.record_lift: rejecting non-numeric "
                "gallons=%r tenant=%s contract=%s",
                gallons,
                tenant_id,
                contract_id,
            )
            return 0.0
        if gallons_float <= 0.0:
            return 0.0

        if self._redis is None:
            logger.debug(
                "ContractLiftService.record_lift: no Redis configured; "
                "skipping counter bump tenant=%s contract=%s gallons=%.3f",
                tenant_id,
                contract_id,
                gallons_float,
            )
            return 0.0

        yyyy_mm = month_bucket(moment)
        try:
            key = _build_key(tenant_id, contract_id, yyyy_mm)
        except ValueError as exc:
            logger.warning(
                "ContractLiftService.record_lift: invalid key parts: %s", exc
            )
            return 0.0

        try:
            new_value = await self._redis.incrbyfloat(key, gallons_float)
        except Exception as exc:
            logger.warning(
                "ContractLiftService.record_lift: INCRBYFLOAT failed "
                "tenant=%s contract=%s key=%s err=%s",
                tenant_id,
                contract_id,
                key,
                exc,
            )
            return 0.0

        # Stamp a TTL on the first write each month. ``expire`` is a
        # separate call; a failure here is logged but not raised — the
        # counter itself already landed.
        try:
            await self._redis.expire(key, self._ttl_seconds)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "ContractLiftService.record_lift: expire failed "
                "tenant=%s contract=%s key=%s err=%s",
                tenant_id,
                contract_id,
                key,
                exc,
            )

        return _coerce_float(new_value, default=gallons_float)

    async def get_monthly_lift(
        self,
        tenant_id: str,
        contract_id: str,
        *,
        moment: Optional[datetime] = None,
    ) -> float:
        """Return the current-month lift total for a contract.

        Falls back to ``0.0`` when:

            * Redis is unavailable or unconfigured.
            * The key has never been written (no lift yet this month).
            * The stored value is not parseable as a float (logs a
              warning and returns ``0.0`` so a corrupt key never
              breaks the admin UI).
        """

        if self._redis is None:
            return 0.0

        yyyy_mm = month_bucket(moment)
        try:
            key = _build_key(tenant_id, contract_id, yyyy_mm)
        except ValueError as exc:
            logger.warning(
                "ContractLiftService.get_monthly_lift: invalid key parts: %s",
                exc,
            )
            return 0.0

        try:
            raw = await self._redis.get(key)
        except Exception as exc:
            logger.warning(
                "ContractLiftService.get_monthly_lift: GET failed "
                "tenant=%s contract=%s key=%s err=%s",
                tenant_id,
                contract_id,
                key,
                exc,
            )
            return 0.0

        if raw is None:
            return 0.0
        return _coerce_float(raw, default=0.0)

    async def get_summary(
        self,
        tenant_id: str,
        contract_id: str,
        minimum_lift_gallons_per_month: Optional[float] = None,
        *,
        moment: Optional[datetime] = None,
    ) -> ContractLiftSummary:
        """Return a :class:`ContractLiftSummary` for the admin UI.

        ``minimum_lift_gallons_per_month`` is taken from the caller (the
        Supplier_Contract) rather than looked up here to keep the service
        dependency-light — the contract record is already in hand at the
        call site.
        """

        yyyy_mm = month_bucket(moment)
        gallons = await self.get_monthly_lift(
            tenant_id=tenant_id, contract_id=contract_id, moment=moment
        )

        minimum = _normalize_minimum(minimum_lift_gallons_per_month)
        percent: Optional[float]
        below: bool
        if minimum is None or minimum == 0.0:
            percent = None
            below = False
        else:
            percent = (gallons / minimum) * 100.0
            below = gallons < minimum

        return ContractLiftSummary(
            tenant_id=tenant_id,
            contract_id=contract_id,
            yyyy_mm=yyyy_mm,
            gallons_lifted_this_month=gallons,
            minimum_lift_gallons_per_month=minimum,
            percent_of_minimum=percent,
            below_minimum=below,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _coerce_float(value: Any, *, default: float) -> float:
    """Best-effort float coercion tolerant of bytes / None / strings."""

    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode()
        except UnicodeDecodeError:
            return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_minimum(value: Any) -> Optional[float]:
    """Normalize a user-supplied minimum to a non-negative float or ``None``."""

    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out < 0.0:
        return None
    return out


__all__ = [
    "CONTRACT_LIFT_KEY_PATTERN",
    "CONTRACT_LIFT_TTL_SECONDS",
    "ContractLiftService",
    "ContractLiftSummary",
    "month_bucket",
]
