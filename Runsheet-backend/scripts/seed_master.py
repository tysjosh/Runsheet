#!/usr/bin/env python3
"""
Master seed orchestrator - handles complete database setup in correct order.

This script coordinates all seeding operations:
1. Recreates indices with proper mappings (optional)
2. Loads JSON seed files (compliance, notifications, fuel ops, etc.)
3. Generates programmatic seed data (trucks, jobs, agents, commerce)

Usage:
    # Full reset and seed (DESTRUCTIVE - deletes all data):
    SEED_TENANT_ID=demo-tenant python3 scripts/seed_master.py --recreate --force
    
    # Seed only (preserves existing data, fills empty indices):
    SEED_TENANT_ID=demo-tenant python3 scripts/seed_master.py
    
    # Force re-seed all data without recreating indices:
    SEED_TENANT_ID=demo-tenant python3 scripts/seed_master.py --force
    
    # Skip JSON seeds, only generate programmatic data:
    SEED_TENANT_ID=demo-tenant python3 scripts/seed_master.py --skip-json
    
    # Skip programmatic seeds, only load JSON:
    SEED_TENANT_ID=demo-tenant python3 scripts/seed_master.py --skip-programmatic

Environment Variables:
    SEED_TENANT_ID: Required. Tenant ID for all seeded data (e.g., demo-tenant)
"""

import sys
import os
import subprocess
from pathlib import Path
from dotenv import load_dotenv

# Load environment
env_file = Path(__file__).parent.parent / ".env.development"
if env_file.exists():
    load_dotenv(env_file)

TENANT_ID = os.getenv("SEED_TENANT_ID", "").strip()
SCRIPT_DIR = Path(__file__).parent


def print_banner(text: str, char: str = "="):
    """Print a formatted banner."""
    print(f"\n{char * 70}")
    print(f"  {text}")
    print(f"{char * 70}\n")


def run_script(script_name: str, args: list = None) -> bool:
    """Run a Python script and return success status."""
    script_path = SCRIPT_DIR / script_name if "/" not in script_name else Path(script_name)
    
    if not script_path.exists():
        # Try parent directory for non-script files
        script_path = SCRIPT_DIR.parent / script_name
    
    if not script_path.exists():
        print(f"❌ Script not found: {script_path}")
        return False
    
    cmd = ["python3", str(script_path)]
    if args:
        cmd.extend(args)
    
    print(f"🔄 Running: {' '.join(cmd)}")
    print("-" * 70)
    
    try:
        result = subprocess.run(
            cmd,
            env={**os.environ, "SEED_TENANT_ID": TENANT_ID},
            cwd=SCRIPT_DIR.parent,
            check=False
        )
        
        if result.returncode == 0:
            print(f"✅ {script_name} completed successfully")
            return True
        else:
            print(f"⚠️  {script_name} exited with code {result.returncode}")
            return False
    except Exception as e:
        print(f"❌ Error running {script_name}: {e}")
        return False


def main():
    """Orchestrate the complete seeding process."""
    
    # Parse arguments
    recreate = "--recreate" in sys.argv
    force = "--force" in sys.argv
    skip_json = "--skip-json" in sys.argv
    skip_programmatic = "--skip-programmatic" in sys.argv
    
    # Validate tenant ID
    if not TENANT_ID:
        print("❌ ERROR: SEED_TENANT_ID environment variable is required")
        print("\nUsage:")
        print("  SEED_TENANT_ID=demo-tenant python3 scripts/seed_master.py [options]")
        print("\nOptions:")
        print("  --recreate          Delete and recreate all indices (DESTRUCTIVE)")
        print("  --force             Force re-seed all data (overwrites existing)")
        print("  --skip-json         Skip JSON seed files")
        print("  --skip-programmatic Skip programmatic seed generation")
        sys.exit(1)
    
    print_banner("Runsheet Master Seed Orchestrator", "=")
    print(f"📋 Configuration:")
    print(f"   Tenant ID: {TENANT_ID}")
    print(f"   Recreate indices: {'YES' if recreate else 'NO'}")
    print(f"   Force re-seed: {'YES' if force else 'NO'}")
    print(f"   Skip JSON seeds: {'YES' if skip_json else 'NO'}")
    print(f"   Skip programmatic seeds: {'YES' if skip_programmatic else 'NO'}")
    
    # Confirm destructive operations
    if recreate:
        print("\n⚠️  WARNING: --recreate will DELETE ALL DATA in Elasticsearch indices!")
        response = input("\nType 'YES' to proceed with index recreation: ")
        if response != "YES":
            print("❌ Aborted. No changes made.")
            sys.exit(0)
    
    results = []
    
    # Step 1: Recreate indices (optional, destructive)
    if recreate:
        print_banner("Step 1: Recreating Indices", "-")
        success = run_script("scripts/recreate_indices.py")
        results.append(("Recreate Indices", success))
        
        if not success:
            print("\n⚠️  Index recreation failed. Continuing anyway...")
    else:
        print_banner("Step 1: Skipping Index Recreation", "-")
        print("ℹ️  Using existing indices. Use --recreate to delete and recreate.")
    
    # Step 2: Load JSON seeds
    if not skip_json:
        print_banner("Step 2: Loading JSON Seed Files", "-")
        args = ["--force"] if force else []
        success = run_script("scripts/load_json_seeds.py", args)
        results.append(("Load JSON Seeds", success))
        
        if not success:
            print("\n⚠️  JSON seed loading had issues. Continuing anyway...")
    else:
        print_banner("Step 2: Skipping JSON Seeds", "-")
        print("ℹ️  Skipped JSON seed files (--skip-json flag)")
    
    # Step 3: Generate programmatic seeds
    if not skip_programmatic:
        print_banner("Step 3: Generating Programmatic Seed Data", "-")
        args = ["--force"] if force else []
        success = run_script("seed_all_data.py", args)
        results.append(("Generate Programmatic Seeds", success))
        
        if not success:
            print("\n⚠️  Programmatic seed generation had issues.")
    else:
        print_banner("Step 3: Skipping Programmatic Seeds", "-")
        print("ℹ️  Skipped programmatic seed generation (--skip-programmatic flag)")
    
    # Summary
    print_banner("Seeding Summary", "=")
    
    for step_name, success in results:
        status = "✅ SUCCESS" if success else "❌ FAILED"
        print(f"  {status:12} {step_name}")
    
    all_success = all(success for _, success in results)
    
    if all_success:
        print(f"\n🎉 All seeding operations completed successfully!")
        print(f"\n📊 Next steps:")
        print(f"   1. Verify data: python3 -c \"from elasticsearch import Elasticsearch; ...")
        print(f"   2. Start backend: python3 main.py")
        print(f"   3. Start frontend: cd runsheet && npm run dev")
    else:
        print(f"\n⚠️  Some operations failed. Check logs above for details.")
        sys.exit(1)
    
    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    main()
