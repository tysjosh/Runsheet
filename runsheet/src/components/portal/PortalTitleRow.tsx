"use client";

/**
 * The title row under the top bar (design §11.1): an optional back button,
 * exactly one `<h1>` (20 px bold), an optional status badge, and at most one
 * primary action on the right. No subtitles.
 *
 * Spacing comes from `space.titleRow`: on phones 8 px above, a 44 px row and
 * 8 px below (56 + 8 + 44 + 8 = 116, leaving 4 px of the 120 px chrome
 * budget); from 768 px 16 px above, a 48 px row and 12 px below (132 ≤ 136).
 * The min-heights (60 / 76 px) keep every page's first content on the same
 * pixel.
 *
 * `wrapTitle` is for titles that carry an identifier the reader must see in
 * full (invoice numbers): below 640 px the title drops to 18/20 px and wraps
 * instead of truncating, so two lines still fit the 44 px row. From 640 px up
 * it behaves like the default.
 */
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { Fragment, forwardRef, type ReactNode } from "react";
import { focusRing, space } from "./styles";

export interface PortalTitleRowProps {
  title: string;
  back?: { href: string; label: string };
  badge?: ReactNode;
  action?: ReactNode;
  wrapTitle?: boolean;
}

/**
 * Each word as an inline-block, so a line breaks between words ("Invoice" /
 * "INV-001020") rather than at the hyphen inside an identifier; a word wider
 * than the row still breaks inside (overflow-wrap on the h1).
 */
function wrapWords(title: string): ReactNode {
  const words = title.split(" ");
  return words.map((word, i) => (
    // biome-ignore lint/suspicious/noArrayIndexKey: words of a fixed string
    <Fragment key={i}>
      {i > 0 && " "}
      <span className="inline-block max-w-full">{word}</span>
    </Fragment>
  ));
}

const PortalTitleRow = forwardRef<HTMLHeadingElement, PortalTitleRowProps>(
  ({ title, back, badge, action, wrapTitle = false }, ref) => (
    <div
      className={`flex min-h-[60px] items-center gap-3 md:min-h-[76px] ${space.titleRow}`}
    >
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
        title={title}
        className={`min-w-0 font-bold text-text focus:outline-none ${
          wrapTitle
            ? "text-lg leading-5 [overflow-wrap:anywhere] sm:truncate sm:text-xl sm:leading-tight"
            : "truncate text-xl leading-tight"
        }`}
      >
        {wrapTitle ? wrapWords(title) : title}
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
