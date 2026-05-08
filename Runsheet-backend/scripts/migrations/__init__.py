# Migrations sub-package for one-time fuel-ops hardening migrations.
#
# Each migration is a standalone Python module with an ``async def main()``
# entrypoint and an idempotent write path so re-running the script against
# a partially migrated environment is safe.
