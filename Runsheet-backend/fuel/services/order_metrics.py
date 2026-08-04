"""
Prometheus metrics for the Order Intake Pipeline — full observability surface.

Defines counters and histograms for the intake pipeline, adapter errors,
shadow divergence, state-machine rejections, drift alerts, and driver
daily reset. All metrics are registered on the shared
:data:`services.metrics.FUELOPS_REGISTRY`.

Validates: Requirements 9.2.1, 9.2.2, 9.2.3, 9.2.5.
"""
from __future__ import annotations

from prometheus_client import Counter, Histogram

from services.metrics import FUELOPS_REGISTRY

__all__ = [
    "orders_intake_received_total",
    "orders_intake_processed_total",
    "orders_intake_latency_seconds",
    "orders_adapter_errors_total",
    "orders_shadow_divergence_total",
    "orders_state_transition_rejections_total",
    "orders_drift_alert_total",
    "fuelops_driver_daily_reset_errors_total",
]

# ---------------------------------------------------------------------------
# Intake Pipeline Counters
# ---------------------------------------------------------------------------

orders_intake_received_total = Counter(
    "orders_intake_received_total",
    "Total order intake events received across all channels. "
    "Incremented at the top of the pipeline before any validation.",
    ["tenant_id", "intake_channel", "schema_version"],
    registry=FUELOPS_REGISTRY,
)

orders_intake_processed_total = Counter(
    "orders_intake_processed_total",
    "Total order intake events that completed processing. "
    "Labels include the terminal status of the intake attempt "
    "(processed, duplicate, queued_for_review).",
    ["tenant_id", "intake_channel", "status"],
    registry=FUELOPS_REGISTRY,
)

# ---------------------------------------------------------------------------
# Intake Latency Histogram
# ---------------------------------------------------------------------------

#: Histogram buckets tuned to intake pipeline latency. The pipeline's
#: P99 on a warm ES cluster is in the 20–100 ms range; cold adapter
#: transforms with tank lookups can reach 500 ms. The upper bucket
#: (10s) catches pathological runs.
_INTAKE_LATENCY_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

orders_intake_latency_seconds = Histogram(
    "orders_intake_latency_seconds",
    "End-to-end latency of order intake processing in seconds. "
    "Measured from the start of ingest_webhook/ingest_dispatcher "
    "to the final mark_processed call.",
    ["tenant_id", "intake_channel", "event_type"],
    buckets=_INTAKE_LATENCY_BUCKETS,
    registry=FUELOPS_REGISTRY,
)

# ---------------------------------------------------------------------------
# Adapter Errors
# ---------------------------------------------------------------------------

orders_adapter_errors_total = Counter(
    "orders_adapter_errors_total",
    "Total adapter errors during intake transformation. "
    "Incremented when an adapter raises AdapterError and the event "
    "is routed to the poison queue.",
    ["tenant_id", "intake_channel", "error_type"],
    registry=FUELOPS_REGISTRY,
)

# ---------------------------------------------------------------------------
# Shadow Divergence
# ---------------------------------------------------------------------------

orders_shadow_divergence_total = Counter(
    "orders_shadow_divergence_total",
    "Total field-level divergences detected during shadow-mode "
    "comparison between the new and legacy adapter outputs.",
    ["tenant_id", "intake_channel", "field"],
    registry=FUELOPS_REGISTRY,
)

# NB: ``orders_legacy_route_hits_total`` was removed with the only route it
# counted (``POST /webhooks/dinee``). It measured remaining consumers of the
# deprecated surface; with the surface gone there is nothing to measure.
#
# ``orders_legacy_mirror_errors_total`` followed it out. It counted
# ``mirror_order`` / ``mirror_driver`` failures on the LegacyDualWriter shim;
# the shim and its ``pending_legacy_mirrors`` retry queue have been retired,
# so no code path can increment it.

# ---------------------------------------------------------------------------
# State Transition Rejections
# ---------------------------------------------------------------------------

orders_state_transition_rejections_total = Counter(
    "orders_state_transition_rejections_total",
    "Total rejected order status transitions. Incremented when "
    "assert_transition raises due to an illegal old→new pair.",
    ["tenant_id", "old_status", "new_status"],
    registry=FUELOPS_REGISTRY,
)

# ---------------------------------------------------------------------------
# Drift Alerts
# ---------------------------------------------------------------------------

orders_drift_alert_total = Counter(
    "orders_drift_alert_total",
    "Total drift alerts emitted when per-channel order drift exceeds "
    "the configured threshold.",
    ["tenant_id", "channel_id"],
    registry=FUELOPS_REGISTRY,
)

# ---------------------------------------------------------------------------
# Driver Daily Reset Errors
# ---------------------------------------------------------------------------

fuelops_driver_daily_reset_errors_total = Counter(
    "fuelops_driver_daily_reset_errors_total",
    "Total failures when resetting driver completed_today counters "
    "at midnight. Labelled by tenant_id so operators can identify "
    "which tenants are affected by reset failures.",
    ["tenant_id"],
    registry=FUELOPS_REGISTRY,
)
