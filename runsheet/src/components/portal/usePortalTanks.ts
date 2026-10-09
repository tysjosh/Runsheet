"use client";

import { useCallback, useMemo, useState } from "react";
import { listPortalTanks, type PortalTank } from "../../services/portalApi";
import { tankTitles } from "./tankTitle";
import { portalErrorMessage, usePortalData } from "./usePortalData";

/** The customer's tanks with their D29 titles (numbered per product). */
export function usePortalTanks() {
  const result = usePortalData(() => listPortalTanks(), []);
  const tanks: PortalTank[] = useMemo(
    () => result.data?.data ?? [],
    [result.data],
  );
  const titles = useMemo(() => tankTitles(tanks), [tanks]);
  const error = result.error
    ? portalErrorMessage(result.error, {
        fallback: "We couldn't load your tanks.",
      })
    : null;
  return {
    tanks,
    titles,
    loading: result.loading,
    error,
    reload: result.reload,
  };
}

/** Open/close state for the request dialog, with the preselected tank. */
export function useRequestDialog(initial?: {
  open: boolean;
  tankId?: string | null;
}) {
  const [state, setState] = useState<{ open: boolean; tankId: string | null }>({
    open: initial?.open ?? false,
    tankId: initial?.tankId ?? null,
  });
  const openFor = useCallback(
    (tankId?: string | null) =>
      setState({ open: true, tankId: tankId ?? null }),
    [],
  );
  const close = useCallback(() => setState((s) => ({ ...s, open: false })), []);
  return { ...state, openFor, close };
}
