#!/usr/bin/env python3
"""Test script to debug elasticsearch search"""
import asyncio
import sys
import os
import json

sys.path.insert(0, '/Users/olukotunjosh/Downloads/Runsheet/Runsheet-backend')
os.environ['ENVIRONMENT'] = 'development'

from dotenv import load_dotenv
load_dotenv('/Users/olukotunjosh/Downloads/Runsheet/Runsheet-backend/.env.development', override=True)

from services.elasticsearch_service import elasticsearch_service
from ops.middleware.tenant_guard import inject_tenant_filter

async def main():
    print("Testing Elasticsearch search...")
    
    # Build query
    query = inject_tenant_filter(
        {'query': {'match_all': {}}},
        'demo-tenant',
    )
    query['size'] = 100
    query['sort'] = [{'created_at': {'order': 'desc'}}]
    
    print(f"\nQuery:")
    print(json.dumps(query, indent=2))
    
    print(f"\nCalling search_documents...")
    try:
        resp = await elasticsearch_service.search_documents(
            'drivers_current', query, 100
        )
        
        print(f"\nResponse type: {type(resp)}")
        print(f"Response keys: {resp.keys() if isinstance(resp, dict) else 'N/A'}")
        
        if isinstance(resp, dict):
            hits = resp.get('hits', {})
            print(f"Hits total: {hits.get('total', {})}")
            print(f"Hits count: {len(hits.get('hits', []))}")
            
            for hit in hits.get('hits', [])[:3]:
                source = hit.get('_source', {})
                print(f"  - {source.get('driver_id')}: {source.get('driver_name')}")
        else:
            print(f"Unexpected response: {resp}")
            
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(main())
