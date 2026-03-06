"""
Unit tests for CargoService.

Tests cargo manifest retrieval, updates, item status changes,
event appending, all-delivered detection, and cross-job cargo search.

Requirements: 6.1-6.6
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from scheduling.models import CargoItem, CargoItemStatus
from scheduling.services.cargo_service import CargoService
from errors.exceptions import AppException


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_es_mock() -> MagicMock:
    """Return a mock ElasticsearchService with default async methods."""
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    es.update_document = AsyncMock(return_value={"result": "updated"})
    # Mock the raw client for painless script updates
    es.client = MagicMock()
    es.client.update = MagicMock(return_value={"result": "updated"})
    return es


def _make_service(es_mock: MagicMock) -> CargoService:
    """Create a CargoService with mocked ES dependency."""
    return CargoService(es_service=es_mock)


def _job_doc_with_manifest(manifest: list[dict] | None = None) -> dict:
    """Return a fake job _source document with a cargo manifest."""
    if manifest is None:
        manifest = [
            {
                "item_id": "CARGO_aaa",
                "description": "Steel pipes",
                "weight_kg": 500.0,
                "container_number": "CONT-001",
                "seal_number": "SEAL-001",
                "item_status": "pending",
            },
            {
                "item_id": "CARGO_bbb",
                "description": "Cement bags",
                "weight_kg": 200.0,
                "container_number": "CONT-002",
                "seal_number": None,
                "item_status": "loaded",
            },
        ]
    return {
        "job_id": "JOB_1",
        "job_type": "cargo_transport",
        "status": "in_progress",
        "tenant_id": "tenant_a",
        "origin": "Port Harcourt",
        "destination": "Lagos",
        "cargo_manifest": manifest,
    }


def _es_hit(source: dict) -> dict:
    """Wrap a source dict in an ES hit envelope."""
    return {"hits": {"hits": [{"_source": source}], "total": {"value": 1}}}


# ---------------------------------------------------------------------------
# Test: get_cargo_manifest returns manifest items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cargo_manifest_returns_items():
    """get_cargo_manifest should return the cargo_manifest list from the job.

    Validates: Requirement 6.1
    """
    es = _make_es_mock()
    job_doc = _job_doc_with_manifest()
    es.search_documents = AsyncMock(return_value=_es_hit(job_doc))
    svc = _make_service(es)

    result = await svc.get_cargo_manifest("JOB_1", "tenant_a")

    assert len(result) == 2
    assert result[0]["item_id"] == "CARGO_aaa"
    assert result[1]["description"] == "Cement bags"


@pytest.mark.asyncio
async def test_get_cargo_manifest_returns_empty_when_no_manifest():
    """get_cargo_manifest should return an empty list when cargo_manifest is None.

    Validates: Requirement 6.1
    """
    es = _make_es_mock()
    job_doc = _job_doc_with_manifest(manifest=None)
    job_doc["cargo_manifest"] = None
    es.search_documents = AsyncMock(return_value=_es_hit(job_doc))
    svc = _make_service(es)

    result = await svc.get_cargo_manifest("JOB_1", "tenant_a")

    assert result == []


# ---------------------------------------------------------------------------
# Test: update_cargo_manifest replaces items and auto-generates item_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_cargo_manifest_replaces_items():
    """update_cargo_manifest should replace the manifest and auto-generate item_ids.

    Validates: Requirement 6.2
    """
    es = _make_es_mock()
    job_doc = _job_doc_with_manifest()
    es.search_documents = AsyncMock(return_value=_es_hit(job_doc))
    svc = _make_service(es)

    new_items = [
        CargoItem(description="New item A", weight_kg=100.0),
        CargoItem(description="New item B", weight_kg=250.0, item_id="EXISTING_ID"),
    ]

    result = await svc.update_cargo_manifest("JOB_1", new_items, "tenant_a", actor_id="user_1")

    assert len(result) == 2
    # First item should have an auto-generated item_id (starts with CARGO_)
    assert result[0]["item_id"].startswith("CARGO_")
    assert result[0]["description"] == "New item A"
    # Second item should keep its provided item_id
    assert result[1]["item_id"] == "EXISTING_ID"

    # Verify update_document was called with the new manifest
    es.update_document.assert_awaited_once()
    call_args = es.update_document.await_args
    assert call_args.args[0] == "jobs_current"
    assert call_args.args[1] == "JOB_1"
    fields = call_args.args[2]
    assert len(fields["cargo_manifest"]) == 2


@pytest.mark.asyncio
async def test_update_cargo_manifest_appends_cargo_updated_event():
    """update_cargo_manifest should append a cargo_updated event.

    Validates: Requirement 6.2
    """
    es = _make_es_mock()
    job_doc = _job_doc_with_manifest()
    es.search_documents = AsyncMock(return_value=_es_hit(job_doc))
    svc = _make_service(es)

    new_items = [CargoItem(description="Item X", weight_kg=50.0)]
    await svc.update_cargo_manifest("JOB_1", new_items, "tenant_a", actor_id="op_1")

    # index_document should be called once for the event
    es.index_document.assert_awaited_once()
    event_call = es.index_document.await_args
    assert event_call.args[0] == "job_events"
    event_doc = event_call.args[2]
    assert event_doc["event_type"] == "cargo_updated"
    assert event_doc["job_id"] == "JOB_1"
    assert event_doc["tenant_id"] == "tenant_a"
    assert event_doc["actor_id"] == "op_1"
    assert event_doc["event_payload"]["old_item_count"] == 2
    assert event_doc["event_payload"]["new_item_count"] == 1


# ---------------------------------------------------------------------------
# Test: update_cargo_item_status updates single item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_cargo_item_status_updates_item():
    """update_cargo_item_status should update the item and return the updated dict.

    Validates: Requirement 6.3
    """
    es = _make_es_mock()
    job_doc = _job_doc_with_manifest()
    # First call: _get_job_doc for the update
    # Second call: _check_all_delivered re-fetches the job
    refetched_doc = _job_doc_with_manifest([
        {**job_doc["cargo_manifest"][0], "item_status": "loaded"},
        job_doc["cargo_manifest"][1],
    ])
    es.search_documents = AsyncMock(
        side_effect=[_es_hit(job_doc), _es_hit(refetched_doc)]
    )
    svc = _make_service(es)

    result = await svc.update_cargo_item_status(
        "JOB_1", "CARGO_aaa", CargoItemStatus.LOADED, "tenant_a"
    )

    assert result["item_id"] == "CARGO_aaa"
    assert result["item_status"] == "loaded"
    # Verify painless script was called on the ES client
    es.client.update.assert_called_once()


# ---------------------------------------------------------------------------
# Test: cargo_status_changed event appended on item status update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_cargo_item_status_appends_event():
    """update_cargo_item_status should append a cargo_status_changed event.

    Validates: Requirement 6.4
    """
    es = _make_es_mock()
    job_doc = _job_doc_with_manifest()
    refetched_doc = _job_doc_with_manifest([
        {**job_doc["cargo_manifest"][0], "item_status": "in_transit"},
        job_doc["cargo_manifest"][1],
    ])
    es.search_documents = AsyncMock(
        side_effect=[_es_hit(job_doc), _es_hit(refetched_doc)]
    )
    svc = _make_service(es)

    await svc.update_cargo_item_status(
        "JOB_1", "CARGO_aaa", CargoItemStatus.IN_TRANSIT, "tenant_a", actor_id="user_2"
    )

    es.index_document.assert_awaited_once()
    event_call = es.index_document.await_args
    assert event_call.args[0] == "job_events"
    event_doc = event_call.args[2]
    assert event_doc["event_type"] == "cargo_status_changed"
    assert event_doc["event_payload"]["item_id"] == "CARGO_aaa"
    assert event_doc["event_payload"]["old_status"] == "pending"
    assert event_doc["event_payload"]["new_status"] == "in_transit"
    assert event_doc["actor_id"] == "user_2"


@pytest.mark.asyncio
async def test_update_cargo_item_status_not_found_raises_404():
    """update_cargo_item_status should raise 404 for a non-existent item_id.

    Validates: Requirement 6.3
    """
    es = _make_es_mock()
    job_doc = _job_doc_with_manifest()
    es.search_documents = AsyncMock(return_value=_es_hit(job_doc))
    svc = _make_service(es)

    with pytest.raises(AppException) as exc_info:
        await svc.update_cargo_item_status(
            "JOB_1", "NONEXISTENT", CargoItemStatus.LOADED, "tenant_a"
        )

    assert exc_info.value.status_code == 404
    assert "NONEXISTENT" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test: all-delivered detection triggers cargo_complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_delivered_triggers_cargo_complete_broadcast():
    """When all items are delivered, _broadcast_cargo_complete should be called.

    Validates: Requirement 6.6
    """
    es = _make_es_mock()
    # Initial manifest: one item pending, one delivered
    manifest = [
        {"item_id": "CARGO_aaa", "description": "A", "weight_kg": 100.0,
         "container_number": None, "seal_number": None, "item_status": "pending"},
        {"item_id": "CARGO_bbb", "description": "B", "weight_kg": 200.0,
         "container_number": None, "seal_number": None, "item_status": "delivered"},
    ]
    job_doc = _job_doc_with_manifest(manifest)

    # After painless update, re-fetch shows all delivered
    all_delivered_manifest = [
        {**manifest[0], "item_status": "delivered"},
        manifest[1],
    ]
    refetched_doc = _job_doc_with_manifest(all_delivered_manifest)

    es.search_documents = AsyncMock(
        side_effect=[_es_hit(job_doc), _es_hit(refetched_doc)]
    )
    svc = _make_service(es)

    # Wire a mock WebSocket manager to verify broadcast
    ws_mock = AsyncMock()
    svc._ws_manager = ws_mock

    await svc.update_cargo_item_status(
        "JOB_1", "CARGO_aaa", CargoItemStatus.DELIVERED, "tenant_a"
    )

    # Should have broadcast cargo_complete
    broadcast_calls = [
        c for c in ws_mock.broadcast.await_args_list
        if c.args[0] == "cargo_complete"
    ]
    assert len(broadcast_calls) == 1
    payload = broadcast_calls[0].args[1]
    assert payload["job_id"] == "JOB_1"


@pytest.mark.asyncio
async def test_not_all_delivered_does_not_trigger_cargo_complete():
    """When not all items are delivered, cargo_complete should NOT be broadcast.

    Validates: Requirement 6.6
    """
    es = _make_es_mock()
    manifest = [
        {"item_id": "CARGO_aaa", "description": "A", "weight_kg": 100.0,
         "container_number": None, "seal_number": None, "item_status": "pending"},
        {"item_id": "CARGO_bbb", "description": "B", "weight_kg": 200.0,
         "container_number": None, "seal_number": None, "item_status": "pending"},
    ]
    job_doc = _job_doc_with_manifest(manifest)

    # After update, only one is loaded — not all delivered
    refetched_manifest = [
        {**manifest[0], "item_status": "loaded"},
        manifest[1],
    ]
    refetched_doc = _job_doc_with_manifest(refetched_manifest)

    es.search_documents = AsyncMock(
        side_effect=[_es_hit(job_doc), _es_hit(refetched_doc)]
    )
    svc = _make_service(es)
    ws_mock = AsyncMock()
    svc._ws_manager = ws_mock

    await svc.update_cargo_item_status(
        "JOB_1", "CARGO_aaa", CargoItemStatus.LOADED, "tenant_a"
    )

    # cargo_complete should NOT have been broadcast
    cargo_complete_calls = [
        c for c in ws_mock.broadcast.await_args_list
        if c.args[0] == "cargo_complete"
    ]
    assert len(cargo_complete_calls) == 0


# ---------------------------------------------------------------------------
# Test: search_cargo by container_number, description, item_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_cargo_by_container_number():
    """search_cargo should build a nested query filtering by container_number.

    Validates: Requirement 6.5
    """
    es = _make_es_mock()
    es.search_documents = AsyncMock(return_value={
        "hits": {
            "hits": [
                {
                    "_source": {
                        "job_id": "JOB_1",
                        "job_type": "cargo_transport",
                        "status": "in_progress",
                        "origin": "Port A",
                        "destination": "Port B",
                    },
                    "inner_hits": {
                        "cargo_manifest": {
                            "hits": {
                                "hits": [
                                    {
                                        "_source": {
                                            "item_id": "CARGO_x",
                                            "description": "Pipes",
                                            "weight_kg": 300.0,
                                            "container_number": "CONT-100",
                                            "item_status": "loaded",
                                        }
                                    }
                                ]
                            }
                        }
                    },
                }
            ],
            "total": {"value": 1},
        }
    })
    svc = _make_service(es)

    result = await svc.search_cargo("tenant_a", container_number="CONT-100")

    assert len(result["data"]) == 1
    assert result["data"][0]["container_number"] == "CONT-100"
    assert result["data"][0]["job_id"] == "JOB_1"
    assert result["pagination"]["total"] == 1


@pytest.mark.asyncio
async def test_search_cargo_by_description():
    """search_cargo should support text search on description.

    Validates: Requirement 6.5
    """
    es = _make_es_mock()
    es.search_documents = AsyncMock(return_value={
        "hits": {
            "hits": [
                {
                    "_source": {
                        "job_id": "JOB_2",
                        "job_type": "cargo_transport",
                        "status": "assigned",
                        "origin": "Dock A",
                        "destination": "Dock B",
                    },
                    "inner_hits": {
                        "cargo_manifest": {
                            "hits": {
                                "hits": [
                                    {
                                        "_source": {
                                            "item_id": "CARGO_y",
                                            "description": "Cement bags",
                                            "weight_kg": 150.0,
                                            "container_number": None,
                                            "item_status": "pending",
                                        }
                                    }
                                ]
                            }
                        }
                    },
                }
            ],
            "total": {"value": 1},
        }
    })
    svc = _make_service(es)

    result = await svc.search_cargo("tenant_a", description="Cement")

    assert len(result["data"]) == 1
    assert result["data"][0]["description"] == "Cement bags"

    # Verify the ES query used a match query for description
    query_body = es.search_documents.await_args.args[1]
    nested_must = query_body["query"]["bool"]["must"][1]["nested"]["query"]["bool"]["must"]
    assert any("match" in f and "cargo_manifest.description" in f["match"] for f in nested_must)


@pytest.mark.asyncio
async def test_search_cargo_by_item_status():
    """search_cargo should filter by item_status using a term query.

    Validates: Requirement 6.5
    """
    es = _make_es_mock()
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [], "total": {"value": 0}}
    })
    svc = _make_service(es)

    await svc.search_cargo("tenant_a", item_status="delivered")

    query_body = es.search_documents.await_args.args[1]
    nested_must = query_body["query"]["bool"]["must"][1]["nested"]["query"]["bool"]["must"]
    assert any(
        "term" in f and f["term"].get("cargo_manifest.item_status") == "delivered"
        for f in nested_must
    )


@pytest.mark.asyncio
async def test_search_cargo_no_filters_raises_validation_error():
    """search_cargo should raise a validation error when no filters are provided.

    Validates: Requirement 6.5
    """
    es = _make_es_mock()
    svc = _make_service(es)

    with pytest.raises(AppException) as exc_info:
        await svc.search_cargo("tenant_a")

    assert exc_info.value.status_code == 400
    assert "filter" in exc_info.value.message.lower()
