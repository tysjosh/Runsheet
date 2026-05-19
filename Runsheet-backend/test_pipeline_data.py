#!/usr/bin/env python3
"""
Quick diagnostic script to check if pipeline data is correctly seeded.
"""
import asyncio
from services.elasticsearch_service import elasticsearch_service

async def check_data():
    es = elasticsearch_service
    tenant_id = "demo-tenant"
    
    print("=" * 60)
    print("PIPELINE DATA DIAGNOSTIC")
    print("=" * 60)
    
    # Check fuel orders
    print("\n1. Fuel Orders (fuel_orders_current):")
    orders_query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant_id}},
                    {"terms": {"status": ["placed", "confirmed", "scheduled"]}},
                ],
            },
        },
        "size": 5,
    }
    orders_resp = await es.search_documents("fuel_orders_current", orders_query, 5)
    orders = orders_resp.get("hits", {}).get("hits", [])
    print(f"   Found {len(orders)} orders in loadable status")
    for order in orders[:3]:
        src = order["_source"]
        print(f"   - {src.get('order_id')}: {src.get('product_code')}, "
              f"status={src.get('status')}, customer_tank_id={src.get('customer_tank_id')}")
    
    # Check forecasts
    print("\n2. Tank Forecasts (mvp_tank_forecasts):")
    forecasts_query = {
        "query": {"term": {"tenant_id": tenant_id}},
        "size": 5,
    }
    forecasts_resp = await es.search_documents("mvp_tank_forecasts", forecasts_query, 5)
    forecasts = forecasts_resp.get("hits", {}).get("hits", [])
    print(f"   Found {len(forecasts)} forecasts")
    for forecast in forecasts[:3]:
        src = forecast["_source"]
        print(f"   - {src.get('station_id')}: {src.get('fuel_grade')}, "
              f"hours_to_runout_p50={src.get('hours_to_runout_p50')}")
    
    # Check truck compartments
    print("\n3. Truck Compartments (truck_compartments):")
    compartments_query = {
        "query": {"term": {"tenant_id": tenant_id}},
        "size": 10,
    }
    compartments_resp = await es.search_documents("truck_compartments", compartments_query, 10)
    compartments = compartments_resp.get("hits", {}).get("hits", [])
    print(f"   Found {len(compartments)} compartments")
    trucks = {}
    for comp in compartments:
        src = comp["_source"]
        truck_id = src.get("truck_id")
        if truck_id not in trucks:
            trucks[truck_id] = []
        trucks[truck_id].append(src)
    
    for truck_id, comps in trucks.items():
        print(f"   - {truck_id}: {len(comps)} compartments")
    
    print("\n" + "=" * 60)
    print("DIAGNOSIS:")
    print("=" * 60)
    
    if not orders:
        print("❌ NO FUEL ORDERS FOUND - Pipeline needs orders to generate plans")
    else:
        print(f"✅ {len(orders)} fuel orders found")
        
        # Check if orders have customer_tank_id
        orders_with_tank = [o for o in orders if o["_source"].get("customer_tank_id")]
        if not orders_with_tank:
            print("❌ Orders missing customer_tank_id field")
        else:
            print(f"✅ {len(orders_with_tank)} orders have customer_tank_id")
    
    if not forecasts:
        print("❌ NO FORECASTS FOUND - Prioritization needs forecasts")
    else:
        print(f"✅ {len(forecasts)} forecasts found")
        
        # Check if forecasts have urgent hours_to_runout
        urgent_forecasts = [f for f in forecasts if f["_source"].get("hours_to_runout_p50", 999) < 48]
        print(f"✅ {len(urgent_forecasts)} forecasts with < 48 hours to runout")
    
    if not compartments:
        print("❌ NO TRUCK COMPARTMENTS FOUND - Loading agent needs compartments")
    else:
        print(f"✅ {len(compartments)} compartments found for {len(trucks)} trucks")
    
    print("\n")

if __name__ == "__main__":
    asyncio.run(check_data())
