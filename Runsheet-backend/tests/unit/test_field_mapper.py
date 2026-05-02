"""Unit tests for the FieldMapper class.

Tests cover normalization, auto-mapping (exact match, substring containment),
unmapped columns, validate_mapping (duplicate targets, unmapped required fields),
and edge cases.
"""

import pytest

from services.field_mapper import FieldMapper
from services.schema_templates import SchemaTemplates


@pytest.fixture
def mapper():
    """Create a FieldMapper with real SchemaTemplates."""
    return FieldMapper(SchemaTemplates())


class TestNormalization:
    """Verify the normalization algorithm."""

    def test_lowercase(self, mapper: FieldMapper):
        assert mapper._normalize("Truck_ID") == "truck_id"

    def test_spaces_to_underscores(self, mapper: FieldMapper):
        assert mapper._normalize("truck id") == "truck_id"

    def test_hyphens_to_underscores(self, mapper: FieldMapper):
        assert mapper._normalize("truck-id") == "truck_id"

    def test_strip_whitespace(self, mapper: FieldMapper):
        assert mapper._normalize("  truck_id  ") == "truck_id"

    def test_mixed_separators(self, mapper: FieldMapper):
        assert mapper._normalize("  Plate - Number  ") == "plate_number"

    def test_empty_string(self, mapper: FieldMapper):
        assert mapper._normalize("") == ""

    def test_multiple_spaces(self, mapper: FieldMapper):
        """Multiple consecutive spaces are collapsed into a single underscore."""
        assert mapper._normalize("cargo   weight") == "cargo_weight"


class TestAutoMapExactMatch:
    """Verify exact match after normalization."""

    def test_exact_match_lowercase(self, mapper: FieldMapper):
        result = mapper.auto_map(["truck_id", "plate_number", "status"], "fleet")
        assert result["truck_id"] == "truck_id"
        assert result["plate_number"] == "plate_number"
        assert result["status"] == "status"

    def test_exact_match_with_spaces(self, mapper: FieldMapper):
        result = mapper.auto_map(["truck id", "plate number"], "fleet")
        assert result["truck id"] == "truck_id"
        assert result["plate number"] == "plate_number"

    def test_exact_match_with_hyphens(self, mapper: FieldMapper):
        result = mapper.auto_map(["truck-id", "plate-number"], "fleet")
        assert result["truck-id"] == "truck_id"
        assert result["plate-number"] == "plate_number"

    def test_exact_match_case_insensitive(self, mapper: FieldMapper):
        result = mapper.auto_map(["Truck_ID", "PLATE_NUMBER", "Status"], "fleet")
        assert result["Truck_ID"] == "truck_id"
        assert result["PLATE_NUMBER"] == "plate_number"
        assert result["Status"] == "status"

    def test_exact_match_with_leading_trailing_spaces(self, mapper: FieldMapper):
        result = mapper.auto_map(["  truck_id  ", " plate_number "], "fleet")
        assert result["  truck_id  "] == "truck_id"
        assert result[" plate_number "] == "plate_number"


class TestAutoMapSubstringContainment:
    """Verify substring containment matching."""

    def test_target_contained_in_source(self, mapper: FieldMapper):
        """If the target field name is a substring of the source column name, it should match."""
        result = mapper.auto_map(["my_truck_id_column"], "fleet")
        assert result.get("my_truck_id_column") == "truck_id"

    def test_source_contained_in_target(self, mapper: FieldMapper):
        """If the source column name is a substring of the target field name, it should match."""
        # "name" is a substring of "driver_name"
        # But "name" is also a substring of "source_name" etc.
        # For orders, "customer" is a target field
        result = mapper.auto_map(["order_id", "customer", "status"], "orders")
        assert result["order_id"] == "order_id"
        assert result["customer"] == "customer"
        assert result["status"] == "status"


class TestAutoMapUnmappedColumns:
    """Verify that unmapped columns are excluded from the result."""

    def test_unrecognized_column_not_in_result(self, mapper: FieldMapper):
        result = mapper.auto_map(["truck_id", "plate_number", "status", "random_column"], "fleet")
        assert "random_column" not in result

    def test_empty_source_columns(self, mapper: FieldMapper):
        result = mapper.auto_map([], "fleet")
        assert result == {}

    def test_no_matches_returns_empty(self, mapper: FieldMapper):
        result = mapper.auto_map(["xyz", "abc", "123"], "fleet")
        assert result == {}


class TestAutoMapTargetUsedOnce:
    """Verify that each target field is mapped at most once."""

    def test_first_match_wins(self, mapper: FieldMapper):
        """If two source columns could map to the same target, only the first gets it."""
        result = mapper.auto_map(["truck_id", "Truck ID"], "fleet")
        # Both normalize to truck_id, but only the first should be mapped
        mapped_targets = list(result.values())
        assert mapped_targets.count("truck_id") == 1


class TestValidateMappingDuplicateTargets:
    """Verify duplicate target mapping detection."""

    def test_no_duplicates_is_valid(self, mapper: FieldMapper):
        mapping = {"col_a": "truck_id", "col_b": "plate_number", "col_c": "status"}
        result = mapper.validate_mapping(mapping, "fleet")
        assert result["valid"] is True
        assert result["errors"] == []

    def test_duplicate_target_produces_error(self, mapper: FieldMapper):
        mapping = {"col_a": "truck_id", "col_b": "truck_id", "col_c": "status"}
        result = mapper.validate_mapping(mapping, "fleet")
        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert "truck_id" in result["errors"][0]

    def test_multiple_duplicate_targets(self, mapper: FieldMapper):
        mapping = {
            "col_a": "truck_id",
            "col_b": "truck_id",
            "col_c": "status",
            "col_d": "status",
        }
        result = mapper.validate_mapping(mapping, "fleet")
        assert result["valid"] is False
        assert len(result["errors"]) == 2


class TestValidateMappingUnmappedRequired:
    """Verify unmapped required field warning detection."""

    def test_all_required_mapped_no_warnings(self, mapper: FieldMapper):
        # Fleet required: truck_id, plate_number, status
        mapping = {"a": "truck_id", "b": "plate_number", "c": "status"}
        result = mapper.validate_mapping(mapping, "fleet")
        assert result["warnings"] == []

    def test_missing_required_field_produces_warning(self, mapper: FieldMapper):
        # Missing truck_id
        mapping = {"b": "plate_number", "c": "status"}
        result = mapper.validate_mapping(mapping, "fleet")
        assert len(result["warnings"]) == 1
        assert "truck_id" in result["warnings"][0]

    def test_all_required_missing_produces_warnings_for_each(self, mapper: FieldMapper):
        # Fleet has 3 required fields: truck_id, plate_number, status
        mapping = {"a": "driver_name"}
        result = mapper.validate_mapping(mapping, "fleet")
        assert len(result["warnings"]) == 3

    def test_empty_mapping_warns_all_required(self, mapper: FieldMapper):
        mapping: dict[str, str] = {}
        result = mapper.validate_mapping(mapping, "fleet")
        # Fleet has 3 required fields
        assert len(result["warnings"]) == 3

    def test_warnings_dont_affect_validity(self, mapper: FieldMapper):
        """Warnings (unmapped required) don't make the mapping invalid — only errors do."""
        mapping = {"b": "plate_number", "c": "status"}  # missing truck_id
        result = mapper.validate_mapping(mapping, "fleet")
        assert result["valid"] is True  # No duplicate errors
        assert len(result["warnings"]) == 1


class TestValidateMappingCombined:
    """Verify combined duplicate + unmapped required scenarios."""

    def test_duplicates_and_unmapped_required(self, mapper: FieldMapper):
        # Duplicate on plate_number, missing truck_id
        mapping = {"a": "plate_number", "b": "plate_number", "c": "status"}
        result = mapper.validate_mapping(mapping, "fleet")
        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert len(result["warnings"]) == 1
        assert "truck_id" in result["warnings"][0]


class TestAutoMapAllDataTypes:
    """Verify auto_map works for all supported data types."""

    @pytest.mark.parametrize("data_type", [
        "fleet", "orders", "riders", "fuel_stations",
        "inventory", "support_tickets", "jobs",
    ])
    def test_identity_mapping(self, mapper: FieldMapper, data_type: str):
        """When source columns exactly match target field names, all should be mapped."""
        template = mapper.schema_templates.get_template(data_type)
        source_cols = [f.name for f in template.fields]
        result = mapper.auto_map(source_cols, data_type)
        assert len(result) == len(source_cols)
        for col in source_cols:
            assert result[col] == col
