"""Validation engine for the data import/migration tool.

Pure validation logic — no side effects. Takes rows + schema + field mapping,
returns validation results with per-row errors and warnings.
"""

import re
from datetime import datetime

from services.import_models import ValidationIssue, ValidationResult
from services.schema_templates import FieldType, SchemaTemplates


class ValidationEngine:
    """Validates imported data rows against schema templates.

    Constructor takes a SchemaTemplates instance. The main entry point is
    ``validate_rows``, which applies field mapping, checks required field
    presence, data type correctness, and value format compliance for every row.
    """

    # Common date formats to accept beyond strict ISO8601
    _DATE_FORMATS = [
        "%Y-%m-%dT%H:%M:%SZ",       # 2024-01-15T14:30:00Z
        "%Y-%m-%dT%H:%M:%S",        # 2024-01-15T14:30:00
        "%Y-%m-%dT%H:%M:%S%z",      # 2024-01-15T14:30:00+00:00
        "%Y-%m-%d",                  # 2024-01-15
        "%m/%d/%Y",                  # 01/15/2024
        "%d/%m/%Y",                  # 15/01/2024
    ]

    # Accepted boolean literals (case-insensitive)
    _BOOLEAN_TRUE = {"true", "yes", "1"}
    _BOOLEAN_FALSE = {"false", "no", "0"}
    _BOOLEAN_VALUES = _BOOLEAN_TRUE | _BOOLEAN_FALSE

    def __init__(self, schema_templates: SchemaTemplates):
        self.schema_templates = schema_templates

    def validate_rows(
        self,
        rows: list[dict[str, str]],
        data_type: str,
        field_mapping: dict[str, str],
    ) -> ValidationResult:
        """Validate all rows against the schema template.

        For each row:
        1. Apply field mapping (source column -> target field)
        2. Check required field presence
        3. Check data type correctness (string, number, date, enum, boolean, geo_point)
        4. Check value format compliance

        Args:
            rows: List of dicts where keys are source column names and values
                  are string cell values.
            data_type: One of the supported data type keys.
            field_mapping: Dict mapping source column names to target field names.

        Returns:
            ValidationResult with per-row errors and warnings, counts, and
            row-level detail.
        """
        template = self.schema_templates.get_template(data_type)
        field_defs = {f.name: f for f in template.fields}

        # Build reverse mapping: target_field -> source_column
        reverse_mapping: dict[str, str] = {
            target: source for source, target in field_mapping.items()
        }

        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []
        rows_with_errors: set[int] = set()

        for row_idx, row in enumerate(rows):
            row_number = row_idx + 1  # 1-based row numbers

            # Step 1: Apply field mapping to get target field values
            mapped_row: dict[str, str] = {}
            for source_col, target_field in field_mapping.items():
                if source_col in row:
                    mapped_row[target_field] = row[source_col]

            # Step 2: Check required field presence
            for field_def in template.fields:
                if not field_def.required:
                    continue

                value = mapped_row.get(field_def.name, "").strip()
                if not value:
                    errors.append(
                        ValidationIssue(
                            row_number=row_number,
                            field_name=field_def.name,
                            description=f"Required field '{field_def.name}' is missing or empty",
                            value=mapped_row.get(field_def.name),
                        )
                    )
                    rows_with_errors.add(row_number)

            # Step 3 & 4: Check data type correctness and value format
            for target_field, value in mapped_row.items():
                if target_field not in field_defs:
                    continue

                field_def = field_defs[target_field]
                stripped = value.strip()

                # Skip empty optional fields — they're fine
                if not stripped:
                    continue

                type_error = self._validate_type(stripped, field_def)
                if type_error:
                    errors.append(
                        ValidationIssue(
                            row_number=row_number,
                            field_name=field_def.name,
                            description=type_error,
                            value=value,
                        )
                    )
                    rows_with_errors.add(row_number)

        total_rows = len(rows)
        error_row_count = len(rows_with_errors)
        valid_rows = total_rows - error_row_count

        return ValidationResult(
            session_id="",  # Caller sets this
            total_rows=total_rows,
            valid_rows=valid_rows,
            error_count=len(errors),
            warning_count=len(warnings),
            errors=errors,
            warnings=warnings,
        )

    def _validate_type(self, value: str, field_def) -> str | None:
        """Validate a single value against its field type.

        Returns an error description string if invalid, or None if valid.
        """
        field_type = field_def.type

        if field_type == FieldType.STRING:
            return self._validate_string(value)
        elif field_type == FieldType.NUMBER:
            return self._validate_number(value)
        elif field_type == FieldType.DATE:
            return self._validate_date(value)
        elif field_type == FieldType.ENUM:
            return self._validate_enum(value, field_def.enum_values or [])
        elif field_type == FieldType.BOOLEAN:
            return self._validate_boolean(value)
        elif field_type == FieldType.GEO_POINT:
            return self._validate_geo_point(value)
        else:
            return None

    @staticmethod
    def _validate_string(value: str) -> str | None:
        """String validation: any non-empty value passes."""
        # Already checked for emptiness in the main loop for required fields.
        # Non-empty strings always pass.
        return None

    @staticmethod
    def _validate_number(value: str) -> str | None:
        """Number validation: accept parseable float/int values."""
        try:
            float(value)
            return None
        except (ValueError, OverflowError):
            return f"Expected a number, got '{value}'"

    @classmethod
    def _validate_date(cls, value: str) -> str | None:
        """Date validation: accept ISO8601 and common date formats."""
        for fmt in cls._DATE_FORMATS:
            try:
                datetime.strptime(value, fmt)
                return None
            except ValueError:
                continue

        # Also try ISO8601 with fractions / timezone via fromisoformat
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return None
        except (ValueError, TypeError):
            pass

        return f"Expected a date, got '{value}'"

    @staticmethod
    def _validate_enum(value: str, allowed: list[str]) -> str | None:
        """Enum validation: value must be in the allowed list (case-sensitive)."""
        if value in allowed:
            return None
        return f"Value '{value}' is not in allowed values: {', '.join(allowed)}"

    @classmethod
    def _validate_boolean(cls, value: str) -> str | None:
        """Boolean validation: accept true/false, yes/no, 1/0 (case-insensitive)."""
        if value.lower() in cls._BOOLEAN_VALUES:
            return None
        return f"Expected a boolean (true/false, yes/no, 1/0), got '{value}'"

    @staticmethod
    def _validate_geo_point(value: str) -> str | None:
        """Geo_point validation: accept 'lat,lon' with valid ranges."""
        parts = value.split(",")
        if len(parts) != 2:
            return f"Expected geo_point format 'lat,lon', got '{value}'"

        try:
            lat = float(parts[0].strip())
            lon = float(parts[1].strip())
        except (ValueError, OverflowError):
            return f"Expected geo_point with numeric lat,lon, got '{value}'"

        if not (-90 <= lat <= 90):
            return f"Latitude {lat} out of range [-90, 90]"
        if not (-180 <= lon <= 180):
            return f"Longitude {lon} out of range [-180, 180]"

        return None
