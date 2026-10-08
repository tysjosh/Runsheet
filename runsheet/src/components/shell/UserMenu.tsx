"use client";

/**
 * User menu in the top bar (R2.1): the account's initials open Profile and
 * Sign out. Replaces the sidebar footer card.
 */
import { LogOut, User } from "lucide-react";
import { initials } from "../../lib/identity";
import { Menu } from "../ui/Menu";

export interface UserMenuProps {
  email?: string | null;
  onProfile: () => void;
  onSignOut: () => void;
}

export default function UserMenu({
  email,
  onProfile,
  onSignOut,
}: UserMenuProps) {
  const label = email || "My account";
  return (
    <Menu
      label="Account"
      items={[
        { id: "who", label: label, disabled: true },
        {
          id: "profile",
          label: "Profile",
          icon: <User className="h-4 w-4" />,
          onSelect: onProfile,
        },
        {
          id: "signout",
          label: "Sign out",
          icon: <LogOut className="h-4 w-4" />,
          onSelect: onSignOut,
        },
      ]}
      trigger={(p) => (
        <button
          {...p}
          type="button"
          aria-label={`Account menu for ${label}`}
          title={label}
          className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-brand-700 text-xs font-bold text-white hover:bg-brand-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-1"
        >
          <span aria-hidden="true">
            {email ? (
              initials(email.split("@")[0].replace(/[._]/g, " "))
            ) : (
              <User className="h-4 w-4" />
            )}
          </span>
        </button>
      )}
    />
  );
}
