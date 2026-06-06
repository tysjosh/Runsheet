#!/usr/bin/env python3
"""
Issue a development/test authentication context via the Test_Auth_Path.

Historically this script minted a homegrown HS256 JWT signed with a hardcoded
development signing secret. That scheme is gone: as part
of the SuperTokens Auth Migration the backend verifies SuperTokens sessions and
no longer trusts a browser/CLI-minted token (Req 10.2, 10.5). There is no
hardcoded signing secret anywhere in source.

Instead, this script exercises the supported **Test_Auth_Path**
(``auth/test_auth.py``), which builds a verified ``TenantContext`` — the
``Auth_Context`` of the requirements glossary — for a given ``tenant_id`` / role
set / ``has_pii_access`` value without driving the production sign-in UI
(Req 11.1). The Test_Auth_Path is available only in the ``test`` and
``development`` environments and fails closed in production (Req 11.3).

How tests should use it
-----------------------
Automated tests do not pass a ``Bearer`` token any more. They install the
context for the duration of a request using the ``override_auth`` context
manager, which wires ``app.dependency_overrides[get_tenant_context]`` and the
``AuthEnforcementMiddleware`` bypass::

    from auth.test_auth import override_auth

    with override_auth(app, tenant_id="demo-tenant", roles=["admin"]):
        response = client.get("/some/protected/route")

This script prints the context that such a call would yield, so you can confirm
the tenant scope / roles / PII flag a given invocation produces.

Usage:
    python scripts/generate_dev_token.py
    python scripts/generate_dev_token.py --tenant-id custom-tenant --user-id user@example.com
    python scripts/generate_dev_token.py --roles admin dispatcher --no-pii-access
"""

import argparse
import os
import sys

# Ensure the backend project root is importable when run as a script
# (``python scripts/generate_dev_token.py``).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Issue a development/test TenantContext via the Test_Auth_Path "
            "(no legacy JWT, no signing secret)."
        )
    )
    parser.add_argument(
        "--tenant-id",
        default="demo-tenant",
        help="Tenant ID (default: demo-tenant)",
    )
    parser.add_argument(
        "--user-id",
        default="admin@runsheet.com",
        help="User ID / email (default: admin@runsheet.com)",
    )
    parser.add_argument(
        "--roles",
        nargs="+",
        default=["admin", "ops_manager"],
        help="User roles (default: admin ops_manager)",
    )
    parser.add_argument(
        "--no-pii-access",
        action="store_true",
        help="Disable PII access (default: enabled)",
    )

    args = parser.parse_args()

    # Imported lazily so ``--help`` works without loading settings, and so the
    # Test_Auth_Path environment guard is evaluated only when actually issuing
    # a context (it raises in production — Req 11.3).
    from auth.test_auth import TestAuthPathDisabledError, issue_test_context

    try:
        context = issue_test_context(
            tenant_id=args.tenant_id,
            roles=args.roles,
            has_pii_access=not args.no_pii_access,
            user_id=args.user_id,
        )
    except TestAuthPathDisabledError as exc:
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        print(
            "The Test_Auth_Path is available only in the 'test' and "
            "'development' environments. Set ENVIRONMENT=development and "
            "retry.\n",
            file=sys.stderr,
        )
        return 1

    bar = "=" * 80
    print(f"\n{bar}")
    print("Test_Auth_Path context issued successfully")
    print(bar)
    print(f"\nTenant ID:     {context.tenant_id}")
    print(f"User ID:       {context.user_id}")
    print(f"Roles:         {', '.join(context.roles) if context.roles else '(none)'}")
    print(f"PII Access:    {context.has_pii_access}")
    print(f"Region:        {context.region}")
    print(f"\n{bar}")
    print("Use this context in a test by wrapping the request:")
    print(bar)
    print("from auth.test_auth import override_auth\n")
    roles_literal = ", ".join(repr(r) for r in context.roles)
    print(
        "with override_auth(\n"
        f"    app, tenant_id={context.tenant_id!r}, "
        f"roles=[{roles_literal}],\n"
        f"    has_pii_access={context.has_pii_access},\n"
        "):\n"
        "    response = client.get(\"/some/protected/route\")\n"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
