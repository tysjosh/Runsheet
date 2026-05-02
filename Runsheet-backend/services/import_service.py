"""Import service for the data import/migration tool.

Orchestrates the import workflow: CSV/Sheets parsing, field mapping,
validation, bulk indexing, and session history. Holds in-memory session
state for active imports and persists completed sessions to Elasticsearch.
"""

import csv
import io
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any, Optional
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

from services.elasticsearch_service import ElasticsearchService
from services.field_mapper import FieldMapper
from services.import_models import (
    ImportResult,
    ImportSessionRecord,
    ImportStatus,
    ParseResult,
    ValidationResult,
)
from services.schema_templates import SchemaTemplates
from services.validation_engine import ValidationEngine

logger = logging.getLogger(__name__)


class _ActiveSession:
    """In-memory state for an active import session."""

    __slots__ = (
        "session_id",
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
        self.created_at: str = datetime.utcnow().isoformat() + "Z"


class ImportService:
    """Orchestrates the data import workflow.

    Manages in-memory sessions for active imports and delegates to
    ``SchemaTemplates``, ``ValidationEngine``, ``FieldMapper``, and
    ``ElasticsearchService`` for the heavy lifting.
    """

    def __init__(self, es_service: ElasticsearchService):
        self.es_service = es_service
        self.schema_templates = SchemaTemplates()
        self.validation_engine = ValidationEngine(self.schema_templates)
        self.field_mapper = FieldMapper(self.schema_templates)
        self._active_sessions: dict[str, _ActiveSession] = {}

    # ------------------------------------------------------------------
    # CSV parsing
    # ------------------------------------------------------------------

    async def parse_csv(self, file_content: bytes, data_type: str) -> ParseResult:
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
            data_type=data_type,
            source_type="csv",
            source_name="uploaded.csv",
            rows=rows,
            columns=columns,
            sample_rows=sample_rows,
            total_rows=total_rows,
            suggested_mapping=suggested_mapping,
        )
        self._active_sessions[session_id] = session

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

    async def parse_sheets(self, url: str, data_type: str) -> ParseResult:
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
        result = await self.parse_csv(file_content, data_type)

        # Update session metadata to reflect Google Sheets source
        session = self._active_sessions[result.session_id]
        session.source_type = "google_sheets"
        session.source_name = url

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
        self, session_id: str, field_mapping: dict[str, str]
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
        session = self._get_active_session(session_id)

        session.field_mapping = field_mapping
        session.status = ImportStatus.VALIDATING

        result = self.validation_engine.validate_rows(
            rows=session.rows,
            data_type=session.data_type,
            field_mapping=field_mapping,
        )
        # Stamp the session_id onto the result
        result.session_id = session_id

        session.validation_result = result
        session.status = ImportStatus.VALIDATED

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
        self, session_id: str, skip_errors: bool = False
    ) -> ImportResult:
        """Commit validated records to Elasticsearch.

        1. Retrieve session and validation result.
        2. If skip_errors, filter out rows that had errors.
        3. Apply field mapping to transform rows to target field names.
        4. Bulk index via ``es_service.bulk_index_documents()``.
        5. Record timing.
        6. Persist ``ImportSessionRecord`` to ``import_sessions`` index.
        7. Return ``ImportResult``.

        Args:
            session_id: The active session identifier.
            skip_errors: If True, skip rows that had validation errors.

        Returns:
            ImportResult with counts and status.

        Raises:
            ValueError: If the session is not found or has not been validated.
        """
        session = self._get_active_session(session_id)

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

        # Build target documents by applying field mapping
        target_index = self.schema_templates.get_index(session.data_type)
        documents: list[dict[str, Any]] = []
        skipped = 0

        for row_idx, row in enumerate(session.rows):
            row_number = row_idx + 1
            if row_number in error_row_numbers:
                skipped += 1
                continue

            # Apply field mapping: source column -> target field
            doc: dict[str, Any] = {}
            for source_col, target_field in session.field_mapping.items():
                if source_col in row:
                    doc[target_field] = row[source_col]
            documents.append(doc)

        # Bulk index
        import_errors: list[str] = []
        imported = 0
        failed = 0

        if documents:
            try:
                bulk_result = await self.es_service.bulk_index_documents(
                    target_index, documents
                )
                imported = bulk_result.get("successful", 0)
                failed = bulk_result.get("failed", 0)
                for err in bulk_result.get("errors", []):
                    import_errors.append(str(err))
            except Exception as exc:
                logger.error(
                    "Bulk indexing failed for session %s: %s",
                    session_id,
                    exc,
                )
                import_errors.append(str(exc))
                failed = len(documents)

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

    async def get_session(self, session_id: str) -> Optional[ImportSessionRecord]:
        """Fetch a single import session record from ES.

        Args:
            session_id: The session identifier.

        Returns:
            ImportSessionRecord if found, else None.
        """
        query: dict[str, Any] = {
            "query": {"term": {"session_id": session_id}},
        }

        try:
            response = await self.es_service.search_documents(
                "import_sessions", query, size=1
            )
            hits = response.get("hits", {}).get("hits", [])
            if hits:
                return ImportSessionRecord(**hits[0]["_source"])
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

    def _get_active_session(self, session_id: str) -> _ActiveSession:
        """Retrieve an active session or raise ValueError."""
        session = self._active_sessions.get(session_id)
        if session is None:
            raise ValueError(f"Import session {session_id} not found")
        return session

    async def _persist_session_record(
        self,
        session: _ActiveSession,
        result: ImportResult,
        duration: float,
    ) -> None:
        """Persist an ImportSessionRecord to the import_sessions ES index."""
        record = ImportSessionRecord(
            session_id=session.session_id,
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
            completed_at=datetime.utcnow().isoformat() + "Z",
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
