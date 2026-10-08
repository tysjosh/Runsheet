/**
 * Chrome-height measurement (design.md §6, ported from the audit's
 * measure.js): the top edge of the first visible, non-empty data row, in CSS
 * px from the viewport top. Rows are `[role=grid] [role=row]` (board lanes,
 * header rows skipped), `tbody tr`, or `[data-feed-row]`.
 */
import type { Page } from "@playwright/test";

export const FIRST_ROW_BUDGET = 172;

export async function firstRowTop(page: Page): Promise<number | null> {
  return page.evaluate(() => {
    const candidates = Array.from(
      document.querySelectorAll<HTMLElement>(
        '[role="grid"] [role="row"], tbody tr, [data-feed-row]',
      ),
    );
    const visible = candidates.filter((el) => {
      if (el.querySelector('[role="columnheader"]')) return false;
      const r = el.getBoundingClientRect();
      if (r.height < 8 || r.width < 40) return false;
      if (r.bottom <= 0 || r.top >= window.innerHeight) return false;
      return (el.textContent ?? "").trim().length > 0;
    });
    if (visible.length === 0) return null;
    const top = Math.min(
      ...visible.map((el) => el.getBoundingClientRect().top),
    );
    return Math.round(top);
  });
}
