"""MarginCostEntryService: validation, create/supersede/void/list, CSV import
and settings updates for the margin feed (design "Cost entries (FR1)").

* Pydantic (``commerce/models/margin.py``) does the shape checks; this service
  does the semantic checks and reports **every** failure at once in one
  ``AppException(VALIDATION_ERROR, 422)`` whose ``details.errors`` items are
  ``{"loc", "msg", "type"}`` (plus ``"row"`` for CSV rows), the same envelope
  as request validation.
* Terminals are checked with ``TerminalRepository.get(tenant_id, id)``, which
  is tenant-scoped, so another tenant's terminal is ``unknown_terminal``
  (AC-26). BOLs are read from ``terminal_bols`` with a ``tenant_id`` filter.
* Entries are immutable: edits are supersede (new row, old row
  ``superseded``) or void, both under a row lock in the repository.
* Every mutation (create, supersede, void, import, settings update) calls
  ``get_telemetry_service().log_audit_event(...)`` once. A failing audit sink
  logs WARNING and never fails the request; an INFO line with the ids follows
  (FR1.7, AC-33).
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from commerce.models.margin import (
    DEFAULT_TIMEZONE,
    DEFAULT_WAC_WINDOW_DAYS,
    FLOOR_MICROS_MAX,
    MAX_UNIT_COST_MICROS,
    CostEntryCreate,
    CostEntryKind,
    CostEntrySource,
    CostEntryStatus,
    CostEntrySupersede,
    CostEntryVoid,
    MarginSettingsUpdate,
    MarginValueError,
    parse_gallons_milli,
    parse_usd_micros,
)
from commerce.services.margin_repository import (
    MarginDuplicateEntryError,
    MarginEntryKindMismatchError,
    MarginEntryNotActiveError,
    MarginEntryNotFoundError,
    MarginRepository,
    Page,
)
from compliance.services.compliance_es_mappings import TERMINAL_BOLS_INDEX
from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services.fuel_product_catalog import UnknownFuelProductError, canonicalize
from ops.middleware.tenant_guard import inject_tenant_filter
from services.time_utils import utcnow
from telemetry.service import get_telemetry_service

logger = logging.getLogger(__name__)

#: Upload read size; the 5 MB limit is enforced while reading, before parsing.
IMPORT_CHUNK_BYTES = 64 * 1024
IMPORT_MAX_BYTES = 5 * 1024 * 1024
IMPORT_MAX_ROWS = 10_000
#: BOL ids per tenant-filtered ``terms`` query, and natural keys per ``IN``.
IMPORT_BATCH = 1_000
LIST_MAX_LIMIT = 200

WARNING_GALLONS_IGNORED = "gallons_ignored_bol_net_used"
WARNING_WAC_WINDOW = "wac_window_may_exceed_bol_scan_cap"

IMPORT_COLUMNS: Tuple[str, ...] = (
    "kind",
    "product_code",
    "terminal_id",
    "supplier_name",
    "effective_at",
    "effective_to",
    "unit_cost_usd",
    "gallons",
    "adder_type",
    "bol_id",
    "reference",
    "notes",
)
IMPORT_REQUIRED_COLUMNS: Tuple[str, ...] = ("kind", "product_code", "effective_at", "unit_cost_usd")

LIST_STATUSES = frozenset(
    {
        CostEntryStatus.ACTIVE.value,
        CostEntryStatus.SUPERSEDED.value,
        CostEntryStatus.VOIDED.value,
        "all",
    }
)

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TEXT_FIELDS = ("product_code", "terminal_id", "supplier_name", "bol_id", "reference", "notes")

_SETTINGS_AUDIT_FIELDS = (
    "wac_window_days",
    "rack_staleness_days",
    "floor_micros",
    "product_floors",
    "timezone",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def natural_key(
    *,
    kind: str,
    product_code: str,
    terminal_id: Optional[str],
    effective_at: datetime,
    bol_id: Optional[str],
    reference: Optional[str],
    adder_type: Optional[str],
) -> str:
    """sha256 hex of ``kind|product|terminal|effective_at|bol or reference|adder_type``.

    ``effective_at`` is converted to UTC and formatted
    ``YYYY-MM-DDTHH:MM:SS.ffffffZ``, so one instant sent with different
    offsets gives the same key.
    """

    if effective_at.tzinfo is None:
        raise ValueError("effective_at must be timezone-aware")
    instant = effective_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    text = "|".join(
        [kind, product_code, terminal_id or "", instant, bol_id or reference or "", adder_type or ""]
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _error(loc: Sequence[Any], msg: str, type_: str, row: Optional[int] = None) -> Dict[str, Any]:
    item: Dict[str, Any] = {"loc": list(loc), "msg": msg, "type": type_}
    if row is not None:
        item = {"row": row, **item}
    return item


def _validation_failed(errors: List[Dict[str, Any]], message: str, **extra: Any) -> AppException:
    return AppException(
        ErrorCode.VALIDATION_ERROR,
        message,
        status_code=422,
        details={"errors": errors, **extra},
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _entry_view(entry: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if entry is None:
        return None
    return {k: _json_safe(v) for k, v in entry.items() if k != "tenant_id"}


def _settings_view(settings: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: _json_safe(settings.get(k)) for k in _SETTINGS_AUDIT_FIELDS}


def _zone(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def _parse_instant(
    text: str, *, field_name: str, zone: ZoneInfo
) -> Tuple[Optional[datetime], Optional[Dict[str, Any]]]:
    """``YYYY-MM-DD`` -> midnight in ``zone``; ISO datetime must carry an offset."""

    value = text.strip()
    if _DATE_ONLY.match(value):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return None, _error([field_name], "not a valid date", "invalid_datetime")
        local = datetime(day.year, day.month, day.day, tzinfo=zone)
        return local.astimezone(timezone.utc), None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, _error(
            [field_name], "use YYYY-MM-DD or an ISO datetime with an offset", "invalid_datetime"
        )
    if parsed.tzinfo is None:
        return None, _error(
            [field_name], "a datetime needs a UTC offset (for example Z)", "invalid_datetime"
        )
    return parsed.astimezone(timezone.utc), None


async def _read_upload(upload: Any) -> bytes:
    """Read an upload in 64 KB chunks; more than 5 MB raises 413 before parsing.

    Accepts ``bytes``, a Starlette ``UploadFile`` (async ``read``) or any
    object with a sync ``read(size)``.
    """

    def too_large() -> AppException:
        return AppException(
            ErrorCode.MARGIN_IMPORT_TOO_LARGE,
            "The import file is larger than 5 MB; split it and import each part",
            status_code=413,
            details={"max_bytes": IMPORT_MAX_BYTES},
        )

    if isinstance(upload, (bytes, bytearray, memoryview)):
        data = bytes(upload)
        if len(data) > IMPORT_MAX_BYTES:
            raise too_large()
        return data
    buffer = bytearray()
    while True:
        chunk = upload.read(IMPORT_CHUNK_BYTES)
        if inspect.isawaitable(chunk):
            chunk = await chunk
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > IMPORT_MAX_BYTES:
            raise too_large()
    return bytes(buffer)


# ---------------------------------------------------------------------------
# Draft (one validated entry, before insert)
# ---------------------------------------------------------------------------


@dataclass
class _Draft:
    row: Optional[int]
    kind: str
    product_code: Optional[str] = None
    terminal_id: Optional[str] = None
    supplier_name: Optional[str] = None
    effective_at: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    unit_cost_micros: Optional[int] = None
    gallons_milli: Optional[int] = None
    adder_type: Optional[str] = None
    bol_id: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    errors: List[Dict[str, Any]] = field(default_factory=list)
    natural_key: Optional[str] = None

    def fail(self, loc: Sequence[Any], msg: str, type_: str) -> None:
        self.errors.append(_error(loc, msg, type_, self.row))

    @property
    def warnings(self) -> List[str]:
        if self.kind == CostEntryKind.PURCHASE.value and self.bol_id:
            return [WARNING_GALLONS_IGNORED]
        return []

    def values(self, *, actor: str, source: str, now: datetime, import_batch_id: Optional[str] = None) -> Dict[str, Any]:
        assert self.effective_at is not None and self.product_code is not None
        assert self.unit_cost_micros is not None and self.natural_key is not None
        return {
            "kind": self.kind,
            "product_code": self.product_code,
            "terminal_id": self.terminal_id,
            "supplier_name": self.supplier_name,
            "effective_at": self.effective_at,
            "effective_to": self.effective_to,
            "unit_cost_micros": self.unit_cost_micros,
            "gallons_milli": self.gallons_milli,
            "adder_type": self.adder_type,
            "bol_id": self.bol_id,
            "reference": self.reference,
            "notes": self.notes,
            "natural_key": self.natural_key,
            "source": source,
            "import_batch_id": import_batch_id,
            "created_by": actor,
            "created_at": now,
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MarginCostEntryService:
    """Cost entries and margin settings for one tenant per call.

    ``terminals`` is a ``TerminalRepository`` (``get(tenant_id, terminal_id)``);
    ``es_service`` serves the tenant-filtered ``terminal_bols`` reads;
    ``telemetry`` defaults to ``get_telemetry_service()`` at call time.
    """

    def __init__(
        self,
        repository: MarginRepository,
        *,
        terminals: Any,
        es_service: Any,
        telemetry: Any = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._repo = repository
        self._terminals = terminals
        self._es = es_service
        self._telemetry = telemetry
        self._clock = clock

    # ------------------------------------------------------------------
    # Create / supersede / void / list
    # ------------------------------------------------------------------

    async def create(
        self, tenant_id: str, actor: str, body: CostEntryCreate
    ) -> Dict[str, Any]:
        """Insert one active entry. 422 on validation, 409 ``CONFLICT`` on a duplicate."""

        now = self._clock()
        draft = await self._validate_single(tenant_id, body, now)
        try:
            entry = await self._repo.insert_entry(
                tenant_id,
                draft.values(actor=actor, source=CostEntrySource.MANUAL.value, now=now),
            )
        except MarginDuplicateEntryError as exc:
            raise self._duplicate(exc) from None
        self._audit(
            event_type="margin_cost_entry_created",
            actor=actor,
            resource_type="margin_cost_entry",
            resource_id=entry["entry_id"],
            action="create",
            details={
                "tenant_id": tenant_id,
                "entry_id": entry["entry_id"],
                "kind": entry["kind"],
                "product_code": entry["product_code"],
                "terminal_id": entry["terminal_id"],
                "bol_id": entry["bol_id"],
            },
            tenant_id=tenant_id,
        )
        return {"entry": entry, "warnings": draft.warnings}

    async def supersede(
        self, tenant_id: str, actor: str, entry_id: str, body: CostEntrySupersede
    ) -> Dict[str, Any]:
        """Replace an active entry with a new version (same kind).

        404 if the entry is not in this tenant, 409 if it is not active, 422
        on a kind change or invalid fields, 409 on a duplicate natural key or BOL.
        """

        now = self._clock()
        current = await self._repo.get_entry(tenant_id, entry_id)
        self._check_transitionable(current, entry_id)
        if current["kind"] != body.kind.value:
            raise _validation_failed(
                [_error(["kind"], "an entry's kind cannot change; void it and create a new one", "kind_mismatch")],
                "Cost entry validation failed",
            )
        reason_errors = self._reason_errors(body.reason)
        draft = await self._validate_single(tenant_id, body, now, extra_errors=reason_errors)
        try:
            transition = await self._repo.transition_entry(
                tenant_id,
                entry_id,
                action="supersede",
                reason=body.reason,
                actor=actor,
                replacement=draft.values(actor=actor, source=CostEntrySource.MANUAL.value, now=now),
                now=now,
            )
        except MarginEntryNotFoundError:
            raise self._not_found(entry_id) from None
        except MarginEntryNotActiveError:
            raise self._not_active(entry_id) from None
        except MarginEntryKindMismatchError:
            raise _validation_failed(
                [_error(["kind"], "an entry's kind cannot change", "kind_mismatch")],
                "Cost entry validation failed",
            ) from None
        except MarginDuplicateEntryError as exc:
            raise self._duplicate(exc) from None
        new_entry = transition.new_entry
        assert new_entry is not None
        self._audit(
            event_type="margin_cost_entry_superseded",
            actor=actor,
            resource_type="margin_cost_entry",
            resource_id=entry_id,
            action="supersede",
            details={
                "tenant_id": tenant_id,
                "entry_id": entry_id,
                "new_entry_id": new_entry["entry_id"],
                "reason": body.reason,
                "before": _entry_view(transition.before),
                "after": _entry_view(new_entry),
            },
            tenant_id=tenant_id,
        )
        return {"entry": new_entry, "superseded": transition.after, "warnings": draft.warnings}

    async def void(
        self, tenant_id: str, actor: str, entry_id: str, body: CostEntryVoid
    ) -> Dict[str, Any]:
        """Void an active entry. 404 other tenant / unknown, 409 not active."""

        now = self._clock()
        current = await self._repo.get_entry(tenant_id, entry_id)
        self._check_transitionable(current, entry_id)
        reason_errors = self._reason_errors(body.reason)
        if reason_errors:
            raise _validation_failed(reason_errors, "Cost entry validation failed")
        try:
            transition = await self._repo.transition_entry(
                tenant_id, entry_id, action="void", reason=body.reason, actor=actor, now=now
            )
        except MarginEntryNotFoundError:
            raise self._not_found(entry_id) from None
        except MarginEntryNotActiveError:
            raise self._not_active(entry_id) from None
        self._audit(
            event_type="margin_cost_entry_voided",
            actor=actor,
            resource_type="margin_cost_entry",
            resource_id=entry_id,
            action="void",
            details={
                "tenant_id": tenant_id,
                "entry_id": entry_id,
                "reason": body.reason,
                "before": {"status": transition.before["status"]},
                "after": {"status": transition.after["status"]},
            },
            tenant_id=tenant_id,
        )
        return {"entry": transition.after}

    async def list_entries(
        self,
        tenant_id: str,
        *,
        kind: Optional[str] = None,
        product_code: Optional[str] = None,
        terminal_id: Optional[str] = None,
        status: str = CostEntryStatus.ACTIVE.value,
        after: Optional[Tuple[datetime, str]] = None,
        limit: int = 50,
    ) -> Page:
        """Keyset list on (``created_at`` desc, ``entry_id`` desc), ``limit <= 200``."""

        errors: List[Dict[str, Any]] = []
        if status not in LIST_STATUSES:
            errors.append(_error(["status"], "use active, superseded, voided or all", "invalid_status"))
        if kind is not None and kind not in {k.value for k in CostEntryKind}:
            errors.append(_error(["kind"], "use purchase, override or adder", "invalid_kind"))
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= LIST_MAX_LIMIT:
            errors.append(_error(["limit"], f"must be between 1 and {LIST_MAX_LIMIT}", "out_of_range"))
        canonical = None
        if product_code is not None:
            try:
                canonical = canonicalize(product_code)
            except UnknownFuelProductError:
                errors.append(_error(["product_code"], "unknown product", "unknown_product"))
        if errors:
            raise _validation_failed(errors, "Invalid cost entry filter")
        return await self._repo.list_entries(
            tenant_id,
            kind=kind,
            product_code=canonical,
            terminal_id=terminal_id,
            status=status,
            after=after,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # CSV import
    # ------------------------------------------------------------------

    async def import_csv(
        self, tenant_id: str, actor: str, upload: Any, *, dry_run: bool = True
    ) -> Dict[str, Any]:
        """Validate (and, unless ``dry_run``, insert) a CSV of cost entries.

        All or nothing: any invalid row -> 422 with ``{row, loc, msg, type}``
        per error and nothing written (AC-35). Duplicates (later rows of the
        file, or rows matching an active entry) are reported and skipped.
        ``rows`` are 1-based data rows (the header is not counted); blank
        lines are ignored. ``rows_valid`` counts the rows that would be (or
        were) created.
        """

        raw = await _read_upload(upload)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise _validation_failed(
                [_error(["file"], "the file must be UTF-8 encoded CSV", "invalid_encoding")],
                "Cost entry import failed",
            ) from None
        header, rows = self._parse_csv(text)
        now = self._clock()
        settings = await self._repo.get_settings(tenant_id)
        zone = _zone(settings.get("timezone"))

        drafts: List[_Draft] = []
        for row_number, cells in rows:
            drafts.append(self._draft_from_cells(row_number, header, cells, zone, now))
        await self._check_references(tenant_id, drafts)
        errors = [e for d in drafts for e in d.errors]
        if errors:
            raise _validation_failed(errors, "Cost entry import failed", rows_total=len(drafts))

        duplicates, to_create = await self._split_duplicates(tenant_id, drafts)
        conflict_errors = await self._bol_conflicts(tenant_id, to_create)
        if conflict_errors:
            raise _validation_failed(conflict_errors, "Cost entry import failed", rows_total=len(drafts))

        warnings = sorted({w for d in to_create for w in d.warnings})
        report: Dict[str, Any] = {
            "dry_run": dry_run,
            "rows_total": len(drafts),
            "rows_valid": len(to_create),
            "duplicates": duplicates,
            "created_entry_ids": [],
            "warnings": warnings,
        }
        if dry_run:
            return report

        batch_id = f"mcb_{uuid.uuid4().hex}"
        created: List[str] = []
        if to_create:
            values = [
                d.values(
                    actor=actor,
                    source=CostEntrySource.CSV_IMPORT.value,
                    now=now,
                    import_batch_id=batch_id,
                )
                for d in to_create
            ]
            try:
                created = await self._repo.insert_entries(tenant_id, values)
            except MarginDuplicateEntryError:
                raise AppException(
                    ErrorCode.CONFLICT,
                    "A matching cost entry was created while importing; re-run the dry run",
                    status_code=409,
                ) from None
        report["created_entry_ids"] = created
        report["import_batch_id"] = batch_id
        self._audit(
            event_type="margin_cost_import",
            actor=actor,
            resource_type="margin_cost_entry",
            resource_id=batch_id,
            action="import",
            details={
                "tenant_id": tenant_id,
                "import_batch_id": batch_id,
                "rows_total": len(drafts),
                "rows_created": len(created),
                "duplicates": len(duplicates),
            },
            tenant_id=tenant_id,
        )
        return report

    def _parse_csv(self, text: str) -> Tuple[List[str], List[Tuple[int, List[str]]]]:
        reader = csv.reader(io.StringIO(text, newline=""))
        try:
            header_cells = next(reader, None)
            if header_cells is None:
                raise _validation_failed(
                    [_error(["header", name], "required column is missing", "missing_header") for name in IMPORT_REQUIRED_COLUMNS],
                    "Cost entry import failed",
                )
            header = [h.strip() for h in header_cells]
            header_errors: List[Dict[str, Any]] = []
            seen: set[str] = set()
            for name in header:
                if name not in IMPORT_COLUMNS:
                    header_errors.append(_error(["header", name], "unknown column", "unknown_header"))
                elif name in seen:
                    header_errors.append(_error(["header", name], "column appears twice", "duplicate_header"))
                seen.add(name)
            for name in IMPORT_REQUIRED_COLUMNS:
                if name not in seen:
                    header_errors.append(_error(["header", name], "required column is missing", "missing_header"))
            if header_errors:
                raise _validation_failed(header_errors, "Cost entry import failed")

            rows: List[Tuple[int, List[str]]] = []
            for cells in reader:
                if not any(cell.strip() for cell in cells):
                    continue
                rows.append((len(rows) + 1, cells))
                if len(rows) > IMPORT_MAX_ROWS:
                    raise AppException(
                        ErrorCode.MARGIN_IMPORT_TOO_LARGE,
                        f"The import file has more than {IMPORT_MAX_ROWS:,} rows; split it",
                        status_code=413,
                        details={"max_rows": IMPORT_MAX_ROWS},
                    )
        except csv.Error as exc:
            raise _validation_failed(
                [_error(["file"], f"the file is not valid CSV ({exc})", "invalid_csv")],
                "Cost entry import failed",
            ) from None
        return header, rows

    def _draft_from_cells(
        self,
        row_number: int,
        header: Sequence[str],
        cells: Sequence[str],
        zone: ZoneInfo,
        now: datetime,
    ) -> _Draft:
        if len(cells) > len(header) and any(c.strip() for c in cells[len(header):]):
            draft = _Draft(row=row_number, kind="")
            draft.fail(["row"], "the row has more cells than the header", "extra_cells")
            return draft
        values = {
            name: cell.strip()
            for name, cell in zip(header, cells)
            if cell.strip() != ""
        }
        try:
            body = CostEntryCreate.model_validate(values)
        except ValidationError as exc:
            draft = _Draft(row=row_number, kind=str(values.get("kind") or ""))
            for err in exc.errors():
                draft.fail(
                    [str(p) if not isinstance(p, int) else p for p in err.get("loc", ())],
                    str(err.get("msg", "")),
                    str(err.get("type", "")),
                )
            return draft
        return self._validate_local(body, zone, now, row=row_number)

    async def _split_duplicates(
        self, tenant_id: str, drafts: Sequence[_Draft]
    ) -> Tuple[List[Dict[str, Any]], List[_Draft]]:
        """In-file duplicates (later rows) and duplicates of active entries."""

        keys = sorted({d.natural_key for d in drafts if d.natural_key})
        active: Dict[str, str] = {}
        for start in range(0, len(keys), IMPORT_BATCH):
            active.update(await self._repo.active_natural_keys(tenant_id, keys[start : start + IMPORT_BATCH]))
        duplicates: List[Dict[str, Any]] = []
        to_create: List[_Draft] = []
        seen: Dict[str, int] = {}
        for draft in drafts:
            key = draft.natural_key
            assert key is not None
            if key in active:
                duplicates.append({"row": draft.row, "natural_key": key, "existing_entry_id": active[key]})
            elif key in seen:
                duplicates.append({"row": draft.row, "natural_key": key, "duplicate_of_row": seen[key]})
            else:
                seen[key] = draft.row or 0
                to_create.append(draft)
        return duplicates, to_create

    async def _bol_conflicts(self, tenant_id: str, drafts: Sequence[_Draft]) -> List[Dict[str, Any]]:
        """A BOL may hold one active price: a second row for one BOL, or a BOL
        already priced by an active entry, is a row error (not a duplicate,
        because the prices may differ; supersede the entry instead)."""

        errors: List[Dict[str, Any]] = []
        bol_drafts = [d for d in drafts if d.bol_id]
        if not bol_drafts:
            return errors
        ids = sorted({d.bol_id for d in bol_drafts if d.bol_id})
        existing: Dict[str, Dict[str, Any]] = {}
        for start in range(0, len(ids), IMPORT_BATCH):
            existing.update(await self._repo.active_entries_for_bols(tenant_id, ids[start : start + IMPORT_BATCH]))
        first_row: Dict[str, int] = {}
        for draft in bol_drafts:
            bol_id = draft.bol_id
            assert bol_id is not None
            if bol_id in existing:
                errors.append(
                    _error(
                        ["bol_id"],
                        f"BOL already priced by active entry {existing[bol_id]['entry_id']}; supersede it instead",
                        "bol_already_priced",
                        draft.row,
                    )
                )
            elif bol_id in first_row:
                errors.append(
                    _error(["bol_id"], f"BOL also priced on row {first_row[bol_id]}", "duplicate_bol", draft.row)
                )
            else:
                first_row[bol_id] = draft.row or 0
        return errors

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    async def update_settings(
        self, tenant_id: str, actor: str, body: MarginSettingsUpdate
    ) -> Dict[str, Any]:
        """``PUT /settings``: validate, upsert, audit with before/after.

        ``wac_window_days > 30`` is accepted with
        ``warnings: ["wac_window_may_exceed_bol_scan_cap"]``.
        """

        errors: List[Dict[str, Any]] = []
        floor_micros: Optional[int] = None
        try:
            floor_micros = parse_usd_micros(
                body.floor_usd_per_gallon, field="floor_usd_per_gallon", max_micros=FLOOR_MICROS_MAX
            )
        except MarginValueError as exc:
            errors.append(exc.as_error())
        product_floors: Dict[str, int] = {}
        for raw_code, value in body.product_floors.items():
            loc = ["product_floors", raw_code]
            try:
                code = canonicalize(raw_code)
            except UnknownFuelProductError:
                errors.append(_error(loc, "unknown product", "unknown_product"))
                continue
            if code in product_floors:
                errors.append(_error(loc, f"{code} is listed more than once", "duplicate_product"))
                continue
            try:
                product_floors[code] = parse_usd_micros(
                    value, field="product_floors", max_micros=FLOOR_MICROS_MAX
                )
            except MarginValueError as exc:
                errors.append(_error(loc, exc.msg, exc.type))
        if _CONTROL.search(body.timezone):
            errors.append(_error(["timezone"], "control characters are not allowed", "control_characters"))
        else:
            try:
                ZoneInfo(body.timezone)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                errors.append(_error(["timezone"], "not an IANA timezone name", "invalid_timezone"))
        if errors:
            raise _validation_failed(errors, "Margin settings validation failed")
        assert floor_micros is not None
        change = await self._repo.put_settings(
            tenant_id,
            {
                "wac_window_days": body.wac_window_days,
                "rack_staleness_days": body.rack_staleness_days,
                "floor_micros": floor_micros,
                "product_floors": product_floors,
                "timezone": body.timezone,
            },
            actor=actor,
            now=self._clock(),
        )
        warnings = [WARNING_WAC_WINDOW] if body.wac_window_days > DEFAULT_WAC_WINDOW_DAYS else []
        self._audit(
            event_type="margin_settings_updated",
            actor=actor,
            resource_type="margin_settings",
            resource_id=tenant_id,
            action="update",
            details={
                "tenant_id": tenant_id,
                "before": _settings_view(change.before),
                "after": _settings_view(change.after),
            },
            tenant_id=tenant_id,
        )
        return {"settings": change.after, "warnings": warnings}

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    async def _validate_single(
        self,
        tenant_id: str,
        body: CostEntryCreate,
        now: datetime,
        *,
        extra_errors: Iterable[Dict[str, Any]] = (),
    ) -> _Draft:
        settings = await self._repo.get_settings(tenant_id)
        draft = self._validate_local(body, _zone(settings.get("timezone")), now)
        draft.errors[:0] = list(extra_errors)
        await self._check_references(tenant_id, [draft])
        if draft.errors:
            raise _validation_failed(draft.errors, "Cost entry validation failed")
        return draft

    def _validate_local(
        self, body: CostEntryCreate, zone: ZoneInfo, now: datetime, *, row: Optional[int] = None
    ) -> _Draft:
        """Every check that needs no store read (the design FR1 table)."""

        kind = body.kind.value
        draft = _Draft(
            row=row,
            kind=kind,
            terminal_id=body.terminal_id,
            supplier_name=body.supplier_name,
            bol_id=body.bol_id,
            reference=body.reference,
            notes=body.notes,
        )
        for name in _TEXT_FIELDS:
            value = getattr(body, name)
            if isinstance(value, str) and _CONTROL.search(value):
                draft.fail([name], "control characters are not allowed", "control_characters")

        try:
            draft.product_code = canonicalize(body.product_code)
        except UnknownFuelProductError:
            draft.fail(["product_code"], "unknown product", "unknown_product")

        is_purchase = kind == CostEntryKind.PURCHASE.value
        if is_purchase and not body.terminal_id:
            draft.fail(["terminal_id"], "a purchase needs a terminal", "required")

        effective_at, err = _parse_instant(body.effective_at, field_name="effective_at", zone=zone)
        if err is not None:
            draft.fail(err["loc"], err["msg"], err["type"])
        elif effective_at is not None:
            if effective_at > now + timedelta(days=1):
                draft.fail(["effective_at"], "must not be more than one day in the future", "too_far_future")
            draft.effective_at = effective_at

        if body.effective_to is not None:
            if is_purchase:
                draft.fail(["effective_to"], "only overrides and adders have an end", "not_allowed")
            else:
                effective_to, err = _parse_instant(body.effective_to, field_name="effective_to", zone=zone)
                if err is not None:
                    draft.fail(err["loc"], err["msg"], err["type"])
                elif effective_to is not None:
                    if draft.effective_at is not None and effective_to <= draft.effective_at:
                        draft.fail(["effective_to"], "must be after effective_at", "before_effective_at")
                    draft.effective_to = effective_to

        try:
            draft.unit_cost_micros = parse_usd_micros(
                body.unit_cost_usd, field="unit_cost_usd", max_micros=MAX_UNIT_COST_MICROS
            )
        except MarginValueError as exc:
            draft.fail([exc.field], exc.msg, exc.type)

        if is_purchase:
            if body.gallons is None:
                draft.fail(["gallons"], "a purchase needs gallons", "required")
            else:
                try:
                    draft.gallons_milli = parse_gallons_milli(body.gallons, field="gallons")
                except MarginValueError as exc:
                    draft.fail([exc.field], exc.msg, exc.type)
        elif body.gallons is not None:
            draft.fail(["gallons"], "only purchases have gallons", "not_allowed")

        if kind == CostEntryKind.ADDER.value:
            if body.adder_type is None:
                draft.fail(["adder_type"], "an adder needs an adder_type", "required")
            else:
                draft.adder_type = body.adder_type.value
        elif body.adder_type is not None:
            draft.fail(["adder_type"], "only adders have an adder_type", "not_allowed")

        if body.bol_id is not None and not is_purchase:
            draft.fail(["bol_id"], "only purchases reference a BOL", "not_allowed")

        if not draft.errors:
            assert draft.product_code is not None and draft.effective_at is not None
            draft.natural_key = natural_key(
                kind=kind,
                product_code=draft.product_code,
                terminal_id=draft.terminal_id,
                effective_at=draft.effective_at,
                bol_id=draft.bol_id,
                reference=draft.reference,
                adder_type=draft.adder_type,
            )
        return draft

    async def _check_references(self, tenant_id: str, drafts: Sequence[_Draft]) -> None:
        """Terminal (memoized per distinct id) and BOL (one ``terms`` query per
        1,000 distinct ids) checks, all tenant-scoped."""

        terminal_ids = sorted({d.terminal_id for d in drafts if d.terminal_id and not _CONTROL.search(d.terminal_id)})
        known_terminals: Dict[str, bool] = {}
        for terminal_id in terminal_ids:
            known_terminals[terminal_id] = (await self._terminals.get(tenant_id, terminal_id)) is not None
        for draft in drafts:
            if draft.terminal_id and not known_terminals.get(draft.terminal_id, False):
                if not any(e["loc"] == ["terminal_id"] for e in draft.errors):
                    draft.fail(["terminal_id"], "unknown terminal", "unknown_terminal")

        bol_drafts = [
            d for d in drafts
            if d.bol_id and d.kind == CostEntryKind.PURCHASE.value and not _CONTROL.search(d.bol_id)
        ]
        bol_ids = sorted({d.bol_id for d in bol_drafts if d.bol_id})
        bols: Dict[str, Mapping[str, Any]] = {}
        for start in range(0, len(bol_ids), IMPORT_BATCH):
            bols.update(await self._fetch_bols(tenant_id, bol_ids[start : start + IMPORT_BATCH]))
        for draft in bol_drafts:
            bol = bols.get(draft.bol_id or "")
            if bol is None:
                draft.fail(["bol_id"], "unknown BOL", "unknown_bol")
                continue
            try:
                bol_product = canonicalize(str(bol.get("product_code") or ""))
            except UnknownFuelProductError:
                bol_product = None
            if bol_product != draft.product_code or bol.get("terminal_id") != draft.terminal_id:
                draft.fail(["bol_id"], "the BOL's product or terminal differs from the entry", "bol_mismatch")
        for draft in drafts:
            if draft.errors:
                draft.natural_key = None

    async def _fetch_bols(self, tenant_id: str, bol_ids: Sequence[str]) -> Dict[str, Mapping[str, Any]]:
        query = inject_tenant_filter(
            {"query": {"terms": {"bol_id": list(bol_ids)}}, "size": len(bol_ids)}, tenant_id
        )
        response = await self._es.search_documents(TERMINAL_BOLS_INDEX, query, len(bol_ids))
        found: Dict[str, Mapping[str, Any]] = {}
        for hit in ((response or {}).get("hits") or {}).get("hits") or []:
            source = hit.get("_source") or {}
            if source.get("tenant_id") != tenant_id:
                logger.error(
                    "margin cost entries: dropping BOL %s from another tenant (owner=%s, requester=%s)",
                    source.get("bol_id"),
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            if source.get("bol_id"):
                found[str(source["bol_id"])] = source
        return found

    @staticmethod
    def _reason_errors(reason: str) -> List[Dict[str, Any]]:
        if _CONTROL.search(reason):
            return [_error(["reason"], "control characters are not allowed", "control_characters")]
        return []

    # ------------------------------------------------------------------
    # Errors and audit
    # ------------------------------------------------------------------

    def _check_transitionable(self, current: Optional[Mapping[str, Any]], entry_id: str) -> None:
        if current is None:
            raise self._not_found(entry_id)
        if current["status"] != CostEntryStatus.ACTIVE.value:
            raise self._not_active(entry_id)

    @staticmethod
    def _not_found(entry_id: str) -> AppException:
        logger.info("margin cost entry %s not found", entry_id)
        return AppException(ErrorCode.RESOURCE_NOT_FOUND, "Cost entry not found", status_code=404)

    @staticmethod
    def _not_active(entry_id: str) -> AppException:
        logger.info("margin cost entry %s is not active", entry_id)
        return AppException(
            ErrorCode.CONFLICT, "The cost entry is no longer active", status_code=409,
            details={"entry_id": entry_id},
        )

    @staticmethod
    def _duplicate(exc: MarginDuplicateEntryError) -> AppException:
        logger.info("margin cost entry duplicate of %s", exc.existing_entry_id)
        return AppException(
            ErrorCode.CONFLICT,
            "An active cost entry with the same key or BOL already exists",
            status_code=409,
            details={"existing_entry_id": exc.existing_entry_id},
        )

    def _audit(
        self,
        *,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        action: str,
        details: Dict[str, Any],
        tenant_id: str,
    ) -> None:
        telemetry = self._telemetry if self._telemetry is not None else get_telemetry_service()
        if telemetry is not None:
            try:
                telemetry.log_audit_event(
                    event_type=event_type,
                    user_id=actor,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    action=action,
                    details=details,
                )
            except Exception as exc:  # noqa: BLE001 - audit must never fail the request
                logger.warning(
                    "margin audit sink failed for %s %s: %s", event_type, resource_id, type(exc).__name__
                )
        else:
            logger.debug("margin audit: telemetry service not initialized")
        logger.info(
            "margin %s tenant=%s %s=%s by=%s",
            event_type,
            tenant_id,
            resource_type,
            resource_id,
            actor,
        )


__all__ = [
    "IMPORT_CHUNK_BYTES",
    "IMPORT_COLUMNS",
    "IMPORT_MAX_BYTES",
    "IMPORT_MAX_ROWS",
    "IMPORT_REQUIRED_COLUMNS",
    "MarginCostEntryService",
    "WARNING_GALLONS_IGNORED",
    "WARNING_WAC_WINDOW",
    "natural_key",
]
