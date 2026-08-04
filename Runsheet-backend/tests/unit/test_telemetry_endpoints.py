"""
Unit tests for breadcrumb ingestion — the router
(``driver/api/telemetry_endpoints.py``) over a real
:class:`~driver.services.telemetry_service.DriverTelemetryService`.

The service is wired to fakes rather than mocked, so the assertions cover the
whole path: the composite document id
``{tenant_id}:{driver_id}:{sample_timestamp_epoch_ms}``, the ``op_type=create``
write that retains an already-stored sample, the two discard filters, and the
``driver_presence.last_location`` refresh.

The fake Elasticsearch client below implements the parts of the bulk contract
the service depends on — ``create`` actions, ``created`` results, and the 409
status a duplicate id produces — so the dedup assertions exercise the real code
path rather than a stub of it.

Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import driver.middleware.idempotency as idempotency_module
from driver.api.telemetry_endpoints import (
    configure_telemetry_endpoints,
    router as telemetry_router,
)
from driver.middleware.idempotency import configure_idempotency_middleware
from driver.services.driver_es_mappings import (
    DRIVER_BREADCRUMBS_INDEX,
    DRIVER_PRESENCE_INDEX,
    IDEMPOTENCY_KEYS_INDEX,
)
from driver.services.telemetry_service import (
    MAX_ACCURACY_METERS,
    breadcrumb_doc_id,
)
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

ENDPOINT = "/api/driver/telemetry/breadcrumbs"

TENANT = "t1"
DRIVER = "drv_1"


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBulkClient:
    """The parts of the raw ES client the breadcrumb write uses.

    ``bulk`` honours ``create`` semantics: a document id already present is
    answered with status 409 and the stored source is left untouched, which is
    what R10.8 turns on.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, dict] = {}
        self.bulk_calls: List[list] = []

    def bulk(self, *, body, refresh=False):
        self.bulk_calls.append(list(body))
        items = []
        for action, document in zip(body[::2], body[1::2]):
            doc_id = action["create"]["_id"]
            if doc_id in self.docs:
                items.append({"create": {"_id": doc_id, "status": 409}})
                continue
            self.docs[doc_id] = dict(document)
            items.append(
                {"create": {"_id": doc_id, "status": 201, "result": "created"}}
            )
        return {"errors": False, "items": items}


class FakeES:
    """Elasticsearch service stand-in: a raw bulk client plus presence writes."""

    def __init__(self, *, presence_exists: bool = True) -> None:
        self.client = FakeBulkClient()
        self.updates: List[tuple] = []
        self.indexed: List[tuple] = []
        self._presence_exists = presence_exists

    async def update_document(self, index, doc_id, partial_doc):
        if index == DRIVER_PRESENCE_INDEX and not self._presence_exists:
            raise RuntimeError("document_missing_exception")
        self.updates.append((index, doc_id, dict(partial_doc)))
        return {"result": "updated"}

    async def index_document(self, index, doc_id, document):
        self.indexed.append((index, doc_id, dict(document)))
        return {"result": "created"}

    def breadcrumbs(self) -> Dict[str, dict]:
        return self.client.docs


class FakeIdempotencyES:
    """In-memory stand-in for the ``idempotency_keys`` index."""

    def __init__(self) -> None:
        self.docs: Dict[str, dict] = {}

    async def index_document(self, index: str, doc_id: str, document: dict) -> None:
        assert index == IDEMPOTENCY_KEYS_INDEX
        self.docs[doc_id] = document

    async def get_document(self, index: str, doc_id: str):
        assert index == IDEMPOTENCY_KEYS_INDEX
        return self.docs.get(doc_id)


@pytest.fixture
def idempotency_store():
    previous = idempotency_module.get_idempotency_middleware()
    store = FakeIdempotencyES()
    configure_idempotency_middleware(es_service=store)
    try:
        yield store
    finally:
        idempotency_module._idempotency_middleware = previous


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(*, es_service: Any = None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(telemetry_router)
    configure_telemetry_endpoints(es_service=es_service)
    install_test_auth(app)
    return app


def _driver_headers(driver_id: str = DRIVER, **kwargs) -> dict:
    kwargs.setdefault("roles", ["driver"])
    return auth_headers(TENANT, sub="user-1", driver_id=driver_id, **kwargs)


def _sample(
    *,
    latitude: float = 30.2672,
    longitude: float = -97.7431,
    sample_timestamp: Optional[str] = None,
    accuracy_meters: Optional[float] = 8.5,
    speed_mph: Optional[float] = 47.5,
    heading_degrees: Optional[float] = 182.0,
) -> dict:
    return {
        "latitude": latitude,
        "longitude": longitude,
        "sample_timestamp": sample_timestamp or _iso(_now()),
        "accuracy_meters": accuracy_meters,
        "speed_mph": speed_mph,
        "heading_degrees": heading_degrees,
    }


def _post(client: TestClient, samples: List[dict], **kwargs):
    headers = kwargs.pop("headers", None) or _driver_headers()
    return client.post(ENDPOINT, json={"samples": samples}, headers=headers)


# ---------------------------------------------------------------------------
# Accepted batches (R10.1, R10.2, R10.3)
# ---------------------------------------------------------------------------


class TestAcceptedBatch:
    """The R10.1 field set, the derived identity, and the composite id."""

    def test_persists_each_sample_on_the_composite_document_id(self):
        """Validates: Requirements 10.1, 10.2, 10.3"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        stamp = _now().replace(microsecond=0)

        resp = _post(client, [_sample(sample_timestamp=_iso(stamp))])

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["submitted_count"] == 1
        assert data["retained_count"] == 1
        assert data["stored_count"] == 1
        assert data["discarded_count"] == 0

        expected_id = breadcrumb_doc_id(TENANT, DRIVER, stamp)
        assert list(es.breadcrumbs()) == [expected_id]
        doc = es.breadcrumbs()[expected_id]
        assert doc["tenant_id"] == TENANT
        assert doc["driver_id"] == DRIVER
        assert doc["breadcrumb_id"] == expected_id
        assert doc["location"] == {"lat": 30.2672, "lon": -97.7431}
        assert doc["accuracy_meters"] == 8.5
        assert doc["speed_mph"] == 47.5
        assert doc["heading_degrees"] == 182.0
        assert doc["sample_timestamp"] == _iso(stamp)
        assert doc["server_received_at"]
        assert doc["batch_id"] == data["batch_id"]

    def test_the_track_write_names_the_breadcrumbs_index_only(self):
        """The track store is distinct from Driver_Presence (R10.3)."""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        _post(client, [_sample()])

        actions = es.client.bulk_calls[0]
        assert all(
            action["create"]["_index"] == DRIVER_BREADCRUMBS_INDEX
            for action in actions[::2]
        )

    def test_a_multi_sample_batch_stores_every_sample(self):
        """Validates: Requirements 10.1, 10.3"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        base = _now().replace(microsecond=0)
        samples = [
            _sample(sample_timestamp=_iso(base - timedelta(seconds=60 * n)))
            for n in range(5)
        ]

        resp = _post(client, samples)

        assert resp.status_code == 200
        assert resp.json()["data"]["stored_count"] == 5
        assert len(es.breadcrumbs()) == 5

    def test_unknown_speed_and_heading_sentinels_become_no_reading(self):
        """Device APIs report -1 for "unknown"; that is not a value.

        Validates: Requirements 10.1
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = _post(client, [_sample(speed_mph=-1, heading_degrees=-1)])

        assert resp.status_code == 200
        doc = next(iter(es.breadcrumbs().values()))
        assert doc["speed_mph"] is None
        assert doc["heading_degrees"] is None


# ---------------------------------------------------------------------------
# Identity (R10.2)
# ---------------------------------------------------------------------------


class TestDerivedIdentity:
    """The batch subject is the session's driver, never the body's."""

    def test_body_driver_id_is_rejected_outright(self):
        """The submission surface carries no ``driver_id`` at all.

        Validates: Requirements 10.2
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = client.post(
            ENDPOINT,
            json={"samples": [_sample()], "driver_id": "drv_2"},
            headers=_driver_headers(),
        )

        assert resp.status_code == 422
        assert es.breadcrumbs() == {}

    def test_per_sample_driver_id_is_rejected_outright(self):
        """Validates: Requirements 10.2"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        sample = _sample()
        sample["driver_id"] = "drv_2"

        resp = _post(client, [sample])

        assert resp.status_code == 422
        assert es.breadcrumbs() == {}

    def test_every_document_id_carries_the_session_driver(self):
        """Validates: Requirements 10.2, 10.3"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        _post(client, [_sample()], headers=_driver_headers("drv_9"))

        assert all(
            doc_id.startswith(f"{TENANT}:drv_9:") for doc_id in es.breadcrumbs()
        )

    def test_non_driver_role_cannot_submit(self):
        client = TestClient(_make_app(es_service=FakeES()))

        resp = client.post(
            ENDPOINT,
            json={"samples": [_sample()]},
            headers=auth_headers(TENANT, roles=["dispatcher"]),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"

    def test_missing_driver_identity_is_rejected(self):
        client = TestClient(_make_app(es_service=FakeES()))

        resp = client.post(
            ENDPOINT,
            json={"samples": [_sample()]},
            headers=auth_headers(TENANT, roles=["driver"]),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "DRIVER_IDENTITY_MISSING"


# ---------------------------------------------------------------------------
# The two filters (R10.6, R10.7)
# ---------------------------------------------------------------------------


class TestDiscardFilters:
    """Accuracy above 100 m and stamps older than 24 h are dropped, and counted."""

    def test_accuracy_above_the_ceiling_is_discarded_and_counted(self):
        """Validates: Requirements 10.6"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = _post(
            client, [_sample(accuracy_meters=MAX_ACCURACY_METERS + 0.5)]
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["discarded_count"] == 1
        assert data["discarded"]["accuracy_exceeded"] == 1
        assert data["retained_count"] == 0
        assert es.breadcrumbs() == {}

    def test_accuracy_exactly_at_the_ceiling_is_retained(self):
        """The rule is "exceeds", so 100 m itself survives.

        Validates: Requirements 10.6
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = _post(client, [_sample(accuracy_meters=MAX_ACCURACY_METERS)])

        assert resp.status_code == 200
        assert resp.json()["data"]["retained_count"] == 1
        assert len(es.breadcrumbs()) == 1

    def test_unreadable_accuracy_is_discarded_by_the_same_rule(self):
        """Validates: Requirements 10.6"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = _post(client, [_sample(accuracy_meters=None)])

        assert resp.status_code == 200
        assert resp.json()["data"]["discarded"]["accuracy_exceeded"] == 1
        assert es.breadcrumbs() == {}

    def test_a_stamp_older_than_24_hours_is_discarded_and_counted(self):
        """Validates: Requirements 10.7"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        stale = _now() - timedelta(hours=24, minutes=5)

        resp = _post(client, [_sample(sample_timestamp=_iso(stale))])

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["discarded_count"] == 1
        assert data["discarded"]["stale"] == 1
        assert es.breadcrumbs() == {}

    def test_a_stamp_inside_the_window_is_retained(self):
        """A day-long offline drain is the normal case, not an error.

        Validates: Requirements 10.7
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        recent = _now() - timedelta(hours=23, minutes=55)

        resp = _post(client, [_sample(sample_timestamp=_iso(recent))])

        assert resp.status_code == 200
        assert resp.json()["data"]["retained_count"] == 1

    def test_a_mixed_batch_keeps_the_good_samples_and_counts_the_rest(self):
        """Validates: Requirements 10.6, 10.7"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        base = _now().replace(microsecond=0)

        resp = _post(
            client,
            [
                _sample(sample_timestamp=_iso(base)),
                _sample(
                    sample_timestamp=_iso(base - timedelta(seconds=30)),
                    accuracy_meters=250.0,
                ),
                _sample(
                    sample_timestamp=_iso(base - timedelta(days=3)),
                ),
                _sample(sample_timestamp=_iso(base - timedelta(seconds=60))),
            ],
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["submitted_count"] == 4
        assert data["retained_count"] == 2
        assert data["discarded_count"] == 2
        assert data["discarded"] == {"accuracy_exceeded": 1, "stale": 1}
        assert len(es.breadcrumbs()) == 2


# ---------------------------------------------------------------------------
# Dedup (R10.8)
# ---------------------------------------------------------------------------


class TestDeduplication:
    """A repeated triple retains the stored sample and creates no duplicate."""

    def test_resubmitting_a_batch_creates_no_duplicate(self):
        """The offline queue draining twice must not double the track.

        Validates: Requirements 10.8
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        stamp = _now().replace(microsecond=0)
        samples = [_sample(sample_timestamp=_iso(stamp))]

        first = _post(client, samples)
        second = _post(client, samples)

        assert first.json()["data"]["stored_count"] == 1
        assert second.json()["data"]["stored_count"] == 0
        assert second.json()["data"]["duplicate_count"] == 1
        assert len(es.breadcrumbs()) == 1

    def test_the_stored_sample_is_retained_untouched(self):
        """The first write wins; the repeat does not overwrite it.

        Validates: Requirements 10.8
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        stamp = _now().replace(microsecond=0)

        _post(client, [_sample(sample_timestamp=_iso(stamp), speed_mph=10.0)])
        stored = dict(next(iter(es.breadcrumbs().values())))

        _post(client, [_sample(sample_timestamp=_iso(stamp), speed_mph=99.0)])

        assert next(iter(es.breadcrumbs().values())) == stored
        assert stored["speed_mph"] == 10.0

    def test_two_samples_sharing_a_stamp_collapse_to_one(self):
        """Validates: Requirements 10.8"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        stamp = _now().replace(microsecond=0)

        resp = _post(
            client,
            [
                _sample(sample_timestamp=_iso(stamp)),
                _sample(sample_timestamp=_iso(stamp), latitude=31.0),
            ],
        )

        data = resp.json()["data"]
        assert data["submitted_count"] == 2
        assert data["retained_count"] == 1
        assert data["duplicate_count"] == 1
        assert len(es.breadcrumbs()) == 1

    def test_a_different_driver_is_a_different_sample(self):
        """The id carries the driver, so two drivers never collide.

        Validates: Requirements 10.3, 10.8
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        stamp = _iso(_now().replace(microsecond=0))

        _post(client, [_sample(sample_timestamp=stamp)])
        _post(
            client,
            [_sample(sample_timestamp=stamp)],
            headers=_driver_headers("drv_2"),
        )

        assert len(es.breadcrumbs()) == 2


# ---------------------------------------------------------------------------
# Presence (R10.4, R10.5)
# ---------------------------------------------------------------------------


class TestPresenceRefresh:
    """``last_location`` follows the newest retained sample, or nothing does."""

    def test_presence_takes_the_newest_retained_sample(self):
        """Not the last element sent — the greatest stamp.

        Validates: Requirements 10.4
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        base = _now().replace(microsecond=0)
        newest = base
        older = base - timedelta(minutes=5)

        resp = _post(
            client,
            [
                _sample(
                    sample_timestamp=_iso(newest),
                    latitude=32.0,
                    longitude=-96.0,
                ),
                _sample(
                    sample_timestamp=_iso(older),
                    latitude=29.0,
                    longitude=-98.0,
                ),
            ],
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["presence_updated"] is True
        assert resp.json()["data"]["presence_sample_timestamp"] == _iso(newest)

        presence_writes = [
            call for call in es.updates if call[0] == DRIVER_PRESENCE_INDEX
        ]
        assert len(presence_writes) == 1
        _, doc_id, partial = presence_writes[0]
        assert doc_id == f"{TENANT}:{DRIVER}"
        assert partial["last_location"] == {"lat": 32.0, "lon": -96.0}
        assert "last_seen" in partial

    def test_presence_carries_no_status_field(self):
        """Connection state belongs to the WS manager, not to a batch.

        Validates: Requirements 10.4, 10.19
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        _post(client, [_sample()])

        _, _, partial = es.updates[0]
        assert "status" not in partial

    def test_an_all_discarded_batch_leaves_presence_untouched(self):
        """Validates: Requirements 10.5"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = _post(
            client,
            [
                _sample(accuracy_meters=500.0),
                _sample(sample_timestamp=_iso(_now() - timedelta(days=2))),
            ],
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["discarded_count"] == 2
        assert data["presence_updated"] is False
        assert data["presence_sample_timestamp"] is None
        assert es.updates == []
        assert es.indexed == []

    def test_a_duplicate_only_batch_still_refreshes_presence(self):
        """A retained sample is one that survived the filters, stored or not.

        Validates: Requirements 10.4, 10.8
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        samples = [_sample(sample_timestamp=_iso(_now().replace(microsecond=0)))]

        _post(client, samples)
        es.updates.clear()
        resp = _post(client, samples)

        assert resp.json()["data"]["stored_count"] == 0
        assert resp.json()["data"]["presence_updated"] is True
        assert len(es.updates) == 1

    def test_presence_is_recreated_when_no_record_exists(self):
        """The record is ephemeral and has no history, so recreating loses nothing.

        Validates: Requirements 10.4, 10.19
        """
        es = FakeES(presence_exists=False)
        client = TestClient(_make_app(es_service=es))

        resp = _post(client, [_sample(latitude=33.0, longitude=-95.0)])

        assert resp.status_code == 200
        assert resp.json()["data"]["presence_updated"] is True
        presence_writes = [
            call for call in es.indexed if call[0] == DRIVER_PRESENCE_INDEX
        ]
        assert len(presence_writes) == 1
        _, doc_id, document = presence_writes[0]
        assert doc_id == f"{TENANT}:{DRIVER}"
        assert document["tenant_id"] == TENANT
        assert document["driver_id"] == DRIVER
        assert document["last_location"] == {"lat": 33.0, "lon": -95.0}
        assert "status" not in document


# ---------------------------------------------------------------------------
# Validation and wiring
# ---------------------------------------------------------------------------


class TestValidationAndWiring:
    """A malformed batch persists nothing; an unwired router fails closed."""

    def test_an_empty_batch_is_rejected(self):
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = client.post(
            ENDPOINT, json={"samples": []}, headers=_driver_headers()
        )

        assert resp.status_code == 422
        assert es.breadcrumbs() == {}

    def test_a_batch_above_the_drain_size_is_rejected(self):
        """R10.12 drains in batches of at most 200."""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        base = _now().replace(microsecond=0)
        samples = [
            _sample(sample_timestamp=_iso(base - timedelta(seconds=n)))
            for n in range(201)
        ]

        resp = _post(client, samples)

        assert resp.status_code == 422
        assert es.breadcrumbs() == {}

    def test_a_malformed_timestamp_rejects_the_batch(self):
        """Validates: Requirements 10.1"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = _post(
            client,
            [_sample(), _sample(sample_timestamp="last tuesday")],
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_REQUEST"
        assert es.breadcrumbs() == {}

    def test_an_out_of_range_coordinate_rejects_the_batch(self):
        """Validates: Requirements 10.1"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = _post(client, [_sample(latitude=91.0)])

        assert resp.status_code == 422
        assert es.breadcrumbs() == {}

    def test_an_implausible_speed_rejects_the_batch(self):
        """A unit error caught at entry, not in a route reconstruction.

        Validates: Requirements 10.1
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = _post(client, [_sample(speed_mph=900.0)])

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_REQUEST"
        assert es.breadcrumbs() == {}

    def test_unconfigured_router_returns_a_structured_error(self):
        client = TestClient(
            _make_app(es_service=None), raise_server_exceptions=False
        )

        resp = _post(client, [_sample()])

        assert resp.status_code == 500
        assert resp.json()["error_code"] == "INTERNAL_ERROR"

    def test_the_route_names_no_driver_id(self):
        """R10.2 as a surface property: the write cannot name another driver."""
        for route in telemetry_router.routes:
            if "POST" not in getattr(route, "methods", set()):
                continue
            assert "driver_id" not in route.path
            params = {p.name for p in getattr(route, "dependant").query_params}
            assert "driver_id" not in params


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """A seen key replays; an unseen key is a first-time submission."""

    def test_repeated_key_replays_the_stored_response(self, idempotency_store):
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        headers = {**_driver_headers(), "X-Idempotency-Key": "key-1"}
        samples = [_sample()]

        first = client.post(ENDPOINT, json={"samples": samples}, headers=headers)
        second = client.post(
            ENDPOINT, json={"samples": samples}, headers=headers
        )

        assert first.status_code == 200
        assert second.json() == first.json()
        assert second.headers.get("X-Idempotent-Replayed") == "true"
        assert len(es.client.bulk_calls) == 1

    def test_no_header_means_no_deduplication_by_key(self, idempotency_store):
        """Without a key the batch is reprocessed — and the id still dedups it."""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        samples = [_sample(sample_timestamp=_iso(_now().replace(microsecond=0)))]

        _post(client, samples)
        _post(client, samples)

        assert len(es.client.bulk_calls) == 2
        assert len(es.breadcrumbs()) == 1
