#!/usr/bin/env python3
"""
Set or reset the password for a provisioned SuperTokens user.

Closes the SuperTokens Auth Migration gap (design Open Question #6): the
provisioning script (``scripts/provision_auth_users.py``) creates each user with
a random password, and no email transport ships with the migration, so there was
no first-class way to establish a credential a user can actually sign in with.

This CLI is the break-glass / local-development path. It either:

  * sets a password directly for a provisioned user (``--password`` /
    interactive prompt), or
  * mints a SuperTokens password-reset link (``--link``) for the operator to
    hand to the user out-of-band, who then sets their own password on the
    self-serve reset page (``/auth/reset-password`` in the frontend).

Both modes require the email to exist in the ``auth_users`` source of truth — a
password can never be set for an un-provisioned address.

Usage:
    # Set a password directly (prompts securely if --password is omitted):
    python -m scripts.set_user_password admin@runsheet.com
    python -m scripts.set_user_password admin@runsheet.com --password 'Demo1234!'

    # Mint a reset link to hand off instead of setting a password:
    python -m scripts.set_user_password admin@runsheet.com --link

Prerequisites:
    * AUTH_PROVIDER must be 'supertokens' with SUPERTOKENS_CONNECTION_URI
      + SUPERTOKENS_API_KEY configured (the SDK must be able to reach the core).
    * DATABASE_URL must point at the PostgreSQL source of truth, and the user
      must already be provisioned (run scripts/provision_auth_users.py first).

Design reference: ``.kiro/specs/supertokens-auth-migration/design.md``
§User_Provisioner (OQ6 follow-up).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

# Ensure the project root is importable when run as a standalone script.
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


async def _run(
    email: str, *, password: Optional[str], make_link: bool
) -> int:
    """Initialize the SDK and either set a password or mint a reset link."""
    from auth.password_admin import (
        PasswordAdminError,
        create_password_set_link,
        set_password_for_email,
    )
    from auth.supertokens_init import init_supertokens
    from config.settings import get_settings

    settings = get_settings()

    # Fails closed (SuperTokensConfigError) when the managed core is not
    # configured — we never operate against a non-functional auth path.
    init_supertokens(settings)

    try:
        if make_link:
            result = await create_password_set_link(email)
            print(f"\n{'=' * 72}")
            print("Password-set link issued")
            print(f"{'=' * 72}")
            print(f"User:  {result.email}")
            print(f"Link:  {result.link}")
            print(
                "\nHand this link to the user out-of-band. Opening it lets them "
                "set their own password on the reset page."
            )
            print(f"{'=' * 72}\n")
            return 0

        if not password:
            password = getpass.getpass("New password: ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                logger.error("Passwords do not match")
                return 1

        await set_password_for_email(email, password)
        print(f"\nPassword set for {email}. They can now sign in.\n")
        return 0
    except PasswordAdminError as exc:
        logger.error("Could not complete the operation: %s", exc.message)
        return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Set/reset the password for a provisioned SuperTokens user, or mint "
            "a reset link to hand off."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("email", help="Email of the provisioned user")
    parser.add_argument(
        "--password",
        default=None,
        help="New password (omit to be prompted securely; ignored with --link)",
    )
    parser.add_argument(
        "--link",
        action="store_true",
        default=False,
        help="Mint a password-reset link instead of setting a password",
    )

    args = parser.parse_args(argv)

    try:
        return asyncio.run(
            _run(args.email, password=args.password, make_link=args.link)
        )
    except Exception as exc:  # noqa: BLE001 — clean CLI failure
        logger.error("Aborted: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
