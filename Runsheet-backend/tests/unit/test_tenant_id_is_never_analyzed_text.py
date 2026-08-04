"""A ``tenant_id`` that is not a ``keyword`` makes every tenant-scoped read empty.

``inject_tenant_filter`` scopes every read with ``{"term": {"tenant_id": ...}}``,
and a ``term`` query matches only when a token produced by the field's analyzer
equals the whole term. The standard analyzer splits ``demo-tenant`` into ``demo``
and ``tenant``, so on an index where ``tenant_id`` was inferred as analyzed
``text`` the filter matches nothing — from a fully populated index, with no error
and no log line. It is the same failure shape as a Postgres mirror column nobody
writes: the query runs, and the answer is silently wrong.

Three live indices were in that state and none of them looked broken:

    trucks                  10 docs, term-matched 0
    locations                4 docs, term-matched 0
    stripe_payment_intents  15 docs, term-matched 0

Two distinct causes, both of which this file pins.

**Declared but unbuildable.** ``trucks``, ``locations``, ``inventory`` and
``support_tickets`` declared ``semantic_text`` fields. That type needs
Elasticsearch 8.15+ plus an inference endpoint, and nothing in this codebase ever
creates one. On a cluster without it, ``indices.create`` fails with ``No handler
for type [semantic_text]``; ``setup_indices`` logs the failure and continues, so
the first write creates the index by dynamic mapping instead — and the declared
``tenant_id: keyword`` never applies. ``support_tickets`` has no writer, so it was
simply absent. The type bought nothing either way: its only consumer,
``ElasticsearchService.semantic_search``, issues a plain ``multi_match``.

**Never declared at all.** ``stripe_payment_intents`` had no mapping anywhere. The
seeder's bulk write created it dynamically.

An index whose mapping cannot be created is worse than one with no mapping,
because the declaration reads as though it is in force.
"""
from __future__ import annotations

import ast
import pathlib
from typing import Dict, List, Set, Tuple

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parents[2]
_SKIP_DIRS = {
    "venv",
    ".hypothesis",
    "__pycache__",
    "coverage_html",
    "node_modules",
    "tests",
    "alembic",
}


def _source_files() -> List[pathlib.Path]:
    out = []
    for path in _BACKEND.rglob("*.py"):
        rel = path.relative_to(_BACKEND)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        out.append(path)
    return out


def _field_specs(path: pathlib.Path) -> List[Tuple[int, str, Dict[str, object]]]:
    """Every ``"<field>": {"type": "<t>", ...}`` literal in a module.

    A deliberately syntactic scan: mapping dicts are module-level literals or
    method return values across a dozen modules, and importing them all to
    introspect would drag in live service singletons. ``(lineno, field, spec)``.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return []

    found: List[Tuple[int, str, Dict[str, object]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if not isinstance(value, ast.Dict):
                continue
            spec = {
                k.value: v.value
                for k, v in zip(value.keys, value.values)
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
            }
            if "type" in spec:
                found.append((key.lineno, key.value, spec))
    return found


@pytest.fixture(scope="module")
def declared_fields() -> List[Tuple[str, int, str, Dict[str, object]]]:
    out = []
    for path in _source_files():
        rel = path.relative_to(_BACKEND).as_posix()
        for lineno, field, spec in _field_specs(path):
            out.append((rel, lineno, field, spec))
    return out


def test_the_scan_actually_finds_mappings(declared_fields):
    """Guard the premise: a broken scan would make every assertion below pass."""
    tenant_decls = [f for f in declared_fields if f[2] == "tenant_id"]
    assert len(tenant_decls) > 50, (
        f"only found {len(tenant_decls)} tenant_id declarations — the AST scan "
        "is not seeing the mapping dicts, so this file is measuring nothing"
    )


def test_no_mapping_declares_tenant_id_as_anything_but_keyword(declared_fields):
    offenders = [
        f"{rel}:{lineno} -> {spec.get('type')!r}"
        for rel, lineno, field, spec in declared_fields
        if field == "tenant_id" and spec.get("type") != "keyword"
    ]
    assert not offenders, (
        "tenant_id must be a keyword: inject_tenant_filter scopes reads with a "
        "term query, which matches nothing against analyzed text. "
        f"Offenders: {offenders}"
    )


def test_no_mapping_declares_semantic_text(declared_fields):
    """The type is unbuildable here and buys nothing.

    ``semantic_text`` needs ES 8.15+ and an inference endpoint that this codebase
    never creates. Where it is unsupported, index creation fails outright and the
    index is then built by dynamic mapping — losing every other field type in the
    declaration, ``tenant_id: keyword`` included. If semantic retrieval is wanted
    later it needs an inference endpoint provisioned first, and
    ``semantic_search`` needs to issue a ``semantic`` query rather than the
    ``multi_match`` it issues today.
    """
    offenders = [
        f"{rel}:{lineno} field={field}"
        for rel, lineno, field, spec in declared_fields
        if spec.get("type") == "semantic_text"
    ]
    assert not offenders, (
        "semantic_text makes indices.create fail on any cluster without the "
        "type; setup_indices swallows that and the index is then created "
        "dynamically, so tenant_id becomes analyzed text and every "
        f"tenant-scoped term filter matches nothing. Offenders: {offenders}"
    )


class TestTheLegacyCoreIndicesDeclareTenantId:
    """The four indices that were created dynamically must now be buildable.

    Removing ``semantic_text`` only helps if the mapping that finally applies
    actually pins ``tenant_id`` — three of these four never declared it, and were
    relying on the inference that broke them.
    """

    @pytest.fixture(scope="class")
    def core_mappings(self):
        from services.elasticsearch_service import elasticsearch_service as es

        return {
            "trucks": es._get_trucks_mapping(),
            "locations": es._get_locations_mapping(),
            "inventory": es._get_inventory_mapping(),
            "support_tickets": es._get_support_tickets_mapping(),
        }

    def test_every_core_mapping_declares_tenant_id_as_keyword(self, core_mappings):
        for index, mapping in sorted(core_mappings.items()):
            props = mapping["mappings"]["properties"]
            assert "tenant_id" in props, f"{index} does not declare tenant_id"
            assert props["tenant_id"]["type"] == "keyword", (
                f"{index}.tenant_id is {props['tenant_id']['type']!r}"
            )

    def test_no_core_mapping_declares_semantic_text(self, core_mappings):
        def types(node) -> Set[str]:
            out: Set[str] = set()
            if isinstance(node, dict):
                if isinstance(node.get("type"), str):
                    out.add(node["type"])
                for value in node.values():
                    out |= types(value)
            return out

        for index, mapping in sorted(core_mappings.items()):
            assert "semantic_text" not in types(mapping), (
                f"{index} still declares semantic_text, so indices.create will "
                f"keep failing and the index will keep being built dynamically"
            )


def test_the_stripe_demo_index_has_a_declared_mapping():
    """It had none, so the seeder's bulk write created it dynamically.

    Same symptom as the four above, different cause: nothing to reject, because
    nothing was declared.
    """
    from integrations.stripe_es_mappings import (
        STRIPE_PAYMENT_INTENTS_INDEX,
        STRIPE_PAYMENT_INTENTS_MAPPING,
    )

    props = STRIPE_PAYMENT_INTENTS_MAPPING["mappings"]["properties"]
    assert props["tenant_id"]["type"] == "keyword"
    # The connector range-filters and sorts on ``created``; a dynamic mapping
    # would have inferred whatever the first document looked like.
    assert props["created"]["type"] == "date"

    import seed_all_data

    assert STRIPE_PAYMENT_INTENTS_INDEX in seed_all_data._managed_index_mappings(), (
        "the index must be in the managed set, or --recreate leaves it behind "
        "with the dynamic mapping it has today"
    )


def test_the_stripe_index_is_created_before_the_fixtures_load():
    """Order matters: a bulk write to a missing index creates it dynamically.

    Registering the mapping is not enough if the fixture load gets there first.
    Step 1 of ``main()`` runs the setup functions and step 2 loads the fixtures,
    so it is sufficient that the setup function is registered — this pins that
    registration, which is the part a future edit could drop.
    """
    import seed_all_data

    labels = [label for label, _fn in seed_all_data._index_setup_functions()]
    assert "Stripe demo indices" in labels, (
        "setup_stripe_indices is not registered, so stripe_payment_intents is "
        "created by the fixture bulk write with a dynamic mapping"
    )
