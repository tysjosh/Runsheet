#!/usr/bin/env python3
"""
Debug script to manually run pipeline agents and see what they produce.
"""
import asyncio
import logging
from services.elasticsearch_service import elasticsearch_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

async def debug_pipeline():
    tenant_id = "demo-tenant"
    
    print("=" * 80)
    print("PIPELINE DEBUG - Manual Agent Execution")
    print("=" * 80)
    
    # Import agents
    from Agents.overlay.delivery_prioritization_agent import DeliveryPrioritizationAgent
    from Agents.overlay.compartment_loading_agent import CompartmentLoadingAgent
    from Agents.overlay.signal_bus import SignalBus
    
    # Create signal bus
    signal_bus = SignalBus(es_service=elasticsearch_service)
    
    # Create agents with minimal dependencies
    print("\n1. Creating Delivery Prioritization Agent...")
    prioritization_agent = DeliveryPrioritizationAgent(
        signal_bus=signal_bus,
        es_service=elasticsearch_service,
        activity_log_service=None,
        ws_manager=None,
        confirmation_protocol=None,
        autonomy_config_service=None,
        feature_flag_service=None,
    )
    
    print("2. Creating Compartment Loading Agent...")
    loading_agent = CompartmentLoadingAgent(
        signal_bus=signal_bus,
        es_service=elasticsearch_service,
        activity_log_service=None,
        ws_manager=None,
        confirmation_protocol=None,
        autonomy_config_service=None,
        feature_flag_service=None,
    )
    
    # Run prioritization agent
    print("\n3. Running Delivery Prioritization Agent...")
    try:
        priorities = await prioritization_agent.prioritize_fuel_orders(tenant_id=tenant_id)
        if priorities:
            print(f"   ✅ Generated {len(priorities.priorities)} priorities")
            for i, priority in enumerate(priorities.priorities[:10]):
                print(f"      {i+1}. Station: {priority.station_id}, "
                      f"Grade: {priority.fuel_grade}, "
                      f"Score: {priority.priority_score:.3f}, "
                      f"Bucket: {priority.priority_bucket}, "
                      f"Reasons: {priority.reasons}")
        else:
            print(f"   ⚠️  No priorities generated")
        
    except Exception as e:
        print(f"   ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        priorities = None
    
    # Run compartment loading agent
    print("\n4. Running Compartment Loading Agent...")
    if priorities:
        try:
            # Inject the priority list into the loading agent's buffer
            loading_agent._priority_buffer.append(priorities)
            
            # Set run_id
            loading_agent._current_run_id = "debug-run-001"
            
            # Run evaluation
            print("   Calling evaluate()...")
            proposals = await loading_agent.evaluate([])
            print(f"   ✅ Generated {len(proposals)} loading proposals")
            for i, proposal in enumerate(proposals):
                print(f"      {i+1}. Proposal: {proposal.proposal_id}, "
                      f"Severity: {proposal.severity}")
            
            # Check if plans were persisted
            es = elasticsearch_service
            plans_query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"tenant_id": tenant_id}},
                            {"term": {"run_id": "debug-run-001"}},
                        ],
                    },
                },
                "size": 10,
            }
            plans_resp = await es.search_documents("mvp_load_plans", plans_query, 10)
            plans = plans_resp.get("hits", {}).get("hits", [])
            print(f"\n   Found {len(plans)} plans in mvp_load_plans for debug-run-001")
            for plan in plans:
                src = plan["_source"]
                print(f"      - {src.get('plan_id')}: truck={src.get('truck_id')}, "
                      f"{len(src.get('assignments', []))} assignments")
            
        except Exception as e:
            print(f"   ❌ Error: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("   ⏭️  Skipping (no priorities generated)")
    
    print("\n" + "=" * 80)

if __name__ == "__main__":
    asyncio.run(debug_pipeline())
