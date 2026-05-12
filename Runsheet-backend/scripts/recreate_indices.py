#!/usr/bin/env python3
"""
Recreate all Elasticsearch indices with updated mappings.

WARNING: This will DELETE all existing data in the indices!
Only use in development environments.
"""

import sys
import os
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Load environment variables
from dotenv import load_dotenv
env_file = Path(__file__).parent.parent / '.env.development'
load_dotenv(env_file)

from services.elasticsearch_service import get_es_service
from notifications.services.notification_es_mappings import setup_notification_indices
from fuel.services.order_es_mappings import setup_order_indices
from Agents.support.mvp_es_mappings import setup_mvp_indices
from Agents.support.overlay_es_mappings import setup_overlay_indices
from compliance.services.compliance_es_mappings import setup_compliance_indices
from commerce.services.commerce_es_mappings import setup_commerce_indices
from inventory.es_mappings import setup_inventory_indices
from scheduling.es_mappings import setup_scheduling_indices
from fuel.services.fuel_ops_es_mappings import setup_fuel_ops_indices

print("=" * 60)
print("  Elasticsearch Index Recreation Script")
print("=" * 60)
print("⚠️  WARNING: This will DELETE all data in the indices!")
print("=" * 60)

# Get confirmation
response = input("\nType 'YES' to proceed with index recreation: ")
if response != "YES":
    print("❌ Aborted. No changes made.")
    sys.exit(0)

print("\n🔄 Connecting to Elasticsearch...")
es_service = get_es_service()
es_client = es_service.client

print("✅ Connected to Elasticsearch\n")

# List of indices to recreate - comprehensive list across all domains
indices_to_recreate = [
    # Notifications
    "notifications_current",
    "notification_preferences", 
    "notification_templates",
    "notification_rules",
    
    # Orders/Intake
    "intake_channels",
    "fuel_orders_current",
    
    # MVP Overlay
    "mvp_delivery_priorities",
    "mvp_load_plans",
    "mvp_tank_forecasts",
    "mvp_routes",
    "mvp_plan_executions",
    "mvp_plan_outcomes",
    "mvp_replan_events",
    "mvp_combinable_groups",
    "mvp_reconciliation",
    
    # Compliance
    "drivers_current",
    "asset_certifications",
    "meter_registry",
    "meter_audit_trail",
    "terminal_bols",
    "ifta_mileage",
    "kfactor_history",
    "dyed_diesel_audit_log",
    "tax_jurisdictions",
    "tax_exemptions",
    "price_protection_contracts",
    
    # Commerce
    "customers_current",
    "accounts_current",
    "invoices_current",
    "payments_current",
    "price_books_current",
    "pricing_rules_current",
    
    # Inventory
    "inventory_current",
    "inventory_events",
    "restock_requests",
    
    # Scheduling
    "jobs_current",
    "routes_current",
    "driver_presence",
    "driver_exceptions",
    "job_messages",
    "proof_of_delivery",
    
    # Fuel Ops
    "fuel_stations",
    "fuel_events",
    "customer_tanks",
    "atg_readings",
    "weather_observations",
    "depots",
    "terminals",
    "rack_prices",
    "supplier_contracts",
    "terminal_wait_reports",
    "weather_alerts",
    "storm_mode_overrides",
    "storm_road_restrictions",
    "integration_instances",
    "integration_sync_runs",
    "truck_telemetry",
    "compartment_cleaning_events",
    "cross_contamination_events",
    "meter_ticket_ocr_results",
    "bill_of_lading",
    "sourcing_recommendations",
    "locations",
    
    # Fleet/Assets
    "trucks",
    "truck_compartments",
    "riders_current",
    
    # Agent System
    "agent_memory",
    "agent_approval_queue",
    "agent_activity_log",
    "ops_poison_queue",
]

print(f"🗑️  Deleting {len(indices_to_recreate)} indices...\n")

deleted_count = 0
for index_name in indices_to_recreate:
    try:
        if es_client.indices.exists(index=index_name):
            es_client.indices.delete(index=index_name)
            print(f"  ✓ Deleted: {index_name}")
            deleted_count += 1
        else:
            print(f"  - Skipped (not found): {index_name}")
    except Exception as e:
        print(f"  ✗ Error deleting {index_name}: {e}")

print(f"\n✅ Deleted {deleted_count} indices\n")

print("🔨 Recreating indices with updated mappings...\n")

# Recreate indices with updated mappings
setup_functions = [
    ("Notification indices", setup_notification_indices),
    ("Order indices", setup_order_indices),
    ("MVP indices", setup_mvp_indices),
    ("Overlay indices", setup_overlay_indices),
    ("Compliance indices", setup_compliance_indices),
    ("Commerce indices", setup_commerce_indices),
    ("Inventory indices", setup_inventory_indices),
    ("Scheduling indices", setup_scheduling_indices),
    ("Fuel Ops indices", setup_fuel_ops_indices),
]

for name, setup_func in setup_functions:
    try:
        print(f"  Creating {name}...")
        setup_func(es_service)
        print(f"  ✓ {name} created")
    except Exception as e:
        print(f"  ✗ Error creating {name}: {e}")

print("\n" + "=" * 60)
print("  ✅ Index recreation complete!")
print("=" * 60)
print("\nNext step: Run the seed data loader:")
print("  SEED_TENANT_ID=tenant-demo python scripts/load_json_seeds.py --force")
print()
