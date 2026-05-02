"""Unit tests for the ValidationEngine class.

Tests cover required field presence detection, data type validation
(string, number, date, enum, boolean, geo_point), field mapping application,
and correct count computation in ValidationResult.
"""

import pytest

from services.schema_templates import SchemaTemplates
from services.validation_engine import ValidationEngine


@pytest.fixture
def engine():
    """Create a ValidationEngine with real SchemaTemplates."""
    return ValidationEngine(SchemaTemplates())


class TestFieldMappingApplication:
    """Verify that field mapping correctly transforms source columns to target fields."""

    def test_mapped_fields_are_validated(self, engine: ValidationEngine):
        """Source columns mapped to target fields should be validated against the schema."""
        rows = [{"Vehicle ID": "TRK-001", "Plate": "ABC-123", "Status": "on_time"}]
        mapping = {"Vehicle ID": "truck_id", "Plate": "plate_number", "Status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        assert result.valid_rows == 1
        assert result.error_count == 0

    def test_unmapped_source_columns_are_ignored(self, engine: ValidationEngine):
        """Source columns not in the mapping should not cause errors."""
        rows = [{"truck_id": "TRK-001", "plate_number": "ABC-123", "status": "on_time", "extra_col": "ignored"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        assert result.valid_rows == 1
        assert result.error_count == 0


class TestRequiredFieldPresence:
    """Verify that missing or empty required fields produce errors."""

    def test_missing_required_field_produces_error(self, engine: ValidationEngine):
        """A row missing a required field should produce an error."""
        rows = [{"plate_number": "ABC-123", "status": "on_time"}]
        # truck_id is required but not mapped
        mapping = {"plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        assert result.error_count >= 1
        assert any("truck_id" in e.field_name for e in result.errors)

    def test_empty_required_field_produces_error(self, engine: ValidationEngine):
        """A row with an empty required field should produce an error."""
        rows = [{"truck_id": "", "plate_number": "ABC-123", "status": "on_time"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        assert result.error_count >= 1
        assert any("truck_id" in e.field_name for e in result.errors)

    def test_whitespace_only_required_field_produces_error(self, engine: ValidationEngine):
        """A required field with only whitespace should be treated as empty."""
        rows = [{"truck_id": "   ", "plate_number": "ABC-123", "status": "on_time"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        assert result.error_count >= 1

    def test_all_required_fields_present_no_errors(self, engine: ValidationEngine):
        """A row with all required fields present and valid should have no errors."""
        rows = [{"truck_id": "TRK-001", "plate_number": "ABC-123", "status": "on_time"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        assert result.error_count == 0
        assert result.valid_rows == 1


class TestNumberValidation:
    """Verify number type validation."""

    def test_integer_is_valid(self, engine: ValidationEngine):
        rows = [{"item_id": "INV-1", "name": "Widget", "quantity": "42"}]
        mapping = {"item_id": "item_id", "name": "name", "quantity": "quantity"}
        result = engine.validate_rows(rows, "inventory", mapping)
        number_errors = [e for e in result.errors if e.field_name == "quantity"]
        assert len(number_errors) == 0

    def test_float_is_valid(self, engine: ValidationEngine):
        rows = [{"item_id": "INV-1", "name": "Widget", "quantity": "3.14"}]
        mapping = {"item_id": "item_id", "name": "name", "quantity": "quantity"}
        result = engine.validate_rows(rows, "inventory", mapping)
        number_errors = [e for e in result.errors if e.field_name == "quantity"]
        assert len(number_errors) == 0

    def test_negative_number_is_valid(self, engine: ValidationEngine):
        rows = [{"item_id": "INV-1", "name": "Widget", "quantity": "-10"}]
        mapping = {"item_id": "item_id", "name": "name", "quantity": "quantity"}
        result = engine.validate_rows(rows, "inventory", mapping)
        number_errors = [e for e in result.errors if e.field_name == "quantity"]
        assert len(number_errors) == 0

    def test_non_numeric_string_is_invalid(self, engine: ValidationEngine):
        rows = [{"item_id": "INV-1", "name": "Widget", "quantity": "abc"}]
        mapping = {"item_id": "item_id", "name": "name", "quantity": "quantity"}
        result = engine.validate_rows(rows, "inventory", mapping)
        number_errors = [e for e in result.errors if e.field_name == "quantity"]
        assert len(number_errors) == 1


class TestDateValidation:
    """Verify date type validation."""

    def test_iso8601_with_z_is_valid(self, engine: ValidationEngine):
        rows = [{"truck_id": "T1", "plate_number": "P1", "status": "on_time", "estimated_arrival": "2024-01-15T14:30:00Z"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status", "estimated_arrival": "estimated_arrival"}
        result = engine.validate_rows(rows, "fleet", mapping)
        date_errors = [e for e in result.errors if e.field_name == "estimated_arrival"]
        assert len(date_errors) == 0

    def test_yyyy_mm_dd_is_valid(self, engine: ValidationEngine):
        rows = [{"truck_id": "T1", "plate_number": "P1", "status": "on_time", "estimated_arrival": "2024-01-15"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status", "estimated_arrival": "estimated_arrival"}
        result = engine.validate_rows(rows, "fleet", mapping)
        date_errors = [e for e in result.errors if e.field_name == "estimated_arrival"]
        assert len(date_errors) == 0

    def test_mm_dd_yyyy_is_valid(self, engine: ValidationEngine):
        rows = [{"truck_id": "T1", "plate_number": "P1", "status": "on_time", "estimated_arrival": "01/15/2024"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status", "estimated_arrival": "estimated_arrival"}
        result = engine.validate_rows(rows, "fleet", mapping)
        date_errors = [e for e in result.errors if e.field_name == "estimated_arrival"]
        assert len(date_errors) == 0

    def test_dd_mm_yyyy_is_valid(self, engine: ValidationEngine):
        rows = [{"truck_id": "T1", "plate_number": "P1", "status": "on_time", "estimated_arrival": "15/01/2024"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status", "estimated_arrival": "estimated_arrival"}
        result = engine.validate_rows(rows, "fleet", mapping)
        date_errors = [e for e in result.errors if e.field_name == "estimated_arrival"]
        assert len(date_errors) == 0

    def test_invalid_date_string_produces_error(self, engine: ValidationEngine):
        rows = [{"truck_id": "T1", "plate_number": "P1", "status": "on_time", "estimated_arrival": "not-a-date"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status", "estimated_arrival": "estimated_arrival"}
        result = engine.validate_rows(rows, "fleet", mapping)
        date_errors = [e for e in result.errors if e.field_name == "estimated_arrival"]
        assert len(date_errors) == 1


class TestEnumValidation:
    """Verify enum type validation."""

    def test_valid_enum_value_passes(self, engine: ValidationEngine):
        rows = [{"truck_id": "T1", "plate_number": "P1", "status": "on_time"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        enum_errors = [e for e in result.errors if e.field_name == "status"]
        assert len(enum_errors) == 0

    def test_invalid_enum_value_produces_error(self, engine: ValidationEngine):
        rows = [{"truck_id": "T1", "plate_number": "P1", "status": "invalid_status"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        enum_errors = [e for e in result.errors if e.field_name == "status"]
        assert len(enum_errors) == 1

    def test_enum_is_case_sensitive(self, engine: ValidationEngine):
        rows = [{"truck_id": "T1", "plate_number": "P1", "status": "On_Time"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        enum_errors = [e for e in result.errors if e.field_name == "status"]
        assert len(enum_errors) == 1


class TestBooleanValidation:
    """Verify boolean type validation."""

    def test_true_false_are_valid(self, engine: ValidationEngine):
        """true and false (case-insensitive) should be valid booleans."""
        # Use fuel_stations which doesn't have boolean fields by default,
        # so we test via the internal method directly.
        engine_instance = engine
        assert engine_instance._validate_boolean("true") is None
        assert engine_instance._validate_boolean("false") is None
        assert engine_instance._validate_boolean("True") is None
        assert engine_instance._validate_boolean("FALSE") is None

    def test_yes_no_are_valid(self, engine: ValidationEngine):
        assert engine._validate_boolean("yes") is None
        assert engine._validate_boolean("no") is None
        assert engine._validate_boolean("YES") is None
        assert engine._validate_boolean("No") is None

    def test_one_zero_are_valid(self, engine: ValidationEngine):
        assert engine._validate_boolean("1") is None
        assert engine._validate_boolean("0") is None

    def test_invalid_boolean_produces_error(self, engine: ValidationEngine):
        assert engine._validate_boolean("maybe") is not None
        assert engine._validate_boolean("2") is not None
        assert engine._validate_boolean("") is not None


class TestGeoPointValidation:
    """Verify geo_point type validation."""

    def test_valid_geo_point(self, engine: ValidationEngine):
        rows = [{"station_id": "FS-1", "name": "Station", "status": "open", "coordinates": "-1.2921,36.8219"}]
        mapping = {"station_id": "station_id", "name": "name", "status": "status", "coordinates": "coordinates"}
        result = engine.validate_rows(rows, "fuel_stations", mapping)
        geo_errors = [e for e in result.errors if e.field_name == "coordinates"]
        assert len(geo_errors) == 0

    def test_lat_out_of_range(self, engine: ValidationEngine):
        assert engine._validate_geo_point("91.0,36.0") is not None
        assert engine._validate_geo_point("-91.0,36.0") is not None

    def test_lon_out_of_range(self, engine: ValidationEngine):
        assert engine._validate_geo_point("0.0,181.0") is not None
        assert engine._validate_geo_point("0.0,-181.0") is not None

    def test_non_numeric_geo_point(self, engine: ValidationEngine):
        assert engine._validate_geo_point("abc,def") is not None

    def test_wrong_format_geo_point(self, engine: ValidationEngine):
        assert engine._validate_geo_point("1.0") is not None
        assert engine._validate_geo_point("1.0,2.0,3.0") is not None

    def test_boundary_values_are_valid(self, engine: ValidationEngine):
        assert engine._validate_geo_point("90,180") is None
        assert engine._validate_geo_point("-90,-180") is None
        assert engine._validate_geo_point("0,0") is None


class TestCountConsistency:
    """Verify that ValidationResult counts are consistent."""

    def test_total_equals_valid_plus_error_rows(self, engine: ValidationEngine):
        """total_rows == valid_rows + error_row_count."""
        rows = [
            {"truck_id": "T1", "plate_number": "P1", "status": "on_time"},
            {"truck_id": "", "plate_number": "P2", "status": "on_time"},
            {"truck_id": "T3", "plate_number": "P3", "status": "invalid"},
        ]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)

        # Count distinct error rows
        error_rows = len(set(e.row_number for e in result.errors))
        assert result.total_rows == result.valid_rows + error_rows

    def test_error_count_gte_error_row_count(self, engine: ValidationEngine):
        """error_count >= number of distinct error rows (one row can have multiple errors)."""
        rows = [
            {"truck_id": "", "plate_number": "", "status": "invalid"},  # 3 errors in one row
        ]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        error_rows = len(set(e.row_number for e in result.errors))
        assert result.error_count >= error_rows

    def test_all_counts_non_negative(self, engine: ValidationEngine):
        rows = [{"truck_id": "T1", "plate_number": "P1", "status": "on_time"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        assert result.total_rows >= 0
        assert result.valid_rows >= 0
        assert result.error_count >= 0
        assert result.warning_count >= 0

    def test_empty_rows_list(self, engine: ValidationEngine):
        result = engine.validate_rows([], "fleet", {})
        assert result.total_rows == 0
        assert result.valid_rows == 0
        assert result.error_count == 0


class TestValidationIssueCompleteness:
    """Verify that every validation issue has required fields populated."""

    def test_error_issues_have_positive_row_number(self, engine: ValidationEngine):
        rows = [{"truck_id": "", "plate_number": "P1", "status": "on_time"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        for error in result.errors:
            assert error.row_number > 0

    def test_error_issues_have_non_empty_field_name(self, engine: ValidationEngine):
        rows = [{"truck_id": "", "plate_number": "P1", "status": "on_time"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        for error in result.errors:
            assert error.field_name

    def test_error_issues_have_non_empty_description(self, engine: ValidationEngine):
        rows = [{"truck_id": "", "plate_number": "P1", "status": "on_time"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        for error in result.errors:
            assert error.description


class TestStringValidation:
    """Verify string type validation — any non-empty value passes."""

    def test_any_string_value_passes(self, engine: ValidationEngine):
        rows = [{"truck_id": "anything-goes", "plate_number": "!@#$%", "status": "on_time"}]
        mapping = {"truck_id": "truck_id", "plate_number": "plate_number", "status": "status"}
        result = engine.validate_rows(rows, "fleet", mapping)
        string_errors = [e for e in result.errors if e.field_name in ("truck_id", "plate_number")]
        assert len(string_errors) == 0
