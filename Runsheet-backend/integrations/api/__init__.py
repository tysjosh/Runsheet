"""
REST API surface for Capability 5 — Integration Layer Expansion.

Task 9.3 of the fuel-ops-hardening spec mounts tenant-scoped CRUD,
lifecycle, and catalog endpoints under ``/api/integrations``. The
router and its ``configure_integrations_endpoints`` wire-up entry
point live in :mod:`integrations.api.integrations_endpoints` to keep
the package importable without triggering FastAPI route registration
for tests that only need the per-provider adapters.

Validates: Requirements 5.1.7, 5.1.8, 5.6.2, 5.6.6.
"""
