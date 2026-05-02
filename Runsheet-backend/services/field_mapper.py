"""Field mapper for the data import/migration tool.

Auto-mapping algorithm for suggesting field mappings from source columns
to target schema fields, plus validation of user-provided mappings.
"""

import re

from services.schema_templates import SchemaTemplates


class FieldMapper:
    """Maps source CSV/Sheets columns to target schema fields.

    Constructor takes a SchemaTemplates instance. Provides auto-mapping
    via normalization and substring matching, and mapping validation
    to detect duplicate targets and unmapped required fields.
    """

    def __init__(self, schema_templates: SchemaTemplates):
        self.schema_templates = schema_templates

    @staticmethod
    def _normalize(name: str) -> str:
        """Normalize a column/field name for comparison.

        Algorithm: strip whitespace, lowercase, replace spaces and hyphens
        with underscores.
        """
        result = name.strip().lower()
        result = re.sub(r"[\s\-]+", "_", result)
        return result

    def auto_map(
        self, source_columns: list[str], data_type: str
    ) -> dict[str, str]:
        """Suggest mappings from source columns to target fields.

        Algorithm:
        1. Normalize both source and target names: lowercase, replace
           spaces/hyphens with underscores, strip whitespace.
        2. Exact match after normalization → map.
        3. Substring containment (target in source or source in target) → map.
        4. Unmapped columns get no suggestion.

        Each target field is mapped at most once (first match wins).

        Args:
            source_columns: List of column names from the uploaded source.
            data_type: One of the supported data type keys.

        Returns:
            Dict of ``{source_column: target_field}`` for suggested mappings.
            Only columns with a suggestion are included.
        """
        template = self.schema_templates.get_template(data_type)
        target_fields = [f.name for f in template.fields]

        # Pre-normalize target field names
        normalized_targets = {
            self._normalize(field): field for field in target_fields
        }

        mapping: dict[str, str] = {}
        used_targets: set[str] = set()

        # Pass 1: Exact match after normalization
        for source_col in source_columns:
            norm_source = self._normalize(source_col)
            if norm_source in normalized_targets:
                target = normalized_targets[norm_source]
                if target not in used_targets:
                    mapping[source_col] = target
                    used_targets.add(target)

        # Pass 2: Substring containment for remaining unmapped columns
        unmapped_sources = [
            col for col in source_columns if col not in mapping
        ]
        for source_col in unmapped_sources:
            norm_source = self._normalize(source_col)
            if not norm_source:
                continue
            for norm_target, target in normalized_targets.items():
                if target in used_targets:
                    continue
                if not norm_target:
                    continue
                # Substring containment: target in source or source in target
                if norm_target in norm_source or norm_source in norm_target:
                    mapping[source_col] = target
                    used_targets.add(target)
                    break

        return mapping

    def validate_mapping(
        self, field_mapping: dict[str, str], data_type: str
    ) -> dict:
        """Validate a field mapping for correctness.

        Checks:
        1. Duplicate target mappings — two source columns mapped to the
           same target field is an error.
        2. Unmapped required fields — required target fields with no source
           column mapped produce warnings.

        Args:
            field_mapping: Dict of ``{source_column: target_field}``.
            data_type: One of the supported data type keys.

        Returns:
            Dict with:
            - ``valid``: bool — whether the mapping is valid (no errors).
            - ``errors``: list of error strings (e.g., duplicate targets).
            - ``warnings``: list of warning strings (e.g., unmapped required
              fields).
        """
        errors: list[str] = []
        warnings: list[str] = []

        # Check for duplicate target mappings
        target_to_sources: dict[str, list[str]] = {}
        for source_col, target_field in field_mapping.items():
            target_to_sources.setdefault(target_field, []).append(source_col)

        for target_field, sources in target_to_sources.items():
            if len(sources) > 1:
                sources_str = ", ".join(repr(s) for s in sources)
                errors.append(
                    f"Duplicate target mapping: columns {sources_str} "
                    f"are both mapped to '{target_field}'"
                )

        # Check for unmapped required fields
        required_fields = self.schema_templates.get_required_fields(data_type)
        mapped_targets = set(field_mapping.values())

        for field_def in required_fields:
            if field_def.name not in mapped_targets:
                warnings.append(
                    f"Required field '{field_def.name}' is not mapped "
                    f"to any source column"
                )

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
        }
