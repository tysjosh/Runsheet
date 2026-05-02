"""
Import API endpoints for Runsheet Logistics Platform.

Provides endpoints for the data import/migration workflow:
CSV upload, Google Sheets import, field mapping validation,
bulk commit, import history, schema templates, and CSV template downloads.

Requirements: 3.3, 3.4, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6
"""

import io
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config.settings import get_settings
from middleware.rate_limiter import limiter
from services.elasticsearch_service import elasticsearch_service
from services.import_models import (
    ImportResult,
    ImportSessionRecord,
    ParseResult,
    ValidationResult,
)
from services.import_service import ImportService
from services.schema_templates import SchemaTemplate, SchemaTemplates

logger = logging.getLogger(__name__)

settings = get_settings()

router = APIRouter(prefix="/api/import")

# Module-level service instances
import_service = ImportService(elasticsearch_service)
schema_templates = SchemaTemplates()

# Max upload file size: 10 MB
MAX_FILE_SIZE = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Request body models
# ---------------------------------------------------------------------------

class SheetsUploadRequest(BaseModel):
    url: str
    data_type: str


class ValidateRequest(BaseModel):
    session_id: str
    field_mapping: dict[str, str]


class CommitRequest(BaseModel):
    session_id: str
    skip_errors: bool = False


# ---------------------------------------------------------------------------
# Upload endpoints
# ---------------------------------------------------------------------------

@router.post("/upload/csv", response_model=ParseResult)
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def upload_csv(
    request: Request,
    file: UploadFile = File(...),
    data_type: str = Form(...),
) -> ParseResult:
    """Upload a CSV file for import.

    Validates file size (≤10MB) and extension (.csv), parses the CSV,
    and returns column names, sample rows, and a suggested field mapping.

    Requirements: 3.3, 3.4, 11.1
    """
    # Validate file extension
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=400,
            detail="Only CSV files are supported. Please select a .csv file.",
        )

    # Read file content and validate size
    file_content = await file.read()
    if len(file_content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail="File exceeds the 10MB size limit. Please split your data or reduce the file size.",
        )

    try:
        result = await import_service.parse_csv(file_content, data_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return result


@router.post("/upload/sheets", response_model=ParseResult)
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def upload_sheets(
    request: Request,
    body: SheetsUploadRequest,
) -> ParseResult:
    """Import data from a Google Sheets URL.

    Fetches the sheet as CSV, parses it, and returns column names,
    sample rows, and a suggested field mapping.

    Requirements: 11.2
    """
    try:
        result = await import_service.parse_sheets(body.url, body.data_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return result


# ---------------------------------------------------------------------------
# Validation endpoint
# ---------------------------------------------------------------------------

@router.post("/validate", response_model=ValidationResult)
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def validate_import(
    request: Request,
    body: ValidateRequest,
) -> ValidationResult:
    """Validate mapped data against the schema template.

    Accepts a session ID and field mapping, runs validation, and returns
    a preview report with per-row errors and warnings.

    Requirements: 11.3
    """
    try:
        result = await import_service.validate(body.session_id, body.field_mapping)
    except ValueError as exc:
        detail = str(exc)
        if "not found" in detail.lower():
            raise HTTPException(status_code=404, detail=detail)
        raise HTTPException(status_code=409, detail=detail)

    return result


# ---------------------------------------------------------------------------
# Commit endpoint
# ---------------------------------------------------------------------------

@router.post("/commit", response_model=ImportResult)
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def commit_import(
    request: Request,
    body: CommitRequest,
) -> ImportResult:
    """Commit validated records to Elasticsearch.

    Indexes the validated (and optionally error-filtered) records into
    the appropriate ES index and returns the import result.

    Requirements: 11.4
    """
    try:
        result = await import_service.commit(body.session_id, body.skip_errors)
    except ValueError as exc:
        detail = str(exc)
        if "not found" in detail.lower():
            raise HTTPException(status_code=404, detail=detail)
        if "not been validated" in detail.lower():
            raise HTTPException(status_code=409, detail=detail)
        raise HTTPException(status_code=422, detail=detail)

    return result


# ---------------------------------------------------------------------------
# History endpoints
# ---------------------------------------------------------------------------

@router.get("/history", response_model=list[ImportSessionRecord])
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_import_history(
    request: Request,
    data_type: Optional[str] = None,
    status: Optional[str] = None,
) -> list[ImportSessionRecord]:
    """List import sessions with optional filters.

    Returns sessions in reverse chronological order (most recent first).

    Requirements: 11.5
    """
    return await import_service.get_history(data_type=data_type, status=status)


@router.get("/history/{session_id}", response_model=ImportSessionRecord)
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_import_session(
    request: Request,
    session_id: str,
) -> ImportSessionRecord:
    """Retrieve a single import session by ID.

    Requirements: 11.6
    """
    record = await import_service.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Import session {session_id} not found",
        )
    return record


# ---------------------------------------------------------------------------
# Template and schema endpoints
# ---------------------------------------------------------------------------

@router.get("/templates/{data_type}")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def download_template(
    request: Request,
    data_type: str,
) -> StreamingResponse:
    """Download a CSV template for the given data type.

    Returns a CSV file with headers matching the schema template
    and 2-3 example rows.

    Requirements: 9.1, 9.2, 9.3
    """
    try:
        csv_content = await import_service.generate_template(data_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return StreamingResponse(
        io.StringIO(csv_content),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{data_type}_template.csv"',
        },
    )


@router.get("/schemas/{data_type}", response_model=SchemaTemplate)
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_schema(
    request: Request,
    data_type: str,
) -> SchemaTemplate:
    """Get the schema template for a data type.

    Returns the field definitions, descriptions, and metadata
    for the specified data type.
    """
    try:
        template = schema_templates.get_template(data_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return template
