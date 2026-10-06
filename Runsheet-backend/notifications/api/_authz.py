"""Who may change notification configuration (C2).

Before this module every route under ``/api/notifications`` resolved a
``TenantContext`` and performed no role check, so any authenticated user,
including a ``driver``, could edit a tenant's notification rules and
templates, rewrite a customer's preferences, or retry a send.

Writes take ``admin`` + ``dispatcher``, the audience of the ``notifications``
nav item in ``runsheet/src/config/modules.ts``. The gated writes are the rule
and template edits plus the other writes of the same kind: customer
preferences, template opt-outs and retry. Their only caller is that
admin/dispatcher notifications UI (``runsheet/src/services/notificationApi.ts``);
the driver app doesn't call them. Reads are unchanged.
"""

from __future__ import annotations

from auth.router_guards import roles_dependency

#: Rule/template edits, preference writes and retry.
NOTIFICATION_WRITE_ROLES: tuple[str, ...] = ("admin", "dispatcher")

#: Dependency for notification writes.
notification_write_dependency = roles_dependency(*NOTIFICATION_WRITE_ROLES)
