"""
Unit tests for ReconciliationService — four-way gallon variance reconciliation.

Covers Requirement 4.4.1 (ReconciliationRecord schema matches the
``mvp_reconciliation`` ES mapping), 4.4.2 (``compute`` populates the record
from POD + order + loading plan at POD finalization), and 4.4.3
(``variance_exceeds_threshold`` alert flag fires when any variance exceeds
the tenant-configured ``variance_alert_pct`` — default 3.0%).

The Elasticsearch service and Redis client are faked in-process; no network
or AWS calls are made.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from services.reconciliation_service import (
    DEFAULT_VARIANCE_ALERT_PCT,
    MVP_RECONCILIATION_INDEX,
    ReconciliationRecord,
    ReconciliationService,
    VARIANCE_ALERT_FLAG,
    _derive_alert_flags,
    _percent_variance,
)
from compliance.services.vcf_calculator import VCFCalculator


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeES:
    """In-memory ES stub that records every ``index_document`` call."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Dict[str, Any]] = []

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any]):
        self.calls.append(
            {"index": index, "doc_id": doc_id, "document": dict(document)}
        )
        self.docs[doc_id] = dict(document)
        return {"result": "created"}


class _FakeRedis:
    """Minimal async Redis stub supporting ``get`` with bytes/str returns."""

    def __init__(self, values: Optional[Dict[str, Any]] = None) -> None:
        self._values = dict(values or {})
        self.get_calls: List[str] = []

    async def get(self, key: str):
        self.get_calls.append(key)
        return self._values.get(key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pod(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "pod_id": "pod-001",
        "tenant_id": "tenant-a",
        "order_id": "order-1",
        "plan_id": "plan-1",
        "delivered_gallons": 500.0,
    }
    base.update(overrides)
    return base


def _order(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "order_id": "order-1",
        "tenant_id": "tenant-a",
        "ordered_gallons": 500.0,
    }
    base.update(overrides)
    return base


def _plan(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "plan_id": "plan-1",
        "tenant_id": "tenant-a",
        "loaded_gallons": 500.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestReconciliationServiceConstruction:
    def test_requires_es_service(self):
        with pytest.raises(ValueError, match="es_service"):
            ReconciliationService(es_service=None)

    def test_rejects_negative_default_threshold(self):
        with pytest.raises(ValueError, match="default_variance_alert_pct"):
            ReconciliationService(es_service=_FakeES(), default_variance_alert_pct=-1.0)

    def test_default_threshold_is_three_percent(self):
        svc = ReconciliationService(es_service=_FakeES())
        # Requirement 4.4.3 — default 3.0%.
        assert DEFAULT_VARIANCE_ALERT_PCT == 3.0
        assert svc._default_threshold == 3.0


# ---------------------------------------------------------------------------
# Variance formula helpers
# ---------------------------------------------------------------------------


class TestPercentVariance:
    """Unit-tests for the pure formula helper.

    The service contract is ``variance = abs(a - b) / b * 100``.
    """

    def test_zero_variance_on_equal_values(self):
        assert _percent_variance(numerator=500.0, denominator=500.0) == 0.0

    def test_positive_variance_symmetric_in_sign(self):
        # Loaded 515 against Ordered 500 → 3% variance.
        assert _percent_variance(numerator=515.0, denominator=500.0) == pytest.approx(3.0)
        # Loaded 485 against Ordered 500 → 3% variance (abs).
        assert _percent_variance(numerator=485.0, denominator=500.0) == pytest.approx(3.0)

    def test_zero_denominator_with_zero_numerator_is_zero(self):
        assert _percent_variance(numerator=0.0, denominator=0.0) == 0.0

    def test_zero_denominator_with_positive_numerator_is_hundred(self):
        # A non-zero actual against a zero baseline is a 100% deviation.
        assert _percent_variance(numerator=12.5, denominator=0.0) == 100.0


class TestDeriveAlertFlags:
    def test_no_flag_when_all_variances_under_threshold(self):
        assert _derive_alert_flags(threshold=3.0, variances=(2.99, 1.0, None)) == []

    def test_no_flag_at_exact_threshold(self):
        # Requirement 4.4.3 uses "exceeds" — equality does not trigger.
        assert _derive_alert_flags(threshold=3.0, variances=(3.0, 3.0, 3.0)) == []

    def test_flag_when_any_variance_exceeds(self):
        assert _derive_alert_flags(
            threshold=3.0, variances=(1.0, 3.001, None)
        ) == [VARIANCE_ALERT_FLAG]

    def test_none_variances_are_ignored(self):
        assert _derive_alert_flags(threshold=3.0, variances=(None, None, None)) == []


# ---------------------------------------------------------------------------
# compute() happy path
# ---------------------------------------------------------------------------


class TestReconciliationServiceCompute:
    @pytest.mark.asyncio
    async def test_compute_returns_record_with_computed_variances(self):
        es = _FakeES()
        svc = ReconciliationService(es_service=es)

        record = await svc.compute(
            pod=_pod(delivered_gallons=500.0),
            order=_order(ordered_gallons=500.0),
            loading_plan=_plan(loaded_gallons=500.0),
        )

        assert isinstance(record, ReconciliationRecord)
        assert record.tenant_id == "tenant-a"
        assert record.pod_id == "pod-001"
        assert record.order_id == "order-1"
        assert record.plan_id == "plan-1"
        assert record.ordered_gallons == 500.0
        assert record.loaded_gallons == 500.0
        assert record.delivered_gallons == 500.0
        assert record.variance_load_vs_order_pct == 0.0
        assert record.variance_delivered_vs_loaded_pct == 0.0
        assert record.variance_invoiced_vs_delivered_pct is None
        assert record.invoice_id is None
        assert record.invoiced_gallons is None
        assert record.alert_flags == []
        # reconciliation_id must carry the tenant_id for operator-scanable IDs.
        assert re.fullmatch(
            r"rec-tenant-a-[0-9a-fA-F-]{36}", record.reconciliation_id
        )

    @pytest.mark.asyncio
    async def test_compute_computes_load_vs_order_variance(self):
        """Requirement 4.4 — variance_load_vs_order_pct = |loaded-ordered|/ordered*100."""
        svc = ReconciliationService(es_service=_FakeES())

        record = await svc.compute(
            pod=_pod(delivered_gallons=515.0),
            order=_order(ordered_gallons=500.0),
            loading_plan=_plan(loaded_gallons=515.0),
        )

        assert record.variance_load_vs_order_pct == pytest.approx(3.0)
        assert record.variance_delivered_vs_loaded_pct == 0.0

    @pytest.mark.asyncio
    async def test_compute_computes_delivered_vs_loaded_variance(self):
        svc = ReconciliationService(es_service=_FakeES())

        record = await svc.compute(
            pod=_pod(delivered_gallons=475.0),
            order=_order(ordered_gallons=500.0),
            loading_plan=_plan(loaded_gallons=500.0),
        )

        assert record.variance_load_vs_order_pct == 0.0
        # |475 - 500| / 500 * 100 = 5.0
        assert record.variance_delivered_vs_loaded_pct == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_compute_computes_invoiced_vs_delivered_when_invoice_supplied(self):
        svc = ReconciliationService(es_service=_FakeES())

        record = await svc.compute(
            pod=_pod(delivered_gallons=500.0),
            order=_order(
                ordered_gallons=500.0,
                invoice_id="INV-42",
                invoiced_gallons=505.0,
            ),
            loading_plan=_plan(loaded_gallons=500.0),
        )

        assert record.invoice_id == "INV-42"
        assert record.invoiced_gallons == 505.0
        # |505 - 500| / 500 * 100 = 1.0
        assert record.variance_invoiced_vs_delivered_pct == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_compute_uses_vcf_corrected_net_gallons_when_measurements_present(self):
        svc = ReconciliationService(es_service=_FakeES())
        calculator = VCFCalculator()
        expected_loaded = calculator.compute_net_gallons(
            gross_gallons=500.0,
            temperature_f=72.0,
            api_gravity=35.0,
        )

        record = await svc.compute(
            pod=_pod(
                delivered_gallons=expected_loaded,
                delivered_unit="gallons",
            ),
            order=_order(ordered_gallons=expected_loaded, ordered_unit="gal"),
            loading_plan=_plan(
                loaded_gallons=500.0,
                loaded_unit="gallons",
                loaded_temperature_f=72.0,
                loaded_api_gravity=35.0,
            ),
        )

        assert record.loaded_gallons == expected_loaded
        assert record.variance_load_vs_order_pct == 0.0
        assert record.variance_delivered_vs_loaded_pct == 0.0


# ---------------------------------------------------------------------------
# Alert-flag emission (Req 4.4.3)
# ---------------------------------------------------------------------------


class TestReconciliationServiceAlertFlags:
    @pytest.mark.asyncio
    async def test_emits_alert_when_load_variance_exceeds_default_threshold(self):
        svc = ReconciliationService(es_service=_FakeES())

        # 516 vs 500 = 3.2% > 3.0% default threshold
        record = await svc.compute(
            pod=_pod(delivered_gallons=516.0),
            order=_order(ordered_gallons=500.0),
            loading_plan=_plan(loaded_gallons=516.0),
        )

        assert record.variance_load_vs_order_pct == pytest.approx(3.2)
        assert VARIANCE_ALERT_FLAG in record.alert_flags

    @pytest.mark.asyncio
    async def test_emits_alert_when_delivered_variance_exceeds_default_threshold(self):
        svc = ReconciliationService(es_service=_FakeES())

        # delivered 480 vs loaded 500 = 4% > 3%
        record = await svc.compute(
            pod=_pod(delivered_gallons=480.0),
            order=_order(ordered_gallons=500.0),
            loading_plan=_plan(loaded_gallons=500.0),
        )

        assert record.variance_delivered_vs_loaded_pct == pytest.approx(4.0)
        assert record.alert_flags == [VARIANCE_ALERT_FLAG]

    @pytest.mark.asyncio
    async def test_emits_alert_when_invoice_variance_exceeds_default_threshold(self):
        svc = ReconciliationService(es_service=_FakeES())

        record = await svc.compute(
            pod=_pod(delivered_gallons=500.0),
            order=_order(
                ordered_gallons=500.0,
                invoice_id="INV-7",
                invoiced_gallons=530.0,  # 6% over delivered
            ),
            loading_plan=_plan(loaded_gallons=500.0),
        )

        assert record.variance_invoiced_vs_delivered_pct == pytest.approx(6.0)
        assert VARIANCE_ALERT_FLAG in record.alert_flags

    @pytest.mark.asyncio
    async def test_no_alert_at_exact_threshold(self):
        svc = ReconciliationService(es_service=_FakeES())

        record = await svc.compute(
            pod=_pod(delivered_gallons=515.0),
            order=_order(ordered_gallons=500.0),
            loading_plan=_plan(loaded_gallons=515.0),
        )

        assert record.variance_load_vs_order_pct == pytest.approx(3.0)
        assert record.alert_flags == []

    @pytest.mark.asyncio
    async def test_flag_emitted_only_once_when_multiple_variances_exceed(self):
        svc = ReconciliationService(es_service=_FakeES())

        # load 525/500 = 5%; delivered 470/525 = 10.47%
        record = await svc.compute(
            pod=_pod(delivered_gallons=470.0),
            order=_order(ordered_gallons=500.0),
            loading_plan=_plan(loaded_gallons=525.0),
        )

        assert record.alert_flags == [VARIANCE_ALERT_FLAG]


# ---------------------------------------------------------------------------
# Tenant-configurable threshold via Redis
# ---------------------------------------------------------------------------


class TestReconciliationServiceTenantThreshold:
    @pytest.mark.asyncio
    async def test_redis_override_raises_alert_threshold(self):
        """Tenant can relax the alert threshold (e.g. 5%) via Redis."""
        redis = _FakeRedis({"variance_alert_pct:tenant-a": "5.0"})
        svc = ReconciliationService(es_service=_FakeES(), redis_client=redis)

        # 4% variance: exceeds default 3% but not tenant-configured 5%.
        record = await svc.compute(
            pod=_pod(delivered_gallons=480.0),
            order=_order(ordered_gallons=500.0),
            loading_plan=_plan(loaded_gallons=500.0),
        )

        assert record.variance_delivered_vs_loaded_pct == pytest.approx(4.0)
        assert record.alert_flags == []
        assert redis.get_calls == ["variance_alert_pct:tenant-a"]

    @pytest.mark.asyncio
    async def test_redis_override_tightens_alert_threshold(self):
        redis = _FakeRedis({"variance_alert_pct:tenant-a": "1.0"})
        svc = ReconciliationService(es_service=_FakeES(), redis_client=redis)

        # 2% variance: below default 3% but above tenant-configured 1%.
        record = await svc.compute(
            pod=_pod(delivered_gallons=510.0),
            order=_order(ordered_gallons=500.0),
            loading_plan=_plan(loaded_gallons=510.0),
        )

        assert record.variance_load_vs_order_pct == pytest.approx(2.0)
        assert record.alert_flags == [VARIANCE_ALERT_FLAG]

    @pytest.mark.asyncio
    async def test_malformed_redis_value_falls_back_to_default(self):
        redis = _FakeRedis({"variance_alert_pct:tenant-a": "not-a-number"})
        svc = ReconciliationService(es_service=_FakeES(), redis_client=redis)

        # 4% — would not trigger at 5%, triggers at default 3%.
        record = await svc.compute(
            pod=_pod(delivered_gallons=480.0),
            order=_order(ordered_gallons=500.0),
            loading_plan=_plan(loaded_gallons=500.0),
        )

        assert record.alert_flags == [VARIANCE_ALERT_FLAG]

    @pytest.mark.asyncio
    async def test_redis_bytes_value_is_decoded(self):
        redis = _FakeRedis({"variance_alert_pct:tenant-a": b"5.0"})
        svc = ReconciliationService(es_service=_FakeES(), redis_client=redis)

        record = await svc.compute(
            pod=_pod(delivered_gallons=480.0),  # 4% variance
            order=_order(ordered_gallons=500.0),
            loading_plan=_plan(loaded_gallons=500.0),
        )

        assert record.alert_flags == []

    @pytest.mark.asyncio
    async def test_missing_redis_override_uses_default(self):
        redis = _FakeRedis()  # empty
        svc = ReconciliationService(es_service=_FakeES(), redis_client=redis)

        record = await svc.compute(
            pod=_pod(delivered_gallons=480.0),  # 4% variance > 3% default
            order=_order(ordered_gallons=500.0),
            loading_plan=_plan(loaded_gallons=500.0),
        )

        assert record.alert_flags == [VARIANCE_ALERT_FLAG]


# ---------------------------------------------------------------------------
# Persistence (Req 4.4.1 — writes to mvp_reconciliation)
# ---------------------------------------------------------------------------


class TestReconciliationServicePersistence:
    @pytest.mark.asyncio
    async def test_persists_to_mvp_reconciliation_index(self):
        es = _FakeES()
        svc = ReconciliationService(es_service=es)

        record = await svc.compute(
            pod=_pod(),
            order=_order(),
            loading_plan=_plan(),
        )

        assert len(es.calls) == 1
        call = es.calls[0]
        assert call["index"] == MVP_RECONCILIATION_INDEX
        assert call["doc_id"] == record.reconciliation_id

    @pytest.mark.asyncio
    async def test_persisted_document_carries_all_schema_fields(self):
        """Requirement 4.4.1 — document matches the ES mapping field list."""
        es = _FakeES()
        svc = ReconciliationService(es_service=es)

        record = await svc.compute(
            pod=_pod(delivered_gallons=510.0),
            order=_order(
                ordered_gallons=500.0,
                invoice_id="INV-9",
                invoiced_gallons=515.0,
            ),
            loading_plan=_plan(loaded_gallons=510.0),
        )

        body = es.calls[0]["document"]
        for field in (
            "reconciliation_id",
            "tenant_id",
            "order_id",
            "plan_id",
            "pod_id",
            "invoice_id",
            "ordered_gallons",
            "loaded_gallons",
            "delivered_gallons",
            "invoiced_gallons",
            "variance_load_vs_order_pct",
            "variance_delivered_vs_loaded_pct",
            "variance_invoiced_vs_delivered_pct",
            "alert_flags",
            "generated_at",
            "created_at",
            "updated_at",
        ):
            assert field in body, f"missing field {field} on persisted document"

        assert body["tenant_id"] == record.tenant_id
        assert body["variance_load_vs_order_pct"] == pytest.approx(2.0)
        assert body["invoice_id"] == "INV-9"
        assert body["invoiced_gallons"] == 515.0
        # Persisted timestamps echo generated_at so downstream range queries
        # work uniformly across Capability-4 artifacts.
        assert body["created_at"] == body["generated_at"]
        assert body["updated_at"] == body["generated_at"]
        # generated_at is an ISO-8601 string (serialization contract).
        parsed = datetime.fromisoformat(body["generated_at"])
        assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# Input validation (guardrails around 4.4.2)
# ---------------------------------------------------------------------------


class TestReconciliationServiceValidation:
    @pytest.mark.asyncio
    async def test_missing_pod_tenant_id_raises(self):
        svc = ReconciliationService(es_service=_FakeES())
        with pytest.raises(ValueError, match="tenant_id"):
            await svc.compute(
                pod=_pod(tenant_id=None),
                order=_order(),
                loading_plan=_plan(),
            )

    @pytest.mark.asyncio
    async def test_cross_tenant_inputs_rejected(self):
        svc = ReconciliationService(es_service=_FakeES())
        with pytest.raises(ValueError, match="tenant_id"):
            await svc.compute(
                pod=_pod(tenant_id="tenant-a"),
                order=_order(tenant_id="tenant-b"),
                loading_plan=_plan(tenant_id="tenant-a"),
            )

    @pytest.mark.asyncio
    async def test_missing_ordered_gallons_raises(self):
        svc = ReconciliationService(es_service=_FakeES())
        order = _order()
        del order["ordered_gallons"]
        with pytest.raises(ValueError, match="ordered_gallons"):
            await svc.compute(pod=_pod(), order=order, loading_plan=_plan())

    @pytest.mark.asyncio
    async def test_negative_delivered_gallons_raises(self):
        svc = ReconciliationService(es_service=_FakeES())
        with pytest.raises(ValueError, match="delivered_gallons"):
            await svc.compute(
                pod=_pod(delivered_gallons=-1.0),
                order=_order(),
                loading_plan=_plan(),
            )

    @pytest.mark.asyncio
    async def test_non_numeric_loaded_gallons_raises(self):
        svc = ReconciliationService(es_service=_FakeES())
        with pytest.raises(ValueError, match="loaded_gallons"):
            await svc.compute(
                pod=_pod(),
                order=_order(),
                loading_plan=_plan(loaded_gallons="not-a-number"),
            )

    @pytest.mark.asyncio
    async def test_missing_plan_id_falls_back_to_pod_plan_id(self):
        svc = ReconciliationService(es_service=_FakeES())
        plan = _plan()
        del plan["plan_id"]
        record = await svc.compute(
            pod=_pod(plan_id="plan-from-pod"),
            order=_order(),
            loading_plan=plan,
        )
        assert record.plan_id == "plan-from-pod"

    @pytest.mark.asyncio
    async def test_invoice_id_without_gallons_is_dropped(self):
        """An invoice_id without an invoiced_gallons value can't produce a
        valid variance; the service records it as absent rather than
        persisting an orphan id."""
        svc = ReconciliationService(es_service=_FakeES())
        record = await svc.compute(
            pod=_pod(),
            order=_order(invoice_id="INV-77"),  # no invoiced_gallons
            loading_plan=_plan(),
        )
        assert record.invoice_id is None
        assert record.invoiced_gallons is None
        assert record.variance_invoiced_vs_delivered_pct is None

    @pytest.mark.asyncio
    async def test_explicit_liter_unit_rejected_before_variance_calculation(self):
        svc = ReconciliationService(es_service=_FakeES())
        with pytest.raises(ValueError, match="gallons"):
            await svc.compute(
                pod=_pod(delivered_gallons=500.0),
                order=_order(ordered_gallons=1892.7, ordered_unit="liters"),
                loading_plan=_plan(loaded_gallons=500.0),
            )

    @pytest.mark.asyncio
    async def test_partial_vcf_metadata_is_rejected(self):
        svc = ReconciliationService(es_service=_FakeES())
        with pytest.raises(ValueError, match="temperature_f and api_gravity"):
            await svc.compute(
                pod=_pod(delivered_gallons=500.0),
                order=_order(ordered_gallons=500.0),
                loading_plan=_plan(
                    loaded_gallons=500.0,
                    loaded_temperature_f=72.0,
                ),
            )
