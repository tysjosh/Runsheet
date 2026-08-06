"""A Postgres-backed document store with the ElasticsearchService data-plane API.

Phase 2 of the Elasticsearch → Postgres migration. This class answers the same
calls the 684 Elasticsearch call sites already make, and returns the same response
shapes, so the migration does not touch them:

===========================  =====  =========================================
Method                       Calls  Backed by
===========================  =====  =========================================
``search_documents``           419  SQL filter/sort/page + Python aggregations
``index_document``             154  upsert on ``(index_name, doc_id)``
``update_document``            105  partial merge into the stored jsonb
``delete_document``             21  delete by primary key
``get_document``                19  primary-key lookup
``bulk_index_documents``        12  one transaction
``semantic_search``              8  ``multi_match`` substring, tenant-scoped
``get_all_documents``            4  ``search_documents`` with ``match_all``
``multi_search``                 2  N searches, one transaction
===========================  =====  =========================================

Response shapes are Elasticsearch's, not a new contract: ``search_documents``
returns ``{"hits": {"total": {"value": N, "relation": "eq"}, "hits": [{"_id",
"_source", "_score"}]}}`` plus ``aggregations`` when asked. Callers index into
``response["hits"]["hits"][0]["_source"]`` all over the codebase; reshaping that
would be a 130-file change for no benefit.

What is deliberately different, and why
---------------------------------------

**Reads are immediately consistent.** Elasticsearch needs a refresh before a
just-written document is searchable, and this codebase works around that in
several places (``index_document`` then ``indices.refresh``, ``refresh="wait_for"``
on deletes). A Postgres commit is visible to the next transaction, so those
workarounds become no-ops rather than breaking. This removes a class of flaky
read-after-write, it does not add one.

**``_score`` is always ``None``.** Nothing here ranks. The only scoring query in
the codebase is ``semantic_search``, whose docstring already records that it is a
plain ``multi_match`` with no inference endpoint configured — so no caller depends
on a meaningful score. Sorting by ``_score`` is a no-op, and that is called out in
``persistence.document_query.build_order_by``.

**An unsupported query fails loudly.** :class:`UnsupportedQueryError` and
:class:`UnsupportedAggregationError` propagate. A store that quietly ignored a
clause it could not translate would return wrong rows, and the caller could not
tell that from an empty result. Every silent-empty defect this migration has
turned up had that shape.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete as sql_delete, func, select

from persistence.document_aggregations import (
    collect_aggregation_fields,
    run_aggregations,
)
from persistence.document_field_policy import assert_searchable
from persistence.document_query import (
    UnsupportedQueryError,
    apply_source_filter,
    build_order_by,
    build_predicate,
    build_search_after,
    collect_query_fields,
    collect_sort_fields,
    resolve_source_filter,
    sort_values_of,
)

logger = logging.getLogger(__name__)

__all__ = ["PostgresDocumentStore"]

#: Cap on the rows a single search returns. Elasticsearch's own ``index.max_result_window``
#: default is 10,000 and several call sites pass ``size=10000`` right up against
#: it, so matching the number keeps their behaviour identical.
MAX_RESULT_WINDOW = 10_000

#: Top-level keys of a ``_search`` body that :meth:`search_documents` acts on.
_HONOURED_BODY_KEYS = frozenset(
    {"query", "size", "from", "aggs", "aggregations", "sort", "_source", "search_after"}
)

#: Top-level keys accepted and ignored, each for a stated reason. This set is
#: deliberately tiny and deliberately explicit: the whole point of
#: :func:`_assert_body_understood` is that "ignored" has to be a decision someone
#: made, not a key nobody thought about.
_IGNORED_BODY_KEYS = {
    # Asks Elasticsearch to count beyond 10,000 rather than reporting a lower
    # bound. A Postgres ``COUNT(*)`` is always exact and always ``"eq"``, so the
    # request is already satisfied.
    "track_total_hits": "Postgres totals are always exact",
    # Per-call timeout. The equivalent here is the statement timeout on the
    # session, so accepting it keeps call sites unchanged.
    "timeout": "handled by the session statement timeout",
}


def _assert_body_understood(index: str, body: Dict[str, Any]) -> None:
    """Raise unless every top-level key in ``body`` is honoured or knowingly ignored.

    The module docstring promises that an unsupported clause fails loudly. That
    promise was kept for clauses *inside* ``query`` and ``aggs`` and quietly broken
    at the top level, where an unrecognised key was simply never read. Two real
    consequences, both silent:

    * ``runtime_mappings`` in ``communication_metrics_service`` computes a latency
      in painless and aggregates ``stats`` over it. Dropped, the aggregation runs
      against a field that does not exist and the endpoint reports zero seconds of
      send latency as though it had measured it.
    * ``search_after`` paginates seventeen commerce, compliance and integration
      reads. Dropped, every request returns the first page.

    Neither logged anything. So the top level is checked the same way the clauses
    are.
    """
    unknown = sorted(set(body) - _HONOURED_BODY_KEYS - set(_IGNORED_BODY_KEYS))
    if unknown:
        raise UnsupportedQueryError(
            f"top-level search body key(s) {unknown} on index {index!r}",
            "search body",
        )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresDocumentStore:
    """The document-store half of :class:`ElasticsearchService`, over Postgres.

    Stateless: every method opens its own transaction through
    ``persistence.database.session_scope``, matching the per-call model the
    Elasticsearch client already has. Nothing here holds a connection between
    calls, so the store can be constructed once at bootstrap and shared.
    """

    def __init__(self, *, session_factory=None, clock=None) -> None:
        # Injected for tests; production passes neither and gets the shared
        # engine and the real clock.
        self._session_factory = session_factory
        self._clock = clock or _utcnow_iso

    # ------------------------------------------------------------------
    # Session plumbing
    # ------------------------------------------------------------------

    def _session_scope(self):
        if self._session_factory is not None:
            return self._session_factory()
        from persistence.database import session_scope

        return session_scope()

    @staticmethod
    def _model():
        from persistence.models import EsDocumentORM

        return EsDocumentORM

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Upsert a whole document, replacing any existing body.

        Mirrors ``ElasticsearchService.index_document``, including its
        timestamp-stamping: ``updated_at`` is always set and ``created_at`` only
        when absent. The stamping is done on the caller's dict, as the ES path
        does, because several callers read the timestamps back off the dict they
        passed in.
        """
        if doc_id is None or str(doc_id) == "":
            raise ValueError(f"index_document({index}) requires a non-empty doc_id")

        from services.elasticsearch_service import TIMESTAMP_SKIP_INDICES

        if index not in TIMESTAMP_SKIP_INDICES:
            document["updated_at"] = self._clock()
            if "created_at" not in document:
                document["created_at"] = self._clock()

        model = self._model()
        async with self._session_scope() as session:
            row = await session.get(model, (index, str(doc_id)))
            if row is None:
                session.add(
                    model(
                        index_name=index,
                        doc_id=str(doc_id),
                        tenant_id=_tenant_of(document),
                        document=dict(document),
                    )
                )
                result = "created"
            else:
                row.document = dict(document)
                row.tenant_id = _tenant_of(document)
                result = "updated"
        return {"_index": index, "_id": str(doc_id), "result": result}

    async def update_document(
        self, index: str, doc_id: str, partial_doc: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Merge ``partial_doc`` into the stored document.

        A shallow top-level merge, which is what the Elasticsearch ``_update``
        API does with ``{"doc": ...}``: a nested object in the partial REPLACES
        the stored one rather than merging into it. Reproduced exactly, because a
        deep merge would silently preserve subfields that the ES path drops, and
        callers have been written against the ES behaviour.

        A missing document raises, as ES's 404 does — the callers that tolerate
        absence check first.
        """
        model = self._model()
        partial_doc["updated_at"] = self._clock()
        async with self._session_scope() as session:
            row = await session.get(model, (index, str(doc_id)))
            if row is None:
                raise DocumentNotFound(index, str(doc_id))
            merged = dict(row.document or {})
            merged.update(partial_doc)
            row.document = merged
            row.tenant_id = _tenant_of(merged)
        return {"_index": index, "_id": str(doc_id), "result": "updated"}

    async def bulk_index_documents(
        self, index: str, documents: Sequence[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Index many documents in one transaction, reporting per-document failures.

        Returns the same summary shape as the Elasticsearch path
        (``success`` / ``total`` / ``successful`` / ``failed`` / ``errors``), so
        the three call sites that inspect it keep working.

        Documents with no derivable id are reported as failures rather than
        skipped silently — the ES path logs a warning and then lets the cluster
        mint a random id, which produces an unreachable document. Failing is
        better: the caller sees a count that does not match what it sent.
        """
        model = self._model()
        errors: List[Dict[str, Any]] = []
        successful = 0

        async with self._session_scope() as session:
            for position, raw in enumerate(documents):
                document = dict(raw)
                document["updated_at"] = self._clock()
                document.setdefault("created_at", document["updated_at"])
                doc_id = _bulk_doc_id(index, document)
                if not doc_id:
                    errors.append(
                        {
                            "position": position,
                            "reason": (
                                "no id field found; expected 'id' or "
                                f"'{_id_field_for(index)}'"
                            ),
                            "available_fields": sorted(document)[:10],
                        }
                    )
                    continue
                row = await session.get(model, (index, doc_id))
                if row is None:
                    session.add(
                        model(
                            index_name=index,
                            doc_id=doc_id,
                            tenant_id=_tenant_of(document),
                            document=document,
                        )
                    )
                else:
                    row.document = document
                    row.tenant_id = _tenant_of(document)
                successful += 1

        return {
            "success": not errors,
            "total": len(documents),
            "successful": successful,
            "failed": len(errors),
            "errors": errors,
        }

    async def atomic_update(
        self,
        index: str,
        doc_id: str,
        transform: "Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]",
        *,
        upsert: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Read-modify-write one document under a row lock.

        Replaces two Elasticsearch patterns that this codebase uses for the same
        purpose, and replaces them with something strictly stronger:

        **Painless scripted upserts.** ``fuel/order_repository.py`` and
        ``ops/services/ops_es_service.py`` ship a painless script that compares
        ``last_event_timestamp`` and sets ``ctx.op = 'noop'`` when the incoming
        event is stale; ``fuel/driver_repository.py`` ships one that increments
        counters. Both are read-modify-write expressed in a language that only
        runs inside Elasticsearch.

        **``if_seq_no`` / ``if_primary_term`` optimistic concurrency.**
        ``fuel/compartment_state_models.py`` and ``Agents/approval_queue_service.py``
        read a document with its sequence number, write with the number asserted,
        and retry with backoff on a 409.

        ``SELECT … FOR UPDATE`` subsumes both. The row is locked for the
        transaction, so a concurrent writer waits rather than losing a race —
        which means **the retry loops disappear**. The compartment repository
        retries three times with jittered backoff and raises
        ``CompartmentStateConflictError`` on persistent contention, a 409 the
        caller has to handle; against a locked row that state cannot arise.

        Args:
            index: Index name.
            doc_id: Document id.
            transform: Called with a copy of the stored document. Returns the new
                document, or ``None`` to leave it unchanged — which is exactly
                what painless ``ctx.op = 'noop'`` means, so a script's control
                flow maps over directly.
            upsert: Document to insert when none exists. ``None`` means "do
                nothing if absent", matching a plain ``_update``.

        Returns:
            ``(document, applied)``. ``document`` is the state after the call, or
            ``None`` when nothing existed and no ``upsert`` was given. ``applied``
            is ``False`` for a no-op, so a caller can distinguish "discarded a
            stale event" from "wrote the update" — the distinction
            ``upsert_with_last_event_timestamp`` returns to its callers.
        """
        model = self._model()
        async with self._session_scope() as session:
            row = (
                await session.execute(
                    select(model)
                    .where(model.index_name == index, model.doc_id == str(doc_id))
                    .with_for_update()
                )
            ).scalars().first()

            if row is None:
                if upsert is None:
                    return (None, False)
                document = dict(upsert)
                document.setdefault("created_at", self._clock())
                document["updated_at"] = self._clock()
                session.add(
                    model(
                        index_name=index,
                        doc_id=str(doc_id),
                        tenant_id=_tenant_of(document),
                        document=document,
                    )
                )
                return (document, True)

            current = dict(row.document or {})
            updated = transform(dict(current))
            if updated is None:
                return (current, False)
            updated["updated_at"] = self._clock()
            row.document = updated
            row.tenant_id = _tenant_of(updated)
            return (updated, True)

    async def upsert_if_newer(
        self,
        index: str,
        doc_id: str,
        document: Dict[str, Any],
        *,
        timestamp_field: str = "last_event_timestamp",
    ) -> bool:
        """Upsert unless the stored ``timestamp_field`` is newer or equal.

        The Postgres equivalent of the ``scripted_upsert`` guard, and the one
        place worth being precise about the comparison: the painless script uses
        ``incoming.isBefore(existing) || incoming.isEqual(existing)`` — so an
        event with a timestamp EQUAL to the stored one is discarded, not applied.
        Reproduced exactly, because at-least-once delivery makes an equal
        timestamp the common case for a redelivery, and applying it would
        overwrite whatever a later event had already written.

        A stored document with no timestamp, or an incoming document with none, is
        applied: the script only compares when the stored field is present and
        non-null.

        Returns ``True`` when written, ``False`` when discarded as stale.
        """
        incoming = document.get(timestamp_field)

        def _transform(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            stored = current.get(timestamp_field)
            if stored is not None and incoming is not None and incoming <= stored:
                return None
            merged = dict(current)
            merged.update(document)
            return merged

        _doc, applied = await self.atomic_update(
            index, doc_id, _transform, upsert=dict(document)
        )
        return applied

    async def document_exists(self, index: str, doc_id: str) -> bool:
        """Whether a document exists, without transferring its body."""
        model = self._model()
        async with self._session_scope() as session:
            found = (
                await session.execute(
                    select(model.doc_id).where(
                        model.index_name == index, model.doc_id == str(doc_id)
                    )
                )
            ).first()
        return found is not None

    async def update_by_query(
        self,
        index: str,
        query: Optional[Dict[str, Any]],
        transform: "Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]",
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """Apply ``transform`` to every document matching ``query``. Returns the count.

        The Postgres equivalent of ``_update_by_query``, which
        ``fuel/driver_repository.py`` uses twice to fix up denormalised driver
        counters. Every matching row is locked for the transaction, so unlike the
        Elasticsearch version there is no window in which a concurrent write to a
        matched document is silently lost — ES runs the query first and then
        updates each hit with a version check, abandoning conflicts.
        """
        model = self._model()
        predicate = build_predicate(
            model.document, query, id_column=model.doc_id, now=now
        )
        assert_searchable(index, collect_query_fields(query))
        changed = 0
        async with self._session_scope() as session:
            rows = (
                await session.execute(
                    select(model)
                    .where(model.index_name == index, predicate)
                    .with_for_update()
                )
            ).scalars().all()
            for row in rows:
                updated = transform(dict(row.document or {}))
                if updated is None:
                    continue
                updated["updated_at"] = self._clock()
                row.document = updated
                row.tenant_id = _tenant_of(updated)
                changed += 1
        return changed

    async def delete_by_query(
        self,
        index: str,
        query: Optional[Dict[str, Any]],
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """Delete every document matching ``query``. Returns the count deleted.

        Note the deliberate asymmetry with :meth:`search_documents`: this has no
        page limit, because a partially-applied delete is worse than a slow one.
        """
        model = self._model()
        predicate = build_predicate(
            model.document, query, id_column=model.doc_id, now=now
        )
        assert_searchable(index, collect_query_fields(query))
        async with self._session_scope() as session:
            result = await session.execute(
                sql_delete(model).where(model.index_name == index, predicate)
            )
        return int(result.rowcount or 0)

    async def delete_document(self, index: str, doc_id: str) -> bool:
        """Delete by id. ``False`` when the document was not there."""
        model = self._model()
        async with self._session_scope() as session:
            row = await session.get(model, (index, str(doc_id)))
            if row is None:
                return False
            await session.delete(row)
        return True

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_document(self, index: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """The document body, or ``None``. Matches the ES facade's 404 → None."""
        model = self._model()
        async with self._session_scope() as session:
            row = await session.get(model, (index, str(doc_id)))
            return dict(row.document or {}) if row is not None else None

    async def search_documents(
        self,
        index: str,
        query: Dict[str, Any],
        size: int = 100,
        request_timeout: int = 10,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Run one search, returning the Elasticsearch response shape.

        ``request_timeout`` is accepted and unused: it exists on the ES facade to
        stop a slow aggregation holding the event loop, and the equivalent here is
        the statement timeout on the Postgres session. Keeping the parameter means
        no call site changes.
        """
        model = self._model()
        body = dict(query or {})
        _assert_body_understood(index, body)
        # ES takes ``size`` from the body when present, from the argument
        # otherwise. The facade normalises this by writing it into the body; we
        # read it the same way round so the two paths page identically.
        effective_size = int(body.get("size", size))
        offset = int(body.get("from", 0) or 0)
        aggs = body.get("aggs") or body.get("aggregations")
        source_spec = resolve_source_filter(body.get("_source"))
        sort = body.get("sort")

        # Refuse to query a field the Elasticsearch mapping declares unsearchable
        # (``binary``, ``index: false``, ``enabled: false``). In jsonb everything
        # is queryable, so without this the move silently widens what callers can
        # filter on — including ``fuel_orders_current.pod_otp``, the delivery
        # one-time code, which ES cannot filter on at all. See
        # persistence.document_field_policy.
        assert_searchable(
            index,
            [
                *collect_query_fields(body.get("query")),
                *collect_sort_fields(body.get("sort")),
                *collect_aggregation_fields(aggs),
            ],
        )

        predicate = build_predicate(
            model.document, body.get("query"), id_column=model.doc_id, now=now
        )
        scope = [model.index_name == index, predicate]

        # ``search_after`` narrows the rows, so it belongs in the scope of the page
        # but NOT in the total: Elasticsearch reports the total for the query, not
        # for the remaining tail, and a caller showing "N results" across pages
        # would watch N shrink as it paged.
        page_scope = list(scope)
        if "search_after" in body:
            page_scope.append(
                build_search_after(model.document, sort, body["search_after"])
            )

        async with self._session_scope() as session:
            total = (
                await session.execute(
                    select(func.count()).select_from(model).where(*scope)
                )
            ).scalar_one()

            hits: List[Dict[str, Any]] = []
            if effective_size > 0:
                order_by = build_order_by(
                    model.document, sort, id_column=model.doc_id
                )
                rows = (
                    await session.execute(
                        select(model)
                        .where(*page_scope)
                        .order_by(*order_by)
                        .offset(offset)
                        .limit(min(effective_size, MAX_RESULT_WINDOW))
                    )
                ).scalars().all()
                hits = []
                for row in rows:
                    document = dict(row.document or {})
                    hit: Dict[str, Any] = {
                        "_index": index,
                        "_id": row.doc_id,
                        "_score": None,
                        "_source": apply_source_filter(document, source_spec),
                    }
                    # ES returns ``sort`` on every hit when a sort is requested and
                    # omits it otherwise. The keyset callers build their next cursor
                    # from it, so omitting it reads to them as "no further pages".
                    if sort:
                        hit["sort"] = sort_values_of(document, sort)
                    hits.append(hit)

            response: Dict[str, Any] = {
                "took": 0,
                "timed_out": False,
                "hits": {
                    "total": {"value": int(total), "relation": "eq"},
                    "max_score": None,
                    "hits": hits,
                },
            }

            if aggs:
                # Aggregations run over the FULL match set, never the page. This
                # is the one place a page limit would silently produce a wrong
                # number rather than a short list.
                all_rows = (
                    await session.execute(
                        select(model.doc_id, model.document).where(*scope)
                    )
                ).all()
                response["aggregations"] = run_aggregations(
                    [(doc_id, dict(doc or {})) for doc_id, doc in all_rows],
                    aggs,
                    now=now,
                )

        return response

    async def multi_search(
        self,
        searches: Sequence[Dict[str, Any]],
        request_timeout: int = 10,
    ) -> Dict[str, Any]:
        """Run several searches, returning ``{"responses": [...]}`` in order.

        The Elasticsearch version exists to collapse an N+1 fan-out into one
        network round trip. Here the round trip is a local connection, but the
        signature and response shape are kept so ``DriverWorkService`` and any
        future caller need no change — and so the fan-out stays visible as one
        call rather than being re-scattered.

        A per-search failure is returned as an ES-shaped error entry instead of
        aborting the batch, matching ``ignore_unavailable`` on the ES header.
        """
        if not searches:
            return {"responses": []}
        responses = []
        for entry in searches:
            try:
                responses.append(
                    await self.search_documents(
                        entry["index"], dict(entry.get("query") or {})
                    )
                )
            except Exception as exc:  # noqa: BLE001 — per-search isolation
                logger.warning(
                    "multi_search entry for index %s failed: %s",
                    entry.get("index"), exc,
                )
                responses.append(
                    {
                        "error": {"type": type(exc).__name__, "reason": str(exc)},
                        "hits": {
                            "total": {"value": 0, "relation": "eq"},
                            "max_score": None,
                            "hits": [],
                        },
                    }
                )
        return {"responses": responses}

    async def get_all_documents(
        self, index: str, size: int = 1000
    ) -> List[Dict[str, Any]]:
        """Every document in an index, newest first. Bounded by ``size``."""
        response = await self.search_documents(
            index,
            {"query": {"match_all": {}}, "sort": [{"created_at": {"order": "desc"}}]},
            size,
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]

    async def semantic_search(
        self,
        tenant_id: str,
        index: str,
        text: str,
        fields: List[str],
        size: int = 10,
    ) -> List[Dict[str, Any]]:
        """Substring search across ``fields``, scoped to one tenant.

        The name is inherited. The Elasticsearch implementation is a plain
        ``multi_match`` with no inference endpoint configured anywhere, so this is
        behaviour-compatible in the sense that matters: neither ranks
        semantically. What does differ is tokenisation — ES matches analyzed
        terms, this matches substrings — so a two-word query behaves differently.
        Recorded in the migration doc, not hidden.

        The tenant filter is mandatory and applied here rather than trusted from
        the caller: ``/api/search`` and every AI tool routes through this method
        on behalf of an authenticated tenant and must not see another's rows.
        """
        if not tenant_id:
            raise ValueError("semantic_search requires a tenant_id")
        response = await self.search_documents(
            index,
            {
                "query": {
                    "bool": {
                        "must": [
                            {
                                "multi_match": {
                                    "query": text,
                                    "fields": fields,
                                    "type": "best_fields",
                                }
                            }
                        ],
                        "filter": [{"term": {"tenant_id": tenant_id}}],
                    }
                }
            },
            size,
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]

    # ------------------------------------------------------------------
    # Maintenance helpers
    # ------------------------------------------------------------------

    async def count(self, index: str, tenant_id: Optional[str] = None) -> int:
        """Documents in an index, optionally for one tenant.

        Used by the migration tooling (backfill progress, parity) rather than by
        application code.
        """
        model = self._model()
        scope = [model.index_name == index]
        if tenant_id is not None:
            scope.append(model.tenant_id == tenant_id)
        async with self._session_scope() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(model).where(*scope)
                    )
                ).scalar_one()
            )

    async def list_indices(self) -> List[str]:
        """Every index name present in the store, sorted."""
        model = self._model()
        async with self._session_scope() as session:
            rows = (
                await session.execute(
                    select(model.index_name).distinct().order_by(model.index_name)
                )
            ).scalars().all()
        return list(rows)

    async def delete_index(self, index: str) -> int:
        """Remove every document in an index. Returns the number deleted.

        The equivalent of ``indices.delete``, and destructive in the same way.
        Exists for the migration's own drop-and-rebuild drills; application code
        has no reason to call it.
        """
        model = self._model()
        async with self._session_scope() as session:
            result = await session.execute(
                sql_delete(model).where(model.index_name == index)
            )
        return int(result.rowcount or 0)


class DocumentNotFound(LookupError):
    """A partial update targeted a document that does not exist.

    Separate from a generic ``KeyError`` so the ``ElasticsearchService`` shim can
    translate it into the same 404-shaped behaviour callers already handle.
    """

    def __init__(self, index: str, doc_id: str) -> None:
        super().__init__(f"document {doc_id!r} not found in index {index!r}")
        self.index = index
        self.doc_id = doc_id
        self.status_code = 404


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_of(document: Dict[str, Any]) -> Optional[str]:
    """Lift ``tenant_id`` into the typed column, if the document has one.

    Nullable on purpose: the legacy dynamically-mapped ``trucks`` and
    ``locations`` documents genuinely carry no tenant, and coercing them to a
    sentinel would make a tenant-scoped query match documents belonging to
    nobody.
    """
    value = document.get("tenant_id")
    return str(value) if isinstance(value, (str, int)) and str(value) else None


#: Index-specific id fields, copied from ``ElasticsearchService.bulk_index_documents``
#: so bulk-indexed documents land under the same ids on both paths.
_BULK_ID_FIELDS = {
    "trucks": "truck_id",
    "inventory": "item_id",
    "support_tickets": "ticket_id",
    "locations": "location_id",
    "analytics_events": "event_id",
}


def _id_field_for(index: str) -> str:
    """The id field for an index, matching the ES path's singularise-and-suffix rule."""
    return _BULK_ID_FIELDS.get(index, f"{index[:-1]}_id")


def _bulk_doc_id(index: str, document: Dict[str, Any]) -> Optional[str]:
    value = document.get("id") or document.get(_id_field_for(index))
    return str(value) if value else None
