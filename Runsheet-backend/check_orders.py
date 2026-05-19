#!/usr/bin/env python3
"""Check what orders exist."""
from services.elasticsearch_service import elasticsearch_service

es = elasticsearch_service.client

# Get all orders
query = {"query": {"match_all": {}}, "size": 20}
result = es.search(index="fuel_orders_current", body=query)
hits = result.get("hits", {}).get("hits", [])

print(f"Total orders in index: {result.get('hits', {}).get('total', {}).get('value', 0)}")
print("\nOrders found:")
for hit in hits:
    src = hit["_source"]
    print(f"  - {src.get('order_id')}: tenant={src.get('tenant_id')}, "
          f"status={src.get('status')}, customer_tank_id={src.get('customer_tank_id')}")
