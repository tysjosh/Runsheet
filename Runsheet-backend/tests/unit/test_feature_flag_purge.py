"""OI-40: the feature-flag rollback purge goes through the document-store facade.

``FeatureFlagService._purge_tenant_data`` used ``ops_es_service.client``, which is
``NoClusterClient`` after the Postgres cutover. Its ``indices.exists`` answers
False, so ``rollback(purge_data=True)`` silently deleted nothing. These tests pin
the facade path: one ``delete_by_query`` per ops index with the tenant term, and
no use of the raw client.
"""

from __future__ import annotations

import logging

import pytest

from ops.services.feature_flags import FeatureFlagService
from ops.services.ops_es_service import OpsElasticsearchService

OPS_INDICES = [
    OpsElasticsearchService.SHIPMENTS_CURRENT,
    OpsElasticsearchService.SHIPMENT_EVENTS,
    OpsElasticsearchService.RIDERS_CURRENT,
    OpsElasticsearchService.POISON_QUEUE,
]


class _FakeFacade:
    """Stands in for ``ElasticsearchService``: records delete_by_query calls."""

    def __init__(self, counts=None, fail_on=None):
        self.calls: list[tuple[str, dict]] = []
        self._counts = counts or {}
        self._fail_on = fail_on or set()

    @property
    def client(self):
        raise AssertionError("the purge must not touch the raw ES client")

    async def delete_by_query(self, index, query):
        self.calls.append((index, query))
        if index in self._fail_on:
            raise RuntimeError(f"store down for {index}")
        return self._counts.get(index, 0)


def _service(facade) -> FeatureFlagService:
    # No connect(): the purge never needs Redis.
    return FeatureFlagService("redis://localhost:6379", ops_es_service=OpsElasticsearchService(facade))


async def test_purge_deletes_tenant_docs_from_every_ops_index_via_facade(caplog):
    facade = _FakeFacade(counts={OpsElasticsearchService.SHIPMENTS_CURRENT: 3})
    with caplog.at_level(logging.INFO, logger="ops.services.feature_flags"):
        await _service(facade)._purge_tenant_data("tenant-a")

    assert facade.calls == [(index, {"term": {"tenant_id": "tenant-a"}}) for index in OPS_INDICES]
    assert f"Purged 3 documents from {OpsElasticsearchService.SHIPMENTS_CURRENT}" in caplog.text


async def test_purge_logs_a_failing_index_and_continues(caplog):
    # A retired index answers 0 through the facade; a store error on one index
    # is logged and the remaining indices are still purged.
    failing = OpsElasticsearchService.SHIPMENT_EVENTS
    facade = _FakeFacade(fail_on={failing})
    with caplog.at_level(logging.ERROR, logger="ops.services.feature_flags"):
        await _service(facade)._purge_tenant_data("tenant-a")

    assert [index for index, _ in facade.calls] == OPS_INDICES
    assert f"Failed to purge tenant data from {failing}" in caplog.text


async def test_purge_without_ops_es_service_is_a_logged_noop(caplog):
    svc = FeatureFlagService("redis://localhost:6379", ops_es_service=None)
    with caplog.at_level(logging.WARNING, logger="ops.services.feature_flags"):
        await svc._purge_tenant_data("tenant-a")
    assert "OpsElasticsearchService not configured" in caplog.text


async def test_ops_es_service_delete_by_query_delegates_to_facade():
    facade = _FakeFacade(counts={"shipments_current": 7})
    ops = OpsElasticsearchService(facade)
    assert await ops.delete_by_query("shipments_current", {"term": {"tenant_id": "t"}}) == 7
    assert facade.calls == [("shipments_current", {"term": {"tenant_id": "t"}})]


@pytest.mark.parametrize("purge", [True, False])
async def test_rollback_purges_only_when_asked(monkeypatch, purge):
    facade = _FakeFacade()
    svc = _service(facade)

    async def _disable(tenant_id, user_id):
        return None

    monkeypatch.setattr(svc, "disable", _disable)
    await svc.rollback("tenant-a", "user-1", purge_data=purge)
    assert len(facade.calls) == (len(OPS_INDICES) if purge else 0)
