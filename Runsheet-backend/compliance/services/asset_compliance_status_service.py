"""Asset compliance status aggregator (cross-module-entity-linkage, task 10.2).

Requirement 11.2 asks that, when an asset is assigned to a job/order, the
system be able to surface that asset's *current* compliance status (e.g. a
certification expired/expiring, a meter out of calibration) at the assignment
decision surface, so an operator does not dispatch a non-compliant asset.

The driver side of this signal already exists — the ops Drivers → Utilization
surface renders a qualification-status chip sourced from
``DriverQualificationService.get_qualification_summary`` (task 4). This module
mirrors that pattern for **assets**: it collapses an asset's certification and
meter-calibration records into a single chip-friendly ``overall_status``
(``valid`` / ``expiring`` / ``expired``) the Fleet assignment surface can
consume, plus the per-record items behind it.

Reference-don't-duplicate: this service owns *no* expiry logic of its own. It
delegates record retrieval to the existing
:class:`~compliance.services.asset_certification_service.AssetCertificationService`
and :class:`~compliance.services.meter_audit_service.MeterAuditService`, and
reuses their published alert thresholds so the chip can never drift from the
daily cron transitions / dispatch-eligibility checks those services already
implement.

All reads are tenant-scoped: both underlying services scope every query via
``inject_tenant_filter``, so this aggregator never crosses a tenant boundary
(Req 5.3).

Validates: Requirements 11.2.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from compliance.services.asset_certification_service import (
    ALERT_THRESHOLD_WARNING_DAYS as CERT_EXPIRING_DAYS,
    AssetCertificationService,
)
from compliance.services.meter_audit_service import (
    CALIBRATION_ALERT_THRESHOLD_DAYS as METER_EXPIRING_DAYS,
    MeterAuditService,
)

logger = logging.getLogger(__name__)

# Page size used for the per-asset record reads. A single asset will never
# carry anywhere near this many certifications/meters, so one page is enough.
_RECORD_PAGE_LIMIT = 200

# Overall-status severity ordering (worst wins when collapsing items).
_STATUS_RANK = {"valid": 0, "expiring": 1, "expired": 2}


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AssetComplianceItem(BaseModel):
    """A single compliance record contributing to an asset's status.

    ``kind`` distinguishes the source record (``certification`` or ``meter``);
    ``label`` is the human-readable subject (the certification type or meter
    number) for display in a tooltip / expanded row.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str  # certification | meter
    reference_id: str  # cert_id | meter_id
    label: str  # certification_type | meter_number
    status: str  # valid | expiring | expired
    expiry_date: Optional[date] = None
    days_until_expiry: Optional[int] = None
    detail: Optional[str] = None


class AssetComplianceSummary(BaseModel):
    """Compact per-asset compliance summary for the assignment surface.

    ``overall_status`` collapses every contributing item into a single
    chip-friendly signal (worst wins): ``expired`` > ``expiring`` > ``valid``.
    ``has_records`` is ``False`` when the asset has no certification or meter
    records at all, so the UI can render an explicit "unlinked"/"no data"
    affordance rather than a misleading green chip (mirroring the Drivers
    qualification chip's unlinked state).

    Validates: Requirements 11.2.
    """

    model_config = ConfigDict(extra="forbid")

    asset_id: str
    overall_status: str  # valid | expiring | expired | unknown
    has_records: bool = False
    items: List[AssetComplianceItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AssetComplianceStatusService:
    """Aggregates an asset's certification + meter status into one signal.

    Args:
        certification_service: Existing AssetCertificationService used to read
            the asset's certifications (reuses its expiry thresholds).
        meter_audit_service: Existing MeterAuditService used to read the
            asset's meters (keyed by ``truck_id == asset_id``).

    Validates: Requirement 11.2.
    """

    def __init__(
        self,
        certification_service: AssetCertificationService,
        meter_audit_service: MeterAuditService,
    ) -> None:
        self._certs = certification_service
        self._meters = meter_audit_service

    async def get_asset_compliance_summary(
        self, tenant_id: str, asset_id: str
    ) -> AssetComplianceSummary:
        """Return the collapsed compliance status for a single asset.

        Reads the asset's certifications and meters (tenant-scoped) and derives
        a per-record ``valid``/``expiring``/``expired`` status from each
        record's expiry date relative to today, reusing the published alert
        thresholds of the owning services. The overall status is the worst of
        the contributing items; an asset with no records returns
        ``overall_status="unknown"`` and ``has_records=False``.

        Validates: Requirement 11.2.
        """
        today = date.today()
        items: List[AssetComplianceItem] = []

        items.extend(await self._certification_items(tenant_id, asset_id, today))
        items.extend(await self._meter_items(tenant_id, asset_id, today))

        if not items:
            return AssetComplianceSummary(
                asset_id=asset_id,
                overall_status="unknown",
                has_records=False,
                items=[],
            )

        overall = max(items, key=lambda it: _STATUS_RANK.get(it.status, 0)).status
        return AssetComplianceSummary(
            asset_id=asset_id,
            overall_status=overall,
            has_records=True,
            items=items,
        )

    # ------------------------------------------------------------------
    # Per-source item builders
    # ------------------------------------------------------------------

    async def _certification_items(
        self, tenant_id: str, asset_id: str, today: date
    ) -> List[AssetComplianceItem]:
        """Build compliance items from the asset's certifications.

        ``superseded`` certifications are skipped — they have been replaced by
        a newer valid cert and no longer reflect the asset's status (mirrors
        ``AssetCertificationService.is_dispatch_eligible``).
        """
        result = await self._certs.list(
            tenant_id, asset_id=asset_id, limit=_RECORD_PAGE_LIMIT
        )
        items: List[AssetComplianceItem] = []
        for cert in result.get("items", []):
            doc_status = cert.get("status")
            if doc_status == "superseded":
                continue

            expiry = self._parse_date(cert.get("expiry_date"))
            days = (expiry - today).days if expiry is not None else None

            # A cert already transitioned to "expired" by the cron is expired
            # regardless of the parsed date; otherwise derive from the date.
            if doc_status == "expired" or (days is not None and days < 0):
                status = "expired"
            elif days is not None and days <= CERT_EXPIRING_DAYS:
                status = "expiring"
            else:
                status = "valid"

            cert_type = cert.get("certification_type", "certification")
            items.append(
                AssetComplianceItem(
                    kind="certification",
                    reference_id=cert.get("cert_id", ""),
                    label=cert_type,
                    status=status,
                    expiry_date=expiry,
                    days_until_expiry=days,
                    detail=(
                        f"Certification '{cert_type}' {status}"
                        if status != "valid"
                        else None
                    ),
                )
            )
        return items

    async def _meter_items(
        self, tenant_id: str, asset_id: str, today: date
    ) -> List[AssetComplianceItem]:
        """Build compliance items from the asset's meters.

        Meters are keyed by ``truck_id`` (== ``asset_id``). Only ``active``
        meters contribute; a meter whose calibration has lapsed is "out of
        calibration" (``expired``).
        """
        result = await self._meters.list_meters(
            tenant_id, truck_id=asset_id, limit=_RECORD_PAGE_LIMIT
        )
        items: List[AssetComplianceItem] = []
        for meter in result.get("items", []):
            if meter.get("status") not in (None, "active"):
                # Retired / inactive meters do not gate assignment.
                continue

            expiry = self._parse_date(meter.get("calibration_expiry_date"))
            days = (expiry - today).days if expiry is not None else None

            if days is not None and days < 0:
                status = "expired"
            elif days is not None and days <= METER_EXPIRING_DAYS:
                status = "expiring"
            else:
                status = "valid"

            meter_number = meter.get("meter_number", "meter")
            items.append(
                AssetComplianceItem(
                    kind="meter",
                    reference_id=meter.get("meter_id", ""),
                    label=meter_number,
                    status=status,
                    expiry_date=expiry,
                    days_until_expiry=days,
                    detail=(
                        f"Meter '{meter_number}' out of calibration"
                        if status == "expired"
                        else (
                            f"Meter '{meter_number}' calibration expiring"
                            if status == "expiring"
                            else None
                        )
                    ),
                )
            )
        return items

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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


__all__ = [
    "AssetComplianceItem",
    "AssetComplianceSummary",
    "AssetComplianceStatusService",
]
