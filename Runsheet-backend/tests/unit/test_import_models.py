"""Unit tests for import_models.py — Pydantic models for the import workflow."""

import pytest
from pydantic import ValidationError

from services.import_models import (
    DataTypeEnum,
    ImportResult,
    ImportSessionRecord,
    ImportStatus,
    ParseResult,
    ValidationIssue,
    ValidationResult,
)
from services.schema_templates import FieldType


class TestDataTypeEnum:
    """Tests for DataTypeEnum values (Requirement 2.1)."""

    def test_all_seven_data_types_present(self):
        expected = {
            "fleet", "orders", "riders", "fuel_stations",
            "inventory", "support_tickets", "jobs",
        }
        assert {dt.value for dt in DataTypeEnum} == expected

    def test_enum_is_str_subclass(self):
        assert isinstance(DataTypeEnum.FLEET, str)
        assert DataTypeEnum.FLEET == "fleet"


class TestImportStatus:
    """Tests for ImportStatus values."""

    def test_all_statuses_present(self):
        expected = {
            "parsing", "mapped", "validating", "validated",
            "importing", "completed", "partial", "failed",
        }
        assert {s.value for s in ImportStatus} == expected

    def test_enum_is_str_subclass(self):
        assert isinstance(ImportStatus.COMPLETED, str)
        assert ImportStatus.COMPLETED == "completed"


class TestFieldTypeReuse:
    """FieldType should be imported from schema_templates, not redefined."""

    def test_field_type_is_same_class(self):
        from services.import_models import FieldType as ImportedFieldType
        assert ImportedFieldType is FieldType


class TestParseResult:
    """Tests for ParseResult model (Requirement 6.3)."""

    def test_valid_parse_result(self):
        pr = ParseResult(
            session_id="sess-001",
            columns=["col_a", "col_b"],
            sample_rows=[{"col_a": "1", "col_b": "2"}],
            total_rows=100,
            suggested_mapping={"col_a": "field_a"},
        )
        assert pr.session_id == "sess-001"
        assert pr.columns == ["col_a", "col_b"]
        assert pr.total_rows == 100
        assert len(pr.sample_rows) == 1

    def test_empty_columns_and_rows(self):
        pr = ParseResult(
            session_id="sess-002",
            columns=[],
            sample_rows=[],
            total_rows=0,
            suggested_mapping={},
        )
        assert pr.columns == []
        assert pr.sample_rows == []

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            ParseResult(
                session_id="sess-003",
                columns=["a"],
                sample_rows=[],
                # total_rows missing
                suggested_mapping={},
            )


class TestValidationIssue:
    """Tests for ValidationIssue model (Requirement 6.4)."""

    def test_with_value(self):
        vi = ValidationIssue(
            row_number=5,
            field_name="status",
            description="Invalid enum value",
            value="unknown_status",
        )
        assert vi.row_number == 5
        assert vi.value == "unknown_status"

    def test_without_value(self):
        vi = ValidationIssue(
            row_number=3,
            field_name="truck_id",
            description="Required field missing",
        )
        assert vi.value is None

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            ValidationIssue(
                row_number=1,
                # field_name missing
                description="Some error",
            )


class TestValidationResult:
    """Tests for ValidationResult model (Requirement 6.3)."""

    def test_valid_result(self):
        vr = ValidationResult(
            session_id="sess-001",
            total_rows=50,
            valid_rows=45,
            error_count=7,
            warning_count=2,
            errors=[
                ValidationIssue(row_number=1, field_name="f", description="err"),
            ],
            warnings=[],
        )
        assert vr.total_rows == 50
        assert vr.valid_rows == 45
        assert len(vr.errors) == 1
        assert len(vr.warnings) == 0


class TestImportResult:
    """Tests for ImportResult model (Requirement 7.4)."""

    def test_completed_import(self):
        ir = ImportResult(
            session_id="sess-001",
            status=ImportStatus.COMPLETED,
            total_records=100,
            imported_records=100,
            skipped_records=0,
            error_count=0,
            errors=[],
            data_type="fleet",
            es_index="trucks",
            duration_seconds=2.5,
        )
        assert ir.status == ImportStatus.COMPLETED
        assert ir.imported_records == 100

    def test_partial_import(self):
        ir = ImportResult(
            session_id="sess-002",
            status=ImportStatus.PARTIAL,
            total_records=50,
            imported_records=40,
            skipped_records=10,
            error_count=10,
            errors=["Row 5: missing truck_id"],
            data_type="fleet",
            es_index="trucks",
            duration_seconds=1.2,
        )
        assert ir.status == ImportStatus.PARTIAL
        assert ir.skipped_records == 10


class TestImportSessionRecord:
    """Tests for ImportSessionRecord model (Requirement 8.2)."""

    def test_minimal_record(self):
        rec = ImportSessionRecord(
            session_id="sess-001",
            data_type="orders",
            source_type="csv",
            source_name="orders.csv",
            total_records=200,
            imported_records=195,
            skipped_records=5,
            error_count=5,
            status=ImportStatus.PARTIAL,
            errors=["Row 10: bad date"],
            field_mapping={"Order ID": "order_id"},
            created_at="2024-03-15T10:00:00Z",
        )
        assert rec.completed_at is None
        assert rec.duration_seconds is None

    def test_full_record(self):
        rec = ImportSessionRecord(
            session_id="sess-002",
            data_type="jobs",
            source_type="google_sheets",
            source_name="https://docs.google.com/spreadsheets/d/abc",
            total_records=30,
            imported_records=30,
            skipped_records=0,
            error_count=0,
            status=ImportStatus.COMPLETED,
            errors=[],
            field_mapping={"Job ID": "job_id"},
            created_at="2024-03-15T10:00:00Z",
            completed_at="2024-03-15T10:02:00Z",
            duration_seconds=120.0,
        )
        assert rec.completed_at == "2024-03-15T10:02:00Z"
        assert rec.duration_seconds == 120.0

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            ImportSessionRecord(
                session_id="sess-003",
                data_type="fleet",
                source_type="csv",
                # source_name missing
                total_records=10,
                imported_records=10,
                skipped_records=0,
                error_count=0,
                status=ImportStatus.COMPLETED,
                errors=[],
                field_mapping={},
                created_at="2024-03-15T10:00:00Z",
            )
