"""Import service for the data import/migration tool.

Orchestrates the import workflow: CSV/Sheets parsing, field mapping,
validation, bulk indexing, and session history. Holds in-memory session
state for active imports and persists completed sessions to Elasticsearch.
"""

import csv
import io
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any, Optional
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

from pydantic import ValidationError as PydanticValidationError

from services.elasticsearch_service import ElasticsearchService
from services.field_mapper import FieldMapper
from services.import_models import (
    ImportResult,
    ImportSessionRecord,
    ImportStatus,
    ParseResult,
    ValidationIssue,
    ValidationResult,
)
from services.schema_templates import FieldType, SchemaTemplates
from services.time_utils import utcnow
from services.validation_engine import ValidationEngine

logger = logging.getLogger(__name__)

ACTIVE_IMPORT_SESSIONS_INDEX = "import_sessions_active"
CANONICAL_IMPORT_TYPES = frozenset({"orders", "customer_tanks", "tank_readings"})
#: Document id field per non-canonical data type (the template's id field).
#: Document ids are global in the store, so these rows are written
#: create-if-absent and never replace a row another tenant owns (C1).
NON_CANONICAL_ID_FIELDS = {
    "fleet": "truck_id",
    "inventory": "item_id",
    "fuel_stations": "station_id",
    "jobs": "job_id",
}


#: Catalog categories (``fuel_product_catalog.FuelCategory``) a tank of each
#: ``CustomerTank.fuel_type`` may hold. Checked on import only (B7); the model
#: itself doesn't enforce it, so stored tanks still load.
_TANK_FUEL_FAMILIES: dict[str, frozenset[str]] = {
    "propane": frozenset({"propane"}),
    "heating_oil": frozenset({"heating_oil", "kerosene"}),
    "diesel": frozenset({"diesel", "off_road"}),
    "gasoline": frozenset({"gasoline", "ethanol"}),
    "farm_fuel": frozenset({"off_road", "diesel", "gasoline"}),
    "generator_fuel": frozenset({"diesel", "off_road", "propane", "gasoline"}),
}


class _RowFieldError(ValueError):
    """A row validation failure attributable to one field."""

    def __init__(self, field_name: str, message: str):
        super().__init__(message)
        self.field_name = field_name


def _check_fuel_family(fuel_type: Optional[str], product_code: Optional[str]) -> None:
    """Refuse a tank whose catalog product isn't in its fuel family (B7)."""
    if not fuel_type or not product_code:
        return
    allowed = _TANK_FUEL_FAMILIES.get(fuel_type)
    if allowed is None:
        return
    from fuel.services.fuel_product_catalog import get_product

    category = get_product(product_code).category
    if category not in allowed:
        raise _RowFieldError(
            "fuel_product_code",
            f"fuel_product_code '{product_code}' ({category}) does not match "
            f"fuel_type '{fuel_type}'",
        )


class _ActiveSession:
    """In-memory state for an active import session."""

    __slots__ = (
        "session_id",
        "tenant_id",
        "data_type",
        "source_type",
        "source_name",
        "rows",
        "columns",
        "sample_rows",
        "total_rows",
        "suggested_mapping",
        "field_mapping",
        "validation_result",
        "status",
        "created_at",
    )

    def __init__(
        self,
        session_id: str,
        tenant_id: str,
        data_type: str,
        source_type: str,
        source_name: str,
        rows: list[dict[str, str]],
        columns: list[str],
        sample_rows: list[dict[str, str]],
        total_rows: int,
        suggested_mapping: dict[str, str],
    ):
        self.session_id = session_id
        self.tenant_id = tenant_id
        self.data_type = data_type
        self.source_type = source_type
        self.source_name = source_name
        self.rows = rows
        self.columns = columns
        self.sample_rows = sample_rows
        self.total_rows = total_rows
        self.suggested_mapping = suggested_mapping
        self.field_mapping: dict[str, str] = {}
        self.validation_result: Optional[ValidationResult] = None
        self.status: ImportStatus = ImportStatus.PARSING
        self.created_at: str = utcnow().isoformat()


class ImportService:
    """Orchestrates the data import workflow.

    Manages in-memory sessions for active imports and delegates to
    ``SchemaTemplates``, ``ValidationEngine``, ``FieldMapper``, and
    ``ElasticsearchService`` for the heavy lifting.
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        *,
        order_intake_pipeline: Any = None,
        tank_import_service: Any = None,
    ):
        self.es_service = es_service
        self.schema_templates = SchemaTemplates()
        self.validation_engine = ValidationEngine(self.schema_templates)
        self.field_mapper = FieldMapper(self.schema_templates)
        self._active_sessions: dict[str, _ActiveSession] = {}
        self._order_intake_pipeline = order_intake_pipeline
        self._tank_import_service = tank_import_service

    def configure_canonical_imports(
        self,
        *,
        order_intake_pipeline: Any,
        tank_import_service: Any,
    ) -> None:
        """Install domain services after application bootstrap completes."""

        self._order_intake_pipeline = order_intake_pipeline
        self._tank_import_service = tank_import_service

    # ------------------------------------------------------------------
    # CSV parsing
    # ------------------------------------------------------------------

    async def parse_csv(
        self,
        file_content: bytes,
        data_type: str,
        *,
        tenant_id: str = "",
        source_name: str = "uploaded.csv",
    ) -> ParseResult:
        """Parse a CSV file and create an import session.

        Decodes the bytes as UTF-8 (handling BOM), extracts headers,
        first 5 sample rows, total row count, and auto-suggests a field
        mapping.

        Args:
            file_content: Raw bytes of the uploaded CSV file.
            data_type: One of the supported data type keys.

        Returns:
            ParseResult with session info, columns, sample rows, and
            suggested mapping.

        Raises:
            ValueError: If the CSV cannot be parsed or has no header row.
        """
        # Validate data type early
        self.schema_templates.get_template(data_type)

        try:
            # Decode UTF-8, strip BOM if present
            text = file_content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Failed to decode CSV as UTF-8: {exc}") from exc

        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise ValueError("CSV file has no header row")

        columns = [col.strip() for col in reader.fieldnames]

        # Read all rows
        rows: list[dict[str, str]] = []
        for row in reader:
            # Re-key with stripped column names
            cleaned: dict[str, str] = {}
            for raw_key, value in row.items():
                key = raw_key.strip() if raw_key else raw_key
                cleaned[key] = value if value is not None else ""
            rows.append(cleaned)

        total_rows = len(rows)
        sample_rows = rows[:5]

        # Auto-suggest field mapping
        suggested_mapping = self.field_mapper.auto_map(columns, data_type)

        # Create session
        session_id = str(uuid.uuid4())
        session = _ActiveSession(
            session_id=session_id,
            tenant_id=tenant_id,
            data_type=data_type,
            source_type="csv",
            source_name=source_name,
            rows=rows,
            columns=columns,
            sample_rows=sample_rows,
            total_rows=total_rows,
            suggested_mapping=suggested_mapping,
        )
        self._active_sessions[session_id] = session
        await self._persist_active_session(session)

        logger.info(
            "CSV parsed: session=%s, data_type=%s, columns=%d, rows=%d",
            session_id,
            data_type,
            len(columns),
            total_rows,
        )

        return ParseResult(
            session_id=session_id,
            columns=columns,
            sample_rows=sample_rows,
            total_rows=total_rows,
            suggested_mapping=suggested_mapping,
        )

    # ------------------------------------------------------------------
    # Google Sheets parsing
    # ------------------------------------------------------------------

    async def parse_sheets(
        self,
        url: str,
        data_type: str,
        *,
        tenant_id: str = "",
    ) -> ParseResult:
        """Fetch a Google Sheet and parse it like a CSV.

        Converts the URL to a CSV export URL, fetches the data, and
        delegates to the same CSV parsing logic.

        Args:
            url: Public Google Sheets URL.
            data_type: One of the supported data type keys.

        Returns:
            ParseResult with session info, columns, sample rows, and
            suggested mapping.

        Raises:
            ValueError: If the URL is invalid or the sheet cannot be fetched.
        """
        # Validate data type early
        self.schema_templates.get_template(data_type)

        # Extract sheet ID from various Google Sheets URL formats
        sheet_id = self._extract_sheet_id(url)
        if not sheet_id:
            raise ValueError(
                "Could not extract a Google Sheets ID from the provided URL. "
                "Expected format: https://docs.google.com/spreadsheets/d/{SHEET_ID}/..."
            )

        export_url = (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        )

        try:
            response = urlopen(export_url, timeout=30)  # noqa: S310
            file_content = response.read()
        except HTTPError as exc:
            raise ValueError(
                f"Failed to fetch Google Sheet (HTTP {exc.code}): "
                "check that the sheet is publicly accessible"
            ) from exc
        except URLError as exc:
            raise ValueError(
                f"Failed to fetch Google Sheet: {exc.reason}"
            ) from exc
        except Exception as exc:
            raise ValueError(
                f"Failed to fetch Google Sheet: {exc}"
            ) from exc

        # Parse the fetched CSV content
        result = await self.parse_csv(
            file_content,
            data_type,
            tenant_id=tenant_id,
            source_name=url,
        )

        # Update session metadata to reflect Google Sheets source
        session = self._active_sessions[result.session_id]
        session.source_type = "google_sheets"
        session.source_name = url
        await self._persist_active_session(session)

        return result

    @staticmethod
    def _extract_sheet_id(url: str) -> Optional[str]:
        """Extract the spreadsheet ID from a Google Sheets URL."""
        # Match /spreadsheets/d/{ID}
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1)
        return None

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    async def validate(
        self,
        session_id: str,
        field_mapping: dict[str, str],
        *,
        tenant_id: str = "",
    ) -> ValidationResult:
        """Validate mapped data against the schema template.

        Retrieves the session, runs the validation engine, stores the
        result in the session, and returns it.

        Args:
            session_id: The active session identifier.
            field_mapping: Dict mapping source column names to target
                field names.

        Returns:
            ValidationResult with per-row errors and warnings.

        Raises:
            ValueError: If the session is not found.
        """
        session = await self._get_active_session(session_id, tenant_id=tenant_id)

        session.field_mapping = field_mapping
        session.status = ImportStatus.VALIDATING

        result = self.validation_engine.validate_rows(
            rows=session.rows,
            data_type=session.data_type,
            field_mapping=field_mapping,
        )
        if session.data_type in CANONICAL_IMPORT_TYPES:
            await self._append_canonical_validation_issues(
                session, field_mapping, result
            )
        elif session.data_type in NON_CANONICAL_ID_FIELDS:
            if session.data_type == "inventory":
                self._append_inventory_issues(session, field_mapping, result)
            await self._append_ownership_issues(session, field_mapping, result)
        # Stamp the session_id onto the result
        result.session_id = session_id

        session.validation_result = result
        session.status = ImportStatus.VALIDATED
        await self._persist_active_session(session)

        logger.info(
            "Validation complete: session=%s, total=%d, valid=%d, errors=%d",
            session_id,
            result.total_rows,
            result.valid_rows,
            result.error_count,
        )

        return result

    # ------------------------------------------------------------------
    # Commit (bulk index)
    # ------------------------------------------------------------------

    async def commit(
        self,
        session_id: str,
        skip_errors: bool = False,
        *,
        tenant: Any = None,
    ) -> ImportResult:
        """Commit validated records through the correct domain write path.

        Canonical fuel orders, customer tanks, and tank readings never use the
        generic bulk-index helper. They pass through their domain services so
        tenant isolation, idempotency, validation, events, and downstream
        visibility are preserved.
        """
        tenant_id = getattr(tenant, "tenant_id", "") if tenant is not None else ""
        session = await self._get_active_session(session_id, tenant_id=tenant_id)

        if session.validation_result is None:
            raise ValueError(
                f"Session {session_id} has not been validated. "
                "Run validation first."
            )

        session.status = ImportStatus.IMPORTING
        start_time = time.time()

        validation = session.validation_result

        # Determine which row numbers had errors
        error_row_numbers: set[int] = set()
        if skip_errors:
            error_row_numbers = {issue.row_number for issue in validation.errors}

        # Build target documents by applying field mapping and coercing values.
        target_index = self.schema_templates.get_index(session.data_type)
        documents: list[tuple[int, dict[str, Any]]] = []
        skipped = 0

        for row_idx, row in enumerate(session.rows):
            row_number = row_idx + 1
            if row_number in error_row_numbers:
                skipped += 1
                continue

            doc = self._map_and_coerce_row(
                row, session.field_mapping, session.data_type
            )
            documents.append((row_number, doc))

        # Bulk index
        import_errors: list[str] = []
        imported = 0
        failed = 0

        if documents and session.data_type in CANONICAL_IMPORT_TYPES:
            imported, failed, canonical_skipped, canonical_errors = (
                await self._commit_canonical_documents(
                    session=session,
                    documents=documents,
                    tenant=tenant,
                )
            )
            skipped += canonical_skipped
            import_errors.extend(canonical_errors)
        elif documents:
            imported, failed, tenant_errors = await self._commit_tenant_documents(
                session=session,
                target_index=target_index,
                documents=documents,
                tenant=tenant,
            )
            import_errors.extend(tenant_errors)

        duration = time.time() - start_time

        # Determine final status
        if failed > 0 and imported > 0:
            status = ImportStatus.PARTIAL
        elif failed > 0 and imported == 0:
            status = ImportStatus.FAILED
        else:
            status = ImportStatus.COMPLETED

        session.status = status

        # Build ImportResult
        result = ImportResult(
            session_id=session_id,
            status=status,
            total_records=len(session.rows),
            imported_records=imported,
            skipped_records=skipped,
            error_count=failed,
            errors=import_errors,
            data_type=session.data_type,
            es_index=target_index,
            duration_seconds=round(duration, 3),
        )

        # Persist session record to ES
        await self._persist_session_record(session, result, duration)

        logger.info(
            "Import committed: session=%s, status=%s, imported=%d, skipped=%d, failed=%d, duration=%.2fs",
            session_id,
            status.value,
            imported,
            skipped,
            failed,
            duration,
        )

        return result

    # ------------------------------------------------------------------
    # History & session retrieval
    # ------------------------------------------------------------------

    async def get_history(
        self,
        data_type: Optional[str] = None,
        status: Optional[str] = None,
        *,
        tenant_id: str = "",
    ) -> list[ImportSessionRecord]:
        """Query the import_sessions ES index with optional filters.

        Results are sorted by ``created_at`` descending (most recent first).

        Args:
            data_type: Optional filter by data type.
            status: Optional filter by import status.

        Returns:
            List of ImportSessionRecord objects.
        """
        must_clauses: list[dict[str, Any]] = []
        if tenant_id:
            must_clauses.append({"term": {"tenant_id": tenant_id}})
        if data_type:
            must_clauses.append({"term": {"data_type": data_type}})
        if status:
            must_clauses.append({"term": {"status": status}})

        query: dict[str, Any] = {
            "query": {
                "bool": {"must": must_clauses} if must_clauses else {"must": [{"match_all": {}}]}
            },
            "sort": [{"created_at": {"order": "desc"}}],
        }

        try:
            response = await self.es_service.search_documents(
                "import_sessions", query, size=100
            )
            hits = response.get("hits", {}).get("hits", [])
            records = []
            for hit in hits:
                source = hit["_source"]
                records.append(ImportSessionRecord(**source))
            return records
        except Exception as exc:
            logger.error("Failed to fetch import history: %s", exc)
            return []

    async def get_session(
        self,
        session_id: str,
        *,
        tenant_id: str = "",
    ) -> Optional[ImportSessionRecord]:
        """Fetch a single import session record from ES.

        Args:
            session_id: The session identifier.

        Returns:
            ImportSessionRecord if found, else None.
        """
        must = [{"term": {"session_id": session_id}}]
        if tenant_id:
            must.append({"term": {"tenant_id": tenant_id}})
        query: dict[str, Any] = {"query": {"bool": {"must": must}}}

        try:
            response = await self.es_service.search_documents(
                "import_sessions", query, size=1
            )
            hits = response.get("hits", {}).get("hits", [])
            if hits:
                source = hits[0]["_source"]
                if tenant_id and source.get("tenant_id") != tenant_id:
                    return None
                return ImportSessionRecord(**source)
            return None
        except Exception as exc:
            logger.error("Failed to fetch session %s: %s", session_id, exc)
            return None

    # ------------------------------------------------------------------
    # Template generation
    # ------------------------------------------------------------------

    async def generate_template(self, data_type: str) -> str:
        """Generate a CSV template for the given data type.

        Delegates to ``SchemaTemplates.generate_csv_template()``.

        Args:
            data_type: One of the supported data type keys.

        Returns:
            CSV-formatted string with headers and example rows.
        """
        return self.schema_templates.generate_csv_template(data_type)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_active_session(
        self,
        session_id: str,
        *,
        tenant_id: str = "",
    ) -> _ActiveSession:
        """Retrieve a cached or durably persisted tenant-owned session."""
        session = self._active_sessions.get(session_id)
        if session is None:
            source = await self.es_service.get_document(
                ACTIVE_IMPORT_SESSIONS_INDEX, session_id
            )
            if source is not None:
                session = self._session_from_document(source)
                self._active_sessions[session_id] = session
        if session is None or (
            tenant_id and session.tenant_id and session.tenant_id != tenant_id
        ):
            raise ValueError(f"Import session {session_id} not found")
        return session

    async def _persist_active_session(self, session: _ActiveSession) -> None:
        validation = (
            session.validation_result.model_dump(mode="json")
            if session.validation_result is not None
            else None
        )
        document = {
            "session_id": session.session_id,
            "tenant_id": session.tenant_id,
            "data_type": session.data_type,
            "source_type": session.source_type,
            "source_name": session.source_name,
            "rows_json": json.dumps(session.rows),
            "columns": session.columns,
            "sample_rows": session.sample_rows,
            "total_rows": session.total_rows,
            "suggested_mapping": session.suggested_mapping,
            "field_mapping": session.field_mapping,
            "validation_result": validation,
            "status": session.status.value,
            "created_at": session.created_at,
            "updated_at": utcnow().isoformat(),
        }
        await self.es_service.index_document(
            ACTIVE_IMPORT_SESSIONS_INDEX, session.session_id, document
        )

    @staticmethod
    def _session_from_document(source: dict[str, Any]) -> _ActiveSession:
        session = _ActiveSession(
            session_id=source["session_id"],
            tenant_id=source.get("tenant_id") or "",
            data_type=source["data_type"],
            source_type=source["source_type"],
            source_name=source["source_name"],
            rows=json.loads(source.get("rows_json") or "[]"),
            columns=list(source.get("columns") or []),
            sample_rows=list(source.get("sample_rows") or []),
            total_rows=int(source.get("total_rows") or 0),
            suggested_mapping=dict(source.get("suggested_mapping") or {}),
        )
        session.field_mapping = dict(source.get("field_mapping") or {})
        validation = source.get("validation_result")
        if validation:
            session.validation_result = ValidationResult.model_validate(validation)
        session.status = ImportStatus(source.get("status", ImportStatus.PARSING.value))
        session.created_at = source.get("created_at") or session.created_at
        return session

    @staticmethod
    def _map_and_coerce_row(
        row: dict[str, str],
        field_mapping: dict[str, str],
        data_type: str,
    ) -> dict[str, Any]:
        template = SchemaTemplates().get_template(data_type)
        fields = {field.name: field for field in template.fields}
        document: dict[str, Any] = {}
        for source_col, target_field in field_mapping.items():
            if source_col not in row or target_field not in fields:
                continue
            raw = row[source_col]
            if raw is None or not str(raw).strip():
                continue
            value = str(raw).strip()
            field_type = fields[target_field].type
            if field_type == FieldType.NUMBER:
                document[target_field] = float(value)
            elif field_type == FieldType.BOOLEAN:
                document[target_field] = value.lower() in {"true", "yes", "1"}
            else:
                document[target_field] = value
        return document

    @staticmethod
    def _complete_inventory_document(
        document: dict[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        """Fill the ``InventoryItem`` fields the import template leaves optional (OI-27).

        The inventory API reads rows as ``InventoryItem``, which requires
        fields a minimal import (``item_id``, ``name``, ``quantity``) leaves
        out. Defaults: category ``general`` (also for an unknown category),
        unit ``units``, min_threshold 0, max_capacity ``max(quantity, 1)``,
        location ``Unassigned``, and a status derived from quantity and
        threshold when missing or not an ``InventoryStatus``. The result is
        validated as ``InventoryItem``; a ``PydanticValidationError`` means
        the row can't be stored (for example an explicit ``max_capacity`` 0).
        """
        from inventory.models import InventoryCategory, InventoryItem, InventoryStatus
        from inventory.service import InventoryService

        doc = dict(document)
        if doc.get("category") not in {c.value for c in InventoryCategory}:
            doc["category"] = InventoryCategory.GENERAL.value
        doc.setdefault("unit", "units")
        doc.setdefault("min_threshold", 0)
        doc.setdefault("max_capacity", max(doc.get("quantity") or 0, 1))
        doc.setdefault("location", "Unassigned")
        if doc.get("status") not in {s.value for s in InventoryStatus}:
            try:
                doc["status"] = InventoryService._derive_status(
                    doc.get("quantity") or 0, doc.get("min_threshold") or 0
                ).value
            except TypeError:
                doc.pop("status", None)  # model validation reports the bad field
        doc["tenant_id"] = tenant_id or doc.get("tenant_id") or ""
        item = InventoryItem.model_validate(doc)
        doc.update(item.model_dump(mode="json", exclude_none=True))
        return doc

    @staticmethod
    def _pydantic_issues(
        row_number: int, exc: PydanticValidationError
    ) -> list[ValidationIssue]:
        issues = []
        for err in exc.errors(include_url=False):
            field = ".".join(str(part) for part in err.get("loc") or ())
            issues.append(
                ValidationIssue(
                    row_number=row_number,
                    field_name=field or "record",
                    description=str(err.get("msg") or "invalid value"),
                )
            )
        return issues

    def _append_inventory_issues(
        self,
        session: _ActiveSession,
        field_mapping: dict[str, str],
        result: ValidationResult,
    ) -> None:
        """Report inventory rows that can't become an ``InventoryItem`` (OI-27)."""
        rows_with_errors = {issue.row_number for issue in result.errors}
        for row_index, row in enumerate(session.rows, start=1):
            if row_index in rows_with_errors:
                continue
            try:
                document = self._map_and_coerce_row(
                    row, field_mapping, session.data_type
                )
                self._complete_inventory_document(
                    document, session.tenant_id or "import"
                )
            except PydanticValidationError as exc:
                result.errors.extend(self._pydantic_issues(row_index, exc))
                rows_with_errors.add(row_index)
            except (TypeError, ValueError) as exc:
                result.errors.append(
                    ValidationIssue(
                        row_number=row_index,
                        field_name="record",
                        description=str(exc),
                    )
                )
                rows_with_errors.add(row_index)
        result.error_count = len(result.errors)
        result.valid_rows = result.total_rows - len(rows_with_errors)

    async def _append_ownership_issues(
        self,
        session: _ActiveSession,
        field_mapping: dict[str, str],
        result: ValidationResult,
    ) -> None:
        """Validate-time copy of the commit-time id ownership check (OI-25).

        Commit refuses a row whose id another tenant (or a legacy row with no
        tenant) already holds. Reporting it here lets the preview show the
        collision before commit. The text is the same generic one commit uses
        and never names the other owner. Commit keeps its own check for rows
        claimed between the two steps.
        """
        if not session.tenant_id:
            return
        id_field = NON_CANONICAL_ID_FIELDS[session.data_type]
        target_index = self.schema_templates.get_index(session.data_type)
        rows_with_errors = {issue.row_number for issue in result.errors}
        for row_index, row in enumerate(session.rows, start=1):
            if row_index in rows_with_errors:
                continue
            try:
                document = self._map_and_coerce_row(
                    row, field_mapping, session.data_type
                )
            except (TypeError, ValueError):
                continue
            doc_id = str(document.get(id_field) or "").strip()
            if not doc_id:
                continue
            existing = await self.es_service.get_document(target_index, doc_id)
            if existing is not None and existing.get("tenant_id") != session.tenant_id:
                result.errors.append(
                    ValidationIssue(
                        row_number=row_index,
                        field_name=id_field,
                        description=f"{id_field} '{doc_id}' is already in use",
                        value=doc_id,
                    )
                )
                rows_with_errors.add(row_index)
        result.error_count = len(result.errors)
        result.valid_rows = result.total_rows - len(rows_with_errors)

    async def _append_canonical_validation_issues(
        self,
        session: _ActiveSession,
        field_mapping: dict[str, str],
        result: ValidationResult,
    ) -> None:
        rows_with_errors = {issue.row_number for issue in result.errors}
        for row_index, row in enumerate(session.rows, start=1):
            if row_index in rows_with_errors:
                continue
            try:
                document = self._map_and_coerce_row(
                    row, field_mapping, session.data_type
                )
                self._validate_canonical_document(
                    session.data_type, document, tenant_id=session.tenant_id
                )
                if session.data_type == "customer_tanks" and session.tenant_id:
                    issue = await self._customer_ref_issue(
                        session.tenant_id, row_index, document
                    )
                    if issue is not None:
                        result.errors.append(issue)
                        rows_with_errors.add(row_index)
                        continue
                if session.data_type == "orders" and session.tenant_id:
                    issue = await self._customer_tank_issue(
                        session.tenant_id, row_index, document
                    )
                    if issue is not None:
                        result.errors.append(issue)
                        rows_with_errors.add(row_index)
                        continue
                if (
                    session.data_type == "tank_readings"
                    and self._tank_import_service is not None
                    and session.tenant_id
                ):
                    await self._tank_import_service.validate_reading(
                        session.tenant_id, document
                    )
            except PydanticValidationError as exc:
                # One readable issue per field. ``include_url=False`` and
                # dropping ``input`` keep pydantic doc links and the
                # placeholder ids out of the message (B7).
                for err in exc.errors(include_url=False):
                    field = ".".join(str(part) for part in err.get("loc") or ())
                    message = str(err.get("msg") or "invalid value")
                    if message.startswith("Value error, "):
                        message = message[len("Value error, "):]
                    result.errors.append(
                        ValidationIssue(
                            row_number=row_index,
                            field_name=field or "record",
                            description=message,
                        )
                    )
                rows_with_errors.add(row_index)
            except _RowFieldError as exc:
                result.errors.append(
                    ValidationIssue(
                        row_number=row_index,
                        field_name=exc.field_name,
                        description=str(exc),
                    )
                )
                rows_with_errors.add(row_index)
            except Exception as exc:
                result.errors.append(
                    ValidationIssue(
                        row_number=row_index,
                        field_name="record",
                        description=str(exc),
                    )
                )
                rows_with_errors.add(row_index)
        result.error_count = len(result.errors)
        result.valid_rows = result.total_rows - len(rows_with_errors)

    async def _customer_ref_issue(
        self,
        tenant_id: str,
        row_number: int,
        document: dict[str, Any],
    ) -> Optional[ValidationIssue]:
        """Validate-time copy of the commit-time customer check (B1 / F7).

        Uses the same resolver choice as ``TankImportService._validate_customer``
        so ``/api/import/validate`` reports ``customer_not_found`` rows instead
        of letting them fail only at commit.
        """
        from errors.exceptions import AppException
        from fuel.services.customer_ref import validate_customer_ref
        from services.ref_resolver import get_ref_resolver

        resolver = (
            getattr(self._tank_import_service, "_ref_resolver", None)
            or get_ref_resolver()
        )
        customer_id = document.get("customer_id")
        # Commit skips the lookup when an existing tank keeps its customer, so
        # validate does too; otherwise the two steps would disagree.
        tanks = getattr(self._tank_import_service, "_tanks", None)
        source_system = str(document.get("source_system") or "").strip()
        external_tank_id = str(document.get("external_tank_id") or "").strip()
        if tanks is not None and source_system and external_tank_id:
            existing = await tanks.get_by_external_id(
                tenant_id, source_system, external_tank_id
            )
            if existing is not None and existing.customer_id == customer_id:
                return None
        try:
            await validate_customer_ref(resolver, tenant_id, customer_id)
        except AppException as exc:
            return ValidationIssue(
                row_number=row_number,
                field_name="customer_id",
                description=exc.message,
                value=str(customer_id),
            )
        return None

    async def _customer_tank_issue(
        self,
        tenant_id: str,
        row_number: int,
        document: dict[str, Any],
    ) -> Optional[ValidationIssue]:
        """Validate-time copy of the intake pipeline's tank check (OI-26).

        Commit refuses an order whose ``customer_tank_id`` is missing from the
        tenant or belongs to another customer. Running the same check here
        lets the preview report it before commit.
        """
        from errors.exceptions import AppException

        tank_id = document.get("customer_tank_id")
        verify = getattr(self._order_intake_pipeline, "verify_customer_tank", None)
        if not tank_id or verify is None:
            return None
        try:
            await verify(document, tenant_id)
        except AppException as exc:
            return ValidationIssue(
                row_number=row_number,
                field_name="customer_tank_id",
                description=exc.message,
                value=str(tank_id),
            )
        return None

    @staticmethod
    def _validate_canonical_document(
        data_type: str,
        document: dict[str, Any],
        tenant_id: str = "",
    ) -> None:
        if data_type == "orders":
            from fuel.api.order_endpoints import BulkOrderRow

            business = {
                key: value
                for key, value in document.items()
                if key not in {"source_system", "source_order_id", "source_updated_at"}
            }
            BulkOrderRow.model_validate(business)
            if not business.get("fill_to_full") and business.get(
                "gallons_requested"
            ) is None:
                raise ValueError(
                    "gallons_requested is required unless fill_to_full is true"
                )
            if business.get("call_type") == "one_off" and (
                not business.get("delivery_window_start")
                or not business.get("delivery_window_end")
            ):
                raise ValueError(
                    "one_off orders require delivery_window_start and delivery_window_end"
                )
            return

        if data_type == "customer_tanks":
            from fuel.customer_tank_models import CustomerTank

            tank = CustomerTank.model_validate(
                {
                    **document,
                    "customer_tank_id": document.get("customer_tank_id")
                    or "import-tank",
                    "tenant_id": tenant_id or "import",
                }
            )
            _check_fuel_family(tank.fuel_type, tank.fuel_product_code)
            return

        if data_type == "tank_readings":
            volume = float(document["volume_gallons"])
            if volume < 0:
                raise ValueError("volume_gallons cannot be negative")
            datetime.fromisoformat(
                str(document["reading_at"]).replace("Z", "+00:00")
            )

    async def _commit_tenant_documents(
        self,
        *,
        session: _ActiveSession,
        target_index: str,
        documents: list[tuple[int, dict[str, Any]]],
        tenant: Any,
    ) -> tuple[int, int, list[str]]:
        """Write non-canonical rows stamped with the tenant, never across tenants (C1).

        Document ids are global in the store, so a blind upsert of an id another
        tenant owns would replace that tenant's row. Each row is instead:

        - updated in place if the importing tenant already owns that id,
        - created with ``create_document`` (insert-if-absent) if the id is free,
        - a per-row error if any other owner (or a legacy row with no tenant)
          holds the id. The error doesn't name the other owner.

        Freeze decision: if the tenant's own row is deleted and another tenant
        recreates the id between the read and the update, the update wins. That
        window is accepted rather than adding a new store primitive.
        """
        tenant_id = (
            getattr(tenant, "tenant_id", None)
            if tenant is not None
            else session.tenant_id
        )
        if not tenant_id:
            raise ValueError("tenant context is required for imports")
        if session.tenant_id and session.tenant_id != tenant_id:
            raise ValueError(f"Import session {session.session_id} not found")

        id_field = NON_CANONICAL_ID_FIELDS.get(session.data_type, "id")
        imported = 0
        failed = 0
        errors: list[str] = []
        for row_number, doc in documents:
            doc["tenant_id"] = tenant_id
            doc_id = str(doc.get(id_field) or "").strip()
            if not doc_id:
                failed += 1
                errors.append(f"row {row_number}: missing {id_field}")
                continue
            in_use = f"row {row_number}: {id_field} '{doc_id}' is already in use"
            if session.data_type == "inventory":
                try:
                    doc = self._complete_inventory_document(doc, tenant_id)
                except (PydanticValidationError, TypeError, ValueError) as exc:
                    failed += 1
                    if isinstance(exc, PydanticValidationError):
                        detail = "; ".join(
                            f"{issue.field_name}: {issue.description}"
                            for issue in self._pydantic_issues(row_number, exc)
                        )
                    else:
                        detail = str(exc)
                    errors.append(f"row {row_number}: {detail}")
                    continue
            try:
                existing = await self.es_service.get_document(target_index, doc_id)
                if existing is not None:
                    if existing.get("tenant_id") != tenant_id:
                        failed += 1
                        errors.append(in_use)
                        continue
                    await self.es_service.index_document(target_index, doc_id, doc)
                    imported += 1
                    continue
                if await self.es_service.create_document(target_index, doc_id, doc):
                    imported += 1
                    continue
                # Lost a create race, or the index takes no writes.
                current = await self.es_service.get_document(target_index, doc_id)
                if current is None:
                    failed += 1
                    errors.append(f"row {row_number}: could not be written")
                elif current.get("tenant_id") == tenant_id:
                    await self.es_service.index_document(target_index, doc_id, doc)
                    imported += 1
                else:
                    failed += 1
                    errors.append(in_use)
            except Exception as exc:
                logger.error(
                    "Import row write failed: session=%s row=%d: %s",
                    session.session_id,
                    row_number,
                    exc,
                )
                failed += 1
                errors.append(f"row {row_number}: {exc}")
        return imported, failed, errors

    async def _commit_canonical_documents(
        self,
        *,
        session: _ActiveSession,
        documents: list[tuple[int, dict[str, Any]]],
        tenant: Any,
    ) -> tuple[int, int, int, list[str]]:
        tenant_id = (
            getattr(tenant, "tenant_id", None)
            if tenant is not None
            else session.tenant_id
        )
        if not tenant_id:
            raise ValueError("tenant context is required for canonical imports")
        if session.tenant_id and session.tenant_id != tenant_id:
            raise ValueError(f"Import session {session.session_id} not found")

        imported = 0
        failed = 0
        skipped = 0
        errors: list[str] = []
        for row_number, document in documents:
            try:
                if session.data_type == "orders":
                    if self._order_intake_pipeline is None:
                        raise RuntimeError("canonical order importer is not configured")
                    source_system = str(document["source_system"]).strip()
                    source_order_id = str(document["source_order_id"]).strip()
                    event_id = f"csv:{source_system}:{source_order_id}"
                    source_version = str(
                        document.get("source_updated_at") or ""
                    ).strip()
                    if source_version:
                        event_id = f"{event_id}:{source_version}"
                    result = await self._order_intake_pipeline.ingest_csv(
                        tenant=tenant
                        or {"tenant_id": tenant_id, "user_id": "import-service"},
                        payload=document,
                        request_id=f"import:{session.session_id}:row:{row_number}",
                        client_event_id=event_id,
                        import_batch_id=session.session_id,
                        csv_row_number=row_number,
                    )
                    if result.status == "processed":
                        imported += 1
                    elif result.status == "duplicate":
                        skipped += 1
                    else:
                        raise RuntimeError(
                            f"order intake returned status {result.status}"
                        )
                else:
                    if self._tank_import_service is None:
                        raise RuntimeError("canonical tank importer is not configured")
                    if session.data_type == "customer_tanks":
                        await self._tank_import_service.import_tank(
                            tenant_id, document
                        )
                        imported += 1
                    else:
                        outcome = await self._tank_import_service.import_reading(
                            tenant_id, document
                        )
                        if outcome.status == "duplicate":
                            skipped += 1
                        else:
                            imported += 1
            except Exception as exc:
                failed += 1
                errors.append(f"row {row_number}: {exc}")
        return imported, failed, skipped, errors

    async def _persist_session_record(
        self,
        session: _ActiveSession,
        result: ImportResult,
        duration: float,
    ) -> None:
        """Persist an ImportSessionRecord to the import_sessions ES index."""
        record = ImportSessionRecord(
            session_id=session.session_id,
            tenant_id=session.tenant_id or None,
            data_type=session.data_type,
            source_type=session.source_type,
            source_name=session.source_name,
            total_records=result.total_records,
            imported_records=result.imported_records,
            skipped_records=result.skipped_records,
            error_count=result.error_count,
            status=result.status,
            errors=result.errors,
            field_mapping=session.field_mapping,
            created_at=session.created_at,
            completed_at=utcnow().isoformat(),
            duration_seconds=round(duration, 3),
        )

        try:
            await self.es_service.index_document(
                "import_sessions",
                session.session_id,
                record.model_dump(),
            )
            logger.info(
                "Persisted session record: session=%s", session.session_id
            )
        except Exception as exc:
            logger.error(
                "Failed to persist session record %s: %s",
                session.session_id,
                exc,
            )
