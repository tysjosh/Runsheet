/**
 * A Home panel (design §11.2): a 4 px accent bar beside a 15 px title and a
 * "View all" link on the right. On phones it sits on the page (rows divided,
 * no card); from 768 px it's a card (`listSection`). Accents follow the staff
 * Dashboard: red for tanks, orange for balance, cyan for orders.
 */
import Link from "next/link";
import type { ReactNode } from "react";
import { COLOR } from "../../styles/tokens";
import { listSection, sectionHeading, space, textLink } from "./styles";

export const PANEL_ACCENT = {
  tanks: COLOR.red["600"],
  balance: COLOR.orange["600"],
  orders: COLOR.cyan["600"],
} as const;

export default function PortalPanel({
  id,
  title,
  accent,
  link,
  children,
  className = "",
  first = false,
}: {
  id: string;
  title: string;
  accent: string;
  link?: { href: string; label: string };
  children: ReactNode;
  className?: string;
  /** Marks the page's first content block for the chrome budget. */
  first?: boolean;
}) {
  return (
    <section
      aria-labelledby={id}
      data-portal-first={first || undefined}
      className={`${listSection} md:overflow-hidden ${className}`}
    >
      <div className={`flex items-center gap-2 md:pt-3 ${space.inset}`}>
        <span
          aria-hidden="true"
          className="h-4 w-1 shrink-0 rounded-sm"
          style={{ backgroundColor: accent }}
        />
        <h2 id={id} className={sectionHeading}>
          {title}
        </h2>
        {link && (
          <Link
            href={link.href}
            className={`${textLink} ml-auto min-h-11 text-[13px] md:min-h-6`}
          >
            {link.label}
          </Link>
        )}
      </div>
      {children}
    </section>
  );
}
