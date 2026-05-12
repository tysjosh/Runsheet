"""Extract Elasticsearch schemas from mapping files to generate seed data reference.

This script reads all ES mapping files and extracts the field definitions
to create a comprehensive schema reference for seed data generation.
"""
import importlib.util
import sys
from pathlib import Path
from typing import Dict, Any
import json


def load_module_from_file(file_path: Path):
    """Dynamically load a Python module from a file path."""
    spec = importlib.util.spec_from_file_location("module", file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["module"] = module
    spec.loader.exec_module(module)
    return module


def extract_properties(mapping: Dict[str, Any]) -> Dict[str, str]:
    """Extract field names and types from a mapping."""
    properties = mapping.get("mappings", {}).get("properties", {})
    schema = {}
    
    for field_name, field_def in properties.items():
        field_type = field_def.get("type", "unknown")
        
        if field_type == "nested":
            # For nested fields, extract nested properties
            nested_props = field_def.get("properties", {})
            nested_schema = {}
            for nested_field, nested_def in nested_props.items():
                nested_schema[nested_field] = nested_def.get("type", "unknown")
            schema[field_name] = {"type": "nested", "properties": nested_schema}
        elif field_type == "object":
            # Object fields
            enabled = field_def.get("enabled", True)
            dynamic = field_def.get("dynamic", False)
            if "properties" in field_def:
                obj_props = {}
                for obj_field, obj_def in field_def["properties"].items():
                    obj_props[obj_field] = obj_def.get("type", "unknown")
                schema[field_name] = {"type": "object", "properties": obj_props, "enabled": enabled}
            else:
                schema[field_name] = {"type": "object", "enabled": enabled, "dynamic": dynamic}
        else:
            schema[field_name] = field_type
    
    return schema


def main():
    """Extract schemas from all mapping files."""
    backend_dir = Path(__file__).parent.parent
    
    mapping_files = [
        "Agents/agent_es_mappings.py",
        "Agents/overlay/overlay_es_mappings.py",
        "Agents/support/mvp_es_mappings.py",
        "commerce/services/commerce_es_mappings.py",
        "compliance/services/compliance_es_mappings.py",
        "driver/services/driver_es_mappings.py",
        "fuel/services/fuel_es_mappings.py",
        "fuel/services/fuel_ops_es_mappings.py",
        "fuel/services/order_es_mappings.py",
        "notifications/services/audit_es_mappings.py",
        "notifications/services/notification_es_mappings.py",
        "scheduling/services/scheduling_es_mappings.py",
        "inventory/es_mappings.py",
    ]
    
    all_schemas = {}
    
    for mapping_file in mapping_files:
        file_path = backend_dir / mapping_file
        if not file_path.exists():
            print(f"Warning: {mapping_file} not found")
            continue
        
        print(f"Processing {mapping_file}...")
        
        try:
            module = load_module_from_file(file_path)
            
            # Find all mapping dictionaries in the module
            for attr_name in dir(module):
                if attr_name.endswith("_MAPPING") and not attr_name.startswith("_"):
                    mapping = getattr(module, attr_name)
                    if isinstance(mapping, dict) and "mappings" in mapping:
                        # Derive index name from mapping name
                        index_name = attr_name.replace("_MAPPING", "").lower()
                        schema = extract_properties(mapping)
                        all_schemas[index_name] = schema
                        print(f"  - Extracted schema for {index_name}")
        
        except Exception as e:
            print(f"Error processing {mapping_file}: {e}")
    
    # Save schemas to JSON file
    output_file = backend_dir / "scripts" / "schemas_reference.json"
    with open(output_file, "w") as f:
        json.dump(all_schemas, f, indent=2)
    
    print(f"\n✅ Extracted {len(all_schemas)} schemas")
    print(f"📄 Saved to: {output_file}")
    
    # Print summary
    print("\n📊 Schema Summary:")
    for index_name in sorted(all_schemas.keys()):
        field_count = len(all_schemas[index_name])
        print(f"  {index_name}: {field_count} fields")


if __name__ == "__main__":
    main()
