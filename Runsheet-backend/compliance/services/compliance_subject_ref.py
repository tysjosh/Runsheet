"""Uniform compliance subject reference (cross-module-entity-linkage, task 10).

Every compliance record is *about* some canonical entity in another module — a
certification is about an **asset**, a terminal BOL about a **driver**, a tax
exemption about a **customer/account**, a k-factor adjustment about a **tank**,
and so on. Historically each record carried that subject id under a different,
domain-specific field name (``asset_id`` / ``truck_id`` / ``driver_id`` /
``customer_id`` / ``tank_id``), so there was no uniform way to ask "what is this
record about?" or to render/validate that reference consistently.

This module introduces a uniform ``subject_ref = {subject_type, subject_id}``
value object plus the per-record-kind mapping that derives it, and the
validate / resolve helpers that tie it to the shared
:class:`services.ref_resolver.RefResolver`:

* **derive** — :func:`subject_ref_for_kind` (and the model-side ``subject_ref``
  properties that delegate to it) map a record's existing canonical id to a
  uniform ``SubjectRef`` (reference-don't-duplicate: the id is *not* stored a
  second time, the ``SubjectRef`` is a uniform view over the field already
  present).
* **validate** — :func:`validate_subject_ref` asserts at write time that the
  subject resolves to an existing entity *in the same tenant*, rejecting a
  dangling / cross-tenant reference (Req 11.1).
* **resolve** — :func:`resolve_subject_ref` resolves the reference for display
  via the same resolver, returning an explicit ``unresolved`` marker rather
  than a silently-dropped field (Req 5.4 / Property 4).

Because the subject types (``asset`` / ``driver`` / ``customer`` / ``account``
/ ``tank``) line up 1:1 with the loader entity types already registered on the
resolver (``services.ref_loaders``), no new loaders are needed — the existing
asset / driver / customer / account / tank loaders resolve every compliance
subject.

Design: ``.kiro/specs/cross-module-entity-linkage/design.md`` §Data Models
(Compliance subject reference).

Validates: Requirements 11.1.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from services.ref_resolver import ResolvedRef, SupportsResolve

# ---------------------------------------------------------------------------
# Subject taxonomy
# ---------------------------------------------------------------------------

#: The canonical entity a compliance record can be *about*. These line up 1:1
#: with the loader entity types registered on the shared ``RefResolver`` so a
#: ``SubjectRef`` is resolvable without any compliance-specific loader.
SubjectType = Literal["asset", "driver", "customer", "account", "tank"]

#: The compliance record kinds that carry a subject reference, mapped to their
#: *default* subject type. ``exemption`` / ``pricing`` / ``contract`` default to
#: ``customer`` but resolve to ``account`` when the record is account-scoped —
#: see :func:`subject_ref_for_kind`.
SUBJECT_TYPE_BY_KIND: Dict[str, SubjectType] = {
    "certification": "asset",  # AssetCertification.asset_id
    "meter": "asset",          # MeterRegistration.truck_id (== asset_id)
    "ifta": "asset",           # TripSegment.truck_id (== asset_id)
    "terminal_bol": "driver",  # TerminalBOL.driver_id
    "exemption": "customer",   # TaxExemption.customer_id / account_id
    "pricing": "customer",     # pricing rule customer_id / account_id
    "contract": "customer",    # supply/customer contract customer_id / account_id
    "kfactor": "tank",         # KFactorAdjustment.tank_id
}

#: Record kinds whose subject is a customer *unless* an account scopes it, in
#: which case the (more specific) account is the canonical subject.
_ACCOUNT_SCOPABLE_KINDS = frozenset({"exemption", "pricing", "contract"})


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


class SubjectRef(BaseModel):
    """A uniform ``{subject_type, subject_id}`` reference to a record's subject.

    Immutable value object shared by every compliance record kind so the
    subject of a certification, meter, IFTA segment, terminal BOL, exemption,
    or k-factor adjustment can be rendered, validated, and resolved the same
    way regardless of which domain-specific field originally held the id.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_type: SubjectType
    subject_id: str

    @field_validator("subject_id")
    @classmethod
    def _subject_id_must_be_non_empty(cls, v: str) -> str:
        """Reject empty / whitespace-only subject ids."""
        if not isinstance(v, str):
            raise ValueError("subject_id must be a string")
        stripped = v.strip()
        if not stripped:
            raise ValueError("subject_id must not be empty or whitespace")
        return stripped

    def to_dict(self) -> Dict[str, str]:
        """Serialize for an API payload / ``<EntityLink>`` props (task 10.1)."""
        return {"subject_type": self.subject_type, "subject_id": self.subject_id}


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def subject_ref_for_kind(
    kind: str,
    *,
    subject_id: Optional[str] = None,
    account_id: Optional[str] = None,
) -> Optional[SubjectRef]:
    """Derive the uniform :class:`SubjectRef` for a compliance record ``kind``.

    ``subject_id`` is the record's primary subject id (``asset_id`` /
    ``truck_id`` / ``driver_id`` / ``customer_id`` / ``tank_id`` depending on
    the kind). For the account-scopable kinds (``exemption`` / ``pricing`` /
    ``contract``) a non-empty ``account_id`` makes the (more specific) account
    the canonical subject; otherwise the ``subject_id`` (the customer) is used.

    Returns ``None`` when no usable id is present (a record that simply has no
    subject yet), so callers can treat "no subject" distinctly from an invalid
    one — mirroring the resolver's ``empty`` vs ``unresolved`` distinction.

    Raises ``KeyError`` for an unknown ``kind`` so a typo fails loudly rather
    than silently producing the wrong subject type.
    """
    if kind not in SUBJECT_TYPE_BY_KIND:
        raise KeyError(f"unknown compliance record kind: {kind!r}")

    if kind in _ACCOUNT_SCOPABLE_KINDS and account_id and account_id.strip():
        return SubjectRef(subject_type="account", subject_id=account_id.strip())

    if subject_id and subject_id.strip():
        return SubjectRef(
            subject_type=SUBJECT_TYPE_BY_KIND[kind], subject_id=subject_id.strip()
        )

    return None


# ---------------------------------------------------------------------------
# Validate (write-time) / Resolve (read-time)
# ---------------------------------------------------------------------------


async def validate_subject_ref(
    resolver: Optional[SupportsResolve],
    tenant_id: str,
    subject_ref: Optional[SubjectRef],
    *,
    required: bool = True,
    skip_if_unregistered: bool = True,
) -> None:
    """Assert ``subject_ref`` resolves to an existing same-tenant entity.

    Delegates to :meth:`RefResolver.validate_ref`, raising ``validation_error``
    (HTTP 400) with ``details.reason = "<subject_type>_not_found"`` when the
    subject does not resolve in ``tenant_id`` — covering both a missing subject
    and a cross-tenant id (which resolves to ``None``), so a compliance record
    can never persist a reference that crossed a tenant boundary (Req 11.1 /
    Property 2 / Property 5).

    Mirrors the partially-wired posture used by the fuel-ops / customer-tank
    write paths: when no ``resolver`` is wired, or ``skip_if_unregistered`` is
    set and no loader is registered for the subject's type, validation is
    skipped so a focused unit test (or a partially-wired environment) stays
    additive/backward-compatible — the reference simply persists unvalidated.

    A ``None`` ``subject_ref`` with ``required=True`` is rejected
    (``<...>_required``); with ``required=False`` it is accepted.
    """
    if subject_ref is None:
        if required:
            # No subject at all: surface a stable, type-agnostic reason.
            from errors.exceptions import validation_error

            raise validation_error(
                message="A compliance subject reference is required",
                details={"reason": "subject_ref_required"},
            )
        return

    if resolver is None:
        return

    if skip_if_unregistered:
        try:
            registered = subject_ref.subject_type in resolver.registered_types()  # type: ignore[attr-defined]
        except AttributeError:
            registered = True
        if not registered:
            return

    await resolver.validate_ref(
        tenant_id,
        subject_ref.subject_type,
        subject_ref.subject_id,
        required=required,
    )


async def resolve_subject_ref(
    resolver: SupportsResolve,
    tenant_id: str,
    subject_ref: Optional[SubjectRef],
) -> ResolvedRef:
    """Resolve ``subject_ref`` for display via the shared resolver.

    Returns a :class:`ResolvedRef` whose ``to_dict()`` is either a resolved
    summary or an explicit ``{status: "unresolved"|"empty", id}`` marker, so a
    dangling subject is never silently dropped from a compliance read (Req 5.4
    / Property 4). A ``None`` ``subject_ref`` resolves to an ``empty`` marker.
    """
    if subject_ref is None:
        return ResolvedRef(entity_type="subject", id=None, status="empty")
    return await resolver.resolve(
        tenant_id, subject_ref.subject_type, subject_ref.subject_id
    )


__all__ = [
    "SubjectType",
    "SUBJECT_TYPE_BY_KIND",
    "SubjectRef",
    "subject_ref_for_kind",
    "validate_subject_ref",
    "resolve_subject_ref",
]
