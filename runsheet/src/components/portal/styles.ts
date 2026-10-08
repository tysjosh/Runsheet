/**
 * Shared Tailwind class strings for the portal, built from the existing design
 * tokens. Primary controls are 40 px tall; every target is at least 24×24 px
 * (WCAG 2.2 target size). Focus is always visible.
 */

const focus =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2";

export const primaryButton = `inline-flex min-h-10 min-w-6 items-center justify-center gap-2 rounded-lg bg-primary px-4 text-sm font-medium text-white hover:bg-primary-hover aria-disabled:cursor-not-allowed aria-disabled:opacity-60 disabled:cursor-not-allowed disabled:opacity-60 ${focus}`;

export const secondaryButton = `inline-flex min-h-10 min-w-6 items-center justify-center gap-2 rounded-lg border border-gray-300 bg-white px-4 text-sm font-medium text-gray-800 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-60 ${focus}`;

export const textLink = `inline-flex min-h-6 min-w-6 items-center font-medium text-primary underline underline-offset-2 hover:text-primary-hover ${focus}`;

export const fieldLabel = "block text-sm font-medium text-gray-800";

export const fieldInput = `mt-1 block min-h-10 w-full max-w-full rounded-lg border border-gray-400 bg-white px-3 text-sm text-gray-900 aria-invalid:border-error-dark ${focus}`;

export const fieldHint = "mt-1 text-sm text-gray-600";

export const fieldError = "mt-1 text-sm font-medium text-error-dark";

export const card = "rounded-xl border border-gray-200 bg-white p-4";

export const pageHeading = "text-2xl font-semibold text-gray-900";

export const sectionHeading = "text-lg font-semibold text-gray-900";
