"""
Tests for Task 9.5 — Staged reconciliation_service.update_invoice_fields migration.

Covers the three sub-steps:
  9.5a — dual-write path (canonical_invoice_id + qbo_invoice_id)
  9.5b — read-side flip via get_invoice_id helper
  9.5c — documented removal of dual-write (tested via flag-off behavior)

Each sub-step is independently committable and reversible via feature flags:
  - commerce_reconciliation_dual_write (9.5a)
  - commerce_reconciliation_prefer_canonical (9.5b)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from services.reconciliation_service import (
    ReconciliationService,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeES:
    """In-memory ES stub supporting index, get, and update operations."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.updates: List[Dict[str, Any]] = []

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any]):
        self.docs[doc_id] = dict(document)
        return {"result": "created"}

    async def get_document(self, index: str, doc_id: str) -> Optional[Dict[str, Any]]:
        doc = self.docs.get(doc_id)
        if doc is None:
            return None
        return {"_source": dict(doc)}

    async def update_document(self, index: str, doc_id: str, patch: Dict[str, Any]):
        if doc_id in self.docs:
            self.docs[doc_id].update(patch)
        self.updates.append({"index": index, "doc_id": doc_id, "patch": dict(patch)})
        return {"result": "updated"}


class _FakeRedis:
    """Minimal async Redis stub."""

    def __init__(self, values: Optional[Dict[str, Any]] = None) -> None:
        self._values = dict(values or {})

    async def get(self, key: str):
        return self._values.get(key)


class _FakeSettings:
    """Fake settings object for controlling feature flags in tests."""

    def __init__(
        self,
        commerce_backbone_enabled: bool = False,
        commerce_reconciliation_dual_write: bool = True,
        commerce_reconciliation_prefer_canonical: bool = False,
    ):
        self.commerce_backbone_enabled = commerce_backbone_enabled
        self.commerce_reconciliation_dual_write = commerce_reconciliation_dual_write
        self.commerce_reconciliation_prefer_canonical = commerce_reconciliation_prefer_canonical


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_record(es: _FakeES, rec_id: str = "rec-tenant-a-001", tenant_id: str = "tenant-a") -> Dict[str, Any]:
    """Seed a minimal reconciliation record into the fake ES."""
    doc = {
        "reconciliation_id": rec_id,
        "tenant_id": tenant_id,
        "order_id": "order-1",
        "plan_id": "plan-1",
        "pod_id": "pod-001",
        "invoice_id": None,
        "ordered_gallons": 500.0,
        "loaded_gallons": 500.0,
        "delivered_gallons": 500.0,
        "invoiced_gallons": None,
        "variance_load_vs_order_pct": 0.0,
        "variance_delivered_vs_loaded_pct": 0.0,
        "variance_invoiced_vs_delivered_pct": None,
        "alert_flags": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    es.docs[rec_id] = doc
    return doc


# ---------------------------------------------------------------------------
# Step 9.5a — Dual-write tests
# ---------------------------------------------------------------------------


class TestDualWrite:
    """Step 9.5a: When dual-write is enabled, update_invoice_fields writes
    both canonical_invoice_id and qbo_invoice_id."""

    @pytest.mark.asyncio
    async def test_dual_write_enabled_writes_both_fields(self):
        """When commerce is on and dual_write is True, both fields are written."""
        es = _FakeES()
        redis = _FakeRedis()
        svc = ReconciliationService(es_service=es, redis_client=redis)
        _seed_record(es)

        settings = _FakeSettings(
            commerce_backbone_enabled=True,
            commerce_reconciliation_dual_write=True,
        )

        with patch("services.reconciliation_service.get_settings", return_value=settings):
            result = await svc.update_invoice_fields(
                tenant_id="tenant-a",
                reconciliation_id="rec-tenant-a-001",
                invoice_id="inv_abc123",
                invoiced_gallons=500.0,
            )

        # Verify the ES document was updated with both fields
        doc = es.docs["rec-tenant-a-001"]
        assert doc["canonical_invoice_id"] == "inv_abc123"
        assert doc["qbo_invoice_id"] == "inv_abc123"
        assert doc["external_refs"] == {"qbo": "inv_abc123"}
        # Legacy field is still written
        assert doc["invoice_id"] == "inv_abc123"

    @pytest.mark.asyncio
    async def test_dual_write_disabled_skips_new_fields(self):
        """When dual_write is False, only the legacy invoice_id is written."""
        es = _FakeES()
        redis = _FakeRedis()
        svc = ReconciliationService(es_service=es, redis_client=redis)
        _seed_record(es)

        settings = _FakeSettings(
            commerce_backbone_enabled=True,
            commerce_reconciliation_dual_write=False,
        )

        with patch("services.reconciliation_service.get_settings", return_value=settings):
            result = await svc.update_invoice_fields(
                tenant_id="tenant-a",
                reconciliation_id="rec-tenant-a-001",
                invoice_id="qbo-inv-456",
                invoiced_gallons=500.0,
            )

        doc = es.docs["rec-tenant-a-001"]
        assert doc["invoice_id"] == "qbo-inv-456"
        assert "canonical_invoice_id" not in doc
        assert "qbo_invoice_id" not in doc

    @pytest.mark.asyncio
    async def test_dual_write_off_when_commerce_disabled(self):
        """When commerce_backbone_enabled is False, dual-write does not activate."""
        es = _FakeES()
        redis = _FakeRedis()
        svc = ReconciliationService(es_service=es, redis_client=redis)
        _seed_record(es)

        settings = _FakeSettings(
            commerce_backbone_enabled=False,
            commerce_reconciliation_dual_write=True,
        )

        with patch("services.reconciliation_service.get_settings", return_value=settings):
            result = await svc.update_invoice_fields(
                tenant_id="tenant-a",
                reconciliation_id="rec-tenant-a-001",
                invoice_id="qbo-inv-789",
                invoiced_gallons=500.0,
            )

        doc = es.docs["rec-tenant-a-001"]
        assert doc["invoice_id"] == "qbo-inv-789"
        assert "canonical_invoice_id" not in doc
        assert "qbo_invoice_id" not in doc

    @pytest.mark.asyncio
    async def test_dual_write_is_reversible(self):
        """Toggling dual_write off after it was on stops writing new fields."""
        es = _FakeES()
        redis = _FakeRedis()
        svc = ReconciliationService(es_service=es, redis_client=redis)
        _seed_record(es)

        # First call with dual-write ON
        settings_on = _FakeSettings(
            commerce_backbone_enabled=True,
            commerce_reconciliation_dual_write=True,
        )
        with patch("services.reconciliation_service.get_settings", return_value=settings_on):
            await svc.update_invoice_fields(
                tenant_id="tenant-a",
                reconciliation_id="rec-tenant-a-001",
                invoice_id="inv_first",
                invoiced_gallons=500.0,
            )

        assert es.docs["rec-tenant-a-001"]["canonical_invoice_id"] == "inv_first"

        # Second call with dual-write OFF — new fields are not written
        # (but existing ones remain in ES since we don't delete them)
        settings_off = _FakeSettings(
            commerce_backbone_enabled=True,
            commerce_reconciliation_dual_write=False,
        )
        # Reset the doc to simulate a fresh record for clarity
        _seed_record(es, rec_id="rec-tenant-a-002")
        with patch("services.reconciliation_service.get_settings", return_value=settings_off):
            await svc.update_invoice_fields(
                tenant_id="tenant-a",
                reconciliation_id="rec-tenant-a-002",
                invoice_id="inv_second",
                invoiced_gallons=500.0,
            )

        doc2 = es.docs["rec-tenant-a-002"]
        assert doc2["invoice_id"] == "inv_second"
        assert "canonical_invoice_id" not in doc2


# ---------------------------------------------------------------------------
# Step 9.5b — Read-side flip tests
# ---------------------------------------------------------------------------


class TestGetInvoiceId:
    """Step 9.5b: get_invoice_id helper returns the correct field based on
    the commerce_reconciliation_prefer_canonical flag."""

    def test_prefer_canonical_returns_canonical_when_present(self):
        """When prefer_canonical is True and canonical_invoice_id exists, return it."""
        es = _FakeES()
        svc = ReconciliationService(es_service=es)

        settings = _FakeSettings(
            commerce_backbone_enabled=True,
            commerce_reconciliation_prefer_canonical=True,
        )

        record = {
            "canonical_invoice_id": "inv_canonical_001",
            "qbo_invoice_id": "qbo-123",
            "invoice_id": "qbo-123",
        }

        with patch("services.reconciliation_service.get_settings", return_value=settings):
            result = svc.get_invoice_id(record)

        assert result == "inv_canonical_001"

    def test_prefer_canonical_falls_back_to_qbo(self):
        """When prefer_canonical is True but canonical is missing, fall back to qbo."""
        es = _FakeES()
        svc = ReconciliationService(es_service=es)

        settings = _FakeSettings(
            commerce_backbone_enabled=True,
            commerce_reconciliation_prefer_canonical=True,
        )

        record = {
            "qbo_invoice_id": "qbo-456",
            "invoice_id": "qbo-456",
        }

        with patch("services.reconciliation_service.get_settings", return_value=settings):
            result = svc.get_invoice_id(record)

        assert result == "qbo-456"

    def test_prefer_canonical_falls_back_to_legacy_invoice_id(self):
        """When prefer_canonical is True but both new fields are missing, use legacy."""
        es = _FakeES()
        svc = ReconciliationService(es_service=es)

        settings = _FakeSettings(
            commerce_backbone_enabled=True,
            commerce_reconciliation_prefer_canonical=True,
        )

        record = {
            "invoice_id": "legacy-inv-789",
        }

        with patch("services.reconciliation_service.get_settings", return_value=settings):
            result = svc.get_invoice_id(record)

        assert result == "legacy-inv-789"

    def test_legacy_mode_returns_qbo_invoice_id(self):
        """When prefer_canonical is False, return qbo_invoice_id."""
        es = _FakeES()
        svc = ReconciliationService(es_service=es)

        settings = _FakeSettings(
            commerce_backbone_enabled=True,
            commerce_reconciliation_prefer_canonical=False,
        )

        record = {
            "canonical_invoice_id": "inv_canonical_001",
            "qbo_invoice_id": "qbo-123",
            "invoice_id": "qbo-123",
        }

        with patch("services.reconciliation_service.get_settings", return_value=settings):
            result = svc.get_invoice_id(record)

        assert result == "qbo-123"

    def test_legacy_mode_falls_back_to_invoice_id(self):
        """When prefer_canonical is False and qbo_invoice_id is missing, use legacy."""
        es = _FakeES()
        svc = ReconciliationService(es_service=es)

        settings = _FakeSettings(
            commerce_backbone_enabled=False,
            commerce_reconciliation_prefer_canonical=False,
        )

        record = {
            "invoice_id": "legacy-inv-001",
        }

        with patch("services.reconciliation_service.get_settings", return_value=settings):
            result = svc.get_invoice_id(record)

        assert result == "legacy-inv-001"

    def test_returns_none_when_no_invoice_fields(self):
        """When no invoice fields are present, return None."""
        es = _FakeES()
        svc = ReconciliationService(es_service=es)

        settings = _FakeSettings(
            commerce_backbone_enabled=True,
            commerce_reconciliation_prefer_canonical=True,
        )

        record = {
            "reconciliation_id": "rec-001",
            "tenant_id": "tenant-a",
        }

        with patch("services.reconciliation_service.get_settings", return_value=settings):
            result = svc.get_invoice_id(record)

        assert result is None

    def test_commerce_off_uses_legacy_behavior(self):
        """When commerce_backbone_enabled is False, always use legacy behavior."""
        es = _FakeES()
        svc = ReconciliationService(es_service=es)

        settings = _FakeSettings(
            commerce_backbone_enabled=False,
            commerce_reconciliation_prefer_canonical=True,  # ignored when commerce is off
        )

        record = {
            "canonical_invoice_id": "inv_canonical_001",
            "qbo_invoice_id": "qbo-123",
            "invoice_id": "qbo-123",
        }

        with patch("services.reconciliation_service.get_settings", return_value=settings):
            result = svc.get_invoice_id(record)

        # Even though prefer_canonical is True, commerce is off so legacy path
        assert result == "qbo-123"


# ---------------------------------------------------------------------------
# Step 9.5c — Removal of dual-write (documented as TODO)
# ---------------------------------------------------------------------------


class TestPostMigrationCleanup:
    """Step 9.5c: After the second soak, dual-write can be removed.
    This test verifies the system works correctly when only canonical
    fields are present (simulating post-cleanup state)."""

    def test_get_invoice_id_works_with_only_canonical_field(self):
        """After cleanup, records only have canonical_invoice_id."""
        es = _FakeES()
        svc = ReconciliationService(es_service=es)

        settings = _FakeSettings(
            commerce_backbone_enabled=True,
            commerce_reconciliation_prefer_canonical=True,
        )

        # Post-cleanup record: only canonical_invoice_id, no qbo_invoice_id
        record = {
            "canonical_invoice_id": "inv_final_001",
            "external_refs": {"qbo": "qbo-old-ref"},
        }

        with patch("services.reconciliation_service.get_settings", return_value=settings):
            result = svc.get_invoice_id(record)

        assert result == "inv_final_001"

    @pytest.mark.asyncio
    async def test_external_refs_qbo_preserved_for_cross_reference(self):
        """After cleanup, external_refs.qbo is still available for cross-reference."""
        es = _FakeES()
        redis = _FakeRedis()
        svc = ReconciliationService(es_service=es, redis_client=redis)
        _seed_record(es)

        settings = _FakeSettings(
            commerce_backbone_enabled=True,
            commerce_reconciliation_dual_write=True,
        )

        with patch("services.reconciliation_service.get_settings", return_value=settings):
            result = await svc.update_invoice_fields(
                tenant_id="tenant-a",
                reconciliation_id="rec-tenant-a-001",
                invoice_id="inv_canonical_002",
                invoiced_gallons=500.0,
            )

        # Verify external_refs.qbo is set for cross-reference
        doc = es.docs["rec-tenant-a-001"]
        assert doc.get("external_refs", {}).get("qbo") == "inv_canonical_002"
