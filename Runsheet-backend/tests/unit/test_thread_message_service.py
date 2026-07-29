"""
Unit tests for ``ThreadMessageService`` — the extracted messaging rule.

Covers the two authorization holes the extraction closes (a body-asserted
``sender_id``, and thread reads that used to skip the assignment check), the
delivery fan-out, and the ``timestamp``-ascending pagination envelope.

Validates: Requirements 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 7.12, 7.17, 15.14
"""

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Patch the ElasticsearchService singleton before imports that trigger it
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from driver.models import MessageRequest
from driver.services.message_service import ThreadMessageService
from driver.services.work_ref import WorkRef
from errors.codes import ErrorCode
from errors.exceptions import AppException

TENANT_ID = "t1"
DRIVER_ID = "DRV_1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _es(hits=None, total=0) -> MagicMock:
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"total": {"value": total}, "hits": hits or []}}
    )
    return es


def _job_ref(**job_doc) -> WorkRef:
    doc = {"job_id": "JOB_1", "tenant_id": TENANT_ID, "asset_assigned": "user-1"}
    doc.update(job_doc)
    return WorkRef(TENANT_ID, DRIVER_ID, "job", "JOB_1", job_doc=doc)


def _order_ref() -> WorkRef:
    return WorkRef(
        TENANT_ID,
        DRIVER_ID,
        "order",
        "ORD_1",
        order_doc={
            "order_id": "ORD_1",
            "tenant_id": TENANT_ID,
            "assigned_driver_id": DRIVER_ID,
        },
    )


def _service(es=None, driver_ws=None, scheduling_ws=None) -> ThreadMessageService:
    return ThreadMessageService(
        es_service=es or _es(),
        driver_ws_manager=driver_ws,
        scheduling_ws_manager=scheduling_ws,
    )


def _body(text="Arrived", sender_id=DRIVER_ID, sender_role="driver") -> MessageRequest:
    return MessageRequest(body=text, sender_id=sender_id, sender_role=sender_role)


# ---------------------------------------------------------------------------
# send — derived sender identity (R7.5, R7.6, R7.7)
# ---------------------------------------------------------------------------


class TestSenderIdentity:
    @pytest.mark.asyncio
    async def test_persists_derived_identity_not_body_role(self):
        """The derived role wins; the body's ``sender_role`` is ignored (R7.7)."""
        es = _es()
        svc = _service(es)

        result = await svc.send(
            _job_ref(),
            _body(sender_role="dispatcher"),
            sender_id=DRIVER_ID,
            sender_role="driver",
            request_id="req-1",
        )

        doc = es.index_document.call_args.args[2]
        assert doc["sender_id"] == DRIVER_ID
        assert doc["sender_role"] == "driver"
        assert result["data"]["sender_role"] == "driver"
        assert result["request_id"] == "req-1"

    @pytest.mark.asyncio
    async def test_body_sender_id_mismatch_is_rejected(self):
        """A body naming another sender is 403 SENDER_IDENTITY_MISMATCH (R7.6)."""
        es = _es()
        svc = _service(es)

        with pytest.raises(AppException) as exc:
            await svc.send(
                _job_ref(),
                _body(sender_id="DRV_OTHER"),
                sender_id=DRIVER_ID,
                sender_role="driver",
                request_id="req-1",
            )

        assert exc.value.error_code == ErrorCode.SENDER_IDENTITY_MISMATCH
        assert exc.value.status_code == 403
        # Details name the work reference only (R15.14).
        assert exc.value.details == {"job_id": "JOB_1"}
        es.index_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsupported_role_is_rejected_without_echoing_held_role(self):
        svc = _service()

        with pytest.raises(AppException) as exc:
            await svc.send(
                _job_ref(),
                _body(),
                sender_id=DRIVER_ID,
                sender_role="accountant",
                request_id="req-1",
            )

        assert exc.value.status_code == 403
        assert "accountant" not in str(exc.value.details)


# ---------------------------------------------------------------------------
# send — persistence shape and delivery (R7.10, R7.15)
# ---------------------------------------------------------------------------


class TestSendPersistenceAndDelivery:
    @pytest.mark.asyncio
    async def test_job_keyed_document_shape_is_unchanged(self):
        es = _es()
        await _service(es).send(
            _job_ref(),
            _body(),
            sender_id=DRIVER_ID,
            sender_role="driver",
            request_id="req-1",
        )

        index, doc_id, doc = es.index_document.call_args.args
        assert index == "job_messages"
        assert doc_id == doc["message_id"]
        assert set(doc) == {
            "message_id",
            "job_id",
            "sender_id",
            "sender_role",
            "body",
            "timestamp",
            "tenant_id",
        }

    @pytest.mark.asyncio
    async def test_order_keyed_document_carries_order_and_driver(self):
        es = _es()
        await _service(es).send(
            _order_ref(),
            _body(),
            sender_id=DRIVER_ID,
            sender_role="driver",
            request_id="req-1",
        )

        doc = es.index_document.call_args.args[2]
        assert doc["order_id"] == "ORD_1"
        assert doc["driver_id"] == DRIVER_ID
        assert "job_id" not in doc

    @pytest.mark.asyncio
    async def test_delivers_to_driver_and_dispatchers(self):
        driver_ws = MagicMock()
        driver_ws.send_to_driver = AsyncMock(return_value=True)
        scheduling_ws = MagicMock()
        scheduling_ws.broadcast = AsyncMock(return_value=1)

        await _service(driver_ws=driver_ws, scheduling_ws=scheduling_ws).send(
            _job_ref(assigned_driver_id=DRIVER_ID),
            _body(),
            sender_id=DRIVER_ID,
            sender_role="driver",
            request_id="req-1",
        )

        assert driver_ws.send_to_driver.call_args.args[0] == DRIVER_ID
        assert scheduling_ws.broadcast.call_args.args[0] == "job_message"

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_fail_the_message(self):
        driver_ws = MagicMock()
        driver_ws.send_to_driver = AsyncMock(side_effect=RuntimeError("socket gone"))
        es = _es()

        result = await _service(es, driver_ws=driver_ws).send(
            _job_ref(),
            _body(),
            sender_id=DRIVER_ID,
            sender_role="driver",
            request_id="req-1",
        )

        assert result["data"]["body"] == "Arrived"
        es.index_document.assert_called_once()


# ---------------------------------------------------------------------------
# list — assignment-scoped, timestamp ascending (R7.8, R7.9, R7.12)
# ---------------------------------------------------------------------------


class TestListThread:
    @pytest.mark.asyncio
    async def test_query_is_thread_and_tenant_scoped_sorted_ascending(self):
        es = _es()
        await _service(es).list(_job_ref(), page=2, size=25, request_id="req-1")

        index, query = es.search_documents.call_args.args[:2]
        assert index == "job_messages"
        assert {"term": {"job_id": "JOB_1"}} in query["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": TENANT_ID}} in query["query"]["bool"]["filter"]
        assert query["sort"] == [{"timestamp": {"order": "asc"}}]
        assert query["from"] == 25
        assert query["size"] == 25

    @pytest.mark.asyncio
    async def test_order_keyed_read_filters_on_order_id(self):
        es = _es()
        await _service(es).list(_order_ref(), page=1, size=50, request_id="req-1")

        query = es.search_documents.call_args.args[1]
        assert {"term": {"order_id": "ORD_1"}} in query["query"]["bool"]["filter"]

    @pytest.mark.asyncio
    async def test_pagination_envelope(self):
        es = _es(hits=[{"_source": {"message_id": "m1"}}], total=7)

        result = await _service(es).list(
            _job_ref(), page=1, size=3, request_id="req-1"
        )

        assert result["data"] == [{"message_id": "m1"}]
        assert result["pagination"] == {
            "page": 1,
            "size": 3,
            "total": 7,
            "total_pages": 3,
        }
        assert result["request_id"] == "req-1"

    @pytest.mark.asyncio
    async def test_empty_thread_reports_one_page(self):
        result = await _service().list(_job_ref(), page=1, size=50, request_id="r")

        assert result["data"] == []
        assert result["pagination"]["total"] == 0
        assert result["pagination"]["total_pages"] == 1
