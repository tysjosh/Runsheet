"""
Startup mapping validator for Elasticsearch index drift detection.

Compares code-defined ES mappings against live index mappings at startup,
reports drift (missing fields, type mismatches), and attempts remediation
for additive changes via put_mapping.

Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class MappingDrift:
    """Represents a single mapping drift item between code and live ES index."""

    index_name: str
    field_path: str  # Dot-notation path, e.g. "context.job_type"
    drift_type: str  # "missing_field" | "type_mismatch"
    expected_type: Optional[str]  # e.g. "date", "keyword"
    actual_type: Optional[str]  # What's in the live index (None if missing)


def _collect_all_index_mappings() -> List[Tuple[str, Dict]]:
    """Collect all (index_name, mapping_dict) pairs from the mapping modules.

    Returns a list of tuples: (index_name, mapping_definition_dict).
    """
    pairs: List[Tuple[str, Dict]] = []

    # 1. overlay_es_mappings
    from Agents.overlay.overlay_es_mappings import (
        AGENT_SIGNALS_INDEX,
        AGENT_SIGNALS_MAPPING,
        AGENT_SHADOW_PROPOSALS_INDEX,
        AGENT_SHADOW_PROPOSALS_MAPPING,
        AGENT_OUTCOMES_INDEX,
        AGENT_OUTCOMES_MAPPING,
        AGENT_REVENUE_REPORTS_INDEX,
        AGENT_REVENUE_REPORTS_MAPPING,
        AGENT_POLICY_EXPERIMENTS_INDEX,
        AGENT_POLICY_EXPERIMENTS_MAPPING,
        JOB_PRIORITIES_INDEX,
        JOB_PRIORITIES_MAPPING,
    )

    pairs.append((AGENT_SIGNALS_INDEX, AGENT_SIGNALS_MAPPING))
    pairs.append((AGENT_SHADOW_PROPOSALS_INDEX, AGENT_SHADOW_PROPOSALS_MAPPING))
    pairs.append((AGENT_OUTCOMES_INDEX, AGENT_OUTCOMES_MAPPING))
    pairs.append((AGENT_REVENUE_REPORTS_INDEX, AGENT_REVENUE_REPORTS_MAPPING))
    pairs.append((AGENT_POLICY_EXPERIMENTS_INDEX, AGENT_POLICY_EXPERIMENTS_MAPPING))
    pairs.append((JOB_PRIORITIES_INDEX, JOB_PRIORITIES_MAPPING))

    # 2. mvp_es_mappings
    from Agents.support.mvp_es_mappings import (
        MVP_TANK_FORECASTS_INDEX,
        MVP_TANK_FORECASTS_MAPPING,
        MVP_DELIVERY_PRIORITIES_INDEX,
        MVP_DELIVERY_PRIORITIES_MAPPING,
        MVP_LOAD_PLANS_INDEX,
        MVP_LOAD_PLANS_MAPPING,
        MVP_ROUTES_INDEX,
        MVP_ROUTES_MAPPING,
        MVP_REPLAN_EVENTS_INDEX,
        MVP_REPLAN_EVENTS_MAPPING,
        MVP_PLAN_OUTCOMES_INDEX,
        MVP_PLAN_OUTCOMES_MAPPING,
        TRUCK_COMPARTMENTS_INDEX,
        TRUCK_COMPARTMENTS_MAPPING,
    )

    pairs.append((MVP_TANK_FORECASTS_INDEX, MVP_TANK_FORECASTS_MAPPING))
    pairs.append((MVP_DELIVERY_PRIORITIES_INDEX, MVP_DELIVERY_PRIORITIES_MAPPING))
    pairs.append((MVP_LOAD_PLANS_INDEX, MVP_LOAD_PLANS_MAPPING))
    pairs.append((MVP_ROUTES_INDEX, MVP_ROUTES_MAPPING))
    pairs.append((MVP_REPLAN_EVENTS_INDEX, MVP_REPLAN_EVENTS_MAPPING))
    pairs.append((MVP_PLAN_OUTCOMES_INDEX, MVP_PLAN_OUTCOMES_MAPPING))
    pairs.append((TRUCK_COMPARTMENTS_INDEX, TRUCK_COMPARTMENTS_MAPPING))

    # 3. agent_es_mappings
    from Agents.agent_es_mappings import (
        AGENT_APPROVAL_QUEUE_INDEX,
        AGENT_APPROVAL_QUEUE_MAPPING,
        AGENT_ACTIVITY_LOG_INDEX,
        AGENT_ACTIVITY_LOG_MAPPING,
        AGENT_MEMORY_INDEX,
        AGENT_MEMORY_MAPPING,
        AGENT_FEEDBACK_INDEX,
        AGENT_FEEDBACK_MAPPING,
    )

    pairs.append((AGENT_APPROVAL_QUEUE_INDEX, AGENT_APPROVAL_QUEUE_MAPPING))
    pairs.append((AGENT_ACTIVITY_LOG_INDEX, AGENT_ACTIVITY_LOG_MAPPING))
    pairs.append((AGENT_MEMORY_INDEX, AGENT_MEMORY_MAPPING))
    pairs.append((AGENT_FEEDBACK_INDEX, AGENT_FEEDBACK_MAPPING))

    # 4. fuel_es_mappings
    from fuel.services.fuel_es_mappings import (
        FUEL_STATIONS_INDEX,
        FUEL_STATIONS_MAPPING,
        FUEL_EVENTS_INDEX,
        FUEL_EVENTS_MAPPING,
    )

    pairs.append((FUEL_STATIONS_INDEX, FUEL_STATIONS_MAPPING))
    pairs.append((FUEL_EVENTS_INDEX, FUEL_EVENTS_MAPPING))

    # 5. inventory/es_mappings
    from inventory.es_mappings import (
        INVENTORY_INDEX,
        INVENTORY_MAPPING,
        INVENTORY_EVENTS_INDEX,
        INVENTORY_EVENTS_MAPPING,
        RESTOCK_REQUESTS_INDEX,
        RESTOCK_REQUESTS_MAPPING,
    )

    pairs.append((INVENTORY_INDEX, INVENTORY_MAPPING))
    pairs.append((INVENTORY_EVENTS_INDEX, INVENTORY_EVENTS_MAPPING))
    pairs.append((RESTOCK_REQUESTS_INDEX, RESTOCK_REQUESTS_MAPPING))

    # 6. driver_es_mappings — without these pairs driver-index drift is neither
    #    detected nor repaired (Requirement 15.12). The whole registry is used
    #    so a newly-declared driver index needs no second edit here.
    from driver.services.driver_es_mappings import DRIVER_INDEX_MAPPINGS

    pairs.extend(DRIVER_INDEX_MAPPINGS.items())

    return pairs


def _flatten_properties(
    properties: Dict, prefix: str = ""
) -> Dict[str, str]:
    """Flatten nested mapping properties into dot-notation paths with their types.

    For example:
        {"context": {"type": "object", "properties": {"job_type": {"type": "keyword"}}}}
    becomes:
        {"context.job_type": "keyword"}

    Object-type fields without sub-properties are included as-is.
    Nested fields are recursed into.
    """
    result: Dict[str, str] = {}
    for field_name, field_def in properties.items():
        full_path = f"{prefix}{field_name}" if not prefix else f"{prefix}.{field_name}"

        field_type = field_def.get("type")

        # If the field has sub-properties, recurse into them
        sub_properties = field_def.get("properties")
        if sub_properties:
            # Include the parent field itself if it has a type
            if field_type:
                result[full_path] = field_type
            sub_result = _flatten_properties(sub_properties, full_path)
            result.update(sub_result)
        else:
            # Leaf field
            if field_type:
                result[full_path] = field_type

    return result


class MappingValidator:
    """Compares code-defined ES mappings against live index mappings at startup.

    Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5
    """

    def __init__(self, es_service):
        self._es = es_service

    async def validate_all(self) -> List[MappingDrift]:
        """Compare all code-defined mappings against live indices.

        Returns list of detected drift items.
        Skips indices that don't exist in ES (logs info).
        Handles ES connection failures gracefully (logs error, returns empty list).
        """
        drift_items: List[MappingDrift] = []

        try:
            index_mappings = _collect_all_index_mappings()
        except Exception as e:
            logger.error(
                f"Failed to collect code-defined mappings: {e}"
            )
            return drift_items

        es_client = self._es.client

        for index_name, mapping_def in index_mappings:
            try:
                # Check if index exists
                if not es_client.indices.exists(index=index_name):
                    logger.info(
                        f"MappingValidator: Index '{index_name}' does not exist, "
                        f"skipping validation (will be created on first use)"
                    )
                    continue

                # Get live mapping
                live_mapping_response = es_client.indices.get_mapping(
                    index=index_name
                )
                live_properties = (
                    live_mapping_response.get(index_name, {})
                    .get("mappings", {})
                    .get("properties", {})
                )

                # Get code-defined properties
                code_properties = (
                    mapping_def.get("mappings", {}).get("properties", {})
                )

                # Flatten both to dot-notation for comparison
                code_flat = _flatten_properties(code_properties)
                live_flat = _flatten_properties(live_properties)

                # Compare: find missing fields and type mismatches
                for field_path, expected_type in code_flat.items():
                    if field_path not in live_flat:
                        drift_items.append(
                            MappingDrift(
                                index_name=index_name,
                                field_path=field_path,
                                drift_type="missing_field",
                                expected_type=expected_type,
                                actual_type=None,
                            )
                        )
                    elif live_flat[field_path] != expected_type:
                        drift_items.append(
                            MappingDrift(
                                index_name=index_name,
                                field_path=field_path,
                                drift_type="type_mismatch",
                                expected_type=expected_type,
                                actual_type=live_flat[field_path],
                            )
                        )

            except Exception as e:
                logger.error(
                    f"MappingValidator: Failed to validate index '{index_name}': {e}"
                )
                # Continue to next index — don't block startup
                continue

        if drift_items:
            logger.warning(
                f"MappingValidator: Detected {len(drift_items)} drift item(s) "
                f"across indices"
            )
        else:
            logger.info("MappingValidator: All indices match code-defined mappings")

        return drift_items

    async def remediate(self, drift_items: List[MappingDrift]) -> None:
        """Attempt to fix additive drift via put_mapping.

        For missing fields: calls put_mapping to add the field.
        For type mismatches: logs an ERROR naming the index and the field. It
        is not repairable at boot and the remedy is an operator-run reindex,
        never an automatic one.

        Args:
            drift_items: List of MappingDrift items to remediate.
        """
        if not drift_items:
            return

        es_client = self._es.client

        # Group missing fields by index for batch put_mapping calls
        missing_by_index: Dict[str, Dict[str, Dict]] = {}
        for item in drift_items:
            if item.drift_type == "missing_field":
                if item.index_name not in missing_by_index:
                    missing_by_index[item.index_name] = {}
                # Build the nested property structure from dot-notation path
                missing_by_index[item.index_name][item.field_path] = item.expected_type
            elif item.drift_type == "type_mismatch":
                logger.error(
                    f"MappingValidator: Type mismatch in index '{item.index_name}' "
                    f"at field '{item.field_path}': expected '{item.expected_type}', "
                    f"actual '{item.actual_type}'. "
                    f"Cannot auto-fix — requires reindexing."
                )

        # Apply put_mapping for missing fields
        for index_name, fields in missing_by_index.items():
            try:
                # Convert dot-notation paths to nested property structure
                properties = _build_nested_properties(fields)
                es_client.indices.put_mapping(
                    index=index_name,
                    body={"properties": properties},
                )
                field_paths = list(fields.keys())
                logger.info(
                    f"MappingValidator: Added missing field(s) to index "
                    f"'{index_name}': {field_paths}"
                )
            except Exception as e:
                logger.error(
                    f"MappingValidator: Failed to remediate index "
                    f"'{index_name}': {e}"
                )


def _build_nested_properties(
    flat_fields: Dict[str, str],
) -> Dict:
    """Convert flat dot-notation field paths to nested ES property structure.

    Example:
        {"context.job_type": "keyword", "updated_at": "date"}
    becomes:
        {
            "context": {"properties": {"job_type": {"type": "keyword"}}},
            "updated_at": {"type": "date"}
        }
    """
    result: Dict = {}
    for field_path, field_type in flat_fields.items():
        parts = field_path.split(".")
        current = result
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                # Leaf node — set the type
                current[part] = {"type": field_type}
            else:
                # Intermediate node — ensure properties dict exists
                if part not in current:
                    current[part] = {"properties": {}}
                elif "properties" not in current[part]:
                    current[part]["properties"] = {}
                current = current[part]["properties"]
    return result
