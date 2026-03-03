"""
Prometheus-compatible metrics for the Ops Intelligence Layer.

Defines all ops_* metrics as module-level prometheus_client objects and
provides a periodic log-based alert checker that evaluates alert rules
against current metric values.

Metrics are exposed via the ``/ops/metrics/prometheus`` endpoint added
to the ops API router.

Validates: Requirements 23.4-23.6
"""

import asyncio
import logging
import time
from collections import defaultdict
from typing import Optional

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom registry (avoids polluting the default global registry with
# process/platform collectors that may not be desired).
# ---------------------------------------------------------------------------
REGISTRY = CollectorRegistry()

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

# -- Webhook ingestion --
ops_webhook_received_total = Counter(
    "ops_webhook_received_total",
    "Total webhooks received",
    ["tenant_id", "schema_version"],
    registry=REGISTRY,
)

ops_webhook_processed_total = Counter(
    "ops_webhook_processed_total",
    "Webhook processing outcomes",
    ["tenant_id", "status"],
    registry=REGISTRY,
)

ops_ingestion_latency_seconds = Histogram(
    "ops_ingestion_latency_seconds",
    "Webhook receipt to ES upsert latency in seconds",
    ["tenant_id", "event_type"],
    registry=REGISTRY,
)

ops_transform_errors_total = Counter(
    "ops_transform_errors_total",
    "Adapter transform failures",
    ["tenant_id", "error_type"],
    registry=REGISTRY,
)

# -- Elasticsearch indexing --
ops_es_indexing_latency_seconds = Histogram(
    "ops_es_indexing_latency_seconds",
    "ES indexing operation latency in seconds",
    ["index_name"],
    registry=REGISTRY,
)

ops_es_indexing_errors_total = Counter(
    "ops_es_indexing_errors_total",
    "ES indexing failures",
    ["index_name", "error_type"],
    registry=REGISTRY,
)

# -- Poison queue --
ops_poison_queue_depth = Gauge(
    "ops_poison_queue_depth",
    "Current poison queue size per tenant",
    ["tenant_id"],
    registry=REGISTRY,
)

ops_poison_queue_oldest_age_seconds = Gauge(
    "ops_poison_queue_oldest_age_seconds",
    "Age of oldest unresolved poison queue entry in seconds",
    registry=REGISTRY,
)

# -- API --
ops_api_request_duration_seconds = Histogram(
    "ops_api_request_duration_seconds",
    "API response latency in seconds",
    ["endpoint", "method"],
    registry=REGISTRY,
)

# -- WebSocket --
ops_ws_active_connections = Gauge(
    "ops_ws_active_connections",
    "Active WebSocket connections per tenant",
    ["tenant_id"],
    registry=REGISTRY,
)

# -- Drift --
ops_drift_percentage = Gauge(
    "ops_drift_percentage",
    "Last drift detection result as percentage",
    ["tenant_id"],
    registry=REGISTRY,
)

# -- Feature flags --
ops_feature_flag_changes_total = Counter(
    "ops_feature_flag_changes_total",
    "Feature flag changes",
    ["tenant_id", "action"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Helper: generate Prometheus text output
# ---------------------------------------------------------------------------

def generate_metrics() -> bytes:
    """Return the current metrics in Prometheus text exposition format."""
    return generate_latest(REGISTRY)


# ---------------------------------------------------------------------------
# Log-based alert rules
# ---------------------------------------------------------------------------

# Track recent values for time-window based alerts
_alert_state: dict = {
    "ingestion_latency_warn_since": None,  # timestamp when p95 first exceeded 5s
    "es_errors_warn_since": None,          # timestamp when rate first exceeded 10/min
    "ws_zero_since": None,                 # timestamp when connections first hit 0
    "last_es_error_count": 0.0,            # previous total for rate calculation
    "last_es_error_check": None,           # timestamp of last rate check
}


def check_alert_rules() -> list[dict]:
    """
    Evaluate log-based alert rules against current metric values.

    Returns a list of triggered alerts, each a dict with keys:
        severity, condition, description

    Alert rules:
    - WARN:  ops_ingestion_latency_seconds p95 > 5s for 5 minutes
    - ERROR: ops_poison_queue_depth > 100 for any tenant
    - WARN:  ops_poison_queue_oldest_age_seconds > 3600 (1 hour)
    - ERROR: ops_es_indexing_errors_total rate > 10/min for 5 minutes
    - WARN:  ops_drift_percentage > 1% for any tenant
    - WARN:  ops_ws_active_connections = 0 for > 10 minutes
    """
    now = time.time()
    alerts: list[dict] = []

    # --- WARN: ingestion latency p95 > 5s for 5 minutes ---
    try:
        # Collect all histogram samples to estimate p95
        # prometheus_client Histogram exposes _sum and _count per label set
        # We approximate p95 from the bucket boundaries
        latency_high = False
        for metric in ops_ingestion_latency_seconds.collect():
            for sample in metric.samples:
                if sample.name.endswith("_count") and sample.value > 0:
                    # Check the corresponding _sum to get average
                    pass
                # Check the 5.0 bucket (le="5.0") — if count == total, p95 <= 5s
                if sample.name.endswith("_bucket"):
                    le = sample.labels.get("le", "")
                    if le == "5.0":
                        # Find the total count for this label set
                        pass
        # Simpler approach: check if any observation exceeded 5s recently
        # by looking at the +Inf bucket vs the 5.0 bucket
        for metric in ops_ingestion_latency_seconds.collect():
            buckets_by_labels: dict = defaultdict(dict)
            for sample in metric.samples:
                if sample.name.endswith("_bucket"):
                    # Build a key from non-le labels
                    key = tuple(
                        (k, v) for k, v in sorted(sample.labels.items()) if k != "le"
                    )
                    buckets_by_labels[key][sample.labels.get("le", "")] = sample.value

            for key, buckets in buckets_by_labels.items():
                total = buckets.get("+Inf", 0)
                at_5s = buckets.get("5.0", 0)
                if total > 0 and (total - at_5s) / total > 0.05:
                    latency_high = True
                    break

        if latency_high:
            if _alert_state["ingestion_latency_warn_since"] is None:
                _alert_state["ingestion_latency_warn_since"] = now
            elif now - _alert_state["ingestion_latency_warn_since"] >= 300:
                alerts.append({
                    "severity": "WARN",
                    "condition": "ops_ingestion_latency_seconds p95 > 5s for 5 minutes",
                    "description": "Ingestion pipeline slow",
                })
        else:
            _alert_state["ingestion_latency_warn_since"] = None
    except Exception as exc:
        logger.debug("Alert check error (ingestion latency): %s", exc)

    # --- ERROR: poison queue depth > 100 for any tenant ---
    try:
        for metric in ops_poison_queue_depth.collect():
            for sample in metric.samples:
                if sample.value > 100:
                    tenant = sample.labels.get("tenant_id", "unknown")
                    alerts.append({
                        "severity": "ERROR",
                        "condition": f"ops_poison_queue_depth > 100 (tenant={tenant})",
                        "description": "Poison queue backlog critical",
                    })
    except Exception as exc:
        logger.debug("Alert check error (poison queue depth): %s", exc)

    # --- WARN: poison queue oldest age > 3600s ---
    try:
        for metric in ops_poison_queue_oldest_age_seconds.collect():
            for sample in metric.samples:
                if sample.value > 3600:
                    alerts.append({
                        "severity": "WARN",
                        "condition": "ops_poison_queue_oldest_age_seconds > 3600",
                        "description": "Stale poison queue entries (oldest > 1 hour)",
                    })
    except Exception as exc:
        logger.debug("Alert check error (poison queue age): %s", exc)

    # --- ERROR: ES indexing errors rate > 10/min for 5 minutes ---
    try:
        current_total = 0.0
        for metric in ops_es_indexing_errors_total.collect():
            for sample in metric.samples:
                if sample.name.endswith("_total"):
                    current_total += sample.value

        last_check = _alert_state["last_es_error_check"]
        last_count = _alert_state["last_es_error_count"]

        if last_check is not None:
            elapsed_minutes = (now - last_check) / 60.0
            if elapsed_minutes > 0:
                rate_per_min = (current_total - last_count) / elapsed_minutes
                if rate_per_min > 10:
                    if _alert_state["es_errors_warn_since"] is None:
                        _alert_state["es_errors_warn_since"] = now
                    elif now - _alert_state["es_errors_warn_since"] >= 300:
                        alerts.append({
                            "severity": "ERROR",
                            "condition": "ops_es_indexing_errors_total rate > 10/min for 5 minutes",
                            "description": "ES indexing failures spiking",
                        })
                else:
                    _alert_state["es_errors_warn_since"] = None

        _alert_state["last_es_error_count"] = current_total
        _alert_state["last_es_error_check"] = now
    except Exception as exc:
        logger.debug("Alert check error (ES indexing errors): %s", exc)

    # --- WARN: drift percentage > 1% for any tenant ---
    try:
        for metric in ops_drift_percentage.collect():
            for sample in metric.samples:
                if sample.value > 1.0:
                    tenant = sample.labels.get("tenant_id", "unknown")
                    alerts.append({
                        "severity": "WARN",
                        "condition": f"ops_drift_percentage > 1% (tenant={tenant})",
                        "description": "Source-replica drift detected",
                    })
    except Exception as exc:
        logger.debug("Alert check error (drift percentage): %s", exc)

    # --- WARN: WS active connections = 0 for > 10 minutes ---
    try:
        total_ws = 0.0
        for metric in ops_ws_active_connections.collect():
            for sample in metric.samples:
                total_ws += sample.value

        if total_ws == 0:
            if _alert_state["ws_zero_since"] is None:
                _alert_state["ws_zero_since"] = now
            elif now - _alert_state["ws_zero_since"] >= 600:
                alerts.append({
                    "severity": "WARN",
                    "condition": "ops_ws_active_connections = 0 for > 10 minutes",
                    "description": "Potential WebSocket connectivity issue",
                })
        else:
            _alert_state["ws_zero_since"] = None
    except Exception as exc:
        logger.debug("Alert check error (WS connections): %s", exc)

    return alerts


async def _periodic_alert_checker(interval_seconds: int = 60) -> None:
    """
    Background task that periodically evaluates alert rules and emits
    structured log entries for triggered alerts.
    """
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                triggered = check_alert_rules()
                for alert in triggered:
                    if alert["severity"] == "ERROR":
                        logger.error(
                            "ALERT [%s]: %s — %s",
                            alert["severity"],
                            alert["condition"],
                            alert["description"],
                        )
                    else:
                        logger.warning(
                            "ALERT [%s]: %s — %s",
                            alert["severity"],
                            alert["condition"],
                            alert["description"],
                        )
            except Exception as exc:
                logger.error("Alert checker iteration failed: %s", exc)
    except asyncio.CancelledError:
        pass


_alert_checker_task: Optional[asyncio.Task] = None


def start_alert_checker(interval_seconds: int = 60) -> None:
    """Start the periodic alert checker background task."""
    global _alert_checker_task
    if _alert_checker_task is None or _alert_checker_task.done():
        _alert_checker_task = asyncio.create_task(
            _periodic_alert_checker(interval_seconds)
        )


def stop_alert_checker() -> None:
    """Cancel the periodic alert checker background task."""
    global _alert_checker_task
    if _alert_checker_task and not _alert_checker_task.done():
        _alert_checker_task.cancel()
        _alert_checker_task = None
