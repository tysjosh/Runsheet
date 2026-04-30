"""
Business Validator for mutation tools.

Validates mutation parameters against business rules before execution.
Uses a dispatcher pattern to route validation to tool-specific validators.
Each validator checks entity existence (with tenant scoping) and enforces
domain constraints such as valid status transitions and quantity limits.

Tools without a specific validator pass validation by default.

Requirements: 1.9, 1.10
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of a business rule validation check."""
    valid: bool
    reason: Optional[str] = None


# Valid job status transitions (mirrors scheduling.models.VALID_TRANSITIONS)
VALID_JOB_TRANSITIONS: Dict[str, set] = {
    "scheduled": {"assigned", "cancelled"},
    "assigned": {"in_progress", "cancelled"},
    "in_progress": {"completed", "failed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
    "failed": set(),
}


class BusinessValidator:
    """Validates mutation parameters against business rules.

    Uses a dispatcher pattern: the ``validate`` method looks up a
    tool-specific validator by name (``_validate_{tool_name}``) and
    delegates to it. Tools without a dedicated validator pass by default.
    """

    def __init__(self, es_service):
        self._es = es_service

    async def validate(
        self, tool_name: str, params: Dict[str, Any], tenant_id: str
    ) -> ValidationResult:
        """Dispatch to tool-specific validation.

        Args:
            tool_name: The mutation tool name (e.g. ``update_job_status``).
            params: The tool invocation parameters.
            tenant_id: Tenant scope for entity lookups.

        Returns:
            A ``ValidationResult`` indicating whether the mutation is allowed.
        """
        validator = getattr(self, f"_validate_{tool_name}", None)
        if validator:
            return await validator(params, tenant_id)
        return ValidationResult(valid=True)  # No validator = pass

    # ------------------------------------------------------------------
    # Tool-specific validators
    # ------------------------------------------------------------------

    async def _validate_update_job_status(
        self, params: dict, tenant_id: str
    ) -> ValidationResult:
        """Validate a job status transition against the state machine."""
        job_id = params.get("job_id")
        new_status = params.get("new_status")

        job = await self._fetch_job(job_id, tenant_id)
        if not job:
            return ValidationResult(False, f"Job {job_id} not found")

        current_status = job.get("status")
        allowed = VALID_JOB_TRANSITIONS.get(current_status, set())
        if new_status not in allowed:
            return ValidationResult(
                False,
                f"Invalid transition: {current_status} → {new_status}. "
                f"Allowed: {sorted(allowed)}",
            )
        return ValidationResult(valid=True)

    async def _validate_assign_asset_to_job(
        self, params: dict, tenant_id: str
    ) -> ValidationResult:
        """Validate that a job and asset exist and the job can accept an assignment."""
        job_id = params.get("job_id")
        asset_id = params.get("asset_id")

        job = await self._fetch_job(job_id, tenant_id)
        if not job:
            return ValidationResult(False, f"Job {job_id} not found")
        if job.get("status") not in ("scheduled", "assigned"):
            return ValidationResult(
                False,
                f"Job {job_id} is {job['status']}, cannot assign asset",
            )

        asset = await self._fetch_asset(asset_id, tenant_id)
        if not asset:
            return ValidationResult(False, f"Asset {asset_id} not found")

        return ValidationResult(valid=True)

    async def _validate_cancel_job(
        self, params: dict, tenant_id: str
    ) -> ValidationResult:
        """Validate that a job exists and is not already in a terminal state."""
        job_id = params.get("job_id")
        job = await self._fetch_job(job_id, tenant_id)
        if not job:
            return ValidationResult(False, f"Job {job_id} not found")
        if job.get("status") in ("completed", "cancelled", "failed"):
            return ValidationResult(
                False, f"Job {job_id} is already {job['status']}"
            )
        return ValidationResult(valid=True)

    async def _validate_request_fuel_refill(
        self, params: dict, tenant_id: str
    ) -> ValidationResult:
        """Validate fuel refill parameters: positive quantity and station_id present."""
        station_id = params.get("station_id")
        if not station_id:
            return ValidationResult(False, "station_id is required")

        quantity = params.get("quantity_liters", 0)
        if quantity <= 0:
            return ValidationResult(False, "Refill quantity must be positive")
        if quantity > 100000:
            return ValidationResult(
                False, "Refill quantity exceeds maximum (100,000 L)"
            )
        return ValidationResult(valid=True)

    # ------------------------------------------------------------------
    # Entity fetch helpers (tenant-scoped)
    # ------------------------------------------------------------------

    async def _fetch_job(self, job_id: str, tenant_id: str) -> Optional[dict]:
        """Fetch a job by ID with tenant scoping.

        Args:
            job_id: The job identifier.
            tenant_id: Tenant scope for the query.

        Returns:
            The job document dict, or ``None`` if not found.
        """
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"job_id": job_id}},
                    ]
                }
            }
        }
        try:
            resp = await self._es.search_documents("jobs_current", query, 1)
            hits = [h["_source"] for h in resp["hits"]["hits"]]
            return hits[0] if hits else None
        except Exception as e:
            logger.error(f"Failed to fetch job {job_id}: {e}")
            return None

    async def _fetch_asset(
        self, asset_id: str, tenant_id: str
    ) -> Optional[dict]:
        """Fetch an asset by ID or plate number with tenant scoping.

        Args:
            asset_id: The asset identifier or plate number.
            tenant_id: Tenant scope for the query.

        Returns:
            The asset document dict, or ``None`` if not found.
        """
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {
                            "bool": {
                                "should": [
                                    {"term": {"asset_id": asset_id}},
                                    {"term": {"plate_number": asset_id}},
                                ]
                            }
                        },
                    ]
                }
            }
        }
        try:
            resp = await self._es.search_documents("trucks", query, 1)
            hits = [h["_source"] for h in resp["hits"]["hits"]]
            return hits[0] if hits else None
        except Exception as e:
            logger.error(f"Failed to fetch asset {asset_id}: {e}")
            return None
