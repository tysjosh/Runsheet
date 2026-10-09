"""Set, show or clear a tenant's customer-facing display name (portal PE1).

The name appears as the supplier on the customer portal (Home, Account),
the invoice PDF "From" line and the portal invite email. It is stored in
Redis under ``tenant:{tenant_id}:display_name`` with no TTL (see
``services/tenant_settings.py``), so it persists until changed or cleared.

Run where ``REDIS_URL`` reaches the app's Redis — on staging, as a one-shot
ECS task on the deployed task definition (the script's ``--help`` lists the
flags)::

    python -m scripts.set_tenant_display_name --tenant demo-tenant --show
    python -m scripts.set_tenant_display_name --tenant demo-tenant --name "Acme Fuels"
    python -m scripts.set_tenant_display_name --tenant demo-tenant --clear

Without a name the portal falls back to the tenant id.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional, Sequence

from services.tenant_settings import TenantSettingsService


async def run(
    service: TenantSettingsService,
    tenant_id: str,
    *,
    name: Optional[str] = None,
    clear: bool = False,
) -> Optional[str]:
    """Apply the change (if any) and return the name now stored."""
    if clear:
        await service.set_display_name(tenant_id, None)
    elif name is not None:
        await service.set_display_name(tenant_id, name)
    return await service.get_display_name(tenant_id)


def _parse(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", required=True, help="tenant id, e.g. demo-tenant")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--name", help="display name to store (1-120 chars)")
    action.add_argument("--clear", action="store_true", help="remove the name")
    action.add_argument("--show", action="store_true", help="print the stored name")
    args = parser.parse_args(argv)
    if args.name is not None and not " ".join(args.name.split()):
        parser.error("--name must not be blank; use --clear to remove the name")
    return args


async def _main(args: argparse.Namespace) -> int:
    import redis.asyncio as redis

    url = os.environ.get("REDIS_URL")
    if not url:
        print("REDIS_URL is not set", file=sys.stderr)
        return 2
    client = redis.from_url(url, decode_responses=True)
    try:
        stored = await run(
            TenantSettingsService(redis_client=client),
            args.tenant,
            name=args.name,
            clear=args.clear,
        )
    finally:
        await client.aclose()
    print(f"TENANT_DISPLAY_NAME tenant={args.tenant} name={stored!r}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    return asyncio.run(_main(_parse(argv)))


if __name__ == "__main__":
    sys.exit(main())
