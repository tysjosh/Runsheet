"""Pydantic models for the data import/migration workflow.

Defines enums and data models used across the import pipeline:
parsing, field mapping, validation, import execution, and session history.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from services.schema_templates import FieldType


class DataTypeEnum(str, Enum):
    """Supported data types for import operations."""
    FLEET = "fleet"
    ORDERS = "orders"
    RIDERS = "riders"
    FUEL_STATIONS = "fuel_stations"
    INVENTORY = "inventory"
    SUPPORT_TICKETS = "support_tickets"
    JOBS = "jobs"


class ImportStatus(str, Enum):
    """Status values for an import session lifecycle."""
    PARSING = "parsing"
    MAPPED = "mapped"
    VALIDATING = "validating"
    VALIDATED = "validated"
    IMPORTING = "importing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class ParseResult(BaseModel):
    """Result of parsing a CSV file or Google Sheets source."""
    session_id: str
    columns: list[str]
    sample_rows: list[dict[str, str]]  # First 5 rows
    total_rows: int
    suggested_mapping: dict[str, str]  # Auto-suggested field mapping


class ValidationIssue(BaseModel):
    """A single validation error or warning for a specific row and field."""
    row_number: int
    field_name: str
    description: str
    value: Optional[str] = None


class ValidationResult(BaseModel):
    """Aggregated result of validating all rows against the schema template."""
    session_id: str
    total_rows: int
    valid_rows: int
    error_count: int
    warning_count: int
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]


class ImportResult(BaseModel):
    """Result of committing validated records to Elasticsearch."""
    session_id: str
    status: ImportStatus
    total_records: int
    imported_records: int
    skipped_records: int
    error_count: int
    errors: list[str]
    data_type: str
    es_index: str
    duration_seconds: float


class ImportSessionRecord(BaseModel):
    """Persisted to the import_sessions ES index."""
    session_id: str
    data_type: str
    source_type: str  # "csv" or "google_sheets"
    source_name: str  # filename or URL
    total_records: int
    imported_records: int
    skipped_records: int
    error_count: int
    status: ImportStatus
    errors: list[str]
    field_mapping: dict[str, str]
    created_at: str  # ISO8601
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
