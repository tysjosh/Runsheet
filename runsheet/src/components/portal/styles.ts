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
/** A smaller row action (still 44 px tall on phones). */
export const rowButton = `inline-flex h-11 min-w-11 shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-[10px] border border-slate-300 bg-surface px-3 text-sm font-semibold text-slate-800 hover:bg-slate-50 md:h-9 aria-disabled:cursor-not-allowed aria-disabled:opacity-60 ${focusRing}`;
export const textLink = `inline-flex min-h-6 items-center font-semibold text-link underline-offset-2 hover:underline ${focusRing}`;

export const fieldLabel = "block text-sm font-semibold text-text";
export const fieldInput = `block h-11 w-full min-w-0 max-w-full rounded-[10px] border border-slate-400 bg-surface px-3 text-base text-text placeholder:text-slate-500 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-600 aria-[invalid=true]:border-red-700 ${focusRing}`;
export const fieldHint = "mt-1 text-sm text-text-muted";
export const fieldError = "mt-1 text-sm font-semibold text-red-700";

export const card = "rounded-xl border border-border bg-surface";
export const sectionHeading = "text-[15px] font-bold text-text";
