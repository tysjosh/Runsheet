/**
 * Jest-only helpers for the board's RTL suites. The suites mock
 * `dispatchBoardApi` (and the socket and Pragmatic, see `testMocks.ts`)
 * before importing this file, so nothing reaches a backend.
 */
import { render, screen } from "@testing-library/react";
import {
  type BoardCommand,
  type BoardSnapshot,
  type CommandResponse,
  getBoard,
  type LaneView,
  sendBoardCommand,
  type ValidateResponse,
  validateBoard,
} from "../../services/dispatchBoardApi";
import DispatchBoard from "./DispatchBoard";
import { makeSnapshot } from "./state/testFixtures";
import { todayIn, writeStoredZone } from "./viewState";

/** The parts of a Jest mock these suites use (no `@types/jest` here). */
export type Mocked<F extends (...args: any[]) => any> = F & {
  mock: { calls: Parameters<F>[] };
  mockResolvedValue: (v: Awaited<ReturnType<F>>) => Mocked<F>;
  mockResolvedValueOnce: (v: Awaited<ReturnType<F>>) => Mocked<F>;
  mockRejectedValue: (e: unknown) => Mocked<F>;
  mockRejectedValueOnce: (e: unknown) => Mocked<F>;
  mockReturnValue: (v: ReturnType<F>) => Mocked<F>;
  mockImplementation: (fn: F) => Mocked<F>;
  mockClear: () => void;
  mockReset: () => void;
};

export const mockGetBoard = getBoard as Mocked<typeof getBoard>;
export const mockSend = sendBoardCommand as Mocked<typeof sendBoardCommand>;
export const mockValidate = validateBoard as Mocked<typeof validateBoard>;

export const TODAY = todayIn("America/Chicago");

export function boardSnap(over: Partial<BoardSnapshot> = {}): BoardSnapshot {
  return makeSnapshot({ service_date: TODAY, ...over });
}

export function okResponse(lanes: LaneView[] = []): CommandResponse {
  return {
    draft_version: 2,
    lanes,
    checks: {},
    already_applied: false,
    audit_degraded: false,
  };
}

export function emptyValidate(): ValidateResponse {
  return { results: {}, degraded_sources: [] };
}

/** Renders the board on `snapshot` and waits until it is on screen. */
export async function renderBoard(
  snapshot: BoardSnapshot,
  mode: "shadow" | "active_gated" = "active_gated",
) {
  mockGetBoard.mockResolvedValue(snapshot);
  const utils = render(<DispatchBoard mode={mode} />);
  await screen.findByRole("region", { name: "Trays" });
  return utils;
}

/** Bodies sent so far, without the per-call id and modality (K14.4 parity). */
export function sentBodies(): Omit<
  BoardCommand,
  "client_command_id" | "input_modality"
>[] {
  return mockSend.mock.calls.map(([, cmd]) => {
    const { client_command_id: _id, input_modality: _m, ...rest } = cmd;
    return rest as Omit<BoardCommand, "client_command_id" | "input_modality">;
  });
}

export function sentModalities(): string[] {
  return mockSend.mock.calls.map(([, cmd]) => cmd.input_modality);
}

export function resetBoardMocks() {
  mockGetBoard.mockReset();
  mockSend.mockReset();
  mockValidate.mockReset();
  mockValidate.mockResolvedValue(emptyValidate());
  window.localStorage.clear();
  pinTenantZone();
}

/**
 * The fixtures' tenant zone, as if a previous visit had stored it. Without it
 * the first fetch uses the browser zone's today, and between 00:00 and 05:00
 * UTC on a UTC machine (CI) that is a different day from Chicago's, so the
 * board refetches the tenant's day and tests expecting one fetch break.
 */
export function pinTenantZone(zone = "America/Chicago") {
  writeStoredZone(window.localStorage, zone);
}
