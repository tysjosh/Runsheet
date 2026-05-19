#!/usr/bin/env python3
"""
Test script to verify TankForecastingAgent fix generates non-zero forecasts.
"""
import asyncio
import sys
import os

# Set environment
os.environ['ENVIRONMENT'] = 'development'
os.environ['TENANT_ID'] = 'demo-tenant'

from services.elasticsearch_service import elasticsearch_service
from Agents.agent_ws_manager import AgentActivityWSManager
from Agents.overlay.signal_bus import SignalBus
from Agents.overlay.tank_forecasting_agent import TankForecastingAgent
from Agents.overlay.delivery_prioritization_agent import DeliveryPrioritizationAgent
from Agents.overlay.compartment_loading_agent import CompartmentLoadingAgent
from Agents.overlay.route_planning_agent import RoutePlanningAgent
from Agents.support.fuel_distribution_pipeline import FuelDistributionPipeline

async def test_pipeline():
    # Initialize services
    es = elasticsearch_service
    ws_manager = AgentActivityWSManager()
    signal_bus = SignalBus(es_service=es)
    
    # Mock services
    class MockService:
        async def log_activity(self, *args, **kwargs): 
            pass
        async def get_mode(self, *args, **kwargs): 
            return 'active_auto'
        async def is_enabled(self, *args, **kwargs): 
            return True
        async def route_proposal(self, *args, **kwargs): 
            pass
    
    mock = MockService()
    
    # Initialize agents
    tank_agent = TankForecastingAgent(
        signal_bus=signal_bus,
        es_service=es,
        activity_log_service=mock,
        ws_manager=ws_manager,
        confirmation_protocol=mock,
        autonomy_config_service=mock,
        feature_flag_service=mock
    )
    
    priority_agent = DeliveryPrioritizationAgent(
        signal_bus=signal_bus,
        es_service=es,
        activity_log_service=mock,
        ws_manager=ws_manager,
        confirmation_protocol=mock,
        autonomy_config_service=mock,
        feature_flag_service=mock
    )
    
    loading_agent = CompartmentLoadingAgent(
        signal_bus=signal_bus,
        es_service=es,
        activity_log_service=mock,
        ws_manager=ws_manager,
        confirmation_protocol=mock,
        autonomy_config_service=mock,
        feature_flag_service=mock
    )
    
    route_agent = RoutePlanningAgent(
        signal_bus=signal_bus,
        es_service=es,
        activity_log_service=mock,
        ws_manager=ws_manager,
        confirmation_protocol=mock,
        autonomy_config_service=mock,
        feature_flag_service=mock
    )
    
    # Create pipeline
    agents = {
        'tank_forecasting': tank_agent,
        'delivery_prioritization': priority_agent,
        'compartment_loading': loading_agent,
        'route_planning': route_agent
    }
    
    pipeline = FuelDistributionPipeline(
        agents=agents,
        ws_manager=ws_manager,
        signal_bus=signal_bus
    )
    
    # Run pipeline
    print('Starting pipeline execution...')
    print('Checking if stations have data...')
    
    # Check stations before pipeline
    stations_query = {'query': {'term': {'tenant_id': 'demo-tenant'}}, 'size': 20}
    stations_result = await es.search_documents('fuel_stations', stations_query, 20)
    print(f'Stations available: {len(stations_result.get("hits", {}).get("hits", []))}')
    
    run_id = await pipeline.run('demo-tenant')
    print(f'Pipeline completed with run_id: {run_id}')
    
    # Check forecasts
    query = {
        'query': {
            'bool': {
                'must': [
                    {'term': {'tenant_id': 'demo-tenant'}},
                    {'term': {'run_id': run_id}}
                ]
            }
        },
        'size': 100
    }
    
    forecasts = await es.search_documents('mvp_tank_forecasts', query, 100)
    print(f'\nForecasts created: {len(forecasts.get("hits", {}).get("hits", []))}')
    
    for hit in forecasts.get('hits', {}).get('hits', [])[:5]:
        src = hit['_source']
        print(f'  Station: {src.get("station_id")}, Grade: {src.get("fuel_grade")}, Hours to runout P90: {src.get("hours_to_runout_p90")}')
    
    # Check plans
    plans_query = {
        'query': {
            'bool': {
                'must': [
                    {'term': {'tenant_id': 'demo-tenant'}},
                    {'term': {'run_id': run_id}}
                ]
            }
        },
        'size': 100
    }
    
    plans = await es.search_documents('mvp_loading_plans', plans_query, 100)
    print(f'\nPlans created: {len(plans.get("hits", {}).get("hits", []))}')
    
    for hit in plans.get('hits', {}).get('hits', [])[:3]:
        src = hit['_source']
        print(f'  Plan: {src.get("plan_id")}, Truck: {src.get("truck_id")}, Deliveries: {len(src.get("deliveries", []))}')
    
    return len(plans.get("hits", {}).get("hits", []))

if __name__ == '__main__':
    plans_count = asyncio.run(test_pipeline())
    if plans_count > 0:
        print(f'\n✅ SUCCESS: Pipeline generated {plans_count} plans!')
        sys.exit(0)
    else:
        print('\n❌ FAILURE: Pipeline generated 0 plans')
        sys.exit(1)
