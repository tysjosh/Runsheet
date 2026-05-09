"""
Tests for commerce metrics instrumentation (Phase 14.4).

Asserts that every metric defined in design §10 is emitted correctly
with the expected labels and values.
"""

import pytest
from commerce.services.commerce_metrics import CommerceMetrics, capture_metrics


class TestPricingMetrics:
    """Tests for pricing-related metrics."""

    def test_pricing_resolve_total_cache_hit(self):
        with capture_metrics() as captured:
            CommerceMetrics.pricing_resolve_total("tenant-1", "cache_hit")

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.pricing.resolve_total"
        assert captured[0]["value"] == 1
        assert captured[0]["labels"]["tenant_id"] == "tenant-1"
        assert captured[0]["labels"]["outcome"] == "cache_hit"

    def test_pricing_resolve_total_cache_miss(self):
        with capture_metrics() as captured:
            CommerceMetrics.pricing_resolve_total("tenant-2", "cache_miss")

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.pricing.resolve_total"
        assert captured[0]["labels"]["outcome"] == "cache_miss"

    def test_pricing_resolve_total_no_rule(self):
        with capture_metrics() as captured:
            CommerceMetrics.pricing_resolve_total("tenant-1", "no_rule")

        assert len(captured) == 1
        assert captured[0]["labels"]["outcome"] == "no_rule"

    def test_pricing_resolve_total_error(self):
        with capture_metrics() as captured:
            CommerceMetrics.pricing_resolve_total("tenant-1", "error")

        assert len(captured) == 1
        assert captured[0]["labels"]["outcome"] == "error"

    def test_pricing_cache_hit_ratio(self):
        with capture_metrics() as captured:
            CommerceMetrics.pricing_cache_hit_ratio("tenant-1", 0.85)

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.pricing.cache_hit_ratio"
        assert captured[0]["value"] == 0.85
        assert captured[0]["labels"]["tenant_id"] == "tenant-1"


class TestCreditMetrics:
    """Tests for credit-related metrics."""

    def test_credit_hold_triggered_total(self):
        with capture_metrics() as captured:
            CommerceMetrics.credit_hold_triggered_total("tenant-1")

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.credit.hold_triggered_total"
        assert captured[0]["value"] == 1
        assert captured[0]["labels"]["tenant_id"] == "tenant-1"

    def test_credit_override_applied_total(self):
        with capture_metrics() as captured:
            CommerceMetrics.credit_override_applied_total("tenant-1")

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.credit.override_applied_total"
        assert captured[0]["value"] == 1
        assert captured[0]["labels"]["tenant_id"] == "tenant-1"


class TestInvoiceMetrics:
    """Tests for invoice-related metrics."""

    def test_invoice_generated_total_order_delivered(self):
        with capture_metrics() as captured:
            CommerceMetrics.invoice_generated_total("tenant-1", "order_delivered")

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.invoice.generated_total"
        assert captured[0]["value"] == 1
        assert captured[0]["labels"]["tenant_id"] == "tenant-1"
        assert captured[0]["labels"]["source"] == "order_delivered"

    def test_invoice_generated_total_manual(self):
        with capture_metrics() as captured:
            CommerceMetrics.invoice_generated_total("tenant-1", "manual")

        assert len(captured) == 1
        assert captured[0]["labels"]["source"] == "manual"

    def test_invoice_qbo_push_total_success(self):
        with capture_metrics() as captured:
            CommerceMetrics.invoice_qbo_push_total("tenant-1", "success")

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.invoice.qbo_push_total"
        assert captured[0]["value"] == 1
        assert captured[0]["labels"]["tenant_id"] == "tenant-1"
        assert captured[0]["labels"]["outcome"] == "success"

    def test_invoice_qbo_push_total_failure(self):
        with capture_metrics() as captured:
            CommerceMetrics.invoice_qbo_push_total("tenant-1", "failure")

        assert len(captured) == 1
        assert captured[0]["labels"]["outcome"] == "failure"

    def test_invoice_state_duration_seconds(self):
        with capture_metrics() as captured:
            CommerceMetrics.invoice_state_duration_seconds(
                "tenant-1", "draft", "open", 12.5
            )

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.invoice.state_duration_seconds"
        assert captured[0]["value"] == 12.5
        assert captured[0]["labels"]["tenant_id"] == "tenant-1"
        assert captured[0]["labels"]["from_state"] == "draft"
        assert captured[0]["labels"]["to_state"] == "open"


class TestPaymentMetrics:
    """Tests for payment-related metrics."""

    def test_payment_ingested_total(self):
        with capture_metrics() as captured:
            CommerceMetrics.payment_ingested_total("tenant-1", "stripe", "credit_card")

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.payment.ingested_total"
        assert captured[0]["value"] == 1
        assert captured[0]["labels"]["tenant_id"] == "tenant-1"
        assert captured[0]["labels"]["source"] == "stripe"
        assert captured[0]["labels"]["method"] == "credit_card"

    def test_payment_ingested_total_manual_check(self):
        with capture_metrics() as captured:
            CommerceMetrics.payment_ingested_total("tenant-1", "manual", "check")

        assert len(captured) == 1
        assert captured[0]["labels"]["source"] == "manual"
        assert captured[0]["labels"]["method"] == "check"

    def test_payment_idempotent_duplicate_total(self):
        with capture_metrics() as captured:
            CommerceMetrics.payment_idempotent_duplicate_total("tenant-1", "qbo")

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.payment.idempotent_duplicate_total"
        assert captured[0]["value"] == 1
        assert captured[0]["labels"]["tenant_id"] == "tenant-1"
        assert captured[0]["labels"]["source"] == "qbo"


class TestARMetrics:
    """Tests for AR aging metrics."""

    def test_ar_open_balance_cents(self):
        with capture_metrics() as captured:
            CommerceMetrics.ar_open_balance_cents("tenant-1", 1_500_000)

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.ar.open_balance_cents"
        assert captured[0]["value"] == 1_500_000
        assert captured[0]["labels"]["tenant_id"] == "tenant-1"

    def test_ar_bucket_90_plus_cents(self):
        with capture_metrics() as captured:
            CommerceMetrics.ar_bucket_90_plus_cents("tenant-1", 250_000)

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.ar.bucket_90_plus_cents"
        assert captured[0]["value"] == 250_000
        assert captured[0]["labels"]["tenant_id"] == "tenant-1"


class TestDunningMetrics:
    """Tests for dunning metrics."""

    def test_dunning_notification_queued_total_7_days(self):
        with capture_metrics() as captured:
            CommerceMetrics.dunning_notification_queued_total("tenant-1", 7)

        assert len(captured) == 1
        assert captured[0]["metric"] == "commerce.dunning.notification_queued_total"
        assert captured[0]["value"] == 1
        assert captured[0]["labels"]["tenant_id"] == "tenant-1"
        assert captured[0]["labels"]["threshold_days"] == 7

    def test_dunning_notification_queued_total_30_days(self):
        with capture_metrics() as captured:
            CommerceMetrics.dunning_notification_queued_total("tenant-1", 30)

        assert len(captured) == 1
        assert captured[0]["labels"]["threshold_days"] == 30


class TestMetricCompleteness:
    """
    Integration-style test asserting every metric from design §10 is
    emitted at least once from a simulated end-to-end flow.
    """

    EXPECTED_METRICS = [
        "commerce.pricing.resolve_total",
        "commerce.pricing.cache_hit_ratio",
        "commerce.credit.hold_triggered_total",
        "commerce.credit.override_applied_total",
        "commerce.invoice.generated_total",
        "commerce.invoice.qbo_push_total",
        "commerce.invoice.state_duration_seconds",
        "commerce.payment.ingested_total",
        "commerce.payment.idempotent_duplicate_total",
        "commerce.ar.open_balance_cents",
        "commerce.ar.bucket_90_plus_cents",
        "commerce.dunning.notification_queued_total",
    ]

    def test_all_metrics_emitted_in_e2e_flow(self):
        """
        Simulate a complete commerce flow and verify every metric fires.

        Flow: pricing resolve → credit check → invoice generation →
        QBO push → payment ingestion → AR update → dunning check.
        """
        with capture_metrics() as captured:
            # 1. Pricing resolution
            CommerceMetrics.pricing_resolve_total("tenant-e2e", "cache_hit")
            CommerceMetrics.pricing_resolve_total("tenant-e2e", "cache_miss")
            CommerceMetrics.pricing_cache_hit_ratio("tenant-e2e", 0.75)

            # 2. Credit check
            CommerceMetrics.credit_hold_triggered_total("tenant-e2e")
            CommerceMetrics.credit_override_applied_total("tenant-e2e")

            # 3. Invoice generation
            CommerceMetrics.invoice_generated_total("tenant-e2e", "order_delivered")
            CommerceMetrics.invoice_state_duration_seconds(
                "tenant-e2e", "draft", "open", 2.3
            )

            # 4. QBO push
            CommerceMetrics.invoice_qbo_push_total("tenant-e2e", "success")

            # 5. Payment ingestion
            CommerceMetrics.payment_ingested_total("tenant-e2e", "stripe", "ach")
            CommerceMetrics.payment_idempotent_duplicate_total("tenant-e2e", "stripe")

            # 6. AR update
            CommerceMetrics.ar_open_balance_cents("tenant-e2e", 500_000)
            CommerceMetrics.ar_bucket_90_plus_cents("tenant-e2e", 100_000)

            # 7. Dunning
            CommerceMetrics.dunning_notification_queued_total("tenant-e2e", 14)

        emitted_metric_names = {m["metric"] for m in captured}

        for expected in self.EXPECTED_METRICS:
            assert expected in emitted_metric_names, (
                f"Metric '{expected}' was NOT emitted during the end-to-end flow. "
                f"Emitted: {sorted(emitted_metric_names)}"
            )

    def test_all_metrics_have_tenant_id_label(self):
        """Every commerce metric must carry a tenant_id label."""
        with capture_metrics() as captured:
            CommerceMetrics.pricing_resolve_total("t1", "cache_hit")
            CommerceMetrics.pricing_cache_hit_ratio("t1", 0.5)
            CommerceMetrics.credit_hold_triggered_total("t1")
            CommerceMetrics.credit_override_applied_total("t1")
            CommerceMetrics.invoice_generated_total("t1", "manual")
            CommerceMetrics.invoice_qbo_push_total("t1", "success")
            CommerceMetrics.invoice_state_duration_seconds("t1", "open", "paid", 1.0)
            CommerceMetrics.payment_ingested_total("t1", "manual", "check")
            CommerceMetrics.payment_idempotent_duplicate_total("t1", "manual")
            CommerceMetrics.ar_open_balance_cents("t1", 100)
            CommerceMetrics.ar_bucket_90_plus_cents("t1", 50)
            CommerceMetrics.dunning_notification_queued_total("t1", 7)

        for metric in captured:
            assert "tenant_id" in metric["labels"], (
                f"Metric '{metric['metric']}' is missing tenant_id label"
            )
            assert metric["labels"]["tenant_id"] == "t1"

    def test_all_metrics_have_timestamp(self):
        """Every emitted metric must carry a timestamp."""
        with capture_metrics() as captured:
            CommerceMetrics.pricing_resolve_total("t1", "cache_hit")
            CommerceMetrics.credit_hold_triggered_total("t1")
            CommerceMetrics.invoice_generated_total("t1", "manual")

        for metric in captured:
            assert "ts" in metric
            assert isinstance(metric["ts"], float)
            assert metric["ts"] > 0
