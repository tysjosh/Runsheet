"use client";

/**
 * The 48 px title row under the top bar (design §11.1): an optional back
 * button, exactly one `<h1>` (20 px bold), an optional status badge, and at
 * most one primary action on the right. No subtitles.
 */
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { forwardRef, type ReactNode } from "react";
import { focusRing } from "./styles";

export interface PortalTitleRowProps {
  title: string;
  back?: { href: string; label: string };
  badge?: ReactNode;
  action?: ReactNode;
}

const PortalTitleRow = forwardRef<HTMLHeadingElement, PortalTitleRowProps>(
  ({ title, back, badge, action }, ref) => (
    <div className="flex min-h-12 items-center gap-2 py-1">
      {back && (
        <Link
          href={back.href}
          aria-label={back.label}
          title={back.label}
          className={`-ml-2 inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-slate-700 hover:bg-slate-100 md:h-9 md:w-9 ${focusRing}`}
        >
          <ArrowLeft aria-hidden="true" className="h-5 w-5" />
        </Link>
      )}
      <h1
        ref={ref}
        tabIndex={-1}
        className="min-w-0 truncate text-xl font-bold leading-tight text-text focus:outline-none"
      >
        {title}
      </h1>
      {badge && <span className="shrink-0">{badge}</span>}
      {action && (
        <div className="ml-auto flex shrink-0 items-center gap-2">{action}</div>
      )}
    </div>
  ),
);
PortalTitleRow.displayName = "PortalTitleRow";

export default PortalTitleRow;
