"""
Unit tests for :class:`driver.services.pod_bol_finalizer.PODBOLFinalizer`.

Validates Task 8.6 of the fuel-ops-hardening spec:

* Requirement 4.3.4 — BOLService is invoked synchronously on POD finalization
  when the ``overlay.bol_generation`` feature flag is enabled.
* Requirement 4.3.5 — on BOL generation failure, the BOL is marked
  ``status: pending_regeneration`` and POD persistence is not blocked
  (the finalizer never raises).

The finalizer is dependency-injectable, so the tests wire a fake feature
flag service, a fake :class:`BOLService`, and an in-memory ES stub — no
S3, KMS, or reportlab calls are made.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from driver.services.pod_bol_finalizer import (
    BOL_GENERATION_FLAG_KEY,
    BOL_STATUS_PENDING_REGENERATION,
    PODBOLFinalizer,
    PODContext,
)
from fuel.services.fuel_ops_es_mappings import BILL_OF_LADING_INDEX
from services.bol_service import BOLDocument, BOLFields


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeFeatureFlags:
    """Minimal FeatureFlagService stub exposing ``get_overlay_state``."""

    def __init__(self, state: str = "disabled") -> None:
        self._state = state
        self.calls: List[Dict[str, str]] = []

    async def get_overlay_state(self, flag_key: str, tenant_id: str) -> str:
        self.calls.append({"flag_key": flag_key, "tenant_id": tenant_id})
        return self._state


class _FakeES:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any]):
        self.calls.append({"index": index, "doc_id": doc_id, "document": dict(document)})
        return {"result": "created"}


class _FakeLoader:
    """Context loader stub that returns a pre-configured :class:`PODContext`."""

    def __init__(self, context: Optional[PODContext] = None, raises: Optional[Exception] = None) -> None:
        self._context = context
        self._raises = raises
        self.calls: List[Dict[str, Any]] = []

    async def load(self, *, tenant_id: str, pod: Dict[str, Any]) -> PODContext:
        self.calls.append({"tenant_id": tenant_id, "pod": dict(pod)})
        if self._raises:
            raise self._raises
        return self._context or PODContext.empty(tenant_name=tenant_id)


def _make_bol_service(*, raises: Optional[Exception] = None) -> MagicMock:
    """Create a mock ``BOLService`` with the ``generate`` coroutine.

    When ``raises`` is supplied the mock raises on every call so the
    failure branch of the finalizer can be exercised.
    """
    svc = MagicMock()
    if raises is not None:
        svc.generate = AsyncMock(side_effect=raises)
    else:
        svc.generate = AsyncMock(
            return_value=BOLDocument(
                bol_id="bol-tenant-a-test",
                tenant_id="tenant-a",
                pod_id="pod-1",
                order_id="order-1",
                file_ref="tenants/tenant-a/bol/2025/01/15/abc.pdf",
                hash="deadbeef" * 8,
                status="generated",
                fields=BOLFields(
                    bol_number="BOL-TEST",
                    product_name="DIESEL_2",
                    fuel_grade="DIESEL_2",
                    gross_gallons=742.5,
                    origin_depot_name="Depot 1",
                    origin_depot_address="100 Depot Rd",
                    destination="42 Oak St",
                    driver_name="Alex Driver",
                    truck_id="TRUCK-42",
                    delivered_at="2025-01-15T14:30:00+00:00",
                ),
                generated_at=__import__("datetime").datetime(
                    2025, 1, 15, 14, 30, tzinfo=__import__("datetime").timezone.utc
                ),
            )
        )
    return svc


def _pod_doc(**overrides: Any) -> Dict[str, Any]:
    """Minimal POD record matching the shape written by the submit_pod endpoint."""
    base = {
        "pod_id": "pod-1",
        "job_id": "JOB_1",
        "order_id": "order-1",
        "tenant_id": "tenant-a",
        "recipient_name": "Jane Receiver",
        "delivered_gallons": 742.5,
        "signature_ref": "tenants/tenant-a/signature/2025/01/15/sig.png",
        "photo_refs": [],
        "geotag": {"lat": 40.0, "lon": -74.0},
        "timestamp": "2025-01-15T14:30:00Z",
        "status": "submitted",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestFinalizerConstruction:
    def test_requires_bol_service(self):
        with pytest.raises(ValueError, match="bol_service"):
            PODBOLFinalizer(
                bol_service=None,
                es_service=_FakeES(),
                feature_flag_service=_FakeFeatureFlags(),
            )

    def test_requires_es_service(self):
        with pytest.raises(ValueError, match="es_service"):
            PODBOLFinalizer(
                bol_service=_make_bol_service(),
                es_service=None,
                feature_flag_service=_FakeFeatureFlags(),
            )

    def test_accepts_missing_feature_flag_service(self):
        """Finalizer can be constructed without a FeatureFlagService so tests and
        early bootstrap paths can wire it pre-Redis; maybe_generate will then
        short-circuit as if the flag were disabled."""
        PODBOLFinalizer(
            bol_service=_make_bol_service(),
            es_service=_FakeES(),
            feature_flag_service=None,
        )


# ---------------------------------------------------------------------------
# Feature-flag gating (Req 4.3.4)
# ---------------------------------------------------------------------------


class TestFeatureFlagGating:
    @pytest.mark.asyncio
    async def test_no_ops_when_flag_disabled(self):
        flags = _FakeFeatureFlags(state="disabled")
        bol = _make_bol_service()
        es = _FakeES()
        finalizer = PODBOLFinalizer(
            bol_service=bol, es_service=es, feature_flag_service=flags
        )

        result = await finalizer.maybe_generate(
            tenant_id="tenant-a", pod=_pod_doc()
        )

        assert result is None
        bol.generate.assert_not_awaited()
        assert es.calls == []
        # Feature flag should be looked up by the canonical key.
        assert flags.calls == [
            {"flag_key": BOL_GENERATION_FLAG_KEY, "tenant_id": "tenant-a"}
        ]

    @pytest.mark.asyncio
    async def test_no_ops_when_flag_shadow(self):
        """Shadow mode must not actually render a BOL (Req 4.3.4 activation semantics)."""
        flags = _FakeFeatureFlags(state="shadow")
        bol = _make_bol_service()
        es = _FakeES()
        finalizer = PODBOLFinalizer(
            bol_service=bol, es_service=es, feature_flag_service=flags
        )

        result = await finalizer.maybe_generate(
            tenant_id="tenant-a", pod=_pod_doc()
        )

        assert result is None
        bol.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_ops_when_feature_flag_service_missing(self):
        bol = _make_bol_service()
        es = _FakeES()
        finalizer = PODBOLFinalizer(
            bol_service=bol, es_service=es, feature_flag_service=None
        )

        result = await finalizer.maybe_generate(
            tenant_id="tenant-a", pod=_pod_doc()
        )

        assert result is None
        bol.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_ops_when_no_tenant_id(self):
        flags = _FakeFeatureFlags(state="active_auto")
        bol = _make_bol_service()
        finalizer = PODBOLFinalizer(
            bol_service=bol, es_service=_FakeES(), feature_flag_service=flags
        )

        result = await finalizer.maybe_generate(tenant_id="", pod=_pod_doc())

        assert result is None
        bol.generate.assert_not_awaited()
        # Flag lookup is skipped — no tenant to scope the flag by.
        assert flags.calls == []

    @pytest.mark.asyncio
    async def test_no_ops_when_pod_missing_pod_id(self):
        flags = _FakeFeatureFlags(state="active_auto")
        bol = _make_bol_service()
        finalizer = PODBOLFinalizer(
            bol_service=bol, es_service=_FakeES(), feature_flag_service=flags
        )

        result = await finalizer.maybe_generate(
            tenant_id="tenant-a",
            pod=_pod_doc(pod_id=""),
        )

        assert result is None
        bol.generate.assert_not_awaited()


# ---------------------------------------------------------------------------
# Happy path (Req 4.3.4)
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_generates_bol_when_flag_active_gated(self):
        flags = _FakeFeatureFlags(state="active_gated")
        bol = _make_bol_service()
        es = _FakeES()
        finalizer = PODBOLFinalizer(
            bol_service=bol, es_service=es, feature_flag_service=flags
        )

        result = await finalizer.maybe_generate(
            tenant_id="tenant-a", pod=_pod_doc(), actor="driver-1"
        )

        assert result is not None
        assert result.bol_id == "bol-tenant-a-test"
        bol.generate.assert_awaited_once()
        # The call should pass tenant_id, BOLRenderInputs, and actor through.
        call_kwargs = bol.generate.await_args.kwargs
        assert call_kwargs["tenant_id"] == "tenant-a"
        assert call_kwargs["actor"] == "driver-1"
        inputs = call_kwargs["inputs"]
        assert inputs.tenant_id == "tenant-a"
        # POD is passed verbatim so BOLService can extract pod_id, etc.
        assert inputs.pod["pod_id"] == "pod-1"
        # No failure stub written on the happy path — BOLService itself
        # persists the generated row.
        assert es.calls == []

    @pytest.mark.asyncio
    async def test_generates_bol_when_flag_active_auto(self):
        flags = _FakeFeatureFlags(state="active_auto")
        bol = _make_bol_service()
        finalizer = PODBOLFinalizer(
            bol_service=bol,
            es_service=_FakeES(),
            feature_flag_service=flags,
        )

        result = await finalizer.maybe_generate(
            tenant_id="tenant-a", pod=_pod_doc()
        )

        assert result is not None
        bol.generate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uses_context_loader_when_supplied(self):
        ctx = PODContext(
            tenant_name="Acme Fuel",
            tenant_logo_bytes=b"fake-logo",
            order={"order_id": "order-1", "product_name": "Diesel #2"},
            depot={"name": "Springfield", "address": "100 Depot Rd"},
            driver={"name": "Alex Driver", "cdl": "CDL-98765"},
            truck={"truck_id": "TRUCK-42"},
            destination={"name": "42 Oak St"},
        )
        loader = _FakeLoader(context=ctx)
        flags = _FakeFeatureFlags(state="active_auto")
        bol = _make_bol_service()
        finalizer = PODBOLFinalizer(
            bol_service=bol,
            es_service=_FakeES(),
            feature_flag_service=flags,
            context_loader=loader,
        )

        await finalizer.maybe_generate(tenant_id="tenant-a", pod=_pod_doc())

        assert len(loader.calls) == 1
        assert loader.calls[0]["tenant_id"] == "tenant-a"
        # BOLService should receive the loader's context.
        inputs = bol.generate.await_args.kwargs["inputs"]
        assert inputs.tenant_name == "Acme Fuel"
        assert inputs.tenant_logo_bytes == b"fake-logo"
        assert inputs.order == ctx.order
        assert inputs.depot == ctx.depot

    @pytest.mark.asyncio
    async def test_degrades_to_empty_context_when_loader_fails(self):
        """A loader raising must not break the pipeline — the BOL is still generated
        with UNKNOWN placeholders and no pending_regeneration stub is written."""
        loader = _FakeLoader(raises=RuntimeError("boom"))
        flags = _FakeFeatureFlags(state="active_auto")
        bol = _make_bol_service()
        es = _FakeES()
        finalizer = PODBOLFinalizer(
            bol_service=bol,
            es_service=es,
            feature_flag_service=flags,
            context_loader=loader,
        )

        result = await finalizer.maybe_generate(
            tenant_id="tenant-a", pod=_pod_doc()
        )

        assert result is not None
        bol.generate.assert_awaited_once()
        assert es.calls == []


# ---------------------------------------------------------------------------
# Failure path (Req 4.3.5)
# ---------------------------------------------------------------------------


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_bol_failure_does_not_raise(self):
        """Req 4.3.5 — BOL failure must not block POD persistence."""
        flags = _FakeFeatureFlags(state="active_auto")
        bol = _make_bol_service(raises=RuntimeError("reportlab exploded"))
        es = _FakeES()
        finalizer = PODBOLFinalizer(
            bol_service=bol, es_service=es, feature_flag_service=flags
        )

        # Must not raise.
        result = await finalizer.maybe_generate(
            tenant_id="tenant-a", pod=_pod_doc()
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_bol_failure_writes_pending_regeneration_stub(self):
        """Req 4.3.5 — failure marks the BOL record ``status: pending_regeneration``."""
        flags = _FakeFeatureFlags(state="active_auto")
        bol = _make_bol_service(raises=RuntimeError("s3 outage"))
        es = _FakeES()
        finalizer = PODBOLFinalizer(
            bol_service=bol, es_service=es, feature_flag_service=flags
        )

        await finalizer.maybe_generate(
            tenant_id="tenant-a", pod=_pod_doc()
        )

        assert len(es.calls) == 1
        call = es.calls[0]
        assert call["index"] == BILL_OF_LADING_INDEX
        document = call["document"]
        assert document["tenant_id"] == "tenant-a"
        assert document["pod_id"] == "pod-1"
        assert document["status"] == BOL_STATUS_PENDING_REGENERATION
        # Stub has an empty file_ref / hash — the regeneration job fills them.
        assert document["file_ref"] == ""
        assert document["hash"] == ""
        # The ES doc_id is unique per stub so repeated failures don't
        # collide with each other.
        assert call["doc_id"].startswith("bol-tenant-a-pending-")
        # Error details are surfaced on the ``fields`` payload so the
        # retry pipeline / ops UI can show what went wrong.
        assert "s3 outage" in document["fields"]["error"]

    @pytest.mark.asyncio
    async def test_bol_permission_error_records_pending_stub(self):
        """Cross-tenant file_ref PermissionError must not bubble out."""
        flags = _FakeFeatureFlags(state="active_auto")
        bol = _make_bol_service(raises=PermissionError("cross_tenant"))
        es = _FakeES()
        finalizer = PODBOLFinalizer(
            bol_service=bol, es_service=es, feature_flag_service=flags
        )

        result = await finalizer.maybe_generate(
            tenant_id="tenant-a", pod=_pod_doc()
        )

        assert result is None
        assert len(es.calls) == 1
        assert es.calls[0]["document"]["status"] == BOL_STATUS_PENDING_REGENERATION

    @pytest.mark.asyncio
    async def test_pending_stub_includes_order_id(self):
        """The stub carries order_id when present on the POD so retries can
        re-hydrate the full context."""
        flags = _FakeFeatureFlags(state="active_auto")
        bol = _make_bol_service(raises=RuntimeError("boom"))
        es = _FakeES()
        finalizer = PODBOLFinalizer(
            bol_service=bol, es_service=es, feature_flag_service=flags
        )

        await finalizer.maybe_generate(
            tenant_id="tenant-a",
            pod=_pod_doc(order_id="ord-abc"),
        )

        assert es.calls[0]["document"]["order_id"] == "ord-abc"

    @pytest.mark.asyncio
    async def test_feature_flag_service_exception_treated_as_disabled(self):
        """A FeatureFlagService that raises must not break POD persistence."""
        failing_ff = MagicMock()
        failing_ff.get_overlay_state = AsyncMock(side_effect=RuntimeError("redis down"))
        bol = _make_bol_service()
        es = _FakeES()
        finalizer = PODBOLFinalizer(
            bol_service=bol,
            es_service=es,
            feature_flag_service=failing_ff,
        )

        result = await finalizer.maybe_generate(
            tenant_id="tenant-a", pod=_pod_doc()
        )

        assert result is None
        bol.generate.assert_not_awaited()
        # No stub either — the flag was treated as disabled.
        assert es.calls == []
