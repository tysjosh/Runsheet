"""``mirror_current_state_fields`` accepts a partial doc that carries ``tenant_id``.

Staging (api:24, 2026-10-06): every fleet location update logged
``Postgres dual-write fields failed for truck … TypeError:
CurrentStateRepository.set_fields() got multiple values for argument
'tenant_id'``. The ingest passes its whole partial truck doc as ``fields``,
and that doc carries ``tenant_id``; as a ``**fields`` key it collided with
``set_fields``'s own ``tenant_id`` parameter, so the relational truck row
never received the new position.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any, Dict, List

import pytest

import commerce.services.commerce_persistence_bridge as bridge


class _Repo:
    """Same signature as ``CurrentStateRepository.set_fields``."""

    calls: List[Dict[str, Any]] = []

    def __init__(self, aggregate_type: str) -> None:
        self.aggregate_type = aggregate_type

    async def set_fields(self, session, tenant_id: str, doc_id: str, **fields: Any):
        _Repo.calls.append({"tenant_id": tenant_id, "doc_id": doc_id, "fields": fields})
        return object()


@contextlib.asynccontextmanager
async def _session_scope():
    yield object()


@pytest.fixture
def wired(monkeypatch):
    import persistence.database as database
    import persistence.repositories as repositories

    _Repo.calls = []
    monkeypatch.setattr(bridge, "_enabled", lambda: True)
    monkeypatch.setattr(repositories, "CurrentStateRepository", _Repo)
    monkeypatch.setattr(database, "session_scope", _session_scope)
    return _Repo.calls


def _failures(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if "dual-write fields" in r.getMessage()]


async def test_fields_carrying_the_same_tenant_are_written(wired, caplog):
    caplog.set_level(logging.DEBUG)
    fields = {"tenant_id": "tenant-1", "last_update": "2026-10-06T17:53:11Z",
              "current_location": {"coordinates": {"lat": 29.76, "lon": -95.37}}}

    await bridge.mirror_current_state_fields("truck", "tenant-1", "TRUCK-1", fields)

    assert _failures(caplog) == []
    (call,) = wired
    assert call["tenant_id"] == "tenant-1" and call["doc_id"] == "TRUCK-1"
    assert "tenant_id" not in call["fields"]
    assert call["fields"]["last_update"] == "2026-10-06T17:53:11Z"
    assert fields["tenant_id"] == "tenant-1"  # caller's dict untouched


async def test_fields_carrying_another_tenant_are_refused(wired, caplog):
    await bridge.mirror_current_state_fields(
        "truck", "tenant-1", "TRUCK-1", {"tenant_id": "tenant-2", "last_update": "x"}
    )

    assert wired == []
    assert any("refused" in m for m in _failures(caplog))


async def test_fields_without_tenant_are_unchanged(wired, caplog):
    await bridge.mirror_current_state_fields("job", "tenant-1", "JOB_1", {"status": "delayed"})

    assert _failures(caplog) == []
    assert wired == [{"tenant_id": "tenant-1", "doc_id": "JOB_1", "fields": {"status": "delayed"}}]
