#!/usr/bin/env python3
"""
Elasticsearch Data Cleanup Script
Clears duplicate data and reseeds with fresh data
"""

import asyncio
import logging
import os
import sys
from config.settings import Environment, get_settings
from services.data_seeder import data_seeder

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def main():
    """Main cleanup function"""
    try:
        settings = get_settings()
        if settings.environment == Environment.PRODUCTION:
            raise RuntimeError(
                "cleanup_data.py is disabled in production; use tenant-scoped "
                "administrative tooling instead."
            )
        print("🧹 Starting Elasticsearch data cleanup...")
        print("=" * 50)
        
        # Clear all existing data
        print("🗑️  Step 1: Clearing existing data...")
        await data_seeder.clear_all_data()
        print("✅ Existing data cleared")
        
        # Reseed with baseline morning data
        print("\n🌅 Step 2: Seeding baseline morning operations...")
        await data_seeder.seed_baseline_data(operational_time="09:00")
        print("✅ Baseline morning data seeded")
        
        print("\n" + "=" * 50)
        print("🎉 Cleanup completed successfully!")
        print("📊 Your Elasticsearch indices now have baseline morning operations data")
        print("🔄 Ready for temporal data demo - upload afternoon/evening data via frontend")
        print("💡 Use the Data Upload component to simulate operational changes")
        
    except Exception as e:
        logger.exception("Cleanup failed")
        print(f"\n💥 Error during cleanup: {e}")
        sys.exit(1)

if __name__ == "__main__":
    print("Runsheet Logistics - Data Cleanup Script")
    print("This will clear all existing data and reseed with fresh data.")

    if os.environ.get("ENVIRONMENT", "").lower() == "production":
        print("❌ Cleanup refused: ENVIRONMENT=production")
        sys.exit(1)
    
    # Confirm with user
    confirm = input("\nDo you want to continue? (y/N): ").lower().strip()
    if confirm not in ['y', 'yes']:
        print("❌ Cleanup cancelled")
        sys.exit(0)
    
    # Run cleanup
    asyncio.run(main())
