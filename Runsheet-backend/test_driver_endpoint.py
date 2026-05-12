#!/usr/bin/env python3
"""Test script to debug driver endpoint"""
import asyncio
import sys
import os

sys.path.insert(0, '/Users/olukotunjosh/Downloads/Runsheet/Runsheet-backend')
os.environ['ENVIRONMENT'] = 'development'

from dotenv import load_dotenv
load_dotenv('/Users/olukotunjosh/Downloads/Runsheet/Runsheet-backend/.env.development', override=True)

from fuel.driver_repository import DriverRepository
from services.elasticsearch_service import elasticsearch_service

async def main():
    print("Testing DriverRepository...")
    
    repo = DriverRepository(elasticsearch_service)
    print(f"Repository created: {repo}")
    print(f"ES service: {elasticsearch_service}")
    print(f"ES client: {elasticsearch_service.client}")
    
    print("\nCalling list_for_tenant('demo-tenant')...")
    drivers = await repo.list_for_tenant('demo-tenant', size=100)
    
    print(f"\nResult: {len(drivers)} drivers")
    for driver in drivers:
        print(f"  - {driver.driver_id}: {driver.driver_name} (status: {driver.status})")

if __name__ == '__main__':
    asyncio.run(main())
