-- Development-only demo accounts, one per canonical role.
--
-- Companion to the single `admin@runsheet.com` row seeded by migration
-- `0002_auth_users`. This file exists so the local role-gating loop can be
-- exercised for every role in `auth.supertokens_init.CANONICAL_ROLES` without
-- hand-typing INSERTs, and so the set is reproducible after a database reset.
--
-- These rows are the *provisioning source of truth* only. They do not create
-- SuperTokens users by themselves — run the provisioner afterwards:
--
--   ENVIRONMENT=development ./venv/bin/python -m scripts.provision_auth_users
--   ENVIRONMENT=development ./venv/bin/python -m scripts.set_user_password <email> --password '...'
--
-- `ops_manager` is deliberately absent: it was retired by
-- `0006_retire_ops_manager_role` because it gated nothing.
--
-- NEVER load this against a shared or production database. Every address is
-- under the reserved-for-testing `demo.runsheet.test` domain so none of them can
-- collide with, or send mail to, a real mailbox.

-- dispatcher — the primary operational role. Sees every nav item except the
-- Admin section and the Feature Flags tab.
INSERT INTO auth_users (email, tenant_id, roles, has_pii_access)
VALUES (
    'dispatcher@demo.runsheet.test',
    'demo-tenant',
    ARRAY['dispatcher']::text[],
    TRUE  -- dispatchers phone customers, so they need contact PII.
)
ON CONFLICT (email) DO NOTHING;

-- platform_admin ALONE. Deliberately not paired with `admin`, to make the
-- additive-role design observable: `require_role('admin')` refuses this account,
-- and the web nav shows it *nothing* — the only descriptors it matches are the
-- Tier 4 commerce surfaces, which `mvpMode` hides as well, so it lands on the
-- shell's "for dispatchers and administrators" wall. (It used to see Settings;
-- that module was folded into AdminHub as the admin-only `agent-settings` tab.)
-- This is the account that proves platform_admin implies nothing.
INSERT INTO auth_users (email, tenant_id, roles, has_pii_access)
VALUES (
    'platform.admin@demo.runsheet.test',
    'demo-tenant',
    ARRAY['platform_admin']::text[],
    FALSE
)
ON CONFLICT (email) DO NOTHING;

-- The shape real Runsheet staff should hold: `platform_admin` for cross-tenant
-- reach, `admin` for authority inside the tenant they are acting on.
INSERT INTO auth_users (email, tenant_id, roles, has_pii_access)
VALUES (
    'staff@demo.runsheet.test',
    'demo-tenant',
    ARRAY['admin', 'platform_admin']::text[],
    TRUE
)
ON CONFLICT (email) DO NOTHING;
