"""Raw ``.client`` data-plane calls bypass the document store, so they are tracked.

``ElasticsearchService`` is now a delegation to
:class:`persistence.document_store.PostgresDocumentStore`. Code that reaches past
that facade to ``es.client.search(...)`` or ``es.client.update(...)`` does not get
routed anywhere at all: Phase 6 deleted the ``elasticsearch`` import and replaced the
client with :class:`services.no_cluster.NoClusterClient`, whose data plane raises.

So the surface is inventoried here with an explicit allowlist. The test fails when a
call site appears that is not on the list, which makes adding one a deliberate act,
and it fails when the list names a site that no longer exists, which stops the list
rotting into a to-do nobody trusts.

**The point of the list is that it shrinks, and it is now empty.** All 41 application
sites that reached past the facade were rewritten onto it before the cutover; the last
four — the facade's own Elasticsearch branch, ``persistence/rebuild_from_postgres.py``,
``scripts/seed_kfactor_demo.py`` and ``services/data_seeder.py`` — went with the
cluster:

* the facade's 13 raw calls were the Elasticsearch half of the backend switch, deleted
  with the switch;
* the rebuild tool became ``persistence/rebuild_document_store.py`` and writes through
  ``index_document(..., stamp_timestamps=False)``, which is exactly the "write this
  document verbatim" guarantee the raw call was there for;
* the two seeders use the facade, ``seed_kfactor_demo`` with the same opt-out because
  its delivery dates are the data.

An empty allowlist creates a new failure mode, and this file guards it: a scanner that
finds nothing makes every assertion vacuous, and "nothing found" is now the expected
result. So non-vacuity is asserted against the set of files *scanned* rather than the
set of hits, and the scanner is separately exercised against a synthetic tree
containing a real raw call. See ``TestTheScannerActuallyScans``.

Two kinds of call are excluded by design rather than allowlisted:

*control plane* — ``indices.*``, ``ilm.*``, ``ping``. These managed indices and
lifecycle policies and have no Postgres equivalent, because there is nothing to
manage: the document store is one table.

*not Elasticsearch* — ``exists``/``get``/``delete`` also exist on the Redis and httpx
clients, and the attribute names collide exactly. Those files are listed in
:data:`_NOT_ELASTICSEARCH`.
"""

from __future__ import annotations

import ast
import pathlib

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

#: ``path -> number of data-plane raw-client calls``. **Empty**, and the empty dict
#: is the finish line rather than an oversight: there is no Elasticsearch client
#: left to call. A new entry here now means a file has grown a call into
#: ``NoClusterClient``, whose data plane raises — so the failure is loud rather than
#: a silent write to the wrong store, but it is still a bug and still worth catching
#: at import-scan time rather than at runtime.
_ALLOWLIST: dict = {}


def _is_elasticsearch_file(relative: str) -> bool:
    return relative not in _NOT_ELASTICSEARCH


def _count_calls(source: str) -> int:
    """Count ``<anything>.client.<data-plane method>(...)`` calls in ``source``.

    Parsed rather than pattern-matched. A regex over lines counted prose: several of
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
        # is not included: half a dozen HTTP providers bind ``client`` to an
        # ``httpx`` object directly, and counting those would swamp the signal
        # this list exists to carry.
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


def _scanned_paths() -> list:
    """Every backend source file the inventory considers, relative to the root.

    Split out from :func:`_scan` because the inventory is empty now. "No hits" is
    the expected result and also exactly what a broken scanner returns, so the
    only way to tell them apart is to assert on what was *looked at*.
    """
    # Filtered on the path RELATIVE to the backend root, not on the absolute
    # path's parts. Filtering absolutely matched ``.worktrees`` in every path when
    # the suite ran from a ``git worktree`` — which is how commits get verified
    # here — so the scan silently found nothing and every allowlist entry looked
    # stale. It was the "no file has been cleaned up" direction that caught it; a
    # one-directional test would have passed while covering zero files.
    skip = ("venv", "tests", "es-full-backup", "__pycache__", ".hypothesis", "alembic")
    paths = []
    for path in sorted(_BACKEND.rglob("*.py")):
        try:
            relative_path = path.relative_to(_BACKEND)
        except ValueError:  # pragma: no cover — defensive
            continue
        if any(part in skip for part in relative_path.parts):
            continue
        relative = str(relative_path)
        if not _is_elasticsearch_file(relative):
            continue
        paths.append(relative)
    return paths


def _scan() -> dict:
    """``{relative path: data-plane raw-client call count}`` across the backend."""
    found: dict = {}
    for relative in _scanned_paths():
        try:
            count = _count_calls((_BACKEND / relative).read_text())
        except (OSError, SyntaxError):  # pragma: no cover — defensive
            continue
        if count:
            found[relative] = count
    return found


class TestTheInventoryIsAccurate:
    def test_no_unlisted_file_reaches_past_the_facade(self):
        """A new raw-client call site has to be added deliberately.

        There is no client left to reach: the attribute is a
        :class:`services.no_cluster.NoClusterClient` whose data plane raises. A hit
        here is a call that will fail at runtime, found statically instead.
        """
        actual = _scan()
        unlisted = sorted(set(actual) - set(_ALLOWLIST))
        assert not unlisted, (
            f"{unlisted} use the raw Elasticsearch client, which no longer exists. "
            "Route the call through ElasticsearchService or PostgresDocumentStore."
        )

    def test_the_inventory_names_no_file_that_has_been_cleaned_up(self):
        """Stops the list rotting into a to-do nobody trusts."""
        actual = _scan()
        stale = sorted(set(_ALLOWLIST) - set(actual))
        assert not stale, (
            f"{stale} no longer use the raw client — remove them from the "
            "inventory so the remaining count means something."
        )

    def test_the_surface_is_closed(self):
        """The ratchet's terminal state, asserted directly.

        Replaces the per-file ``actual <= allowlisted`` ratchet, which had nothing
        left to parametrise over. Pinning zero is stronger than pinning each
        file's count was: it also forbids a *new* file appearing with calls,
        which the parametrised version could not see.
        """
        assert _scan() == {}, (
            f"{sorted(_scan())} reach past the facade. The Elasticsearch client "
            "was deleted in Phase 6, so these calls raise at runtime."
        )


class TestReadinessIsNotOverstated:
    def test_the_migration_doc_records_the_raw_client_surface(self):
        """The dangerous state is the migration LOOKING done.

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
        """An empty allowlist is the overstatement risk.

        With the raw-client surface closed and the cluster gone, this inventory has
        nothing left to report — and it never covered the two things that are
        genuinely outstanding: the aggregation shapes the Postgres store refuses,
        and the environments that have not been cut over. Neither shows up here, so
        nothing else would stop the doc from reading as done.
        """
        doc = (_BACKEND.parent / "docs" / "elasticsearch-to-postgres-migration.md")
        text = doc.read_text()
        assert "Still outstanding" in text, (
            "the migration doc has no outstanding-work section, so an operator "
            "would read a closed raw-client surface as a green light"
        )
        outstanding = text.split("Still outstanding", 1)[1].lower()
        for topic in ("aggregation", "staging"):
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
        directly, because the facade is what the application holds. A primitive
        that existed only on ``PostgresDocumentStore`` would leave the caller with
        no choice but the raw client.
        """
        from services.elasticsearch_service import ElasticsearchService

        for method in ("atomic_update", "upsert_if_newer", "update_by_query"):
            assert callable(getattr(ElasticsearchService, method, None)), (
                f"ElasticsearchService.{method} is missing, so a caller needing "
                "it has to reach past the facade"
            )

    def test_the_verbatim_write_opt_out_exists(self):
        """What replaced the last three raw ``client.index`` calls.

        The rebuild tool and ``seed_kfactor_demo`` used the raw client for one
        reason: ``index_document`` force-stamps ``updated_at`` to now(), which
        would restamp a rebuilt index and collapse the demo's three-week delivery
        spacing into one instant. Without the opt-out those three had nowhere to
        go but the client, so this keyword argument is load-bearing for the empty
        allowlist above.
        """
        import inspect

        from persistence.document_store import PostgresDocumentStore
        from services.elasticsearch_service import ElasticsearchService

        for owner in (PostgresDocumentStore, ElasticsearchService):
            signature = inspect.signature(owner.index_document)
            assert "stamp_timestamps" in signature.parameters, owner.__name__


class TestTheScannerActuallyScans:
    """A scanner that finds nothing makes every assertion above vacuous.

    This mattered even when the allowlist was populated: the first version
    filtered on the absolute path's parts, so running the suite from a ``git
    worktree`` — the way commits are verified here — matched ``.worktrees`` in
    every path and skipped the entire backend, and the allowlist read as entirely
    stale. It matters more now. "Found nothing" is the expected result, so a
    scanner that reads zero files agrees with a clean tree and no other test can
    tell the difference.
    """

    def test_it_reads_the_backend(self):
        scanned = _scanned_paths()
        assert len(scanned) > 100, (
            f"the scanner considered only {len(scanned)} files, so the empty "
            "inventory above proves nothing. Check the skip filter — it must "
            "apply to the path RELATIVE to the backend root, since an absolute "
            "path can contain '.worktrees' or a checkout directory name."
        )

    def test_it_reads_the_files_that_used_to_hold_the_raw_calls(self):
        """Specific anchors, so a scanner that finds only leaf modules fails.

        These four are where the last raw calls lived. Each must still be *read* —
        with zero hits — for "the surface is closed" to mean anything about them.
        """
        scanned = set(_scanned_paths())
        for anchor in (
            "services/elasticsearch_service.py",
            "persistence/rebuild_document_store.py",
            "scripts/seed_kfactor_demo.py",
            "services/data_seeder.py",
        ):
            assert anchor in scanned, f"{anchor} was not scanned"

    def test_it_finds_a_real_call_in_a_synthetic_tree(self, tmp_path, monkeypatch):
        """The positive control, and a direct reproduction of the path bug.

        With no real hits left in the repository this is the only test that proves
        the scanner can still detect a raw call at all. It doubles as the
        regression test for the absolute-path filter: the synthetic backend root
        is placed under a ``.worktrees`` directory, the string that broke it.
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
    included the docstrings several of these files carry explaining which raw call
    they used to make. ``ops/ingestion/poison_queue.py`` had all seven of its
    calls migrated and still reported one, so the ratchet was measuring
    documentation and the file could not be removed from the list.

    This is load-bearing for the empty inventory: the surviving explanatory
    docstrings in ``ops/services/ops_es_service.py``,
    ``Agents/tools/ops_report_tools.py`` and this module's own prose all contain
    the pattern, so a regex scanner would report three hits on a clean tree.
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
        """Half a dozen HTTP providers bind a bare ``client``.

        Counting ``client.get(url)`` would add them all to the list and drown the
        signal. The inventory is about reaching past the facade, which looks like
        ``<something>.client``.
        """
        assert _count_calls('client.get("https://example.test")') == 0
