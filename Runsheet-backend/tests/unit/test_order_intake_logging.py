"""
Unit tests for the Order Intake Pipeline structured logging helper.

Asserts that every structured log line emitted by the intake pipeline
includes the required fields: ``tenant_id``, ``intake_channel_id``,
``order_id`` (when known), ``event_id``, ``trace_id``, ``request_id``.

Validates: Requirements 9.2.4, 10.2.1.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from telemetry.intake_logging import IntakeLogContext, IntakeLogger, intake_logger
from telemetry.service import JSONFormatter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def full_context() -> IntakeLogContext:
    """A fully populated intake log context."""
    return IntakeLogContext(
        tenant_id="tenant-abc",
        intake_channel_id="voice-ai-prod",
        order_id="ord_abc123def456",
        event_id="evt_xyz789000111",
        trace_id="trace-001",
        request_id="req-002",
    )


@pytest.fixture
def partial_context() -> IntakeLogContext:
    """A context without order_id and event_id (early pipeline stage)."""
    return IntakeLogContext(
        tenant_id="tenant-xyz",
        intake_channel_id="dispatcher-main",
        trace_id="trace-999",
        request_id="req-888",
    )


@pytest.fixture
def json_handler() -> logging.Handler:
    """A handler with JSONFormatter that captures output."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    return handler


# ---------------------------------------------------------------------------
# Tests — IntakeLogContext
# ---------------------------------------------------------------------------


class TestIntakeLogContext:
    def test_as_extra_data_full_context(self, full_context: IntakeLogContext):
        """All six fields are present when fully populated."""
        data = full_context.as_extra_data()
        assert data["tenant_id"] == "tenant-abc"
        assert data["intake_channel_id"] == "voice-ai-prod"
        assert data["order_id"] == "ord_abc123def456"
        assert data["event_id"] == "evt_xyz789000111"
        assert data["trace_id"] == "trace-001"
        assert data["request_id"] == "req-002"

    def test_as_extra_data_partial_context(self, partial_context: IntakeLogContext):
        """order_id and event_id are omitted when None."""
        data = partial_context.as_extra_data()
        assert data["tenant_id"] == "tenant-xyz"
        assert data["intake_channel_id"] == "dispatcher-main"
        assert data["trace_id"] == "trace-999"
        assert data["request_id"] == "req-888"
        assert "order_id" not in data
        assert "event_id" not in data

    def test_as_extra_data_updates_after_mutation(self, partial_context: IntakeLogContext):
        """After setting order_id, it appears in subsequent calls."""
        partial_context.order_id = "ord_new123"
        data = partial_context.as_extra_data()
        assert data["order_id"] == "ord_new123"

    def test_as_extra_data_event_id_appears_after_set(self, partial_context: IntakeLogContext):
        """After setting event_id, it appears in subsequent calls."""
        partial_context.event_id = "evt_new456"
        data = partial_context.as_extra_data()
        assert data["event_id"] == "evt_new456"


# ---------------------------------------------------------------------------
# Tests — IntakeLogger
# ---------------------------------------------------------------------------


class TestIntakeLogger:
    def test_info_includes_all_context_fields(self, full_context: IntakeLogContext):
        """An info log line includes all six context fields."""
        log = intake_logger("test.intake", full_context)
        with patch.object(log._logger, "info") as mock_info:
            log.info("Order received")
            mock_info.assert_called_once()
            _, kwargs = mock_info.call_args
            extra_data = kwargs["extra"]["extra_data"]
            assert extra_data["tenant_id"] == "tenant-abc"
            assert extra_data["intake_channel_id"] == "voice-ai-prod"
            assert extra_data["order_id"] == "ord_abc123def456"
            assert extra_data["event_id"] == "evt_xyz789000111"
            assert extra_data["trace_id"] == "trace-001"
            assert extra_data["request_id"] == "req-002"

    def test_warning_includes_context_fields(self, full_context: IntakeLogContext):
        """A warning log line includes all context fields."""
        log = intake_logger("test.intake", full_context)
        with patch.object(log._logger, "warning") as mock_warn:
            log.warning("Dual-write failed")
            mock_warn.assert_called_once()
            _, kwargs = mock_warn.call_args
            extra_data = kwargs["extra"]["extra_data"]
            assert extra_data["tenant_id"] == "tenant-abc"
            assert extra_data["trace_id"] == "trace-001"

    def test_error_includes_context_fields(self, partial_context: IntakeLogContext):
        """An error log line includes context fields even without order_id."""
        log = intake_logger("test.intake", partial_context)
        with patch.object(log._logger, "error") as mock_err:
            log.error("Adapter failed")
            mock_err.assert_called_once()
            _, kwargs = mock_err.call_args
            extra_data = kwargs["extra"]["extra_data"]
            assert extra_data["tenant_id"] == "tenant-xyz"
            assert extra_data["intake_channel_id"] == "dispatcher-main"
            assert "order_id" not in extra_data

    def test_debug_includes_context_fields(self, full_context: IntakeLogContext):
        """A debug log line includes all context fields."""
        log = intake_logger("test.intake", full_context)
        with patch.object(log._logger, "debug") as mock_debug:
            log.debug("Idempotency check passed")
            mock_debug.assert_called_once()
            _, kwargs = mock_debug.call_args
            extra_data = kwargs["extra"]["extra_data"]
            assert extra_data["tenant_id"] == "tenant-abc"
            assert extra_data["request_id"] == "req-002"

    def test_extra_data_merges_with_caller_supplied_fields(self, full_context: IntakeLogContext):
        """Caller-supplied extra fields are merged with context fields."""
        log = intake_logger("test.intake", full_context)
        with patch.object(log._logger, "info") as mock_info:
            log.info("Custom event", extra={"custom_field": "value123"})
            mock_info.assert_called_once()
            _, kwargs = mock_info.call_args
            extra_data = kwargs["extra"]["extra_data"]
            assert extra_data["custom_field"] == "value123"
            assert extra_data["tenant_id"] == "tenant-abc"

    def test_context_property_returns_bound_context(self, full_context: IntakeLogContext):
        """The context property returns the bound IntakeLogContext."""
        log = intake_logger("test.intake", full_context)
        assert log.context is full_context

    def test_exception_includes_context_fields(self, full_context: IntakeLogContext):
        """An exception log line includes all context fields."""
        log = intake_logger("test.intake", full_context)
        with patch.object(log._logger, "exception") as mock_exc:
            log.exception("Unexpected error")
            mock_exc.assert_called_once()
            _, kwargs = mock_exc.call_args
            extra_data = kwargs["extra"]["extra_data"]
            assert extra_data["tenant_id"] == "tenant-abc"
            assert extra_data["order_id"] == "ord_abc123def456"


# ---------------------------------------------------------------------------
# Tests — JSONFormatter integration
# ---------------------------------------------------------------------------


class TestJSONFormatterIntegration:
    """Verify that IntakeLogger output is correctly formatted by JSONFormatter."""

    def test_json_output_contains_context_fields(self, full_context: IntakeLogContext):
        """JSONFormatter produces JSON with all intake context fields."""
        formatter = JSONFormatter()
        logger_instance = logging.getLogger("test.json_integration")
        logger_instance.setLevel(logging.DEBUG)

        log = IntakeLogger(logger_instance, full_context)

        # Create a log record manually
        record = logging.LogRecord(
            name="test.json_integration",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Order persisted",
            args=(),
            exc_info=None,
        )
        record.extra_data = full_context.as_extra_data()

        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["tenant_id"] == "tenant-abc"
        assert parsed["intake_channel_id"] == "voice-ai-prod"
        assert parsed["order_id"] == "ord_abc123def456"
        assert parsed["event_id"] == "evt_xyz789000111"
        assert parsed["trace_id"] == "trace-001"
        assert parsed["request_id"] == "req-002"
        assert parsed["message"] == "Order persisted"

    def test_json_output_omits_none_fields(self, partial_context: IntakeLogContext):
        """JSONFormatter output omits order_id/event_id when not set."""
        formatter = JSONFormatter()

        record = logging.LogRecord(
            name="test.json_integration",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="Channel resolution",
            args=(),
            exc_info=None,
        )
        record.extra_data = partial_context.as_extra_data()

        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["tenant_id"] == "tenant-xyz"
        assert parsed["intake_channel_id"] == "dispatcher-main"
        assert "order_id" not in parsed
        assert "event_id" not in parsed


# ---------------------------------------------------------------------------
# Tests — Pipeline code path coverage
# ---------------------------------------------------------------------------


class TestPipelineCodePaths:
    """Verify that the intake pipeline's key code paths would include
    the required fields when using IntakeLogContext."""

    def test_webhook_path_context_has_all_fields(self):
        """Webhook path: after channel resolution + order creation."""
        ctx = IntakeLogContext(
            tenant_id="t-webhook",
            intake_channel_id="partner-edi-01",
            trace_id="trace-wh-001",
            request_id="req-wh-001",
        )
        # Simulate pipeline progression
        ctx.event_id = "evt_wh_abc"
        ctx.order_id = "ord_wh_xyz"

        data = ctx.as_extra_data()
        required = {"tenant_id", "intake_channel_id", "order_id", "event_id", "trace_id", "request_id"}
        assert required.issubset(data.keys())

    def test_dispatcher_path_context_has_all_fields(self):
        """Dispatcher path: after JWT auth + order creation."""
        ctx = IntakeLogContext(
            tenant_id="t-dispatch",
            intake_channel_id="dispatcher-main",
            trace_id="trace-dp-001",
            request_id="req-dp-001",
        )
        ctx.event_id = "evt_dp_abc"
        ctx.order_id = "ord_dp_xyz"

        data = ctx.as_extra_data()
        required = {"tenant_id", "intake_channel_id", "order_id", "event_id", "trace_id", "request_id"}
        assert required.issubset(data.keys())

    def test_poison_queue_path_context_omits_order_id(self):
        """Poison queue path: adapter failure before order creation."""
        ctx = IntakeLogContext(
            tenant_id="t-poison",
            intake_channel_id="csv-upload",
            trace_id="trace-pq-001",
            request_id="req-pq-001",
        )
        ctx.event_id = "evt_pq_abc"
        # order_id stays None — adapter failed before minting

        data = ctx.as_extra_data()
        assert data["tenant_id"] == "t-poison"
        assert data["intake_channel_id"] == "csv-upload"
        assert data["event_id"] == "evt_pq_abc"
        assert "order_id" not in data

    def test_idempotency_duplicate_path_context(self):
        """Duplicate detection path: event_id known, order_id not set."""
        ctx = IntakeLogContext(
            tenant_id="t-dup",
            intake_channel_id="voice-ai",
            trace_id="trace-dup-001",
            request_id="req-dup-001",
        )
        ctx.event_id = "evt_dup_existing"

        data = ctx.as_extra_data()
        assert data["tenant_id"] == "t-dup"
        assert data["event_id"] == "evt_dup_existing"
        assert "order_id" not in data

    def test_legacy_dual_write_path_context(self):
        """Legacy dual-write path: all fields populated."""
        ctx = IntakeLogContext(
            tenant_id="t-legacy",
            intake_channel_id="dinee-legacy",
            order_id="ord_legacy_001",
            event_id="evt_legacy_001",
            trace_id="trace-leg-001",
            request_id="req-leg-001",
        )

        data = ctx.as_extra_data()
        required = {"tenant_id", "intake_channel_id", "order_id", "event_id", "trace_id", "request_id"}
        assert required.issubset(data.keys())
        assert data["intake_channel_id"] == "dinee-legacy"
