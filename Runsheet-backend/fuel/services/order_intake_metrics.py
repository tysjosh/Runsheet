"""
Prometheus metrics for the Order Intake Pipeline.

Defines counters and histograms specific to the intake channel
administration and order ingestion surface. Metrics are registered on
the shared :data:`services.metrics.FUELOPS_REGISTRY` so they appear
alongside other fuel-ops metrics on the admin dashboard endpoint.

This module re-exports the ``fuelops_driver_daily_reset_errors_total``
counter from :mod:`fuel.services.order_metrics` for backward
compatibility. New code should import directly from
:mod:`fuel.services.order_metrics`.

Validates: Requirement 2.1 (intake channel observability).
"""
from __future__ import annotations

from prometheus_client import Counter

from services.metrics import FUELOPS_REGISTRY

# Re-export from the centralized order_metrics module for backward compat
from fuel.services.order_metrics import (  # noqa: F401
    fuelops_driver_daily_reset_errors_total,
)

__all__ = [
    "orders_intake_channel_rotations_total",
    "fuelops_driver_daily_reset_errors_total",
]

# ---------------------------------------------------------------------------
# Intake Channel Admin Metrics
# ---------------------------------------------------------------------------

orders_intake_channel_rotations_total = Counter(
    "orders_intake_channel_rotations_total",
    "Total HMAC secret rotations performed on intake channels. "
    "Tracks how often tenants rotate their webhook signing secrets "
    "so security teams can audit rotation cadence.",
    ["tenant_id"],
    registry=FUELOPS_REGISTRY,
)
