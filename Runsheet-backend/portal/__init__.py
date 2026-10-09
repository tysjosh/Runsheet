"""Customer portal (OI-06): the ``customer`` identity's API surface.

See ``.kiro/specs/customer-portal/design.md``. Isolation has three layers:
the central default-deny (:mod:`portal.scope`), the portal guard
(:mod:`portal.api._authz`), and scoped readers (later FEATs).
"""
