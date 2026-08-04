"""
Driver report domain model — backs the ``driver_reports`` Elasticsearch index.

A :class:`DriverReport` is an immutable record submitted by a driver (via the
Dinee driver voice agent) against an active assignment so dispatch is informed
of delays and exceptions. The model uses ``ConfigDict(extra="forbid")`` so
unknown fields are rejected at construction time, matching the strict ES
mapping declared in
:mod:`fuel.services.order_es_mappings` (``DRIVER_REPORTS_MAPPING``).

The report ``kind`` is constrained to the closed set required by the Dinee
contract: ``delay``, ``terminal_wait``, ``exception``, ``note`` (Req 21.1/21.2).
Optional ``detail`` / ``eta_minutes`` fields are stored verbatim when supplied
(Req 21.3).

Validates: Requirements 21.1, 21.3.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Type Aliases
# ---------------------------------------------------------------------------

#: Closed set of accepted report kinds (Req 21.1/21.2). Any other value — or an
#: absent value — is rejected with HTTP 422 at the endpoint boundary.
DriverReportKind = Literal["delay", "terminal_wait", "exception", "note"]


# ---------------------------------------------------------------------------
# DriverReport
# ---------------------------------------------------------------------------


class DriverReport(BaseModel):
    """An immutable driver report scoped to a tenant, driver, and assignment.

    ``assignment_id`` references the order/assignment the report is filed
    against; the :class:`~fuel.driver_report_repository.DriverReportRepository`
    validates that this assignment is owned by the same tenant and driver
    before any write (Req 21.4/21.5).
    """

    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(..., description="Unique report identifier.")
    tenant_id: str = Field(..., description="Owning tenant.")
    driver_id: str = Field(..., description="Driver who filed the report.")
    assignment_id: str = Field(
        ..., description="Order/assignment the report is filed against."
    )
    kind: DriverReportKind = Field(..., description="Report category.")
    detail: Optional[str] = Field(
        default=None, description="Free-text detail supplied with the report."
    )
    eta_minutes: Optional[int] = Field(
        default=None, description="Revised ETA in minutes, when supplied."
    )
    created_at: Optional[datetime] = Field(
        default=None, description="Server-stamped creation timestamp."
    )


__all__ = [
    "DriverReport",
    "DriverReportKind",
]
