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

**The point of the list is that it shrinks.** It is the definition of "ready to
flip the flag": while it is non-empty, `DOCUMENT_STORE_BACKEND=postgres` splits
writes across two stores. That is stated in
``docs/elasticsearch-to-postgres-migration.md`` rather than left for someone to
infer from a green test run.

Not every entry is a problem. Three kinds are on the list for different reasons:

*control plane* — ``indices.*``, ``ilm.*``, ``ping``. These manage indices and
lifecycle policies, and they have no Postgres equivalent because there is nothing
to manage: the document store is one table. They become no-ops when Elasticsearch
goes, and they are excluded from the count entirely.

*migration tooling* — ``persistence/rebuild_from_postgres.py``,
``scripts/*``. These exist to talk to Elasticsearch. They are the last things to
go, not the first.

*application data plane* — everything else. These are the ones that must move.
"""

from __future__ import annotations

import pathlib
import re

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
    # The facade's own Elasticsearch branch: these are the calls the backend
    # switch chooses BETWEEN, so they are the one place a raw call belongs. Nine,
    # not six, because ``upsert_if_newer`` moved three here out of
    # ``fuel/order_repository.py`` — a net reduction in call sites that bypass the
    # switch, even though this file's own count went up.
    "services/elasticsearch_service.py": 9,
    # --- Read-modify-write: painless scripts and if_seq_no OCC loops. --------
    # ``PostgresDocumentStore.atomic_update`` is the replacement and is tested,
    # including under real contention. ``fuel/order_repository.py`` and
    # ``ops/services/ops_es_service.py`` have been moved onto
    # ``ElasticsearchService.upsert_if_newer`` and are gone from this list; these
    # three have not been rewritten yet.
    "fuel/driver_repository.py": 4,              # counter scripts + update_by_query
    "fuel/compartment_state_models.py": 4,       # if_seq_no OCC, two loops
    "Agents/approval_queue_service.py": 2,       # if_seq_no OCC
    # --- Mechanical searches: the store already answers these unchanged. -----
    "ops/api/endpoints.py": 21,
    "ops/ingestion/poison_queue.py": 7,
    "Agents/tools/ops_report_tools.py": 1,
    "Agents/tools/ops_search_tools.py": 1,
    "bootstrap/core.py": 1,                      # scan over an index at startup
}

#: Files where a raw-client call is expected and is NOT part of the migration
#: debt, because it manages indices rather than documents.
_CONTROL_PLANE_ONLY = frozenset({"services/mapping_validator.py"})


def _is_elasticsearch_file(relative: str) -> bool:
    return relative not in _NOT_ELASTICSEARCH


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
    pattern = re.compile(r"\.client\.([a-z_]+)(?:\.[a-z_]+)?\s*\(")
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
            text = path.read_text()
        except Exception:  # noqa: BLE001
            continue
        count = 0
        for line in text.splitlines():
            for match in pattern.finditer(line):
                # ``client.indices.exists`` is control plane; ``client.exists`` is
                # not. The regex captures the first attribute, so a two-segment
                # call is identified by the text following it.
                attribute = match.group(1)
                if f".client.{attribute}." in line:
                    continue
                if attribute in _DATA_PLANE:
                    count += 1
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
    def test_the_migration_doc_records_that_the_flag_is_not_yet_flippable(self):
        """The dangerous state is the flag LOOKING ready while this list is long.

        Flipping ``DOCUMENT_STORE_BACKEND=postgres`` today would split writes
        across two stores. That has to be written down where an operator reads,
        not inferred from a test file.
        """
        doc = (_BACKEND.parent / "docs" / "elasticsearch-to-postgres-migration.md")
        assert doc.is_file(), "the migration doc is the operator-facing record"
        text = doc.read_text()
        assert "raw" in text and "client" in text, (
            "the migration doc does not mention the raw-client surface, so an "
            "operator reading it would think the cutover is a flag flip"
        )

    def test_the_replacement_primitive_exists_and_is_reachable(self):
        """The four read-modify-write sites need somewhere to go."""
        from persistence.document_store import PostgresDocumentStore

        for method in ("atomic_update", "upsert_if_newer", "update_by_query",
                       "delete_by_query", "document_exists"):
            assert hasattr(PostgresDocumentStore, method), method


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

    def test_it_finds_the_facade_which_definitely_has_raw_calls(self):
        """A specific anchor, so a scanner that finds only test fixtures fails."""
        found = _scan()
        assert found.get("services/elasticsearch_service.py", 0) > 0, (
            "the scanner did not find the raw client calls in "
            "ElasticsearchService, which certainly has them"
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
