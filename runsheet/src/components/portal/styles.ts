/**
 * Portal class strings on the Phase 1 tokens (D31, R14.19).
 *
 * Comfortable density only: targets are 44 px tall on phones and 36 px from
 * 768 px; inputs are 44 px with 16 px text (no iOS zoom). Focus is always
 * the `--rs-focus` ring. The shared `Button` and `INPUT_CLASS` are sized for
 * the staff app (28–40 px, 14 px text), so the portal keeps its own strings
 * (follow-up: a `comfortable` size on the shared controls).
 */
export const focusRing =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2";

const buttonBase = `inline-flex h-11 min-w-11 shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-[10px] px-4 text-[15px] font-semibold transition-colors md:h-9 md:text-sm aria-disabled:cursor-not-allowed aria-disabled:opacity-60 disabled:cursor-not-allowed disabled:opacity-60 ${focusRing}`;

export const primaryButton = `${buttonBase} border border-primary bg-primary text-on-primary hover:bg-primary-hover`;
export const secondaryButton = `${buttonBase} border border-slate-300 bg-surface text-slate-800 hover:bg-slate-50`;
/** A destructive confirm (Cancel request). */
export const dangerButton = `${buttonBase} border border-red-600 bg-red-600 text-white hover:bg-red-700`;
/** A smaller row action (still 44 px tall on phones). */
export const rowButton = `inline-flex h-11 min-w-11 shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-[10px] border border-slate-300 bg-surface px-3 text-sm font-semibold text-slate-800 hover:bg-slate-50 md:h-9 aria-disabled:cursor-not-allowed aria-disabled:opacity-60 ${focusRing}`;
export const textLink = `inline-flex min-h-6 items-center font-semibold text-link underline-offset-2 hover:underline ${focusRing}`;

export const fieldLabel = "block text-sm font-semibold text-text";
export const fieldInput = `block h-11 w-full min-w-0 max-w-full rounded-[10px] border border-slate-400 bg-surface px-3 text-base text-text placeholder:text-slate-500 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-600 aria-[invalid=true]:border-red-700 ${focusRing}`;
export const fieldHint = "mt-1 text-sm text-text-muted";
export const fieldError = "mt-1 text-sm font-semibold text-red-700";

export const card = "rounded-xl border border-border bg-surface";
export const sectionHeading = "text-[15px] font-bold text-text";

/**
 * Portal spacing scale, on the shared `SPACE` tokens (0 4 8 12 16 20 24 32,
 * `design/tokens.json`) as Tailwind steps. Use these instead of one-off
 * paddings so phone and desktop rhythm stay consistent.
 */
export const space = {
  /** Page gutter: SPACE[5] = 20 px, the driver app's screen padding. */
  gutter: "px-5",
  /** Title row: 8 / 8 on phones (116 px, 4 px under the 120 px budget with a 44 px row), 16 / 12 from 768 px. */
  titleRow: "py-2 md:pt-4 md:pb-3",
  /** Gap between page sections: 24 px on phones (no cards to separate them), 16 px between cards. */
  sectionGap: "gap-6 md:gap-4",
  /**
   * Row vertical padding: 16 px on phones (the owner's 16–20 px), 12 px
   * inside desktop cards. Account rows, invoice line items, Home's empty
   * orders row.
   */
  rowY: "py-4 md:py-3",
  /**
   * Tank rows only: 12 px, so Home's Balance due stays above the fold within
   * the 116 px phone chrome (P3P-R11; portal-fixes A9).
   */
  tankRowY: "py-3",
  /**
   * Two-line list rows (orders, invoices, delivery history): R14.9 caps them
   * at 72 px below 1024, and a 24 px badge line + a 20 px text line leaves
   * 14 px a side; the 1 px divider above counts toward the top.
   */
  listRowY: "pt-[13px] pb-3.5 md:py-3",
  /** Horizontal inset inside a list section: none on phones (the gutter applies), 14 px in a card. */
  inset: "md:px-3.5",
} as const;

/**
 * A list section: on phones it sits on the page with dividers between rows
 * (no card-within-a-page); from 768 px it's a card, as in the mockup.
 */
export const listSection =
  "md:rounded-xl md:border md:border-border md:bg-surface";

/**
 * The title row's primary action. Still 44 px tall on phones (R14.19), with
 * a narrower 14 px label so it sits beside the title without crowding it.
 */
export const titleActionButton = `${primaryButton} max-md:px-3.5 max-md:text-sm`;
