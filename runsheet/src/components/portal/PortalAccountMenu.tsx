"use client";

/**
 * Account button and its menu (D19, R14.5): the signed-in email, the
 * supplier, a link to the read-only Account page (PE6, v1.1) and Sign out.
 *
 * A disclosure (button + panel), not an ARIA `menu`, because it mixes text
 * with two actions; every action is 44 px tall on phones (R14.19), which the
 * shared 32 px `Menu` items aren't. Escape and an outside click close it and
 * focus returns to the button.
 */
import { LogOut, UserRound } from "lucide-react";
import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { IdentityAvatar } from "../ui/IdentityAvatar";
import { focusRing } from "./styles";

/**
 * The signed-in person, not the customer account (portal-fixes A3): the
 * email's local part, so "olukotunjosh@…" → "OL" and "jane.doe@…" → "JD",
 * the same `initials()` rule the staff `IdentityAvatar` uses.
 */
export function avatarLabel(email: string): string {
  return (email.split("@")[0] ?? "").trim();
}

export default function PortalAccountMenu({
  email,
  supplierName,
  customerName,
  onSignOut,
  signingOut,
}: {
  email: string;
  supplierName: string;
  customerName: string;
  onSignOut: () => void;
  signingOut: boolean;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (panelRef.current?.contains(t) || buttonRef.current?.contains(t))
        return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const item = `flex h-11 w-full items-center gap-2 rounded-lg px-3 text-left text-[15px] font-semibold text-slate-800 hover:bg-slate-100 md:h-9 md:text-sm ${focusRing}`;

  return (
    <div className="relative ml-auto shrink-0">
      <button
        ref={buttonRef}
        type="button"
        aria-label="Account menu"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className={`inline-flex h-11 w-11 items-center justify-center rounded-full ${focusRing}`}
      >
        <IdentityAvatar
          id={email || customerName}
          label={avatarLabel(email) || customerName}
          size="md"
          className="!h-9 !w-9 !text-[13px]"
        />
      </button>
      {open && (
        <div
          ref={panelRef}
          id={panelId}
          className="absolute right-0 top-full z-50 mt-1 w-72 max-w-[calc(100vw-2rem)] rounded-xl border border-border bg-surface p-2 shadow-lg"
        >
          <div className="px-3 py-2">
            <p className="text-xs text-text-muted">Signed in</p>
            <p className="break-all text-sm font-semibold text-text">{email}</p>
            <p className="mt-1 text-xs text-text-muted">Supplier</p>
            <p className="text-sm text-text">{supplierName}</p>
          </div>
          <div aria-hidden="true" className="my-1 border-t border-border" />
          <Link
            href="/portal/account"
            className={item}
            onClick={() => setOpen(false)}
          >
            <UserRound aria-hidden="true" className="h-4 w-4" />
            Account
          </Link>
          <button
            type="button"
            className={item}
            onClick={onSignOut}
            aria-disabled={signingOut || undefined}
          >
            <LogOut aria-hidden="true" className="h-4 w-4" />
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
