"""
Unit tests for fuel.intake.adapter_base module.

Covers:
- IntakeContext dataclass construction (with and without optional fields)
- IntakeResult dataclass construction (default and explicit event_docs)
- IntakeAdapter Protocol structural subtyping
- IntakeAdapterRegistry register/get round-trip
- IntakeAdapterRegistry raises AdapterError(error_type="unknown_schema_version")
  for unknown (channel_type, schema_version) combinations
- AdapterError carries error_type attribute

Validates: Requirements 2.3.1, 2.3.2.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

import pytest

from fuel.intake.adapter_base import (
    AdapterError,
    IntakeAdapter,
    IntakeAdapterRegistry,
    IntakeContext,
    IntakeResult,
)
from fuel.intake_channel_models import IntakeChannel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_channel(**overrides) -> IntakeChannel:
    """Build a minimal IntakeChannel for testing."""
    defaults = {
        "channel_id": "test-channel-01",
        "tenant_id": "tenant_abc",
        "channel_type": "dispatcher",
        "display_name": "Test Channel",
        "hmac_secret_ref": "vault://intake_channel_hmac:test-channel-01",
        "supported_schema_versions": ["1.0"],
        "created_at": datetime(2026, 1, 1),
        "updated_at": datetime(2026, 1, 1),
    }
    defaults.update(overrides)
    return IntakeChannel(**defaults)


class _FakeAdapter:
    """Concrete adapter satisfying the IntakeAdapter protocol."""

    channel_type = "dispatcher"

    def transform(
        self, payload: Dict[str, Any], context: IntakeContext
    ) -> IntakeResult:
        return IntakeResult(order_doc={"from_fake": True})


# ---------------------------------------------------------------------------
# AdapterError tests
# ---------------------------------------------------------------------------


class TestAdapterError:
    def test_error_type_attribute(self):
        err = AdapterError(error_type="unknown_schema_version")
        assert err.error_type == "unknown_schema_version"

    def test_custom_message(self):
        err = AdapterError(
            error_type="adapter_output_invalid",
            message="field X is missing",
        )
        assert err.error_type == "adapter_output_invalid"
        assert "field X is missing" in str(err)

    def test_default_message_is_error_type(self):
        err = AdapterError(error_type="some_error")
        assert str(err) == "some_error"

    def test_is_exception(self):
        assert issubclass(AdapterError, Exception)


# ---------------------------------------------------------------------------
# IntakeContext tests
# ---------------------------------------------------------------------------


class TestIntakeContext:
    def test_all_fields(self):
        channel = _make_channel()
        ctx = IntakeContext(
            tenant_id="tenant_abc",
            channel=channel,
            trace_id="trace-001",
            request_id="req-002",
            actor_user_id="user-003",
        )
        assert ctx.tenant_id == "tenant_abc"
        assert ctx.channel is channel
        assert ctx.trace_id == "trace-001"
        assert ctx.request_id == "req-002"
        assert ctx.actor_user_id == "user-003"

    def test_actor_user_id_defaults_to_none(self):
        channel = _make_channel()
        ctx = IntakeContext(
            tenant_id="tenant_abc",
            channel=channel,
            trace_id="trace-001",
            request_id="req-002",
        )
        assert ctx.actor_user_id is None


# ---------------------------------------------------------------------------
# IntakeResult tests
# ---------------------------------------------------------------------------


class TestIntakeResult:
    def test_order_doc_required(self):
        result = IntakeResult(order_doc={"customer_id": "cust_1"})
        assert result.order_doc == {"customer_id": "cust_1"}

    def test_event_docs_defaults_to_empty_list(self):
        result = IntakeResult(order_doc={})
        assert result.event_docs == []

    def test_event_docs_explicit(self):
        events = [{"event_type": "order_placed"}]
        result = IntakeResult(order_doc={}, event_docs=events)
        assert result.event_docs == events
        assert len(result.event_docs) == 1

    def test_event_docs_default_is_independent_per_instance(self):
        """Each IntakeResult gets its own list — no shared mutable default."""
        r1 = IntakeResult(order_doc={})
        r2 = IntakeResult(order_doc={})
        r1.event_docs.append({"event_type": "test"})
        assert r2.event_docs == []


# ---------------------------------------------------------------------------
# IntakeAdapter Protocol tests
# ---------------------------------------------------------------------------


class TestIntakeAdapterProtocol:
    def test_fake_adapter_satisfies_protocol(self):
        """A class with channel_type and transform() is structurally compatible."""
        adapter: IntakeAdapter = _FakeAdapter()
        assert adapter.channel_type == "dispatcher"

    def test_transform_returns_intake_result(self):
        adapter = _FakeAdapter()
        channel = _make_channel()
        ctx = IntakeContext(
            tenant_id="tenant_abc",
            channel=channel,
            trace_id="t",
            request_id="r",
        )
        result = adapter.transform({"raw": "data"}, ctx)
        assert isinstance(result, IntakeResult)
        assert result.order_doc == {"from_fake": True}


# ---------------------------------------------------------------------------
# IntakeAdapterRegistry tests
# ---------------------------------------------------------------------------


class TestIntakeAdapterRegistry:
    def test_register_and_get(self):
        registry = IntakeAdapterRegistry()
        adapter = _FakeAdapter()
        registry.register(adapter, channel_type="dispatcher", schema_version="1.0")
        assert registry.get("dispatcher", "1.0") is adapter

    def test_get_unknown_raises_adapter_error(self):
        registry = IntakeAdapterRegistry()
        with pytest.raises(AdapterError) as exc_info:
            registry.get("voice", "1.0")
        assert exc_info.value.error_type == "unknown_schema_version"

    def test_get_unknown_schema_version_for_known_channel(self):
        """Known channel_type but unknown schema_version still raises."""
        registry = IntakeAdapterRegistry()
        adapter = _FakeAdapter()
        registry.register(adapter, channel_type="dispatcher", schema_version="1.0")
        with pytest.raises(AdapterError) as exc_info:
            registry.get("dispatcher", "2.0")
        assert exc_info.value.error_type == "unknown_schema_version"

    def test_multiple_schema_versions_coexist(self):
        registry = IntakeAdapterRegistry()
        v1 = _FakeAdapter()
        v2 = _FakeAdapter()
        registry.register(v1, channel_type="csv", schema_version="1.0")
        registry.register(v2, channel_type="csv", schema_version="2.0")
        assert registry.get("csv", "1.0") is v1
        assert registry.get("csv", "2.0") is v2

    def test_multiple_channel_types(self):
        registry = IntakeAdapterRegistry()
        dispatcher_adapter = _FakeAdapter()
        csv_adapter = _FakeAdapter()
        registry.register(
            dispatcher_adapter, channel_type="dispatcher", schema_version="1.0"
        )
        registry.register(csv_adapter, channel_type="csv", schema_version="1.0")
        assert registry.get("dispatcher", "1.0") is dispatcher_adapter
        assert registry.get("csv", "1.0") is csv_adapter

    def test_default_schema_version_is_1_0(self):
        """get() defaults schema_version to '1.0' when not specified."""
        registry = IntakeAdapterRegistry()
        adapter = _FakeAdapter()
        registry.register(adapter, channel_type="dispatcher", schema_version="1.0")
        assert registry.get("dispatcher") is adapter

    def test_register_overwrites_existing(self):
        """Re-registering the same key replaces the previous adapter."""
        registry = IntakeAdapterRegistry()
        old = _FakeAdapter()
        new = _FakeAdapter()
        registry.register(old, channel_type="voice", schema_version="1.0")
        registry.register(new, channel_type="voice", schema_version="1.0")
        assert registry.get("voice", "1.0") is new
