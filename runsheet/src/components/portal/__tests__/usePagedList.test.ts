/**
 * usePagedList: the refresh() stuck-`loadingMore` LOW from
 * `customer-portal/review.md` (task 3P.7).
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import type { PortalList } from "../../../services/portalApi";
import { usePagedList } from "../usePagedList";

const page = (data: number[], next: string | null): PortalList<number> => ({
  data,
  next_cursor: next,
  limit: 25,
  request_id: "r",
});

describe("usePagedList", () => {
  it("clears loadingMore when a refresh supersedes an in-flight load-more", async () => {
    let releaseMore: (v: PortalList<number>) => void = () => {};
    const fetchPage = jest.fn((cursor: string | null) => {
      if (cursor === "c1") {
        return new Promise<PortalList<number>>((resolve) => {
          releaseMore = resolve;
        });
      }
      return Promise.resolve(page([1, 2], "c1"));
    });
    const { result } = renderHook(() => usePagedList(fetchPage, [fetchPage]));
    await waitFor(() => expect(result.current.loading).toBe(false));

    act(() => result.current.loadMore());
    expect(result.current.loadingMore).toBe(true);

    await act(async () => {
      await result.current.refresh();
    });
    // The superseded load-more resolves late and must not leave the flag on.
    await act(async () => {
      releaseMore(page([3], null));
    });
    expect(result.current.loadingMore).toBe(false);
    expect(result.current.items).toEqual([1, 2]);
  });

  it("clears loadingMore when the refresh fails", async () => {
    let fail = false;
    const fetchPage = jest.fn((cursor: string | null) => {
      if (cursor === "c1") return new Promise<PortalList<number>>(() => {});
      return fail
        ? Promise.reject(new Error("x"))
        : Promise.resolve(page([1], "c1"));
    });
    const { result } = renderHook(() => usePagedList(fetchPage, [fetchPage]));
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => result.current.loadMore());
    fail = true;
    await act(async () => {
      await expect(result.current.refresh()).rejects.toThrow("x");
    });
    expect(result.current.loadingMore).toBe(false);
  });
});
