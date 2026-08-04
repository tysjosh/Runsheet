"""
Unit tests for ``DutyStatusService`` (``driver/services/duty_status_service.py``).

Covers the accepted driver transitions, the two rejections, the event document
shape, the append-only discipline, the write order that makes R13.17 and R13.18
distinguishable, the 202 on a lagging projection, the read-time reconciliation,
and the history range read.

Validates: Requirements 13.1, 13.2, 13.3, 13.6, 13.7, 13.11, 13.12, 13.13,
13.14, 13.15, 13.17, 13.18, 13.19, 13.20, 13.22, 17.28
"""

import pytest

from driver.services.driver_es_mappings import (
    DUTY_STATUS_EVENTS_INDEX,
    DUTY_STATUS_EVENTS_MAPPING,
)
from driver.services.duty_status_service import (
    DRIVER_SETTABLE_STATUSES,
    DutyStatusService,
    new_ulid,
)
from errors.codes import ErrorCode
from errors.exceptions import AppException

TENANT = "t1"
OTHER_TENANT = "t2"
DRIVER = "drv-1"
ADMIN = "admin-9"
NOW = "2026-02-03T08:00:00+00:00"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeES:
    """Records every write so the append-only rule can actually be asserted."""

    def __init__(self, *, by_index=None, index_error=None):
        self._by_index = by_index or {}
        self._index_error = index_error
        self.indexed: list[tuple[str, str, dict]] = []
        self.updated: list[tuple[str, str, dict]] = []
        self.deleted: list[tuple[str, str]] = []
        self.searches: list[tuple[str, dict]] = []

    async def index_document(self, index, doc_id, document):
        if self._index_error is not None and index == DUTY_STATUS_EVENTS_INDEX:
            raise self._index_error
        self.indexed.append((index, doc_id, dict(document)))
        self._by_index.setdefault(index, []).append(dict(document))
        return {"result": "created"}

    async def update_document(self, index, doc_id, partial_doc):
        self.updated.append((index, doc_id, dict(partial_doc)))
        return {"result": "updated"}

    async def delete_document(self, index, doc_id):  # pragma: no cover
        self.deleted.append((index, doc_id))
        return {"result": "deleted"}

    async def search_documents(self, index, query, size=100):
        self.searches.append((index, query))
        sources = list(self._by_index.get(index, []))
        sources = self._apply_filters(sources, query)
        sources = self._apply_sort(sources, query)
        return {"hits": {"hits": [{"_source": s} for s in sources[:size]]}}

    @classmethod
    def _apply_filters(cls, sources, query):
        clauses = cls._clauses(query.get("query", {}))
        return [
            source
            for source in sources
            if all(cls._matches(source, clause) for clause in clauses)
        ]

    @classmethod
    def _clauses(cls, node):
        """Flatten the ``bool`` tree ``inject_tenant_filter`` may nest."""
        bool_node = node.get("bool", {}) if isinstance(node, dict) else {}
        out = []
        for key in ("filter", "must"):
            for clause in bool_node.get(key, []) or []:
                if isinstance(clause, dict) and "bool" in clause:
                    out.extend(cls._clauses(clause))
                else:
                    out.append(clause)
        return out

    @staticmethod
    def _matches(source, clause):
        if "term" in clause:
            field, value = next(iter(clause["term"].items()))
            return source.get(field) == value
        if "terms" in clause:
            field, values = next(iter(clause["terms"].items()))
            return source.get(field) in values
        if "range" in clause:
            field, bounds = next(iter(clause["range"].items()))
            value = source.get(field)
            if value is None:
                return False
            if "gte" in bounds and value < bounds["gte"]:
                return False
            if "lte" in bounds and value > bounds["lte"]:
                return False
            return True
        if "match_all" in clause:
            return True
        return True

    @staticmethod
    def _apply_sort(sources, query):
        for spec in reversed(query.get("sort", []) or []):
            field, options = next(iter(spec.items()))
            reverse = options.get("order") == "desc"
            sources = sorted(
                sources, key=lambda s: (s.get(field) or ""), reverse=reverse
            )
        return sources


class _FakeDriverRepository:
    """Stands in for ``DriverRepository`` on the projection write."""

    def __init__(self, *, record=None, update_error=None, missing=False):
        self.record = record
        self._update_error = update_error
        self._missing = missing
        self.updates: list[dict] = []

    async def get(self, tenant_id, driver_id):
        if self.record is None:
            return None
        if self.record.get("tenant_id") != tenant_id:
            return None
        return dict(self.record)

    async def update(self, tenant_id, driver_id, updates):
        if self._update_error is not None:
            raise self._update_error
        if self._missing or self.record is None:
            return None
        self.updates.append(dict(updates))
        self.record.update(updates)
        return dict(self.record)


class _FakeOrderRepository:
    def __init__(self, *, orders=None):
        self._orders = orders or []
        self.calls: list[dict] = []

    async def search_for_driver(
        self, tenant_id, driver_id, *, statuses=(), page=1, size=50, **kwargs
    ):
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "driver_id": driver_id,
                "statuses": tuple(statuses),
                "size": size,
            }
        )
        matching = [o for o in self._orders if o.get("status") in tuple(statuses)]
        return {
            "orders": matching,
            "total": len(matching),
            "page": page,
            "size": size,
        }


def _driver_record(status="off_duty", **overrides):
    record = {
        "driver_id": DRIVER,
        "tenant_id": TENANT,
        "driver_name": "Ada",
        "status": status,
    }
    record.update(overrides)
    return record


def _service(*, es=None, driver_repo=None, order_repo=None):
    return DutyStatusService(
        es_service=es if es is not None else _FakeES(),
        driver_repository=driver_repo,
        order_repository=order_repo,
    )


# ---------------------------------------------------------------------------
# ULID / document id
# ---------------------------------------------------------------------------


def test_new_ulid_is_26_chars_and_sorts_by_creation():
    """Doc ids must sort by creation, which is why a ULID is used at all."""
    early = new_ulid(now_ms=1_700_000_000_000)
    later = new_ulid(now_ms=1_700_000_001_000)

    assert len(early) == 26 and len(later) == 26
    assert early < later
    assert new_ulid(now_ms=1_700_000_000_000) != early  # random tail


# ---------------------------------------------------------------------------
# Accepted transitions (R13.1, R13.3, R13.7, R13.12)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", DRIVER_SETTABLE_STATUSES)
@pytest.mark.asyncio
async def test_driver_may_set_active_on_break_and_off_duty(status):
    """R13.1: the three driver-settable values are accepted."""
    es = _FakeES()
    repo = _FakeDriverRepository(record=_driver_record("active"))
    service = _service(es=es, driver_repo=repo, order_repo=_FakeOrderRepository())

    result = await service.transition(
        TENANT, DRIVER, status, actor_id=DRIVER, source="driver",
        event_timestamp=NOW,
    )

    assert result["new_status"] == status
    assert result["projection_applied"] is True


@pytest.mark.asyncio
async def test_transition_appends_one_event_then_projects_it():
    """R13.3, R13.7, R13.12: one event, then the projection, in that order."""
    es = _FakeES()
    repo = _FakeDriverRepository(record=_driver_record("off_duty"))
    service = _service(es=es, driver_repo=repo)

    result = await service.transition(
        TENANT, DRIVER, "active", actor_id=DRIVER, source="driver",
        event_timestamp=NOW,
    )

    assert len(es.indexed) == 1
    index, doc_id, event = es.indexed[0]
    assert index == DUTY_STATUS_EVENTS_INDEX
    tenant_part, driver_part, ulid_part = doc_id.split(":")
    assert (tenant_part, driver_part) == (TENANT, DRIVER)
    assert len(ulid_part) == 26
    assert event["event_id"] == doc_id
    assert event["tenant_id"] == TENANT
    assert event["driver_id"] == DRIVER
    assert event["previous_status"] == "off_duty"
    assert event["new_status"] == "active"
    assert event["event_timestamp"] == NOW
    assert event["server_received_at"]
    assert event["actor_id"] == DRIVER
    assert event["source"] == "driver"

    # The projection carries the value plus the bookkeeping pair.
    assert repo.updates == [
        {
            "status": "active",
            "duty_status_event_id": event["event_id"],
            "duty_status_updated_at": event["server_received_at"],
        }
    ]
    assert result["previous_status"] == "off_duty"


@pytest.mark.asyncio
async def test_first_event_for_a_driver_carries_a_null_previous_status():
    """The mapping declares ``previous_status`` nullable for exactly this case."""
    es = _FakeES()
    service = _service(es=es, driver_repo=_FakeDriverRepository(record=None))

    with pytest.raises(AppException):
        # No drivers_current record means the projection cannot land, which is
        # the R13.18 path; the event itself is still appended.
        await service.transition(
            TENANT, DRIVER, "active", actor_id="system", source="system",
            event_timestamp=NOW,
        )

    assert es.indexed[0][2]["previous_status"] is None


@pytest.mark.asyncio
async def test_admin_set_inactive_is_recorded_with_the_admin_as_actor():
    """R13.19: an administrator-set ``inactive`` is an event like any other."""
    es = _FakeES()
    repo = _FakeDriverRepository(record=_driver_record("active"))
    service = _service(es=es, driver_repo=repo)

    result = await service.transition(
        TENANT, DRIVER, "inactive", actor_id=ADMIN, source="admin",
        event_timestamp=NOW, reason="terminated",
    )

    event = es.indexed[0][2]
    assert event["new_status"] == "inactive"
    assert event["actor_id"] == ADMIN
    assert event["source"] == "admin"
    assert event["reason"] == "terminated"
    assert result["new_status"] == "inactive"


# ---------------------------------------------------------------------------
# Rejections (R13.2, R13.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_driver_submitted_inactive_is_forbidden_and_writes_nothing():
    """R13.2: ``inactive`` stays an administrator-set value."""
    es = _FakeES()
    repo = _FakeDriverRepository(record=_driver_record("active"))
    service = _service(es=es, driver_repo=repo)

    with pytest.raises(AppException) as excinfo:
        await service.transition(
            TENANT, DRIVER, "inactive", actor_id=DRIVER, source="driver",
            event_timestamp=NOW,
        )

    assert excinfo.value.error_code == ErrorCode.FORBIDDEN
    assert excinfo.value.status_code == 403
    assert es.indexed == []
    assert repo.updates == []


@pytest.mark.asyncio
async def test_off_duty_is_rejected_while_an_order_is_in_transit():
    """R13.6: 409 ``ACTIVE_DELIVERY_IN_PROGRESS``, nothing written."""
    es = _FakeES()
    repo = _FakeDriverRepository(record=_driver_record("active"))
    orders = _FakeOrderRepository(
        orders=[{"order_id": "ord-7", "status": "in_transit"}]
    )
    service = _service(es=es, driver_repo=repo, order_repo=orders)

    with pytest.raises(AppException) as excinfo:
        await service.transition(
            TENANT, DRIVER, "off_duty", actor_id=DRIVER, source="driver",
            event_timestamp=NOW,
        )

    assert excinfo.value.error_code == ErrorCode.ACTIVE_DELIVERY_IN_PROGRESS
    assert excinfo.value.status_code == 409
    assert excinfo.value.details["order_id"] == "ord-7"
    assert es.indexed == []
    assert repo.updates == []


@pytest.mark.asyncio
async def test_on_break_is_allowed_while_an_order_is_in_transit():
    """The gate is scoped to ``off_duty`` — a break mid-run is legitimate."""
    es = _FakeES()
    repo = _FakeDriverRepository(record=_driver_record("active"))
    orders = _FakeOrderRepository(
        orders=[{"order_id": "ord-7", "status": "in_transit"}]
    )
    service = _service(es=es, driver_repo=repo, order_repo=orders)

    result = await service.transition(
        TENANT, DRIVER, "on_break", actor_id=DRIVER, source="driver",
        event_timestamp=NOW,
    )

    assert result["new_status"] == "on_break"
    assert orders.calls == []


@pytest.mark.asyncio
async def test_unknown_status_and_unknown_source_are_rejected():
    es = _FakeES()
    service = _service(es=es, driver_repo=_FakeDriverRepository(record=_driver_record()))

    with pytest.raises(AppException) as status_exc:
        await service.transition(
            TENANT, DRIVER, "on_duty", actor_id=DRIVER, source="driver",
            event_timestamp=NOW,
        )
    with pytest.raises(AppException) as source_exc:
        await service.transition(
            TENANT, DRIVER, "active", actor_id=DRIVER, source="dispatcher",
            event_timestamp=NOW,
        )

    assert status_exc.value.error_code == ErrorCode.INVALID_REQUEST
    assert source_exc.value.error_code == ErrorCode.INVALID_REQUEST
    assert es.indexed == []


# ---------------------------------------------------------------------------
# Durability split (R13.13, R13.17, R13.18)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_failure_rejects_the_transition_and_leaves_the_projection():
    """R13.17: no projection value may exist without an event behind it."""
    es = _FakeES(index_error=RuntimeError("es down"))
    repo = _FakeDriverRepository(record=_driver_record("active"))
    service = _service(es=es, driver_repo=repo)

    with pytest.raises(RuntimeError):
        await service.transition(
            TENANT, DRIVER, "off_duty", actor_id=DRIVER, source="admin",
            event_timestamp=NOW,
        )

    assert repo.updates == []
    assert repo.record["status"] == "active"


@pytest.mark.asyncio
async def test_projection_failure_after_a_durable_append_is_a_202():
    """R13.18: a 2xx carrying an error code, so the offline queue dequeues."""
    es = _FakeES()
    repo = _FakeDriverRepository(
        record=_driver_record("active"), update_error=RuntimeError("es down")
    )
    service = _service(es=es, driver_repo=repo)

    with pytest.raises(AppException) as excinfo:
        await service.transition(
            TENANT, DRIVER, "off_duty", actor_id=ADMIN, source="admin",
            event_timestamp=NOW,
        )

    exc = excinfo.value
    assert exc.error_code == ErrorCode.DUTY_STATUS_PROJECTION_PENDING
    assert 200 <= exc.status_code < 300
    # The event is durable — that is the whole reason a retry must not happen.
    assert len(es.indexed) == 1
    assert exc.details["event_id"] == es.indexed[0][2]["event_id"]


@pytest.mark.asyncio
async def test_two_transitions_append_two_documents_and_update_none():
    """R13.13: append-only — a new id every time, no update, no delete."""
    es = _FakeES()
    repo = _FakeDriverRepository(record=_driver_record("off_duty"))
    service = _service(es=es, driver_repo=repo)

    await service.transition(
        TENANT, DRIVER, "active", actor_id=DRIVER, source="driver",
        event_timestamp=NOW,
    )
    await service.transition(
        TENANT, DRIVER, "on_break", actor_id=DRIVER, source="driver",
        event_timestamp="2026-02-03T09:00:00+00:00",
    )

    event_ids = [doc_id for _, doc_id, _ in es.indexed]
    assert len(event_ids) == len(set(event_ids)) == 2
    assert es.deleted == []
    assert [i for i, _, _ in es.updated if i == DUTY_STATUS_EVENTS_INDEX] == []
    # The second event's previous_status is the first event's new_status.
    assert es.indexed[1][2]["previous_status"] == "active"


def test_event_mapping_declares_no_eld_fields():
    """R13.22, R17.28: no certification, edit history, or annotation field."""
    properties = DUTY_STATUS_EVENTS_MAPPING["mappings"]["properties"]

    assert not [
        field
        for field in properties
        if any(
            token in field
            for token in ("certif", "edit", "annotat", "hos", "hours_of_service")
        )
    ]


# ---------------------------------------------------------------------------
# current() — reconciliation (R13.14, R13.15, R13.18)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_returns_the_latest_event_value():
    """R13.15: the greatest ``server_received_at`` wins."""
    es = _FakeES(
        by_index={
            DUTY_STATUS_EVENTS_INDEX: [
                {
                    "event_id": "e1", "tenant_id": TENANT, "driver_id": DRIVER,
                    "new_status": "active",
                    "server_received_at": "2026-02-03T08:00:00+00:00",
                },
                {
                    "event_id": "e2", "tenant_id": TENANT, "driver_id": DRIVER,
                    "new_status": "on_break",
                    "server_received_at": "2026-02-03T09:00:00+00:00",
                },
            ]
        }
    )
    repo = _FakeDriverRepository(record=_driver_record("on_break"))

    assert await _service(es=es, driver_repo=repo).current(TENANT, DRIVER) == (
        "on_break"
    )
    assert repo.updates == []  # already in agreement, nothing to repair


@pytest.mark.asyncio
async def test_current_reconciles_a_stale_projection_from_the_event_log():
    """R13.18: the projection is repaired on the next read."""
    es = _FakeES(
        by_index={
            DUTY_STATUS_EVENTS_INDEX: [
                {
                    "event_id": "e9", "tenant_id": TENANT, "driver_id": DRIVER,
                    "new_status": "off_duty",
                    "server_received_at": "2026-02-03T09:00:00+00:00",
                }
            ]
        }
    )
    repo = _FakeDriverRepository(record=_driver_record("active"))

    result = await _service(es=es, driver_repo=repo).current(TENANT, DRIVER)

    assert result == "off_duty"
    assert repo.updates == [
        {
            "status": "off_duty",
            "duty_status_event_id": "e9",
            "duty_status_updated_at": "2026-02-03T09:00:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_current_falls_back_to_the_projection_when_there_is_no_event():
    """A driver created before the event log existed still has a status."""
    repo = _FakeDriverRepository(record=_driver_record("inactive"))

    assert await _service(driver_repo=repo).current(TENANT, DRIVER) == "inactive"


@pytest.mark.asyncio
async def test_current_is_404_when_the_tenant_holds_nothing_for_the_driver():
    with pytest.raises(AppException) as excinfo:
        await _service(driver_repo=_FakeDriverRepository(record=None)).current(
            TENANT, DRIVER
        )

    assert excinfo.value.error_code == ErrorCode.RESOURCE_NOT_FOUND


# ---------------------------------------------------------------------------
# history() (R13.20)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_returns_in_range_events_sorted_ascending():
    """R13.20: the range closes over ``event_timestamp``, oldest first."""
    es = _FakeES(
        by_index={
            DUTY_STATUS_EVENTS_INDEX: [
                {
                    "event_id": "e2", "tenant_id": TENANT, "driver_id": DRIVER,
                    "new_status": "on_break",
                    "event_timestamp": "2026-02-03T10:00:00+00:00",
                },
                {
                    "event_id": "e1", "tenant_id": TENANT, "driver_id": DRIVER,
                    "new_status": "active",
                    "event_timestamp": "2026-02-03T08:00:00+00:00",
                },
                {
                    "event_id": "e3", "tenant_id": TENANT, "driver_id": DRIVER,
                    "new_status": "off_duty",
                    "event_timestamp": "2026-02-04T08:00:00+00:00",
                },
                {
                    "event_id": "x1", "tenant_id": OTHER_TENANT,
                    "driver_id": DRIVER, "new_status": "active",
                    "event_timestamp": "2026-02-03T09:00:00+00:00",
                },
                {
                    "event_id": "o1", "tenant_id": TENANT, "driver_id": "drv-2",
                    "new_status": "active",
                    "event_timestamp": "2026-02-03T09:00:00+00:00",
                },
            ]
        }
    )

    events = await _service(es=es).history(
        TENANT,
        DRIVER,
        range_start="2026-02-03T00:00:00+00:00",
        range_end="2026-02-03T23:59:59+00:00",
    )

    assert [e["event_id"] for e in events] == ["e1", "e2"]


@pytest.mark.asyncio
async def test_history_rejects_an_unparseable_or_inverted_range():
    service = _service()

    with pytest.raises(AppException) as bad:
        await service.history(
            TENANT, DRIVER, range_start="yesterday", range_end=NOW
        )
    with pytest.raises(AppException) as inverted:
        await service.history(
            TENANT, DRIVER, range_start="2026-02-04T00:00:00+00:00",
            range_end=NOW,
        )

    assert bad.value.error_code == ErrorCode.INVALID_REQUEST
    assert inverted.value.error_code == ErrorCode.INVALID_REQUEST
