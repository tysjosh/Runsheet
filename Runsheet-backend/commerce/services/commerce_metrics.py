"""
Commerce Backbone — Metrics Instrumentation (Design §10)

Provides structured metric emission for all commerce services.
Each metric follows the naming convention: commerce.<domain>.<metric_name>{labels}

Metrics are emitted via structured logging (JSON) for ingestion by
Prometheus/Datadog/CloudWatch via log-based metric extraction.
"""

import time
import logging
import json
from typing import Optional
from functools import wraps

logger = logging.getLogger("commerce.metrics")


class CommerceMetrics:
    """
    Centralized metrics helper for the commerce backbone.

    All metrics are emitted as structured JSON log lines with a consistent
    schema that metric pipelines can parse:

        {"metric": "<name>", "value": <number>, "labels": {...}, "ts": <epoch>}

    Counter metrics use value=1 per emission (increment).
    Gauge metrics use the actual value.
    Histogram metrics use the observed duration/amount.
    """

    # --- Pricing metrics ---

    @staticmethod
    def pricing_resolve_total(tenant_id: str, outcome: str) -> None:
        """
        Counter: commerce.pricing.resolve_total{tenant_id, outcome}
        Outcome is one of: cache_hit, cache_miss, no_rule, error
        """
        _emit_metric("commerce.pricing.resolve_total", 1, {
            "tenant_id": tenant_id,
            "outcome": outcome,
        })

    @staticmethod
    def pricing_cache_hit_ratio(tenant_id: str, ratio: float) -> None:
        """
        Gauge: commerce.pricing.cache_hit_ratio{tenant_id}
        Value between 0.0 and 1.0 representing the rolling cache hit ratio.
        """
        _emit_metric("commerce.pricing.cache_hit_ratio", ratio, {
            "tenant_id": tenant_id,
        })

    # --- Credit metrics ---

    @staticmethod
    def credit_hold_triggered_total(tenant_id: str) -> None:
        """
        Counter: commerce.credit.hold_triggered_total{tenant_id}
        Incremented each time a credit hold is placed on an account.
        """
        _emit_metric("commerce.credit.hold_triggered_total", 1, {
            "tenant_id": tenant_id,
        })

    @staticmethod
    def credit_override_applied_total(tenant_id: str) -> None:
        """
        Counter: commerce.credit.override_applied_total{tenant_id}
        Incremented each time a credit override is applied.
        """
        _emit_metric("commerce.credit.override_applied_total", 1, {
            "tenant_id": tenant_id,
        })

    # --- Invoice metrics ---

    @staticmethod
    def invoice_generated_total(tenant_id: str, source: str) -> None:
        """
        Counter: commerce.invoice.generated_total{tenant_id, source}
        Source is one of: order_delivered, manual, backfill
        """
        _emit_metric("commerce.invoice.generated_total", 1, {
            "tenant_id": tenant_id,
            "source": source,
        })

    @staticmethod
    def invoice_qbo_push_total(tenant_id: str, outcome: str) -> None:
        """
        Counter: commerce.invoice.qbo_push_total{tenant_id, outcome}
        Outcome is one of: success, failure, retry
        """
        _emit_metric("commerce.invoice.qbo_push_total", 1, {
            "tenant_id": tenant_id,
            "outcome": outcome,
        })

    @staticmethod
    def invoice_state_duration_seconds(
        tenant_id: str, from_state: str, to_state: str, duration_seconds: float
    ) -> None:
        """
        Histogram: commerce.invoice.state_duration_seconds{tenant_id, from_state, to_state}
        Measures time spent in each invoice state before transitioning.
        """
        _emit_metric("commerce.invoice.state_duration_seconds", duration_seconds, {
            "tenant_id": tenant_id,
            "from_state": from_state,
            "to_state": to_state,
        })

    # --- Payment metrics ---

    @staticmethod
    def payment_ingested_total(tenant_id: str, source: str, method: str) -> None:
        """
        Counter: commerce.payment.ingested_total{tenant_id, source, method}
        Source: qbo, stripe, manual
        Method: ach, check, credit_card, wire, credit_balance
        """
        _emit_metric("commerce.payment.ingested_total", 1, {
            "tenant_id": tenant_id,
            "source": source,
            "method": method,
        })

    @staticmethod
    def payment_idempotent_duplicate_total(tenant_id: str, source: str) -> None:
        """
        Counter: commerce.payment.idempotent_duplicate_total{tenant_id, source}
        Incremented when a duplicate payment ingestion is detected and deduplicated.
        """
        _emit_metric("commerce.payment.idempotent_duplicate_total", 1, {
            "tenant_id": tenant_id,
            "source": source,
        })

    # --- AR metrics ---

    @staticmethod
    def ar_open_balance_cents(tenant_id: str, balance_cents: int) -> None:
        """
        Gauge: commerce.ar.open_balance_cents{tenant_id}
        Total open AR balance across all accounts for the tenant.
        """
        _emit_metric("commerce.ar.open_balance_cents", balance_cents, {
            "tenant_id": tenant_id,
        })

    @staticmethod
    def ar_bucket_90_plus_cents(tenant_id: str, amount_cents: int) -> None:
        """
        Gauge: commerce.ar.bucket_90_plus_cents{tenant_id}
        Total AR in the 90+ day aging bucket for the tenant.
        """
        _emit_metric("commerce.ar.bucket_90_plus_cents", amount_cents, {
            "tenant_id": tenant_id,
        })

    # --- Dunning metrics ---

    @staticmethod
    def dunning_notification_queued_total(tenant_id: str, threshold_days: int) -> None:
        """
        Counter: commerce.dunning.notification_queued_total{tenant_id, threshold_days}
        Incremented each time a dunning notification is queued.
        """
        _emit_metric("commerce.dunning.notification_queued_total", 1, {
            "tenant_id": tenant_id,
            "threshold_days": threshold_days,
        })


# --- Internal helpers ---

_metric_log: list = []
"""In-memory log for testing. Only populated when capture_metrics() context is active."""

_capturing: bool = False


class capture_metrics:
    """
    Context manager for capturing emitted metrics in tests.

    Usage:
        with capture_metrics() as captured:
            CommerceMetrics.pricing_resolve_total("t1", "cache_hit")
        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.pricing.resolve_total"
    """

    def __enter__(self):
        global _capturing, _metric_log
        _capturing = True
        _metric_log = []
        return _metric_log

    def __exit__(self, *args):
        global _capturing
        _capturing = False


def _emit_metric(name: str, value, labels: dict) -> None:
    """Emit a metric as a structured JSON log line."""
    record = {
        "metric": name,
        "value": value,
        "labels": labels,
        "ts": time.time(),
    }

    if _capturing:
        _metric_log.append(record)

    logger.info(json.dumps(record))


def metrics_timer(metric_name: str, tenant_id_arg: str = "tenant_id"):
    """
    Decorator that measures function execution time and emits it as a histogram metric.

    Usage:
        @metrics_timer("commerce.invoice.state_duration_seconds")
        def transition_invoice(self, tenant_id, invoice_id, ...):
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            result = func(*args, **kwargs)
            duration = time.time() - start
            # Try to extract tenant_id from kwargs or first positional arg
            tid = kwargs.get(tenant_id_arg, "unknown")
            if tid == "unknown" and len(args) > 1:
                tid = args[1] if isinstance(args[1], str) else "unknown"
            _emit_metric(metric_name, duration, {"tenant_id": tid})
            return result
        return wrapper
    return decorator
