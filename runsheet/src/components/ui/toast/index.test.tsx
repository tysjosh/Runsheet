/**
 * Tests for the shared `useToasts` hook.
 *
 * `useToasts` is the single source of truth for the canonical success/error
 * toast behaviour that pages rely on, so these tests pin its observable
 * contract: adding a toast makes it present, the 4000ms timeout auto-dismisses
 * it, `dismissToast` removes a toast manually, and multiple toasts stack.
 *
 * Validates: Requirements 1.3, 3.2
 */
import "@testing-library/jest-dom";
import { act, renderHook } from "@testing-library/react";
import { useToasts } from "./index";

describe("useToasts", () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    // Drop any still-pending auto-dismiss timers without firing them (which
    // would queue a state update outside `act`); then restore real timers.
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  it("adds a toast that becomes present with its message and type", () => {
    const { result } = renderHook(() => useToasts());

    act(() => {
      result.current.addToast("Saved", "success");
    });

    expect(result.current.toasts).toHaveLength(1);
    expect(result.current.toasts[0]).toMatchObject({
      message: "Saved",
      type: "success",
    });
  });

  it("auto-dismisses a toast after the 4000ms timeout", () => {
    const { result } = renderHook(() => useToasts());

    act(() => {
      result.current.addToast("Saved", "success");
    });
    expect(result.current.toasts).toHaveLength(1);

    // Just before the timeout it is still present.
    act(() => {
      jest.advanceTimersByTime(3999);
    });
    expect(result.current.toasts).toHaveLength(1);

    // At the 4000ms boundary it is removed.
    act(() => {
      jest.advanceTimersByTime(1);
    });
    expect(result.current.toasts).toHaveLength(0);
  });

  it("removes a toast manually via dismissToast", () => {
    const { result } = renderHook(() => useToasts());

    act(() => {
      result.current.addToast("Failed", "error");
    });
    const id = result.current.toasts[0].id;
    expect(result.current.toasts).toHaveLength(1);

    act(() => {
      result.current.dismissToast(id);
    });
    expect(result.current.toasts).toHaveLength(0);
  });

  it("stacks multiple toasts, preserving order and ids", () => {
    const { result } = renderHook(() => useToasts());

    act(() => {
      result.current.addToast("First", "success");
      result.current.addToast("Second", "error");
      result.current.addToast("Third", "success");
    });

    expect(result.current.toasts).toHaveLength(3);
    expect(result.current.toasts.map((t) => t.message)).toEqual([
      "First",
      "Second",
      "Third",
    ]);
    // Each stacked toast gets a unique id from the module-scoped counter.
    const ids = result.current.toasts.map((t) => t.id);
    expect(new Set(ids).size).toBe(3);

    // Dismissing the middle toast leaves the other two stacked.
    act(() => {
      result.current.dismissToast(ids[1]);
    });
    expect(result.current.toasts.map((t) => t.message)).toEqual([
      "First",
      "Third",
    ]);
  });
});
