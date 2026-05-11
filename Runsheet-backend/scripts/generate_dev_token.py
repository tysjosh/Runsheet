#!/usr/bin/env python3
"""
Generate a development JWT token for testing.

Usage:
    python scripts/generate_dev_token.py
    python scripts/generate_dev_token.py --tenant-id custom-tenant --user-id user@example.com
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

from jose import jwt


def generate_token(
    tenant_id: str = "demo-tenant",
    user_id: str = "admin@runsheet.com",
    has_pii_access: bool = True,
    roles: list[str] = None,
    expiry_hours: int = 24,
    jwt_secret: str = "dev-jwt-secret-change-me-in-production",
    jwt_algorithm: str = "HS256",
) -> str:
    """Generate a JWT token with the specified claims."""
    if roles is None:
        roles = ["admin", "ops_manager"]

    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=expiry_hours)

    payload = {
        "tenant_id": tenant_id,
        "sub": user_id,
        "user_id": user_id,
        "has_pii_access": has_pii_access,
        "roles": roles,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }

    token = jwt.encode(payload, jwt_secret, algorithm=jwt_algorithm)
    return token


def main():
    parser = argparse.ArgumentParser(
        description="Generate a development JWT token for testing"
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
    parser.add_argument(
        "--expiry-hours",
        type=int,
        default=24,
        help="Token expiry in hours (default: 24)",
    )
    parser.add_argument(
        "--jwt-secret",
        default="dev-jwt-secret-change-me-in-production",
        help="JWT secret key (default: dev secret)",
    )

    args = parser.parse_args()

    token = generate_token(
        tenant_id=args.tenant_id,
        user_id=args.user_id,
        has_pii_access=not args.no_pii_access,
        roles=args.roles,
        expiry_hours=args.expiry_hours,
        jwt_secret=args.jwt_secret,
    )

    print(f"\n{'='*80}")
    print("JWT Token Generated Successfully")
    print(f"{'='*80}")
    print(f"\nTenant ID:     {args.tenant_id}")
    print(f"User ID:       {args.user_id}")
    print(f"Roles:         {', '.join(args.roles)}")
    print(f"PII Access:    {not args.no_pii_access}")
    print(f"Expires:       {args.expiry_hours} hours from now")
    print(f"\n{'='*80}")
    print("Token:")
    print(f"{'='*80}")
    print(token)
    print(f"{'='*80}\n")
    print("Use this token in the Authorization header:")
    print(f"Authorization: Bearer {token}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
