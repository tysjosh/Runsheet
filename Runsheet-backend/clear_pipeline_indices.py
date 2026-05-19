#!/usr/bin/env python3
"""
Clear pipeline-related indices to start fresh.
"""
import asyncio
from services.elasticsearch_service import elasticsearch_service

async def clear_indices():
    es = elasticsearch_service.client  # Use the sync client
    
    indices_to_clear = [
        "fuel_orders_current",
        "mvp_tank_forecasts",
        "truck_compartments",
        "mvp_load_plans",
        "mvp_routes",
        "mvp_delivery_priorities",
    ]
    
    print("Clearing pipeline indices...")
    for index in indices_to_clear:
        try:
            # Delete all documents for demo-tenant
            query = {"query": {"term": {"tenant_id": "demo-tenant"}}}
            result = es.delete_by_query(index=index, body=query, refresh=True)
            deleted = result.get("deleted", 0)
            print(f"✅ Cleared {deleted} documents from {index}")
        except Exception as e:
            print(f"⚠️  Error clearing {index}: {e}")
    
    print("\nDone! Run seed_all_data.py --force to re-seed.")

if __name__ == "__main__":
    asyncio.run(clear_indices())
