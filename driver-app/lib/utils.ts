/**
 * `cn` — the Tailwind class merger the `components/ui/` primitive layer imports.
 *
 * Written fresh for this repository, **not** copied from `azumi-rider/lib/utils.ts`.
 * That donor module also exports `formatPrice` / `formatNaira` (`:8-37`), the
 * Nigerian Naira helpers Requirement 16.9 excludes, and `getImageUrl`, which
 * hard-codes a donor bucket host. None of the three is carried: monetary values
 * are formatted by `formatCurrency` in `lib/units.ts`, which defaults to USD.
 *
 * The body below is the two-line `clsx` + `tailwind-merge` composition published
 * by `tailwind-merge` itself; it carries no donor content.
 *
 * Requirements: 16.9, 16.21
 */

import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/** Merge conditional class names, letting later Tailwind utilities win. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
