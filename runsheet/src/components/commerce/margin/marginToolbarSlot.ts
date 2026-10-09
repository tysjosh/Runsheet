"use client";

import { createContext, useContext } from "react";

/**
 * The end of the sub-tab row, where the active view puts its toolbar
 * (filters, export) so the hub stays one 44 px row above the table (the
 * 172 px chrome budget). Null outside the hub: views render their own row.
 */
export const MarginToolbarSlot = createContext<HTMLElement | null>(null);
export const useMarginToolbarSlot = () => useContext(MarginToolbarSlot);
