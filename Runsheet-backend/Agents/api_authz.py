"""Who may reach the agent control surface (``/api/agent/*``).

Before this module exactly one of the fourteen routes in ``agent_endpoints.py``
performed a role check: ``PATCH /config/autonomy``, via an inline
``if "admin" not in tenant.roles``. Verified against a running server, the
``driver`` account could:

* ``GET  /api/agent/health``            -> 200
* ``GET  /api/agent/approvals``         -> 200
* ``GET  /api/agent/config/autonomy``   -> 200
* ``POST /api/agent/{agent_id}/pause``  -> 200  (paused a live autonomous agent)
* ``POST /api/agent/{agent_id}/resume`` -> 200

The frontend passes an ``isAdmin`` flag to ``AutonomySection`` only, so the
pause/resume and memory-delete controls were never even hidden — and hiding is
presentation, not enforcement.

Policy vs operation
-------------------
The split below follows the boundary the agent design already implies, and which
``OperationsControlView`` states outright ("the supervisor needs this state
visible alongside the live activity feed and pause controls in the right rail"):

* **Policy and lifecycle are the tenant admin's.** How much autonomy the agents
  may exercise, whether an agent runs at all, and what long-lived memory they
  retain are tenant-wide settings that outlive any one shift.
* **The approval queue is the dispatcher's.** Agents *propose* work — delivery
  prioritisation, route planning, delay response. The human-in-the-loop who
  accepts or rejects those proposals is the dispatcher running the shift. Making
  approve/reject admin-only would leave dispatchers staring at a queue they
  cannot action, which defeats the human-in-the-loop design rather than securing
  it.

Reads therefore go to both roles, because three non-admin surfaces already depend
on them:

* ``/dashboard`` -> ``DispatchCockpit`` calls ``getApprovals`` for its pending
  agent-proposal count. This is the dispatcher's landing page.
* ``Header`` -> ``NotificationBell`` calls ``getActivityLog`` + ``getApprovals``
  on every ``/dashboard/*`` page.
* ``/ops/control`` -> ``OperationsControlView`` renders ``AgentAutonomyBanner``,
  ``AgentActivityFeed``, ``AgentHealth`` and ``ApprovalQueuePanel``.

Both owning nav items (``today``, ``control``) already carry
``requiredRoles: ["admin", "dispatcher"]`` in ``runsheet/src/config/modules.ts``,
so this matches the audience the UI was already built for.

Drivers get nothing here. The dashboard shell's ``hasAnyNavAccess`` check already
shows them "This workspace is for dispatchers and administrators" and never
renders ``Header``, so no driver reaches these routes through the UI — but a
driver *token* did, which is the hole this closes.
"""

from __future__ import annotations

from auth.router_guards import roles_dependency

#: Operations roles — the shift audience. Applied at the router level so a route
#: added later inherits it instead of defaulting to no gate at all (the failure
#: mode that left 13 of 14 routes open here).
AGENT_OPS_ROLES: tuple[str, ...] = ("admin", "dispatcher")

#: Tenant-wide agent policy and lifecycle. A strict subset of
#: :data:`AGENT_OPS_ROLES`, layered on top of the router-level gate for the
#: handful of routes that change how the agents behave for everyone.
AGENT_ADMIN_ROLES: tuple[str, ...] = ("admin",)

#: Router-level dependency: reads and the approval queue.
agent_ops_dependency = roles_dependency(*AGENT_OPS_ROLES)

#: Per-route dependency: autonomy level, agent pause/resume, memory deletion.
agent_admin_dependency = roles_dependency(*AGENT_ADMIN_ROLES)
