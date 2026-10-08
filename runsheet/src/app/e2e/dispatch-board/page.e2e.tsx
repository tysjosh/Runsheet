"use client";
/**
 * Playwright harness for the Dispatch Board (plan tasks 37–40). This route
 * only exists when the dev server runs with NEXT_PUBLIC_E2E_HARNESS=1 (see
 * `pageExtensions` in next.config.ts and `playwright.dispatch-board.config.ts`).
 * It renders the real board without the dashboard's session gate; the spec
 * mocks every API call and the board socket, so nothing reaches a backend.
 */
import { useSearchParams } from "next/navigation";
import { lazy, Suspense } from "react";
import type { BoardMode } from "../../../services/dispatchBoardApi";

const DispatchBoard = lazy(
  () => import("../../../components/dispatch-board/DispatchBoard"),
);

function Harness() {
  const params = useSearchParams();
  const mode = (params?.get("mode") as BoardMode | null) ?? "active_gated";
  return <DispatchBoard mode={mode} />;
}

export default function DispatchBoardHarnessPage() {
  return (
    <main className="flex h-screen flex-col bg-gray-50">
      <h1 className="sr-only">Dispatch board</h1>
      <Suspense fallback={null}>
        <Harness />
      </Suspense>
    </main>
  );
}
