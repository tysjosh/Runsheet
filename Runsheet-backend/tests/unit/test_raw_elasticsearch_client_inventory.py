"""Raw ``.client`` data-plane calls bypass the document store, so they are tracked.

``ElasticsearchService`` now routes its nine async document methods to Postgres
when ``DOCUMENT_STORE_BACKEND=postgres``. Code that reaches past that facade to
``es.client.search(...)`` or ``es.client.update(...)`` does **not** get routed. At
cutover those sites keep talking to Elasticsearch while everything else talks to
Postgres, and the two stores diverge — silently, because each call individually
succeeds.

So the surface is inventoried here with an explicit allowlist. The test fails when
a call site appears that is not on the list, which makes adding one a deliberate
act, and it fails when the list names a site that no longer exists, which stops
the list rotting into a to-do nobody trusts.

**The point of the list is that it shrinks, and it has now shrunk to nothing in
the application data plane.** All 41 sites that reached past the facade have been
rewritten onto it; what is left is migration tooling and the facade's own
Elasticsearch branch. So ``DOCUMENT_STORE_BACKEND=postgres`` no longer splits
application writes across two stores.

That removes one precondition for the cutover; it does not make the flag safe on
its own, and this file is careful not to imply otherwise — see
``TestReadinessIsNotOverstated``. What remains is recorded in
``docs/elasticsearch-to-postgres-migration.md``, which is where an operator looks,
rather than left to be inferred from a green test run.

Not every entry is a problem. Three kinds are on the list for different reasons:

*control plane* — ``indices.*``, ``ilm.*``, ``ping``. These manage indices and
lifecycle policies, and they have no Postgres equivalent because there is nothing
to manage: the document store is one table. They become no-ops when Elasticsearch
goes, and they are excluded from the count entirely.

*migration tooling* — ``persistence/rebuild_from_postgres.py``,
``scripts/*``. These exist to talk to Elasticsearch. They are the last things to
go, not the first.

*application data plane* — everything else. These were the ones that had to move,
and they have. The category is kept in the scanner so the next one that appears is
caught rather than discovered after a cutover.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parents[2]

#: Elasticsearch client methods that read or write documents. Anything here
#: bypasses the document store. ``exists``/``get``/``delete`` also exist on the
#: Redis client, so the scan additionally checks that the file actually deals in
#: Elasticsearch (see :func:`_is_elasticsearch_file`).
_DATA_PLANE = frozenset(
    {
        "index", "update", "delete", "get", "search", "count", "msearch", "bulk",
        "update_by_query", "delete_by_query", "exists", "scan", "mget", "scroll",
        "reindex",
    }
)

#: Files whose ``.client`` is Redis or httpx, not Elasticsearch. Listed rather
#: than pattern-matched because the attribute names collide exactly — a Redis
#: ``client.get(key)`` and an Elasticsearch ``client.get(index=…, id=…)`` differ
#: only in their arguments.
_NOT_ELASTICSEARCH = frozenset(
    {
        "session/redis_store.py",
        "ops/ingestion/idempotency.py",
        "ops/services/feature_flags.py",
        "fuel/voice/voice_submission_ledger.py",
        "scripts/soak_personas.py",
        "health/service.py",
        # ``feature_flag_service.client.scan(...)`` — Redis key iteration during
        # startup, not an Elasticsearch scroll. It was on the allowlist as
        # Elasticsearch debt, which overstated the remaining work by one and would
        # have sent someone to migrate a call that has nothing to migrate.
        "bootstrap/core.py",
    }
)

#: ``path -> number of data-plane raw-client calls``, as of the document-store
#: work. Each entry is a site that will keep writing to Elasticsearch after the
#: cutover, with the reason it has not moved yet.
_ALLOWLIST = {
    # --- Migration tooling: exists to talk to Elasticsearch. Moves last. -----
    "persistence/rebuild_from_postgres.py": 1,   # projects PG -> ES, by design
    "scripts/seed_kfactor_demo.py": 1,           # seeds ES directly
    "services/data_seeder.py": 1,                # clears ES indices
    #
    # ``services/elasticsearch_service.py`` is GONE from this list. It held 13 raw
    # calls — the two implementations the backend switch chose between — and Phase 6
    # deleted the Elasticsearch half along with the ``Elasticsearch`` import itself.
    # The facade is now a delegation to ``PostgresDocumentStore``, so the count went
    # 9 → 13 → 0: up while it absorbed raw calls from elsewhere, then to nothing.
    #
    # --- The application data plane is empty. --------------------------------
    #
    # Every entry that used to be here has been rewritten onto the facade:
    #
    #   ops/api/endpoints.py                 21 searches -> search_documents
    #   ops/ingestion/poison_queue.py         7 mixed    -> the passthroughs
    #   fuel/compartment_state_models.py      4 OCC      -> atomic_update
    #   fuel/driver_repository.py             4 painless -> atomic_update,
    #                                                        update_by_query
    #   Agents/approval_queue_service.py      2 OCC      -> atomic_update
    #   Agents/tools/ops_report_tools.py      1 search   -> search_documents
    #   Agents/tools/ops_search_tools.py      1 search   -> search_documents
    #   ops/services/ops_es_service.py        1 bulk     -> upsert_if_newer
    #   bootstrap/core.py                     1          -> was never
    #                                                        Elasticsearch (Redis)
    #
    # So ``DOCUMENT_STORE_BACKEND=postgres`` no longer splits application writes
    # across two stores. That is a necessary condition for the cutover, not a
    # sufficient one — see ``TestReadinessIsNotOverstated`` and the migration doc
    # for what remains (aggregation shapes, the outbox relay target).
}

#: Files where a raw-client call is expected and is NOT part of the migration
#: debt, because it manages indices rather than documents.
_CONTROL_PLANE_ONLY = frozenset({"services/mapping_validator.py"})


def _is_elasticsearch_file(relative: str) -> bool:
    return relative not in _NOT_ELASTICSEARCH


def _count_calls(source: str) -> int:
    """Count ``<anything>.client.<data-plane method>(...)`` calls in ``source``.

    Parsed rather than pattern-matched. A regex over lines counted prose: five of
    these files carry a docstring explaining which raw call they used to make, so
    ``ops/ingestion/poison_queue.py`` read as having a raw call left after all
    seven were migrated, and the ratchet was measuring documentation. Only real
    call expressions count now, which also means the inventory can be described in
    the code it describes.

    ``client.indices.exists(...)`` is control plane and ``client.exists(...)`` is
    not. The AST separates them without inspecting text: for the control-plane
    form the callee's parent attribute is ``indices``, not ``client``.
    """
    count = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if _passes_the_client_along(node):
            count += 1
            continue
        callee = node.func
        if not isinstance(callee, ast.Attribute):
            continue
        if callee.attr not in _DATA_PLANE:
            continue
        # Deliberately only the ``<obj>.client.<method>(...)`` form, which is what
        # "reaching past the facade" looks like. A bare local ``client.get(...)``
        # is not included: the migration scripts and half a dozen HTTP providers
        # bind ``client`` to an ``httpx`` or ``Elasticsearch`` object directly, and
        # counting those would swamp the signal this list exists to carry.
        parent = callee.value
        if isinstance(parent, ast.Attribute) and parent.attr == "client":
            count += 1
    return count


def _passes_the_client_along(node: ast.Call) -> bool:
    """Whether ``node`` hands ``<obj>.client`` to another function as an argument.

    ``elasticsearch.helpers.bulk(self.client, actions)`` is a data-plane write
    that never appears as ``.client.<method>(...)``, so counting method calls
    alone missed it — and it was not a hypothetical miss: ``bulk_upsert`` in
    ``ops/services/ops_es_service.py`` wrote every batched shipment and rider
    through exactly that shape while this inventory reported the application
    data plane as clean. That is the specific failure this file exists to
    prevent, so the shape is counted.

    The callee still has to be a data-plane name. Handing the client to
    ``_ensure_index(es.client, index)`` or ``setup_fuel_indices(es.client, ...)``
    is control plane, and counting those would put four index-management helpers
    on a list that is supposed to mean "documents are going to the wrong store".
    """
    name = node.func.attr if isinstance(node.func, ast.Attribute) else (
        node.func.id if isinstance(node.func, ast.Name) else None
    )
    if name not in _DATA_PLANE:
        return False
    for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
        if isinstance(argument, ast.Attribute) and argument.attr == "client":
            return True
    return False


def _scan() -> dict:
    """``{relative path: data-plane raw-client call count}`` across the backend."""
    # Filtered on the path RELATIVE to the backend root, not on the absolute
    # path's parts. Filtering absolutely matched ``.worktrees`` in every path when
    # the suite ran from a ``git worktree`` — which is how commits get verified
    # here — so the scan silently found nothing and every allowlist entry looked
    # stale. It was the "no file has been cleaned up" direction that caught it; a
    # one-directional test would have passed while covering zero files.
    skip = ("venv", "tests", "es-full-backup", "__pycache__", ".hypothesis", "alembic")
    found: dict = {}
    for path in sorted(_BACKEND.rglob("*.py")):
        try:
            relative_path = path.relative_to(_BACKEND)
        except ValueError:  # pragma: no cover — defensive
            continue
        if any(part in skip for part in relative_path.parts):
            continue
        relative = str(relative_path)
        if not _is_elasticsearch_file(relative) or relative in _CONTROL_PLANE_ONLY:
            continue
        try:
            count = _count_calls(path.read_text())
        except (OSError, SyntaxError):  # pragma: no cover — defensive
            continue
        if count:
            found[relative] = count
    return found


class TestTheInventoryIsAccurate:
    def test_no_unlisted_file_reaches_past_the_facade(self):
        """A new raw-client call site has to be added deliberately.

        Reaching past ``ElasticsearchService`` is sometimes the right answer, but
        it is never the right accident: after the cutover such a call writes to a
        different store from everything around it.
        """
        actual = _scan()
        unlisted = sorted(set(actual) - set(_ALLOWLIST))
        assert not unlisted, (
            f"{unlisted} use the raw Elasticsearch client and are not in the "
            "inventory. If the call is deliberate, add it with a reason; if it is "
            "a document read or write, route it through ElasticsearchService or "
            "PostgresDocumentStore instead."
        )

    def test_the_inventory_names_no_file_that_has_been_cleaned_up(self):
        """Stops the list rotting into a to-do nobody trusts."""
        actual = _scan()
        stale = sorted(set(_ALLOWLIST) - set(actual))
        assert not stale, (
            f"{stale} no longer use the raw client — remove them from the "
            "inventory so the remaining count means something."
        )

    @pytest.mark.parametrize("path", sorted(_ALLOWLIST))
    def test_no_file_grew_more_raw_calls(self, path):
        """The count is a ratchet: a listed file may shrink, never grow."""
        actual = _scan().get(path, 0)
        assert actual <= _ALLOWLIST[path], (
            f"{path} now has {actual} raw client calls, up from "
            f"{_ALLOWLIST[path]}. The inventory only goes down."
        )


class TestReadinessIsNotOverstated:
    def test_the_migration_doc_records_the_raw_client_surface(self):
        """The dangerous state is the flag LOOKING ready.

        That has to be written down where an operator reads, not inferred from a
        test file.
        """
        doc = (_BACKEND.parent / "docs" / "elasticsearch-to-postgres-migration.md")
        assert doc.is_file(), "the migration doc is the operator-facing record"
        text = doc.read_text()
        assert "raw" in text and "client" in text, (
            "the migration doc does not mention the raw-client surface, so an "
            "operator reading it would think the cutover is a flag flip"
        )

    def test_the_migration_doc_still_records_what_is_outstanding(self):
        """An empty allowlist is the new overstatement risk.

        With the application data plane clean, the honest summary is "necessary,
        not sufficient": the aggregation shapes the Postgres store refuses, and the
        outbox relay still pointed at Elasticsearch. Neither shows up in this
        inventory, so nothing else would stop the doc from reading as done.
        """
        doc = (_BACKEND.parent / "docs" / "elasticsearch-to-postgres-migration.md")
        text = doc.read_text()
        assert "Still outstanding" in text, (
            "the migration doc has no outstanding-work section, so an operator "
            "would read a closed raw-client surface as a green light"
        )
        outstanding = text.split("Still outstanding", 1)[1]
        for topic in ("aggregation", "outbox relay"):
            assert topic in outstanding, (
                f"{topic!r} is not named as outstanding. It is not covered by this "
                "inventory, so if the doc stops mentioning it nothing else will."
            )

    def test_the_replacement_primitive_exists_and_is_reachable(self):
        """The read-modify-write sites needed somewhere to go."""
        from persistence.document_store import PostgresDocumentStore

        for method in ("atomic_update", "upsert_if_newer", "update_by_query",
                       "delete_by_query", "document_exists"):
            assert hasattr(PostgresDocumentStore, method), method

    def test_every_replacement_is_reachable_through_the_facade_too(self):
        """A store method nothing can call from application code is not a fix.

        The sites were migrated onto ``ElasticsearchService``, not onto the store
        directly, because the facade is what holds the backend switch. A primitive
        that exists only on ``PostgresDocumentStore`` would leave the caller with
        no choice but the raw client on the Elasticsearch branch.
        """
        from services.elasticsearch_service import ElasticsearchService

        for method in ("atomic_update", "upsert_if_newer", "update_by_query"):
            assert callable(getattr(ElasticsearchService, method, None)), (
                f"ElasticsearchService.{method} is missing, so a caller needing "
                "it has to reach past the facade"
            )


class TestTheScannerActuallyScans:
    """A scanner that finds nothing makes every assertion above vacuous.

    The first version filtered on the absolute path's parts, so running the suite
    from a ``git worktree`` — the way commits are verified here — matched
    ``.worktrees`` in every path and skipped the entire backend. The allowlist
    then read as entirely stale. These two tests make the failure mode explicit
    rather than relying on another test noticing.
    """

    def test_it_finds_files(self):
        assert _scan(), (
            "the scanner found no files at all, so every assertion about the "
            "raw-client inventory is vacuous. Check the skip filter — it must "
            "apply to the path RELATIVE to the backend root, since an absolute "
            "path can contain '.worktrees' or a checkout directory name."
        )

    def test_it_finds_the_projector_which_definitely_has_a_raw_call(self):
        """A specific anchor, so a scanner that finds only test fixtures fails.

        The anchor used to be ``ElasticsearchService`` itself, which held 13 raw
        calls. Phase 6 deleted them, so the anchor moved to
        ``persistence/rebuild_from_postgres.py`` — which projects Postgres back into
        a cluster and therefore keeps a raw ``client.index`` by design.
        """
        found = _scan()
        assert found.get("persistence/rebuild_from_postgres.py", 0) > 0, (
            "the scanner did not find the raw client call in "
            "rebuild_from_postgres, which projects into Elasticsearch by design"
        )

    def test_it_runs_from_a_path_containing_the_worktree_marker(self, tmp_path, monkeypatch):
        """Directly reproduces the bug: a backend root under a '.worktrees' path.

        Rather than trusting that the filter is now relative, put the tree
        somewhere whose absolute path contains the string that broke it.
        """
        import textwrap

        root = tmp_path / ".worktrees" / "checkout" / "Runsheet-backend"
        (root / "svc").mkdir(parents=True)
        (root / "svc" / "thing.py").write_text(
            textwrap.dedent(
                """
                async def read(es):
                    return es.client.search(index="i", body={})
                """
            )
        )
        monkeypatch.setattr("tests.unit.test_raw_elasticsearch_client_inventory._BACKEND", root)
        assert _scan() == {"svc/thing.py": 1}


class TestTheScannerCountsCallsAndNotProse:
    """The counting rules, pinned individually.

    The regex version counted any line containing ``.client.<method>(``, which
    included the docstrings five of these files carry explaining which raw call
    they used to make. ``ops/ingestion/poison_queue.py`` had all seven of its
    calls migrated and still reported one, so the ratchet was measuring
    documentation and the file could not be removed from the list.
    """

    def test_a_docstring_describing_a_raw_call_is_not_a_raw_call(self):
        source = '''
def read(es):
    """Reaching ``es.client.search(...)`` directly bypassed the switch."""
    return es.search_documents("i", {})
'''
        assert _count_calls(source) == 0

    def test_a_comment_describing_a_raw_call_is_not_a_raw_call(self):
        source = """
def read(es):
    # was: es.client.get(index=i, id=doc_id)
    return es.get_document(i, doc_id)
"""
        assert _count_calls(source) == 0

    def test_a_real_call_still_counts(self):
        assert _count_calls('es.client.search(index="i", body={})') == 1

    def test_the_control_plane_form_is_not_counted(self):
        """``client.indices.exists`` manages an index; ``client.exists`` reads a doc."""
        assert _count_calls('es.client.indices.exists(index="i")') == 0
        assert _count_calls('es.client.exists(index="i", id="d")') == 1

    def test_handing_the_client_to_a_data_plane_helper_counts(self):
        """``helpers.bulk(self.client, actions)`` is a write with no ``.client.`` in it.

        This is not hypothetical: ``bulk_upsert`` wrote every batched shipment and
        rider through exactly this shape while the inventory reported the
        application data plane as clean.
        """
        assert _count_calls("bulk(self.client, actions, refresh=True)") == 1
        assert _count_calls("helpers.bulk(self._es.client, actions)") == 1

    def test_handing_the_client_to_a_control_plane_helper_does_not(self):
        """Otherwise four index-setup helpers land on a document-store list."""
        assert _count_calls("setup_fuel_indices(es_service.client)") == 0
        assert _count_calls("_ensure_index(es.client, index)") == 0

    def test_a_bare_local_client_is_out_of_scope(self):
        """The migration scripts and the HTTP providers both bind a bare ``client``.

        Counting ``client.get(url)`` would add six httpx providers to the list and
        drown the signal. The inventory is about reaching past the facade, which
        looks like ``<something>.client``.
        """
        assert _count_calls('client.get("https://example.test")') == 0
