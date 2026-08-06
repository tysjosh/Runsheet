"""Which document fields must stay unsearchable after the move off Elasticsearch.

An Elasticsearch mapping can declare that a field is stored but **not indexed**:

* ``"type": "binary"`` — stored, never searchable, and an aggregation over it
  errors;
* ``"index": false`` on a leaf — stored and returned, not queryable;
* ``"enabled": false`` on an object — the whole subtree is stored as opaque JSON
  and not queryable.

In a jsonb column every field is queryable. So moving the document plane to
Postgres silently *widens* what can be filtered on, and two of the affected fields
make that a security property rather than a curiosity:

* ``fuel_orders_current.pod_otp`` — the proof-of-delivery one-time code. With ES
  it cannot appear in a filter at all. Made searchable, an authenticated caller
  who can reach any order-search endpoint can confirm a guessed OTP a query at a
  time.
* ``driver_devices.push_token`` — an Expo push credential, same reasoning.

The rest are ``enabled: false`` payload blobs (``job_events.event_payload``,
``invoices_current.tax_breakdown``, ``idempotency_keys.response`` …). Nothing
queries them today, and nothing should start by accident: a filter on an
unindexed blob is a full scan in Postgres and an error in Elasticsearch, so
allowing it would also make the two backends behave differently under load.

This module reads the declared mappings — the code, not the cluster, so it keeps
working after Elasticsearch is gone — and the document store refuses any query
that filters, sorts or aggregates on one of these fields. Refusing is what keeps
the migration behaviour-preserving; the alternative is a quiet capability
increase in exactly the place nobody audits.

Returning the document itself is unaffected. These fields are stored and returned
by Elasticsearch too; only *querying* them is blocked.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["UnsearchableFieldError", "unsearchable_fields", "assert_searchable"]


class UnsearchableFieldError(PermissionError):
    """A query touched a field the Elasticsearch mapping declares unsearchable.

    A ``PermissionError`` subclass rather than a ``ValueError`` because the two
    fields that matter most are credentials: the correct HTTP translation is 403,
    not 400, and middleware that maps exception types already does the right thing
    with ``PermissionError``.
    """

    def __init__(self, index: str, field: str, reason: str) -> None:
        super().__init__(
            f"field {field!r} of index {index!r} is not searchable ({reason}). "
            "Elasticsearch cannot filter, sort or aggregate on it either, so the "
            "Postgres document store refuses to — allowing it would widen what "
            "callers can query. Fetch the document and read the field instead."
        )
        self.index = index
        self.field = field
        self.reason = reason


# ---------------------------------------------------------------------------
# Mapping discovery
# ---------------------------------------------------------------------------

#: Every registry of ``{index_name: mapping_body}`` in the codebase. Listed rather
#: than discovered because an import-time scan of every module would drag the
#: whole application into any process that reads a document.
_REGISTRIES: Tuple[Tuple[str, str], ...] = (
    ("Agents.overlay.overlay_es_mappings", "OVERLAY_INDEX_MAPPINGS"),
    ("Agents.support.mvp_es_mappings", "MVP_INDEX_MAPPINGS"),
    ("commerce.services.commerce_es_mappings", "COMMERCE_INDEX_MAPPINGS"),
    ("compliance.services.compliance_es_mappings", "COMPLIANCE_INDEX_MAPPINGS"),
    ("driver.services.driver_es_mappings", "DRIVER_INDEX_MAPPINGS"),
    ("fuel.services.fuel_es_mappings", "FUEL_INDEX_MAPPINGS"),
    ("fuel.services.fuel_ops_es_mappings", "FUEL_OPS_INDEX_MAPPINGS"),
    ("fuel.services.order_es_mappings", "ORDER_INTAKE_INDEX_MAPPINGS"),
    ("fuel.voice.voice_es_mappings", "VOICE_INDEX_MAPPINGS"),
    ("integrations.stripe_es_mappings", "STRIPE_INDEX_MAPPINGS"),
    ("scheduling.services.scheduling_es_mappings", "SCHEDULING_INDEX_MAPPINGS"),
    # Added when Phase 6 deleted ``setup_ops_indices``: the ops mappings used to
    # live as four methods on ``OpsElasticsearchService`` and were never in this
    # registry, so ``ops_poison_queue.original_payload`` — declared
    # ``enabled: false``, holding the raw payload of a failed ingestion — was
    # freely queryable in jsonb where Elasticsearch could not filter on it at all.
    ("ops.services.ops_es_mappings", "OPS_INDEX_MAPPINGS"),
)

_cache: Dict[str, FrozenSet[str]] = {}


def _iter_mappings() -> Iterable[Tuple[str, Dict[str, Any]]]:
    for module_path, attribute in _REGISTRIES:
        try:
            module = __import__(module_path, fromlist=[attribute])
        except Exception as exc:  # noqa: BLE001 — narrowed by the log level below
            # Logged at ERROR, not DEBUG. This function decides which fields are
            # unqueryable, so a registry that fails to import does not degrade the
            # policy, it DELETES part of it — and silently. That is not
            # hypothetical: ``ops.services.ops_es_mappings`` was first generated
            # with ``json.dumps``, so it contained a lowercase ``false``, parsed
            # fine, and raised ``NameError`` on import. At DEBUG the policy simply
            # reported no restrictions for the ops indices and nothing said so.
            logger.error(
                "field policy: registry %s FAILED to import, so any unsearchable "
                "field it declares is now queryable: %s",
                module_path, exc,
            )
            continue
        registry = getattr(module, attribute, None)
        if isinstance(registry, dict):
            yield from registry.items()
        else:
            logger.error(
                "field policy: %s.%s is %s, not a dict — its unsearchable fields "
                "are not being enforced",
                module_path, attribute, type(registry).__name__,
            )


def _walk(properties: Dict[str, Any], prefix: str = "") -> Iterable[Tuple[str, str]]:
    """Yield ``(dotted_path, reason)`` for every unsearchable field."""
    for name, spec in (properties or {}).items():
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}{name}"
        if spec.get("type") == "binary":
            yield (path, "mapped as binary: stored but never indexed")
            continue
        if spec.get("index") is False:
            yield (path, "mapped with index: false")
            continue
        if spec.get("enabled") is False:
            # The whole subtree is opaque. Yielding the parent is enough: the
            # store checks prefixes, so ``payload.anything`` is covered.
            yield (path, "object mapped with enabled: false")
            continue
        if "properties" in spec:
            yield from _walk(spec["properties"], f"{path}.")


def all_index_mappings() -> List[Tuple[str, Dict[str, Any]]]:
    """Every ``(index_name, mapping)`` the codebase declares.

    Rehomed from ``services/mapping_validator._collect_all_index_mappings``, which
    Phase 6 deleted along with the rest of the cluster-management code. The
    *collection* is not cluster-management: it is the union of the schema
    declarations, and it is still the input to two contracts worth keeping —
    ``tests/unit/test_mapping_timestamp_contract.py`` (every auto-stamped index
    declares ``created_at`` and ``updated_at``) and the driver-surface
    ``dynamic: strict`` assertions.

    It also reads better here than it did there: the old version named ~70
    ``*_INDEX`` / ``*_MAPPING`` pairs by hand across a 160-line function, so a new
    index was only covered if someone remembered to add it. This walks the same
    registries the field policy already walks, so a new index in any registry is
    included automatically.
    """
    return list(_iter_mappings())


def unsearchable_fields(index: str) -> FrozenSet[str]:
    """Dotted field paths that must not appear in a filter/sort/aggregation.

    Empty for an index with no declared mapping — a dynamically-mapped index has
    no unsearchable fields by definition, since everything in it was indexed
    dynamically.
    """
    cached = _cache.get(index)
    if cached is not None:
        return cached
    found: List[str] = []
    for name, body in _iter_mappings():
        if name != index:
            continue
        properties = (body.get("mappings") or {}).get("properties") or {}
        found = [path for path, _reason in _walk(properties)]
        break
    result = frozenset(found)
    _cache[index] = result
    return result


def reasons_for(index: str) -> Dict[str, str]:
    """``{field: reason}`` for one index — for error messages and documentation."""
    for name, body in _iter_mappings():
        if name != index:
            continue
        properties = (body.get("mappings") or {}).get("properties") or {}
        return dict(_walk(properties))
    return {}


def assert_searchable(index: str, fields: Iterable[str]) -> None:
    """Raise :class:`UnsearchableFieldError` if any field is not queryable.

    Prefix-aware: a field under an ``enabled: false`` object is blocked by the
    object's entry, so the mapping does not have to enumerate a subtree that
    Elasticsearch itself does not describe.
    """
    blocked = unsearchable_fields(index)
    if not blocked:
        return
    for field in fields:
        # ``.keyword`` is a multi-field suffix, stripped the same way the query
        # translator strips it, so ``pod_otp.keyword`` cannot slip past.
        candidate = field[: -len(".keyword")] if field.endswith(".keyword") else field
        for blocked_path in blocked:
            if candidate == blocked_path or candidate.startswith(f"{blocked_path}."):
                raise UnsearchableFieldError(
                    index, field, reasons_for(index).get(blocked_path, "not indexed")
                )
