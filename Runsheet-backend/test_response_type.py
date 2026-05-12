#!/usr/bin/env python3
"""Test ObjectApiResponse behavior"""
import asyncio
import sys
import os

sys.path.insert(0, '/Users/olukotunjosh/Downloads/Runsheet/Runsheet-backend')
os.environ['ENVIRONMENT'] = 'development'

from dotenv import load_dotenv
load_dotenv('/Users/olukotunjosh/Downloads/Runsheet/Runsheet-backend/.env.development', override=True)

from services.elasticsearch_service import elasticsearch_service
from ops.middleware.tenant_guard import inject_tenant_filter

async def main():
    query = inject_tenant_filter({'query': {'match_all': {}}}, 'demo-tenant')
    query['size'] = 100
    
    resp = await elasticsearch_service.search_documents('drivers_current', query, 100)
    
    print(f"Type: {type(resp)}")
    print(f"Is dict: {isinstance(resp, dict)}")
    print(f"Has .get method: {hasattr(resp, 'get')}")
    
    # Try to access like a dict
    try:
        hits_outer = resp.get("hits")
        print(f"\nresp.get('hits') works: {hits_outer is not None}")
        if hits_outer:
            hits = hits_outer.get("hits")
            print(f"hits_outer.get('hits') works: {hits is not None}")
            print(f"Number of hits: {len(hits) if hits else 0}")
    except Exception as e:
        print(f"Error accessing as dict: {e}")
    
    # Try dict() conversion
    try:
        resp_dict = dict(resp)
        print(f"\ndict(resp) works: True")
        print(f"dict keys: {list(resp_dict.keys())}")
    except Exception as e:
        print(f"dict(resp) failed: {e}")

if __name__ == '__main__':
    asyncio.run(main())
