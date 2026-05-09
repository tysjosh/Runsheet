"""Tests for ``services.time_utils`` and tz-awareness on ES write paths.

Covers:
- ``utcnow()`` returns a tz-aware ``datetime`` whose ``tzinfo`` is
  ``timezone.utc``.
- ``utcnow().isoformat()`` serialises with a ``+00:00`` suffix (no naive
  local-time leakage, no ``"Z"`` / ``+00:00`` double-suffix bugs).
- ``ElasticsearchService.index_document`` stamps every written document
  with tz-aware ``created_at`` / ``updated_at`` values so the ES write
  hot path can never emit a naive timestamp on a non-UTC host.

Validates code-review finding F13 — naive ``datetime.now()`` /
``datetime.utcnow()`` calls must not survive on the ES write hot path.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

# Other unit tests in the suite register a ``MagicMock`` under
# ``services.elasticsearch_service`` via ``sys.modules.setdefault`` so
# their transitive imports don't hit a live cluster. That leaves us with
# a mocked class when pytest collects this file after them. Force a
# fresh load of the real module so we can instantiate the real
# ``ElasticsearchService`` for hot-path assertions.
sys.modules.pop("services.elasticsearch_service", None)
_es_module = importlib.import_module("services.elasticsearch_service")
ElasticsearchService = _es_module.ElasticsearchService

from services.time_utils import utcnow  # noqa: E402


def test_utcnow_returns_timezone_aware() -> None:
    """``utcnow()`` must return a tz-aware datetime anchored to UTC."""
    now = utcnow()
    assert isinstance(now, datetime)
    assert now.tzinfo is timezone.utc


def test_utcnow_serialises_to_iso() -> None:
    """``utcnow().isoformat()`` must end with ``+00:00``, not a naive or ``Z`` suffix."""
    iso = utcnow().isoformat()
    assert iso.endswith("+00:00"), iso
    # Must be a proper ISO-8601 string that round-trips via fromisoformat.
    roundtrip = datetime.fromisoformat(iso)
    assert roundtrip.tzinfo is not None


def _make_es_service_with_stub_client() -> tuple[ElasticsearchService, MagicMock]:
    """Build an ``ElasticsearchService`` whose client is a recording stub.

    Bypasses ``__init__`` (which would try to connect to a real cluster)
    and wires up the fields the write-path touches.
    """
    from resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

    service = ElasticsearchService.__new__(ElasticsearchService)
    service.client = MagicMock()
    service.client.index = MagicMock(return_value={"result": "created"})
    service._circuit_breaker = CircuitBreaker(
        name="test_write",
        config=CircuitBreakerConfig(failure_threshold=10),
    )
    service._read_circuit_breaker = CircuitBreaker(
        name="test_read",
        config=CircuitBreakerConfig(failure_threshold=15),
    )
    return service, service.client


def test_index_document_stamps_tz_aware_timestamps() -> None:
    """``index_document`` must inject tz-aware ``created_at`` / ``updated_at``."""
    service, client = _make_es_service_with_stub_client()

    document: dict = {"truck_id": "T-1", "plate_number": "KAA-001"}
    asyncio.run(service.index_document("trucks", "T-1", document))

    # The mock received the merged document — every timestamp the write
    # path injected must be tz-aware.
    assert client.index.called
    _, kwargs = client.index.call_args
    body = kwargs["body"]
    assert "updated_at" in body
    assert "created_at" in body
    updated_at = datetime.fromisoformat(body["updated_at"])
    created_at = datetime.fromisoformat(body["created_at"])
    assert updated_at.tzinfo is not None
    assert updated_at.utcoffset() == timezone.utc.utcoffset(updated_at)
    assert created_at.tzinfo is not None
    assert created_at.utcoffset() == timezone.utc.utcoffset(created_at)
    # No legacy ``"Z"`` + ``+00:00`` double-suffix leak.
    assert not body["updated_at"].endswith("Z+00:00")
    assert body["updated_at"].endswith("+00:00")


def test_index_document_preserves_existing_created_at() -> None:
    """``index_document`` must not overwrite an existing ``created_at`` value."""
    service, client = _make_es_service_with_stub_client()

    original_created = "2024-01-01T00:00:00+00:00"
    document: dict = {
        "truck_id": "T-2",
        "created_at": original_created,
    }
    asyncio.run(service.index_document("trucks", "T-2", document))

    _, kwargs = client.index.call_args
    body = kwargs["body"]
    assert body["created_at"] == original_created
    # ``updated_at`` is still refreshed to a tz-aware timestamp.
    updated_at = datetime.fromisoformat(body["updated_at"])
    assert updated_at.tzinfo is not None


def test_update_document_stamps_tz_aware_updated_at() -> None:
    """``update_document`` must stamp a tz-aware ``updated_at``."""
    service, client = _make_es_service_with_stub_client()
    client.update = MagicMock(return_value={"result": "updated"})

    partial: dict = {"status": "delivered"}
    asyncio.run(service.update_document("trucks", "T-3", partial))

    _, kwargs = client.update.call_args
    body = kwargs["body"]
    doc = body["doc"]
    assert "updated_at" in doc
    updated_at = datetime.fromisoformat(doc["updated_at"])
    assert updated_at.tzinfo is not None


@pytest.mark.parametrize(
    "skip_index",
    ["job_events", "shipment_events"],
)
def test_index_document_skips_timestamps_for_strict_indices(skip_index: str) -> None:
    """Strict-mapped indices still have timestamps skipped post-refactor."""
    service, client = _make_es_service_with_stub_client()

    document: dict = {"event_id": "E-1"}
    asyncio.run(service.index_document(skip_index, "E-1", document))

    _, kwargs = client.index.call_args
    body = kwargs["body"]
    assert "updated_at" not in body
    assert "created_at" not in body
