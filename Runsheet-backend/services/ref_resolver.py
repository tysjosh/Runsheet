"""
Cross-module reference resolver (cross-module-entity-linkage, Phase A).

This is the shared seam that lets one domain reference another's canonical
entity (customer / asset / driver / order / job / ...) and either:

* **resolve** the reference to a small summary for display (read-time), or
* **validate** that the reference exists in the same tenant before it is
  persisted (write-time).

Design: ``.kiro/specs/cross-module-entity-linkage/design.md`` §Referential
Integrity Strategy.

Because the domains span heterogeneous stores (Postgres source-of-truth + ES
projections) we do **not** use DB foreign keys. Instead each entity type
registers an async *loader* ``(tenant_id, entity_id) -> summary | None``. The
loader is responsible for tenant-scoping its lookup, so a reference to an id
that belongs to another tenant resolves to ``None`` and is surfaced as
``unresolved`` (read) or rejected (write) — never leaked across tenants
(Property 2 / Req 5.3).

Validates: Requirements 5.1, 5.3, 5.4, 2.3, 3.3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol, runtime_checkable

from errors.exceptions import validation_error

logger = logging.getLogger(__name__)

#: The canonical entity types references can point at. Extended as later phases
#: add resolver reads (tank, terminal, depot, invoice, account, payment, ...).
EntityType = str

#: A loader resolves ``(tenant_id, entity_id)`` to a summary mapping or ``None``
#: when no such entity exists in that tenant. Loaders MUST tenant-scope their
#: lookup so a cross-tenant id returns ``None``.
EntityLoader = Callable[[str, str], Awaitable[Optional[Dict[str, Any]]]]


@runtime_checkable
class SupportsResolve(Protocol):
    async def resolve(
        self, tenant_id: str, entity_type: EntityType, entity_id: Optional[str]
    ) -> "ResolvedRef": ...


@dataclass(frozen=True)
class ResolvedRef:
    """The outcome of resolving a single reference.

    ``status`` is ``"resolved"`` (``summary`` populated), ``"unresolved"``
    (the id did not resolve in this tenant), or ``"empty"`` (no id was supplied
    — the reference is simply absent, not dangling).
    """

    entity_type: EntityType
    id: Optional[str]
    status: str  # "resolved" | "unresolved" | "empty"
    summary: Optional[Dict[str, Any]] = None

    @property
    def is_resolved(self) -> bool:
        return self.status == "resolved"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for an API ``links`` payload (Req 5.4).

        A resolved ref returns ``{status, id, summary}``; an unresolved/empty ref
        returns ``{status, id}`` so the UI can render an explicit "unlinked"
        affordance rather than a silently-dropped field. The summary is nested
        (not spread) so a summary field named ``status``/``id`` can never clobber
        the resolution marker.
        """
        if self.status == "resolved":
            return {"status": "resolved", "id": self.id, "summary": self.summary or {}}
        return {"status": self.status, "id": self.id}


class RefResolver:
    """Registry of per-entity-type loaders with resolve + validate operations.

    Production wiring registers a loader per entity type at startup; tests
    inject in-memory fakes. The resolver never reaches into a store itself —
    all I/O is delegated to the registered loader, keeping this class trivially
    testable and free of import-time side effects.
    """

    def __init__(self) -> None:
        self._loaders: Dict[EntityType, EntityLoader] = {}

    def register(self, entity_type: EntityType, loader: EntityLoader) -> None:
        """Register (or replace) the loader for ``entity_type``."""
        self._loaders[entity_type] = loader

    def registered_types(self) -> tuple[EntityType, ...]:
        return tuple(sorted(self._loaders))

    async def resolve(
        self, tenant_id: str, entity_type: EntityType, entity_id: Optional[str]
    ) -> ResolvedRef:
        """Resolve a single reference to a :class:`ResolvedRef` (never raises).

        * No id supplied → ``empty`` (the reference is absent).
        * Id supplied but no loader / loader returns ``None`` → ``unresolved``.
        * Loader returns a summary → ``resolved``.

        A loader exception is treated as ``unresolved`` (defensive): a flaky
        backend must not turn a linked-entity read into a 500.
        """
        if not entity_id or not isinstance(entity_id, str):
            return ResolvedRef(entity_type=entity_type, id=entity_id, status="empty")

        loader = self._loaders.get(entity_type)
        if loader is None:
            logger.debug("RefResolver: no loader registered for %r", entity_type)
            return ResolvedRef(
                entity_type=entity_type, id=entity_id, status="unresolved"
            )

        try:
            summary = await loader(tenant_id, entity_id)
        except Exception as exc:  # noqa: BLE001 - defensive; never 500 a read
            logger.warning(
                "RefResolver: loader for %r raised resolving id=%s: %s",
                entity_type,
                entity_id,
                exc,
            )
            return ResolvedRef(
                entity_type=entity_type, id=entity_id, status="unresolved"
            )

        if summary is None:
            return ResolvedRef(
                entity_type=entity_type, id=entity_id, status="unresolved"
            )
        return ResolvedRef(
            entity_type=entity_type,
            id=entity_id,
            status="resolved",
            summary=dict(summary),
        )

    async def resolve_many(
        self,
        tenant_id: str,
        refs: Dict[str, tuple[EntityType, Optional[str]]],
    ) -> Dict[str, ResolvedRef]:
        """Resolve a labeled batch of references for an API ``links`` object.

        ``refs`` maps an output key (e.g. ``"customer"``) to
        ``(entity_type, id)``. Returns the same keys mapped to their
        :class:`ResolvedRef`.
        """
        out: Dict[str, ResolvedRef] = {}
        for key, (entity_type, entity_id) in refs.items():
            out[key] = await self.resolve(tenant_id, entity_type, entity_id)
        return out

    async def validate_ref(
        self,
        tenant_id: str,
        entity_type: EntityType,
        entity_id: Optional[str],
        *,
        required: bool = True,
    ) -> None:
        """Write-time guard: assert ``entity_id`` exists in ``tenant_id``.

        Raises ``validation_error`` (HTTP 400) with a stable ``details.reason``
        when the reference cannot be resolved:

        * ``<entity_type>_not_found`` — id supplied but not resolvable in this
          tenant (covers cross-tenant ids, which resolve to ``None``).
        * ``<entity_type>_required`` — no id supplied while ``required``.

        A ``None``/empty id with ``required=False`` is accepted (the reference
        is optional and simply absent).

        Validates: Requirements 2.3, 3.3, 5.3.
        """
        if not entity_id:
            if required:
                raise validation_error(
                    message=f"A {entity_type} reference is required",
                    details={"reason": f"{entity_type}_required"},
                )
            return

        resolved = await self.resolve(tenant_id, entity_type, entity_id)
        if not resolved.is_resolved:
            raise validation_error(
                message=(
                    f"Referenced {entity_type} {entity_id!r} does not exist in "
                    "this tenant"
                ),
                details={"reason": f"{entity_type}_not_found", "id": entity_id},
            )


# ---------------------------------------------------------------------------
# Process-wide default resolver
# ---------------------------------------------------------------------------

_default_resolver: Optional[RefResolver] = None


def get_ref_resolver() -> RefResolver:
    """Return the process-wide :class:`RefResolver`, constructing it if unset.

    Loaders are registered against this instance during bootstrap (per later
    phase) via :func:`configure_ref_resolver`.
    """
    global _default_resolver
    if _default_resolver is None:
        _default_resolver = RefResolver()
    return _default_resolver


def configure_ref_resolver(resolver: Optional[RefResolver]) -> None:
    """Install (or reset to ``None``) the process-wide resolver. Test seam."""
    global _default_resolver
    _default_resolver = resolver


__all__ = [
    "EntityType",
    "EntityLoader",
    "ResolvedRef",
    "RefResolver",
    "SupportsResolve",
    "get_ref_resolver",
    "configure_ref_resolver",
]
