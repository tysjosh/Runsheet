"""MarginRepository: the only module that touches the margin tables.

Tenant isolation is enforced here (design "Invariants and their owners"):
every public method of :class:`MarginRepository` takes ``tenant_id`` as its
first argument and filters on it, and writes take the tenant from that
argument, never from a payload. Rows leave this module as plain dicts, so no
caller holds an ORM object.

Two methods need to find tenants rather than serve one (RevenueGuard's queue
scan and the weekly report's tenant list). They live on
:class:`MarginTenantDiscovery` (``repo.discovery``) and return tenant ids
only, never row data.

The versioned write protocol (design "Versioned write protocol") is
:meth:`MarginRepository.write_record`: one locked read-decide-write per key,
with the rule table below and one retry on a racing ``IntegrityError``.

=========================  ======  ===========  ======  =================
Latest row                 live    finalize     void    recompute
=========================  ======  ===========  ======  =================
none                       v1      v1 frozen    v1 void v1 (frozen if final)
void                       skip    skip         skip    skip
active+frozen, same hash   skip    skip         void    skip
active+frozen, new hash    skip    skip         void    v+1 frozen
active, same hash          skip    freeze       void    skip
active, new hash           v+1     v+1 frozen   void    v+1 (frozen if final)
=========================  ======  ===========  ======  =================

``with_for_update()`` is a no-op on SQLite; the partial unique indexes give
the same guarantee there.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from sqlalchemy import and_, delete, distinct, func, or_, select, union, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from commerce.models.margin import (
    DEFAULT_FLOOR_MICROS,
    DEFAULT_RACK_STALENESS_DAYS,
    DEFAULT_TIMEZONE,
    DEFAULT_WAC_WINDOW_DAYS,
    AlertState,
    AlertStatus,
    AlertType,
    CostEntryKind,
    CostEntryStatus,
    DigestState,
    MarginFlag,
    MarginStage,
    RecordOrigin,
    RecordStatus,
    RunStatus,
    WriteMode,
)
from persistence.database import session_scope
from persistence.models import (
    MarginAlertORM,
    MarginCostEntryORM,
    MarginRecomputeRunORM,
    MarginRecordORM,
    MarginSettingsORM,
    MarginSkippedSourceORM,
    MarginWeeklyReportORM,
)
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: A running recompute whose heartbeat is older than this is taken over.
RUN_STALE_AFTER = timedelta(hours=1)
#: RevenueGuard drains at most this many pending records per tenant per cycle.
PENDING_RECORDS_LIMIT = 500
#: Batch size for ``IN (...)`` lookups.
_IN_CHUNK = 1_000

_ALERTING_STAGES = frozenset({MarginStage.DELIVERY.value, MarginStage.INVOICE.value})
_FLAG_COLUMNS = {
    MarginFlag.MISSING_COST.value: MarginRecordORM.flag_missing_cost,
    MarginFlag.NEGATIVE_MARGIN.value: MarginRecordORM.flag_negative_margin,
    MarginFlag.BELOW_FLOOR.value: MarginRecordORM.flag_below_floor,
    MarginFlag.TERMINAL_UNATTRIBUTED.value: MarginRecordORM.flag_terminal_unattributed,
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MarginRepositoryError(Exception):
    """Base class; the service layer maps these to ``AppException`` codes."""


class MarginDuplicateEntryError(MarginRepositoryError):
    """An active entry already holds this natural key or BOL (409 CONFLICT)."""

    def __init__(self, existing_entry_id: Optional[str]) -> None:
        super().__init__("duplicate active cost entry")
        self.existing_entry_id = existing_entry_id


class MarginEntryNotFoundError(MarginRepositoryError):
    """No entry with that id in this tenant (404)."""


class MarginEntryNotActiveError(MarginRepositoryError):
    """The entry is superseded or voided (409)."""


class MarginEntryKindMismatchError(MarginRepositoryError):
    """A supersede tried to change the entry kind (422)."""


class MarginAlertNotFoundError(MarginRepositoryError):
    """No alert with that id in this tenant (404)."""


class MarginAlertStateError(MarginRepositoryError):
    """The alert cannot take that action from its current status (409)."""


class MarginRecomputeRunningError(MarginRepositoryError):
    """A recompute run is already running for the tenant (409)."""

    def __init__(self, run_id: Optional[str]) -> None:
        super().__init__("a margin recompute is already running")
        self.run_id = run_id


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarginCandidate:
    """A computed margin record, before the write protocol decides its fate.

    Carries no ``tenant_id``: the tenant comes from the ``write_record``
    argument. ``source_final`` is true for an invoice that is no longer a
    draft; recompute freezes the rows it inserts for such a source.
    """

    stage: str
    source_key: str
    product_code: str
    gallons_ugal: int
    unit_price_micros: int
    revenue_cents: int
    method: str
    floor_micros_used: int
    cost_snapshot: Mapping[str, Any]
    as_of: datetime
    input_hash: str
    order_id: Optional[str] = None
    invoice_id: Optional[str] = None
    line_index: Optional[int] = None
    line_id: Optional[str] = None
    customer_id: Optional[str] = None
    account_id: Optional[str] = None
    terminal_id: Optional[str] = None
    product_cost_micros: Optional[int] = None
    adders_micros: Optional[int] = None
    landed_cost_micros: Optional[int] = None
    cost_cents: Optional[int] = None
    margin_cents: Optional[int] = None
    margin_per_gallon_micros: Optional[int] = None
    margin_bp: Optional[int] = None
    no_cost_reason: Optional[str] = None
    flag_missing_cost: bool = False
    flag_negative_margin: bool = False
    flag_below_floor: bool = False
    flag_terminal_unattributed: bool = False
    recompute_run_id: Optional[str] = None
    source_final: bool = False


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one write: ``inserted``, ``superseded`` (new version),
    ``frozen`` (in place), ``voided``, ``skipped``, ``missing`` (``void_latest``
    found no row) or ``conflict`` (the retry also raced)."""

    written: bool
    outcome: str
    record: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class Page:
    """A keyset page. ``next_key`` is the key of the last item, or ``None``."""

    items: List[Dict[str, Any]]
    next_key: Optional[Tuple[Any, ...]]
    total: Optional[int] = None


@dataclass(frozen=True)
class RecordFilters:
    """``GET /records`` filters. ``as_of_to`` is exclusive."""

    as_of_from: Optional[datetime] = None
    as_of_to: Optional[datetime] = None
    customer_id: Optional[str] = None
    product_code: Optional[str] = None
    terminal_id: Optional[str] = None
    stage: Optional[str] = None
    flag: Optional[str] = None
    status: Optional[str] = RecordStatus.ACTIVE.value  # None or "all": any status


@dataclass(frozen=True)
class EntryTransition:
    """Result of supersede / void: the old row before and after, plus the new row."""

    before: Dict[str, Any]
    after: Dict[str, Any]
    new_entry: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class AlertTransition:
    from_status: str
    alert: Dict[str, Any]


@dataclass(frozen=True)
class SettingsChange:
    before: Dict[str, Any]
    after: Dict[str, Any]


@dataclass
class _Sale:
    sale_key: str
    record_ids: List[str] = field(default_factory=list)
    below_floor: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """Bind-side: require an aware datetime and convert it to UTC.

    SQLite stores the wall-clock digits only, so every bound value must be UTC
    for comparisons to hold on both backends.
    """

    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("margin repository needs timezone-aware datetimes")
    return value.astimezone(timezone.utc)


def _aware(value: Any) -> Any:
    """Read-side: SQLite returns naive UTC; Postgres returns aware values."""

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return value


def _row_dict(row: Any) -> Dict[str, Any]:
    return {col.key: _aware(getattr(row, col.key)) for col in row.__table__.columns}


def _is_unique_violation(exc: IntegrityError) -> bool:
    """A unique / PK race (retryable), as opposed to a CHECK or NOT NULL bug."""

    orig = getattr(exc, "orig", None)
    if getattr(orig, "sqlstate", None) == "23505":  # psycopg 3
        return True
    return "UNIQUE constraint failed" in str(orig)  # SQLite


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _chunks(values: Sequence[Any], size: int = _IN_CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _default_settings(tenant_id: str) -> Dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "wac_window_days": DEFAULT_WAC_WINDOW_DAYS,
        "rack_staleness_days": DEFAULT_RACK_STALENESS_DAYS,
        "floor_micros": DEFAULT_FLOOR_MICROS,
        "product_floors": {},
        "timezone": DEFAULT_TIMEZONE,
        "feed_activated_at": None,
        "updated_by": None,
        "updated_at": None,
        "persisted": False,
    }


def _settings_dict(row: Optional[MarginSettingsORM], tenant_id: str) -> Dict[str, Any]:
    if row is None:
        return _default_settings(tenant_id)
    data = _row_dict(row)
    data["product_floors"] = dict(data.get("product_floors") or {})
    data["persisted"] = True
    return data


def _new_settings_row(tenant_id: str) -> MarginSettingsORM:
    return MarginSettingsORM(
        tenant_id=tenant_id,
        wac_window_days=DEFAULT_WAC_WINDOW_DAYS,
        rack_staleness_days=DEFAULT_RACK_STALENESS_DAYS,
        floor_micros=DEFAULT_FLOOR_MICROS,
        product_floors={},
        timezone=DEFAULT_TIMEZONE,
    )


_SETTINGS_FIELDS = (
    "wac_window_days",
    "rack_staleness_days",
    "floor_micros",
    "product_floors",
    "timezone",
)

_ENTRY_FIELDS = frozenset(
    col.key
    for col in MarginCostEntryORM.__table__.columns
    if col.key not in {"tenant_id"}
)

_ALERT_FIELDS = frozenset(
    {
        "alert_type",
        "severity",
        "status",
        "dedupe_key",
        "record_id",
        "order_id",
        "run_id",
        "proposal_id",
        "customer_id",
        "product_code",
        "details",
    }
)


def _check_tenant(values: Mapping[str, Any], tenant_id: str) -> None:
    other = values.get("tenant_id")
    if other is not None and other != tenant_id:
        raise ValueError("payload tenant_id does not match the repository call")


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class MarginRepository:
    """Tenant-scoped data access for the seven margin tables."""

    def __init__(self, session_factory: Optional[SessionFactory] = None) -> None:
        self._scope: SessionFactory = session_factory or session_scope
        self.discovery = MarginTenantDiscovery(self._scope)

    # ------------------------------------------------------------------
    # Cost entries
    # ------------------------------------------------------------------

    async def insert_entry(self, tenant_id: str, values: Mapping[str, Any]) -> Dict[str, Any]:
        """Insert one active entry.

        Raises :class:`MarginDuplicateEntryError` (with the conflicting id)
        when an active entry already holds the natural key or BOL. Any other
        ``IntegrityError`` (for example ``ck_mce_kind_fields``) propagates.
        """

        row = self._entry_row(tenant_id, values)
        try:
            async with self._scope() as session:
                session.add(row)
                await session.flush()
                return _row_dict(row)
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            existing = await self._conflicting_entry_id(
                tenant_id, values.get("natural_key"), values.get("bol_id")
            )
            if existing is None:
                raise
            raise MarginDuplicateEntryError(existing) from None

    async def insert_entries(
        self, tenant_id: str, rows: Sequence[Mapping[str, Any]]
    ) -> List[str]:
        """Insert many entries in one transaction (CSV import). All or nothing.

        A racing duplicate raises :class:`MarginDuplicateEntryError` with no id.
        """

        orm_rows = [self._entry_row(tenant_id, values) for values in rows]
        try:
            async with self._scope() as session:
                session.add_all(orm_rows)
                await session.flush()
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            raise MarginDuplicateEntryError(None) from None
        return [row.entry_id for row in orm_rows]

    async def transition_entry(
        self,
        tenant_id: str,
        entry_id: str,
        *,
        action: str,
        reason: str,
        actor: str,
        replacement: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> EntryTransition:
        """Supersede (``replacement`` required) or void one active entry.

        Locks the old row, checks tenant (404), status (409) and kind (422),
        marks it, flushes so the new row can reuse its natural key, then
        inserts the replacement and links both ways.
        """

        if action not in ("supersede", "void"):
            raise ValueError("action must be 'supersede' or 'void'")
        if action == "supersede" and replacement is None:
            raise ValueError("supersede needs a replacement entry")
        moment = _utc(now) or utcnow()
        try:
            async with self._scope() as session:
                old = (
                    await session.execute(
                        select(MarginCostEntryORM)
                        .where(
                            MarginCostEntryORM.tenant_id == tenant_id,
                            MarginCostEntryORM.entry_id == entry_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if old is None:
                    raise MarginEntryNotFoundError(entry_id)
                if old.status != CostEntryStatus.ACTIVE.value:
                    raise MarginEntryNotActiveError(entry_id)
                if action == "supersede" and replacement.get("kind") != old.kind:
                    raise MarginEntryKindMismatchError(entry_id)
                before = _row_dict(old)
                old.status = (
                    CostEntryStatus.SUPERSEDED.value
                    if action == "supersede"
                    else CostEntryStatus.VOIDED.value
                )
                old.status_reason = reason
                old.status_changed_by = actor
                old.status_changed_at = moment
                await session.flush()
                new_row: Optional[MarginCostEntryORM] = None
                if action == "supersede":
                    values = dict(replacement)
                    values["supersedes_id"] = old.entry_id
                    new_row = self._entry_row(tenant_id, values)
                    session.add(new_row)
                    await session.flush()
                    old.superseded_by_id = new_row.entry_id
                    await session.flush()
                return EntryTransition(
                    before=before,
                    after=_row_dict(old),
                    new_entry=_row_dict(new_row) if new_row is not None else None,
                )
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            existing = None
            if replacement is not None:
                existing = await self._conflicting_entry_id(
                    tenant_id, replacement.get("natural_key"), replacement.get("bol_id")
                )
            if existing is None:
                raise
            raise MarginDuplicateEntryError(existing) from None

    async def get_entry(self, tenant_id: str, entry_id: str) -> Optional[Dict[str, Any]]:
        async with self._scope() as session:
            row = (
                await session.execute(
                    select(MarginCostEntryORM).where(
                        MarginCostEntryORM.tenant_id == tenant_id,
                        MarginCostEntryORM.entry_id == entry_id,
                    )
                )
            ).scalar_one_or_none()
            return _row_dict(row) if row is not None else None

    async def list_entries(
        self,
        tenant_id: str,
        *,
        kind: Optional[str] = None,
        product_code: Optional[str] = None,
        terminal_id: Optional[str] = None,
        status: Optional[str] = CostEntryStatus.ACTIVE.value,
        after: Optional[Tuple[datetime, str]] = None,
        limit: int = 50,
    ) -> Page:
        """Keyset list on (``created_at`` desc, ``entry_id`` desc).

        ``status`` of ``None`` or ``"all"`` lists every status.
        """

        model = MarginCostEntryORM
        stmt = select(model).where(model.tenant_id == tenant_id)
        if kind is not None:
            stmt = stmt.where(model.kind == kind)
        if product_code is not None:
            stmt = stmt.where(model.product_code == product_code)
        if terminal_id is not None:
            stmt = stmt.where(model.terminal_id == terminal_id)
        if status not in (None, "all"):
            stmt = stmt.where(model.status == status)
        if after is not None:
            created_at, last_id = _utc(after[0]), after[1]
            stmt = stmt.where(
                or_(
                    model.created_at < created_at,
                    and_(model.created_at == created_at, model.entry_id < last_id),
                )
            )
        stmt = stmt.order_by(model.created_at.desc(), model.entry_id.desc()).limit(limit + 1)
        async with self._scope() as session:
            rows = list((await session.execute(stmt)).scalars())
        items = [_row_dict(r) for r in rows[:limit]]
        next_key = (
            (items[-1]["created_at"], items[-1]["entry_id"]) if len(rows) > limit else None
        )
        return Page(items=items, next_key=next_key)

    async def active_effective_entries(
        self,
        tenant_id: str,
        *,
        kind: str,
        product_code: str,
        terminal_id: Optional[str],
        as_of: datetime,
    ) -> List[Dict[str, Any]]:
        """Active ``override`` / ``adder`` entries in effect at ``as_of``.

        In effect means ``effective_at <= as_of`` and either no
        ``effective_to`` or ``as_of < effective_to``. With a terminal, both
        terminal-specific and tenant-wide entries are returned; with none
        (unattributed), only tenant-wide ones. Ordered by ``effective_at``
        desc, ``created_at`` desc, ``entry_id`` desc; precedence between
        scopes is the resolver's job.
        """

        if kind not in (CostEntryKind.OVERRIDE.value, CostEntryKind.ADDER.value):
            raise ValueError("kind must be 'override' or 'adder'")
        at = _utc(as_of)
        model = MarginCostEntryORM
        scope = (
            or_(model.terminal_id == terminal_id, model.terminal_id.is_(None))
            if terminal_id is not None
            else model.terminal_id.is_(None)
        )
        stmt = (
            select(model)
            .where(
                model.tenant_id == tenant_id,
                model.status == CostEntryStatus.ACTIVE.value,
                model.kind == kind,
                model.product_code == product_code,
                scope,
                model.effective_at <= at,
                or_(model.effective_to.is_(None), model.effective_to > at),
            )
            .order_by(model.effective_at.desc(), model.created_at.desc(), model.entry_id.desc())
        )
        async with self._scope() as session:
            return [_row_dict(r) for r in (await session.execute(stmt)).scalars()]

    async def active_purchase_lots(
        self,
        tenant_id: str,
        *,
        product_code: str,
        terminal_id: Optional[str],
        window_start: datetime,
        as_of: datetime,
    ) -> List[Dict[str, Any]]:
        """Standalone purchase lots (no ``bol_id``) with ``window_start < effective_at <= as_of``.

        ``terminal_id=None`` (unattributed) means any terminal.
        """

        model = MarginCostEntryORM
        stmt = select(model).where(
            model.tenant_id == tenant_id,
            model.status == CostEntryStatus.ACTIVE.value,
            model.kind == CostEntryKind.PURCHASE.value,
            model.product_code == product_code,
            model.bol_id.is_(None),
            model.effective_at > _utc(window_start),
            model.effective_at <= _utc(as_of),
        )
        if terminal_id is not None:
            stmt = stmt.where(model.terminal_id == terminal_id)
        stmt = stmt.order_by(model.effective_at.asc(), model.entry_id.asc())
        async with self._scope() as session:
            return [_row_dict(r) for r in (await session.execute(stmt)).scalars()]

    async def active_entries_for_bols(
        self, tenant_id: str, bol_ids: Sequence[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Active purchase entries referencing these BOLs, keyed by ``bol_id``."""

        ids = sorted({b for b in bol_ids if b})
        found: Dict[str, Dict[str, Any]] = {}
        if not ids:
            return found
        model = MarginCostEntryORM
        async with self._scope() as session:
            for chunk in _chunks(ids):
                stmt = select(model).where(
                    model.tenant_id == tenant_id,
                    model.status == CostEntryStatus.ACTIVE.value,
                    model.kind == CostEntryKind.PURCHASE.value,
                    model.bol_id.in_(chunk),
                )
                for row in (await session.execute(stmt)).scalars():
                    found[row.bol_id] = _row_dict(row)
        return found

    async def active_natural_keys(
        self, tenant_id: str, natural_keys: Sequence[str]
    ) -> Dict[str, str]:
        """``{natural_key: entry_id}`` for active entries among ``natural_keys``."""

        keys = sorted(set(natural_keys))
        found: Dict[str, str] = {}
        if not keys:
            return found
        model = MarginCostEntryORM
        async with self._scope() as session:
            for chunk in _chunks(keys):
                stmt = select(model.natural_key, model.entry_id).where(
                    model.tenant_id == tenant_id,
                    model.status == CostEntryStatus.ACTIVE.value,
                    model.natural_key.in_(chunk),
                )
                for natural_key, entry_id in await session.execute(stmt):
                    found[natural_key] = entry_id
        return found

    # ------------------------------------------------------------------
    # Settings and the activation watermark
    # ------------------------------------------------------------------

    async def get_settings(self, tenant_id: str) -> Dict[str, Any]:
        """The tenant's settings, or the defaults (``persisted=False``) with no row."""

        async with self._scope() as session:
            row = await session.get(MarginSettingsORM, tenant_id)
            return _settings_dict(row, tenant_id)

    async def put_settings(
        self,
        tenant_id: str,
        values: Mapping[str, Any],
        *,
        actor: str,
        now: Optional[datetime] = None,
    ) -> SettingsChange:
        """Upsert the editable settings. Never touches ``feed_activated_at``."""

        unknown = set(values) - set(_SETTINGS_FIELDS)
        if unknown:
            raise ValueError(f"unknown settings fields: {sorted(unknown)}")
        moment = _utc(now) or utcnow()
        for attempt in (1, 2):
            try:
                async with self._scope() as session:
                    row = (
                        await session.execute(
                            select(MarginSettingsORM)
                            .where(MarginSettingsORM.tenant_id == tenant_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    before = _settings_dict(row, tenant_id)
                    if row is None:
                        row = _new_settings_row(tenant_id)
                        session.add(row)
                    for key in _SETTINGS_FIELDS:
                        if key in values:
                            setattr(
                                row,
                                key,
                                dict(values[key]) if key == "product_floors" else values[key],
                            )
                    row.updated_by = actor
                    row.updated_at = moment
                    await session.flush()
                    return SettingsChange(before=before, after=_settings_dict(row, tenant_id))
            except IntegrityError as exc:
                if attempt == 2 or not _is_unique_violation(exc):
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def ensure_activated(
        self, tenant_id: str, *, now: Optional[datetime] = None
    ) -> datetime:
        """Return ``feed_activated_at``, setting it to now the first time.

        Select-for-update; insert a default row if absent, or fill a null
        watermark. A racing insert (PK ``IntegrityError``) is retried once as
        the select-and-update path, so concurrent callers get one value. The
        value never moves once set.
        """

        moment = _utc(now) or utcnow()
        for attempt in (1, 2):
            try:
                async with self._scope() as session:
                    row = (
                        await session.execute(
                            select(MarginSettingsORM)
                            .where(MarginSettingsORM.tenant_id == tenant_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if row is None:
                        row = _new_settings_row(tenant_id)
                        row.feed_activated_at = moment
                        session.add(row)
                        await session.flush()
                    elif row.feed_activated_at is None:
                        row.feed_activated_at = moment
                        await session.flush()
                    return _aware(row.feed_activated_at)
            except IntegrityError as exc:
                if attempt == 2 or not _is_unique_violation(exc):
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------
    # Records: the versioned write protocol
    # ------------------------------------------------------------------

    async def write_record(
        self,
        tenant_id: str,
        candidate: MarginCandidate,
        mode: str,
        *,
        now: Optional[datetime] = None,
    ) -> WriteResult:
        """Apply the rule table for ``candidate`` under ``mode`` (module docstring).

        A racing first insert (``uq_mr_version`` / ``uq_mr_active``) retries
        the whole write once; a second failure logs ERROR and returns
        ``outcome="conflict"`` for the gap sweep to repair. Other database
        errors propagate.
        """

        write_mode = WriteMode(mode)
        for attempt in (1, 2):
            try:
                async with self._scope() as session:
                    return await self._write_once(
                        session, tenant_id, candidate, write_mode, now
                    )
            except IntegrityError as exc:
                if not _is_unique_violation(exc):
                    raise  # a CHECK / NOT NULL failure is a bug, not a race
                if attempt == 2:
                    logger.error(
                        "margin write conflict after retry tenant=%s stage=%s source_key=%s",
                        tenant_id,
                        candidate.stage,
                        candidate.source_key,
                    )
                    return WriteResult(written=False, outcome="conflict")
                logger.info(
                    "margin write raced; retrying tenant=%s stage=%s source_key=%s",
                    tenant_id,
                    candidate.stage,
                    candidate.source_key,
                )
        raise AssertionError("unreachable")  # pragma: no cover

    async def void_latest(
        self,
        tenant_id: str,
        stage: str,
        source_key: str,
    ) -> WriteResult:
        """Void the key's active row without a candidate (no resolver read).

        ``outcome="missing"`` when the key has no row, so the caller can build
        a tombstone candidate and call ``write_record(..., mode="void")``.
        A void keeps the row's ``computed_at``; no transition time is stored.
        """

        async with self._scope() as session:
            latest = await self._lock_latest(session, tenant_id, stage, source_key)
            if latest is None:
                return WriteResult(written=False, outcome="missing")
            if latest.status != RecordStatus.ACTIVE.value:
                return WriteResult(written=False, outcome="skipped", record=_row_dict(latest))
            self._retire(latest, RecordStatus.VOID)
            await session.flush()
            logger.debug("margin record voided tenant=%s record_id=%s", tenant_id, latest.record_id)
            return WriteResult(written=True, outcome="voided", record=_row_dict(latest))

    async def _write_once(
        self,
        session: AsyncSession,
        tenant_id: str,
        candidate: MarginCandidate,
        mode: WriteMode,
        now: Optional[datetime],
    ) -> WriteResult:
        moment = _utc(now) or utcnow()
        latest = await self._lock_latest(session, tenant_id, candidate.stage, candidate.source_key)

        if latest is None or latest.status == RecordStatus.SUPERSEDED.value:
            version = 1 if latest is None else latest.version + 1
            status = RecordStatus.VOID if mode is WriteMode.VOID else RecordStatus.ACTIVE
            frozen = mode is WriteMode.FINALIZE or (
                mode is WriteMode.RECOMPUTE and candidate.source_final
            )
            row = await self._insert(
                session, tenant_id, candidate, mode, version, status, frozen, moment
            )
            return WriteResult(written=True, outcome="inserted", record=_row_dict(row))

        if latest.status == RecordStatus.VOID.value:
            return WriteResult(written=False, outcome="skipped", record=_row_dict(latest))

        # Latest row is active.
        if mode is WriteMode.VOID:
            self._retire(latest, RecordStatus.VOID)
            await session.flush()
            return WriteResult(written=True, outcome="voided", record=_row_dict(latest))

        is_frozen = latest.frozen_at is not None
        same_hash = latest.input_hash == candidate.input_hash

        if is_frozen:
            if mode is WriteMode.RECOMPUTE and not same_hash:
                row = await self._supersede(
                    session, tenant_id, latest, candidate, mode, True, moment
                )
                return WriteResult(written=True, outcome="superseded", record=_row_dict(row))
            return WriteResult(written=False, outcome="skipped", record=_row_dict(latest))

        if same_hash:
            if mode is WriteMode.FINALIZE:
                latest.frozen_at = moment
                await session.flush()
                return WriteResult(written=True, outcome="frozen", record=_row_dict(latest))
            return WriteResult(written=False, outcome="skipped", record=_row_dict(latest))

        frozen = mode is WriteMode.FINALIZE or (
            mode is WriteMode.RECOMPUTE and candidate.source_final
        )
        row = await self._supersede(session, tenant_id, latest, candidate, mode, frozen, moment)
        return WriteResult(written=True, outcome="superseded", record=_row_dict(row))

    async def _lock_latest(
        self, session: AsyncSession, tenant_id: str, stage: str, source_key: str
    ) -> Optional[MarginRecordORM]:
        stmt = (
            select(MarginRecordORM)
            .where(
                MarginRecordORM.tenant_id == tenant_id,
                MarginRecordORM.stage == stage,
                MarginRecordORM.source_key == source_key,
            )
            .order_by(MarginRecordORM.version.desc())
            .limit(1)
            .with_for_update()
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _retire(row: MarginRecordORM, status: RecordStatus) -> None:
        """``superseded`` / ``void``: only active records alert, so pending -> done."""

        row.status = status.value
        if row.alert_state == AlertState.PENDING.value:
            row.alert_state = AlertState.DONE.value

    async def _supersede(
        self,
        session: AsyncSession,
        tenant_id: str,
        latest: MarginRecordORM,
        candidate: MarginCandidate,
        mode: WriteMode,
        frozen: bool,
        moment: datetime,
    ) -> MarginRecordORM:
        self._retire(latest, RecordStatus.SUPERSEDED)
        # Flush first so the new active row does not collide on uq_mr_active.
        await session.flush()
        return await self._insert(
            session,
            tenant_id,
            candidate,
            mode,
            latest.version + 1,
            RecordStatus.ACTIVE,
            frozen,
            moment,
        )

    async def _insert(
        self,
        session: AsyncSession,
        tenant_id: str,
        candidate: MarginCandidate,
        mode: WriteMode,
        version: int,
        status: RecordStatus,
        frozen: bool,
        moment: datetime,
    ) -> MarginRecordORM:
        origin = RecordOrigin.RECOMPUTE if mode is WriteMode.RECOMPUTE else RecordOrigin.LIVE
        alerting = (
            origin is RecordOrigin.LIVE
            and candidate.stage in _ALERTING_STAGES
            and status is RecordStatus.ACTIVE
            and (
                candidate.flag_negative_margin
                or candidate.flag_missing_cost
                or candidate.flag_below_floor
            )
        )
        row = MarginRecordORM(
            record_id=_new_id("mr"),
            tenant_id=tenant_id,
            stage=candidate.stage,
            source_key=candidate.source_key,
            order_id=candidate.order_id,
            invoice_id=candidate.invoice_id,
            line_index=candidate.line_index,
            line_id=candidate.line_id,
            customer_id=candidate.customer_id,
            account_id=candidate.account_id,
            product_code=candidate.product_code,
            terminal_id=candidate.terminal_id,
            gallons_ugal=candidate.gallons_ugal,
            unit_price_micros=candidate.unit_price_micros,
            revenue_cents=candidate.revenue_cents,
            method=candidate.method,
            product_cost_micros=candidate.product_cost_micros,
            adders_micros=candidate.adders_micros,
            landed_cost_micros=candidate.landed_cost_micros,
            cost_cents=candidate.cost_cents,
            margin_cents=candidate.margin_cents,
            margin_per_gallon_micros=candidate.margin_per_gallon_micros,
            margin_bp=candidate.margin_bp,
            no_cost_reason=candidate.no_cost_reason,
            flag_missing_cost=candidate.flag_missing_cost,
            flag_negative_margin=candidate.flag_negative_margin,
            flag_below_floor=candidate.flag_below_floor,
            flag_terminal_unattributed=candidate.flag_terminal_unattributed,
            floor_micros_used=candidate.floor_micros_used,
            cost_snapshot=dict(candidate.cost_snapshot),
            as_of=_utc(candidate.as_of),
            version=version,
            status=status.value,
            origin=origin.value,
            frozen_at=moment if frozen else None,
            input_hash=candidate.input_hash,
            recompute_run_id=candidate.recompute_run_id,
            computed_at=moment,
            alert_state=(AlertState.PENDING if alerting else AlertState.NONE).value,
        )
        session.add(row)
        # A record now exists for the key, so a stored phase-1 skip is stale.
        await session.execute(
            delete(MarginSkippedSourceORM).where(
                MarginSkippedSourceORM.tenant_id == tenant_id,
                MarginSkippedSourceORM.stage == candidate.stage,
                MarginSkippedSourceORM.source_key == candidate.source_key,
            )
        )
        await session.flush()
        return row

    # ------------------------------------------------------------------
    # Records: reads
    # ------------------------------------------------------------------

    async def get_record(self, tenant_id: str, record_id: str) -> Optional[Dict[str, Any]]:
        async with self._scope() as session:
            row = (
                await session.execute(
                    select(MarginRecordORM).where(
                        MarginRecordORM.tenant_id == tenant_id,
                        MarginRecordORM.record_id == record_id,
                    )
                )
            ).scalar_one_or_none()
            return _row_dict(row) if row is not None else None

    async def record_versions(
        self, tenant_id: str, stage: str, source_key: str
    ) -> List[Dict[str, Any]]:
        """Every version of one key, oldest first (record detail history)."""

        stmt = (
            select(MarginRecordORM)
            .where(
                MarginRecordORM.tenant_id == tenant_id,
                MarginRecordORM.stage == stage,
                MarginRecordORM.source_key == source_key,
            )
            .order_by(MarginRecordORM.version.asc())
        )
        async with self._scope() as session:
            return [_row_dict(r) for r in (await session.execute(stmt)).scalars()]

    async def latest_for_keys(
        self, tenant_id: str, stage: str, source_keys: Sequence[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Latest ``active`` or ``void`` row per key (gap sweep, recompute).

        Keys with neither are absent from the result.
        """

        keys = sorted(set(source_keys))
        found: Dict[str, Dict[str, Any]] = {}
        if not keys:
            return found
        model = MarginRecordORM
        async with self._scope() as session:
            for chunk in _chunks(keys):
                stmt = (
                    select(model)
                    .where(
                        model.tenant_id == tenant_id,
                        model.stage == stage,
                        model.status.in_(
                            (RecordStatus.ACTIVE.value, RecordStatus.VOID.value)
                        ),
                        model.source_key.in_(chunk),
                    )
                    .order_by(model.source_key, model.version.asc())
                )
                for row in (await session.execute(stmt)).scalars():
                    found[row.source_key] = _row_dict(row)  # highest version wins
        return found

    def _record_filter_clauses(
        self, tenant_id: str, filters: Optional[RecordFilters]
    ) -> List[Any]:
        model = MarginRecordORM
        f = filters or RecordFilters()
        clauses: List[Any] = [model.tenant_id == tenant_id]
        if f.status not in (None, "all"):
            clauses.append(model.status == f.status)
        if f.as_of_from is not None:
            clauses.append(model.as_of >= _utc(f.as_of_from))
        if f.as_of_to is not None:
            clauses.append(model.as_of < _utc(f.as_of_to))
        if f.customer_id is not None:
            clauses.append(model.customer_id == f.customer_id)
        if f.product_code is not None:
            clauses.append(model.product_code == f.product_code)
        if f.terminal_id is not None:
            clauses.append(model.terminal_id == f.terminal_id)
        if f.stage is not None:
            clauses.append(model.stage == f.stage)
        if f.flag is not None:
            column = _FLAG_COLUMNS.get(f.flag)
            if column is None:
                raise ValueError(f"unknown margin flag {f.flag!r}")
            clauses.append(column.is_(True))
        return clauses

    async def list_records(
        self,
        tenant_id: str,
        filters: Optional[RecordFilters] = None,
        *,
        after: Optional[Tuple[datetime, str]] = None,
        limit: int = 50,
    ) -> Page:
        """Keyset page on (``as_of`` desc, ``record_id`` desc), newest first."""

        model = MarginRecordORM
        stmt = select(model).where(*self._record_filter_clauses(tenant_id, filters))
        if after is not None:
            as_of, last_id = _utc(after[0]), after[1]
            stmt = stmt.where(
                or_(model.as_of < as_of, and_(model.as_of == as_of, model.record_id < last_id))
            )
        stmt = stmt.order_by(model.as_of.desc(), model.record_id.desc()).limit(limit + 1)
        async with self._scope() as session:
            rows = list((await session.execute(stmt)).scalars())
        items = [_row_dict(r) for r in rows[:limit]]
        next_key = (items[-1]["as_of"], items[-1]["record_id"]) if len(rows) > limit else None
        return Page(items=items, next_key=next_key)

    async def count_records(
        self, tenant_id: str, filters: Optional[RecordFilters] = None
    ) -> int:
        stmt = select(func.count()).select_from(MarginRecordORM).where(
            *self._record_filter_clauses(tenant_id, filters)
        )
        async with self._scope() as session:
            return int((await session.execute(stmt)).scalar_one())

    async def iter_summary_rows(
        self,
        tenant_id: str,
        *,
        as_of_from: datetime,
        as_of_to: datetime,
        page_size: int = 5_000,
    ) -> AsyncIterator[List[Dict[str, Any]]]:
        """Stream active records in ``[as_of_from, as_of_to)`` as integer-column dicts.

        Pages of ``page_size`` on (``as_of``, ``record_id``); no snapshot JSON.
        """

        model = MarginRecordORM
        columns = (
            model.record_id,
            model.stage,
            model.order_id,
            model.invoice_id,
            model.customer_id,
            model.product_code,
            model.terminal_id,
            model.method,
            model.as_of,
            model.gallons_ugal,
            model.revenue_cents,
            model.cost_cents,
            model.margin_cents,
            model.flag_missing_cost,
            model.flag_negative_margin,
            model.flag_below_floor,
            model.flag_terminal_unattributed,
        )
        start, end = _utc(as_of_from), _utc(as_of_to)
        after: Optional[Tuple[datetime, str]] = None
        while True:
            stmt = select(*columns).where(
                model.tenant_id == tenant_id,
                model.status == RecordStatus.ACTIVE.value,
                model.as_of >= start,
                model.as_of < end,
            )
            if after is not None:
                stmt = stmt.where(
                    or_(
                        model.as_of > after[0],
                        and_(model.as_of == after[0], model.record_id > after[1]),
                    )
                )
            stmt = stmt.order_by(model.as_of.asc(), model.record_id.asc()).limit(page_size)
            async with self._scope() as session:
                rows = [
                    {key: _aware(value) for key, value in r._mapping.items()}
                    for r in await session.execute(stmt)
                ]
            if not rows:
                return
            yield rows
            if len(rows) < page_size:
                return
            after = (_utc(rows[-1]["as_of"]), rows[-1]["record_id"])

    # ------------------------------------------------------------------
    # Skipped sources (phase-1 invalid inputs)
    # ------------------------------------------------------------------

    async def record_skip(
        self,
        tenant_id: str,
        *,
        stage: str,
        source_key: str,
        error_type: str,
        reason: str = "invalid_inputs",
        order_id: Optional[str] = None,
        invoice_id: Optional[str] = None,
        line_index: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> int:
        """Upsert the key's skip row and return its ``seen_count``.

        Select-for-update, then insert or bump; a racing PK insert is
        retried once as the update path.
        """

        moment = _utc(now) or utcnow()
        for attempt in (1, 2):
            try:
                async with self._scope() as session:
                    row = (
                        await session.execute(
                            select(MarginSkippedSourceORM)
                            .where(
                                MarginSkippedSourceORM.tenant_id == tenant_id,
                                MarginSkippedSourceORM.stage == stage,
                                MarginSkippedSourceORM.source_key == source_key,
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if row is None:
                        row = MarginSkippedSourceORM(
                            tenant_id=tenant_id,
                            stage=stage,
                            source_key=source_key,
                            order_id=order_id,
                            invoice_id=invoice_id,
                            line_index=line_index,
                            reason=reason,
                            error_type=error_type,
                            first_seen_at=moment,
                            last_seen_at=moment,
                            seen_count=1,
                        )
                        session.add(row)
                    else:
                        row.reason = reason
                        row.error_type = error_type
                        row.last_seen_at = moment
                        row.seen_count = row.seen_count + 1
                    await session.flush()
                    return row.seen_count
            except IntegrityError as exc:
                if attempt == 2 or not _is_unique_violation(exc):
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def skipped_sources(self, tenant_id: str, *, sample_size: int = 20) -> Dict[str, Any]:
        """``{count, sample: [source_key, ...]}`` for ``GET /summary``."""

        model = MarginSkippedSourceORM
        async with self._scope() as session:
            count = int(
                (
                    await session.execute(
                        select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
                    )
                ).scalar_one()
            )
            sample = list(
                (
                    await session.execute(
                        select(model.source_key)
                        .where(model.tenant_id == tenant_id)
                        .order_by(model.last_seen_at.desc(), model.source_key.asc())
                        .limit(sample_size)
                    )
                ).scalars()
            )
        return {"count": count, "sample": sample}

    # ------------------------------------------------------------------
    # RevenueGuard queue and alerts
    # ------------------------------------------------------------------

    async def expire_stale_pending(
        self,
        tenant_id: str,
        *,
        older_than: timedelta = timedelta(days=7),
        now: Optional[datetime] = None,
    ) -> int:
        """Set ``pending`` rows computed before ``now - older_than`` to ``expired``."""

        cutoff = (_utc(now) or utcnow()) - older_than
        model = MarginRecordORM
        async with self._scope() as session:
            result = await session.execute(
                update(model)
                .where(
                    model.tenant_id == tenant_id,
                    model.alert_state == AlertState.PENDING.value,
                    model.computed_at < cutoff,
                )
                .values(alert_state=AlertState.EXPIRED.value)
                .execution_options(synchronize_session=False)
            )
            count = int(result.rowcount or 0)
        if count:
            logger.info("margin alert queue expired tenant=%s count=%d", tenant_id, count)
        return count

    async def pending_records(
        self, tenant_id: str, limit: int = PENDING_RECORDS_LIMIT
    ) -> List[Dict[str, Any]]:
        """Up to ``limit`` pending records, ordered by ``computed_at``, ``record_id``.

        Served by the partial index ``ix_mr_alert_pending``.
        """

        stmt = self._pending_records_stmt(tenant_id, limit)
        async with self._scope() as session:
            return [_row_dict(r) for r in (await session.execute(stmt)).scalars()]

    @staticmethod
    def _pending_records_stmt(tenant_id: str, limit: int) -> Any:
        model = MarginRecordORM
        return (
            select(model)
            .where(model.tenant_id == tenant_id, model.alert_state == AlertState.PENDING.value)
            .order_by(model.computed_at.asc(), model.record_id.asc())
            .limit(limit)
        )

    async def pending_digests(self, tenant_id: str) -> List[Dict[str, Any]]:
        model = MarginRecomputeRunORM
        stmt = (
            select(model)
            .where(model.tenant_id == tenant_id, model.digest_state == DigestState.PENDING.value)
            .order_by(model.started_at.asc(), model.run_id.asc())
        )
        async with self._scope() as session:
            return [_row_dict(r) for r in (await session.execute(stmt)).scalars()]

    async def _insert_alerts(
        self,
        session: AsyncSession,
        tenant_id: str,
        alerts: Sequence[Mapping[str, Any]],
        moment: datetime,
    ) -> List[str]:
        """Insert each alert in its own savepoint; a dedupe conflict is a no-op."""

        inserted: List[str] = []
        for alert in alerts:
            unknown = set(alert) - _ALERT_FIELDS - {"tenant_id", "alert_id"}
            if unknown:
                raise ValueError(f"unknown alert fields: {sorted(unknown)}")
            _check_tenant(alert, tenant_id)
            row = MarginAlertORM(
                alert_id=alert.get("alert_id") or _new_id("malert"),
                tenant_id=tenant_id,
                alert_type=alert["alert_type"],
                severity=alert["severity"],
                status=alert.get("status") or AlertStatus.OPEN.value,
                dedupe_key=alert["dedupe_key"],
                record_id=alert.get("record_id"),
                order_id=alert.get("order_id"),
                run_id=alert.get("run_id"),
                proposal_id=alert.get("proposal_id"),
                customer_id=alert.get("customer_id"),
                product_code=alert.get("product_code"),
                details=dict(alert.get("details") or {}),
                created_at=moment,
            )
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
                inserted.append(row.alert_id)
            except IntegrityError as exc:
                if not _is_unique_violation(exc):
                    raise
                logger.debug(
                    "margin alert deduplicated tenant=%s type=%s dedupe_key=%s",
                    tenant_id,
                    alert["alert_type"],
                    alert["dedupe_key"],
                )
        return inserted

    async def record_alert_outcome(
        self,
        tenant_id: str,
        record_id: str,
        alerts: Sequence[Mapping[str, Any]] = (),
        *,
        now: Optional[datetime] = None,
    ) -> List[str]:
        """Insert ``alerts`` and mark the record ``done`` in one transaction.

        Returns the ids of alerts actually inserted; duplicates (same
        ``(tenant, type, dedupe_key)``) are skipped, so two concurrent calls
        for one record leave one alert row.
        """

        moment = _utc(now) or utcnow()
        async with self._scope() as session:
            record = (
                await session.execute(
                    select(MarginRecordORM)
                    .where(
                        MarginRecordORM.tenant_id == tenant_id,
                        MarginRecordORM.record_id == record_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if record is None:
                return []
            inserted = await self._insert_alerts(session, tenant_id, alerts, moment)
            if record.alert_state == AlertState.PENDING.value:
                record.alert_state = AlertState.DONE.value
            await session.flush()
            return inserted

    async def mark_record_done(self, tenant_id: str, record_id: str) -> bool:
        model = MarginRecordORM
        async with self._scope() as session:
            result = await session.execute(
                update(model)
                .where(
                    model.tenant_id == tenant_id,
                    model.record_id == record_id,
                    model.alert_state == AlertState.PENDING.value,
                )
                .values(alert_state=AlertState.DONE.value)
                .execution_options(synchronize_session=False)
            )
            return bool(result.rowcount)

    async def mark_digest_done(
        self,
        tenant_id: str,
        run_id: str,
        *,
        alert: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> List[str]:
        """Optionally insert the digest alert, then set ``digest_state='done'``."""

        moment = _utc(now) or utcnow()
        async with self._scope() as session:
            run = (
                await session.execute(
                    select(MarginRecomputeRunORM)
                    .where(
                        MarginRecomputeRunORM.tenant_id == tenant_id,
                        MarginRecomputeRunORM.run_id == run_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                return []
            inserted = (
                await self._insert_alerts(session, tenant_id, [alert], moment) if alert else []
            )
            if run.digest_state == DigestState.PENDING.value:
                run.digest_state = DigestState.DONE.value
            await session.flush()
            return inserted

    async def leakage_window(
        self,
        tenant_id: str,
        *,
        customer_id: str,
        product_code: str,
        stage: str,
        threshold: int,
    ) -> List[Dict[str, Any]]:
        """The latest ``threshold`` sales for (customer, product, stage), any origin.

        A sale is one ``invoice_id`` at the invoice stage and one ``order_id``
        otherwise, so an OI-14 split counts once. Reads active records by
        ``as_of`` desc, ``record_id`` desc, at most ``threshold * 4`` rows.
        Each item is ``{sale_key, record_ids, below_floor}``; a sale is below
        floor when any of its lines is.
        """

        model = MarginRecordORM
        stmt = (
            select(model)
            .where(
                model.tenant_id == tenant_id,
                model.customer_id == customer_id,
                model.product_code == product_code,
                model.stage == stage,
                model.status == RecordStatus.ACTIVE.value,
            )
            .order_by(model.as_of.desc(), model.record_id.desc())
            .limit(max(threshold, 1) * 4)
        )
        async with self._scope() as session:
            rows = list((await session.execute(stmt)).scalars())
        sales: Dict[str, _Sale] = {}
        for row in rows:
            key = (
                row.invoice_id if stage == MarginStage.INVOICE.value else row.order_id
            ) or row.source_key
            sale = sales.get(key)
            if sale is None:
                if len(sales) >= threshold:
                    continue
                sale = sales[key] = _Sale(sale_key=key)
            sale.record_ids.append(row.record_id)
            sale.below_floor = sale.below_floor or bool(row.flag_below_floor)
        return [
            {"sale_key": s.sale_key, "record_ids": list(s.record_ids), "below_floor": s.below_floor}
            for s in sales.values()
        ]

    async def has_pending_leakage(
        self, tenant_id: str, *, customer_id: str, product_code: str
    ) -> bool:
        model = MarginAlertORM
        stmt = (
            select(model.alert_id)
            .where(
                model.tenant_id == tenant_id,
                model.alert_type == AlertType.LEAKAGE_PROPOSAL.value,
                model.status == AlertStatus.PENDING_REVIEW.value,
                model.customer_id == customer_id,
                model.product_code == product_code,
            )
            .limit(1)
        )
        async with self._scope() as session:
            return (await session.execute(stmt)).first() is not None

    async def get_alert(self, tenant_id: str, alert_id: str) -> Optional[Dict[str, Any]]:
        async with self._scope() as session:
            row = (
                await session.execute(
                    select(MarginAlertORM).where(
                        MarginAlertORM.tenant_id == tenant_id,
                        MarginAlertORM.alert_id == alert_id,
                    )
                )
            ).scalar_one_or_none()
            return _row_dict(row) if row is not None else None

    async def list_alerts(
        self,
        tenant_id: str,
        *,
        statuses: Optional[Sequence[str]] = None,
        alert_type: Optional[str] = None,
        after: Optional[Tuple[datetime, str]] = None,
        limit: int = 50,
    ) -> Page:
        """Keyset page on (``created_at`` desc, ``alert_id`` desc) plus ``total``."""

        model = MarginAlertORM
        clauses: List[Any] = [model.tenant_id == tenant_id]
        if statuses:
            clauses.append(model.status.in_(list(statuses)))
        if alert_type is not None:
            clauses.append(model.alert_type == alert_type)
        stmt = select(model).where(*clauses)
        if after is not None:
            created_at, last_id = _utc(after[0]), after[1]
            stmt = stmt.where(
                or_(
                    model.created_at < created_at,
                    and_(model.created_at == created_at, model.alert_id < last_id),
                )
            )
        stmt = stmt.order_by(model.created_at.desc(), model.alert_id.desc()).limit(limit + 1)
        count_stmt = select(func.count()).select_from(model).where(*clauses)
        async with self._scope() as session:
            rows = list((await session.execute(stmt)).scalars())
            total = int((await session.execute(count_stmt)).scalar_one())
        items = [_row_dict(r) for r in rows[:limit]]
        next_key = (
            (items[-1]["created_at"], items[-1]["alert_id"]) if len(rows) > limit else None
        )
        return Page(items=items, next_key=next_key, total=total)

    async def transition_alert(
        self,
        tenant_id: str,
        alert_id: str,
        *,
        action: str,
        actor: str,
        note: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> AlertTransition:
        """Acknowledge (``open`` only) or approve / dismiss (``leakage_proposal``
        in ``pending_review`` only). 404 / 409 as repository errors."""

        targets = {
            "acknowledge": AlertStatus.ACKNOWLEDGED.value,
            "approve": AlertStatus.APPROVED.value,
            "dismiss": AlertStatus.DISMISSED.value,
        }
        if action not in targets:
            raise ValueError("action must be acknowledge, approve or dismiss")
        moment = _utc(now) or utcnow()
        async with self._scope() as session:
            row = (
                await session.execute(
                    select(MarginAlertORM)
                    .where(
                        MarginAlertORM.tenant_id == tenant_id,
                        MarginAlertORM.alert_id == alert_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise MarginAlertNotFoundError(alert_id)
            if action == "acknowledge":
                allowed = row.status == AlertStatus.OPEN.value
            else:
                allowed = (
                    row.alert_type == AlertType.LEAKAGE_PROPOSAL.value
                    and row.status == AlertStatus.PENDING_REVIEW.value
                )
            if not allowed:
                raise MarginAlertStateError(alert_id)
            from_status = row.status
            row.status = targets[action]
            row.resolved_by = actor
            row.resolved_at = moment
            row.resolution_note = note
            await session.flush()
            return AlertTransition(from_status=from_status, alert=_row_dict(row))

    # ------------------------------------------------------------------
    # Recompute runs
    # ------------------------------------------------------------------

    async def start_run(
        self,
        tenant_id: str,
        *,
        requested_by: str,
        start_date: date,
        end_date: date,
        stages: Sequence[str],
        only_missing: bool,
        reason: str,
        run_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Insert a ``running`` run, or raise :class:`MarginRecomputeRunningError`.

        A running row whose heartbeat is older than :data:`RUN_STALE_AFTER`
        is marked ``failed`` first (WARNING) and the new run proceeds.
        ``uq_mrun_running`` decides a concurrent start.
        """

        moment = _utc(now) or utcnow()
        model = MarginRecomputeRunORM
        try:
            async with self._scope() as session:
                running = (
                    await session.execute(
                        select(model)
                        .where(model.tenant_id == tenant_id, model.status == RunStatus.RUNNING.value)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if running is not None:
                    if _aware(running.heartbeat_at) >= moment - RUN_STALE_AFTER:
                        raise MarginRecomputeRunningError(running.run_id)
                    logger.warning(
                        "margin recompute run stale; marking failed tenant=%s run_id=%s",
                        tenant_id,
                        running.run_id,
                    )
                    running.status = RunStatus.FAILED.value
                    running.finished_at = moment
                    await session.flush()
                row = model(
                    run_id=run_id or _new_id("mrun"),
                    tenant_id=tenant_id,
                    requested_by=requested_by,
                    start_date=start_date,
                    end_date=end_date,
                    stages=list(stages),
                    only_missing=only_missing,
                    reason=reason,
                    status=RunStatus.RUNNING.value,
                    counts={},
                    started_at=moment,
                    heartbeat_at=moment,
                    digest_state=DigestState.NONE.value,
                )
                session.add(row)
                await session.flush()
                return _row_dict(row)
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            raise MarginRecomputeRunningError(None) from None

    async def heartbeat(
        self,
        tenant_id: str,
        run_id: str,
        *,
        counts: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        values: Dict[str, Any] = {"heartbeat_at": _utc(now) or utcnow()}
        if counts is not None:
            values["counts"] = dict(counts)
        model = MarginRecomputeRunORM
        async with self._scope() as session:
            result = await session.execute(
                update(model)
                .where(
                    model.tenant_id == tenant_id,
                    model.run_id == run_id,
                    model.status == RunStatus.RUNNING.value,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            return bool(result.rowcount)

    async def finish_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        status: str,
        counts: Mapping[str, Any],
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Set ``completed`` / ``failed`` with counts.

        On ``completed`` the same transaction sets ``digest_state='pending'``
        when ``sum(counts["by_flag"].values()) > 0`` and ``'done'`` otherwise.
        """

        final = RunStatus(status)
        if final is RunStatus.RUNNING:
            raise ValueError("finish_run needs completed or failed")
        moment = _utc(now) or utcnow()
        async with self._scope() as session:
            row = (
                await session.execute(
                    select(MarginRecomputeRunORM)
                    .where(
                        MarginRecomputeRunORM.tenant_id == tenant_id,
                        MarginRecomputeRunORM.run_id == run_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = final.value
            row.counts = dict(counts)
            row.finished_at = moment
            row.heartbeat_at = moment
            if final is RunStatus.COMPLETED:
                flagged = sum(int(v) for v in dict(counts.get("by_flag") or {}).values())
                row.digest_state = (
                    DigestState.PENDING.value if flagged > 0 else DigestState.DONE.value
                )
            await session.flush()
            return _row_dict(row)

    async def get_run(self, tenant_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        async with self._scope() as session:
            row = (
                await session.execute(
                    select(MarginRecomputeRunORM).where(
                        MarginRecomputeRunORM.tenant_id == tenant_id,
                        MarginRecomputeRunORM.run_id == run_id,
                    )
                )
            ).scalar_one_or_none()
            return _row_dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Weekly reports
    # ------------------------------------------------------------------

    async def report_exists(self, tenant_id: str, iso_week: str) -> bool:
        async with self._scope() as session:
            return await session.get(MarginWeeklyReportORM, (tenant_id, iso_week)) is not None

    async def insert_report_if_absent(
        self, tenant_id: str, report: Mapping[str, Any]
    ) -> bool:
        """Insert one weekly report; ``False`` when the (tenant, week) row exists."""

        _check_tenant(report, tenant_id)
        values = {k: v for k, v in report.items() if k != "tenant_id"}
        for key in ("period_start", "period_end", "generated_at"):
            if key in values:
                values[key] = _utc(values[key])
        try:
            async with self._scope() as session:
                if await session.get(MarginWeeklyReportORM, (tenant_id, values["iso_week"])):
                    return False
                session.add(MarginWeeklyReportORM(tenant_id=tenant_id, **values))
                await session.flush()
                return True
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            return False

    async def list_reports(self, tenant_id: str, *, limit: int = 12) -> List[Dict[str, Any]]:
        model = MarginWeeklyReportORM
        stmt = (
            select(model)
            .where(model.tenant_id == tenant_id)
            .order_by(model.iso_week.desc())
            .limit(limit)
        )
        async with self._scope() as session:
            return [_row_dict(r) for r in (await session.execute(stmt)).scalars()]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _entry_row(self, tenant_id: str, values: Mapping[str, Any]) -> MarginCostEntryORM:
        _check_tenant(values, tenant_id)
        data = {k: v for k, v in values.items() if k != "tenant_id"}
        unknown = set(data) - _ENTRY_FIELDS
        if unknown:
            raise ValueError(f"unknown cost entry fields: {sorted(unknown)}")
        for key in ("effective_at", "effective_to", "status_changed_at", "created_at"):
            if data.get(key) is not None:
                data[key] = _utc(data[key])
        data.setdefault("entry_id", _new_id("mce"))
        data.setdefault("status", CostEntryStatus.ACTIVE.value)
        data.setdefault("created_at", utcnow())
        return MarginCostEntryORM(tenant_id=tenant_id, **data)

    async def _conflicting_entry_id(
        self, tenant_id: str, natural_key: Optional[str], bol_id: Optional[str]
    ) -> Optional[str]:
        model = MarginCostEntryORM
        matches = []
        if natural_key:
            matches.append(model.natural_key == natural_key)
        if bol_id:
            matches.append(model.bol_id == bol_id)
        if not matches:
            return None
        stmt = (
            select(model.entry_id)
            .where(
                model.tenant_id == tenant_id,
                model.status == CostEntryStatus.ACTIVE.value,
                or_(*matches),
            )
            .limit(1)
        )
        async with self._scope() as session:
            return (await session.execute(stmt)).scalar_one_or_none()


class MarginTenantDiscovery:
    """Tenant discovery for leader-only background work. Returns ids only."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._scope = session_factory

    async def pending_work_tenants(self) -> List[str]:
        """Tenants with a pending record or a pending recompute digest."""

        stmt = union(
            select(distinct(MarginRecordORM.tenant_id)).where(
                MarginRecordORM.alert_state == AlertState.PENDING.value
            ),
            select(distinct(MarginRecomputeRunORM.tenant_id)).where(
                MarginRecomputeRunORM.digest_state == DigestState.PENDING.value
            ),
        )
        async with self._scope() as session:
            return sorted({row[0] for row in await session.execute(stmt)})

    async def tenants_with_records(self) -> List[str]:
        """Tenants that have any margin record (weekly report)."""

        stmt = select(distinct(MarginRecordORM.tenant_id))
        async with self._scope() as session:
            return sorted(row[0] for row in await session.execute(stmt))


__all__ = [
    "AlertTransition",
    "EntryTransition",
    "MarginAlertNotFoundError",
    "MarginAlertStateError",
    "MarginCandidate",
    "MarginDuplicateEntryError",
    "MarginEntryKindMismatchError",
    "MarginEntryNotActiveError",
    "MarginEntryNotFoundError",
    "MarginRecomputeRunningError",
    "MarginRepository",
    "MarginRepositoryError",
    "MarginTenantDiscovery",
    "PENDING_RECORDS_LIMIT",
    "Page",
    "RUN_STALE_AFTER",
    "RecordFilters",
    "SettingsChange",
    "WriteResult",
]
