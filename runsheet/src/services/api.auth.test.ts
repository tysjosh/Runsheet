/**
 * Tests for the API_Client session-recovery flow in :file:`services/api.ts`.
 *
 * Covers the "refresh-then-redirect on auth failure" contract of the
 * SuperTokens migration (Req 8.4, 8.5):
 *
 *   - `withSessionCredentials` always opts into credentialed requests so the
 *     SDK attaches the session cookie + anti-CSRF token (no sessionStorage).
 *   - `handleAuthFailure` attempts a SuperTokens session refresh; on success it
 *     reports a retry is safe and does NOT redirect; on failure it redirects
 *     the browser to the sign-in page.
 *   - `fetchWithSession` retries the request once after a successful refresh
 *     when the first response is an auth failure (401/403), and surfaces the
 *     refreshed response.
 *   - `fetchWithSession` redirects (and does not retry) when refresh fails.
 *   - A non-auth-failure response passes straight through with no refresh.
 *
 * `Session.attemptRefreshingSession` is mocked so the test never touches the
 * network; `window.location.assign` is stubbed to observe the redirect.
 *
 * Validates: Requirements 8.5.
 */

import Session from "supertokens-auth-react/recipe/session";
import {
  fetchWithSession,
  handleAuthFailure,
  isAuthFailure,
  SIGN_IN_PATH,
  withSessionCredentials,
} from "./api";

jest.mock("supertokens-auth-react/recipe/session", () => ({
  __esModule: true,
  default: {
    attemptRefreshingSession: jest.fn(),
  },
}));

const refreshMock = Session.attemptRefreshingSession as jest.MockedFunction<
  typeof Session.attemptRefreshingSession
>;

let assignMock: jest.Mock;

beforeEach(() => {
  refreshMock.mockReset();
  assignMock = jest.fn();
  // jsdom's location.assign is a no-op; replace it with a spy.
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { assign: assignMock },
  });
});

function fakeResponse(status: number): Response {
  return { status, ok: status >= 200 && status < 300 } as Response;
}

describe("withSessionCredentials", () => {
  it("always sets credentials to include without dropping caller options", () => {
    const result = withSessionCredentials({
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    expect(result.credentials).toBe("include");
    expect(result.method).toBe("POST");
    expect(result.headers).toEqual({ "Content-Type": "application/json" });
  });
});

describe("isAuthFailure", () => {
  it("flags 401 and 403 and nothing else", () => {
    expect(isAuthFailure(401)).toBe(true);
    expect(isAuthFailure(403)).toBe(true);
    expect(isAuthFailure(200)).toBe(false);
    expect(isAuthFailure(500)).toBe(false);
  });
});

describe("handleAuthFailure", () => {
  it("returns true and does not redirect when the session refreshes", async () => {
    refreshMock.mockResolvedValue(true);

    await expect(handleAuthFailure()).resolves.toBe(true);
    expect(assignMock).not.toHaveBeenCalled();
  });

  it("redirects to sign-in and returns false when refresh reports no session", async () => {
    refreshMock.mockResolvedValue(false);

    await expect(handleAuthFailure()).resolves.toBe(false);
    expect(assignMock).toHaveBeenCalledWith(SIGN_IN_PATH);
  });

  it("redirects to sign-in and returns false when refresh throws", async () => {
    refreshMock.mockRejectedValue(new Error("network down"));

    await expect(handleAuthFailure()).resolves.toBe(false);
    expect(assignMock).toHaveBeenCalledWith(SIGN_IN_PATH);
  });
});

describe("fetchWithSession", () => {
  it("attaches credentials and returns the response when not an auth failure", async () => {
    const fetcher = jest.fn().mockResolvedValue(fakeResponse(200));

    const response = await fetchWithSession(fetcher, "/fleet/summary");

    expect(response.status).toBe(200);
    expect(refreshMock).not.toHaveBeenCalled();
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [, options] = fetcher.mock.calls[0];
    expect(options.credentials).toBe("include");
  });

  it("refreshes then retries once and returns the retried response on 401", async () => {
    const fetcher = jest
      .fn()
      .mockResolvedValueOnce(fakeResponse(401))
      .mockResolvedValueOnce(fakeResponse(200));
    refreshMock.mockResolvedValue(true);

    const response = await fetchWithSession(fetcher, "/fleet/summary");

    expect(refreshMock).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(response.status).toBe(200);
    expect(assignMock).not.toHaveBeenCalled();
  });

  it("redirects to sign-in and does not retry when refresh fails after a 401", async () => {
    const fetcher = jest.fn().mockResolvedValue(fakeResponse(401));
    refreshMock.mockResolvedValue(false);

    const response = await fetchWithSession(fetcher, "/fleet/summary");

    expect(refreshMock).toHaveBeenCalledTimes(1);
    // No retry — the original failing response is returned.
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(response.status).toBe(401);
    expect(assignMock).toHaveBeenCalledWith(SIGN_IN_PATH);
  });

  it("treats a 403 the same as a 401 for the recovery cycle", async () => {
    const fetcher = jest
      .fn()
      .mockResolvedValueOnce(fakeResponse(403))
      .mockResolvedValueOnce(fakeResponse(200));
    refreshMock.mockResolvedValue(true);

    const response = await fetchWithSession(fetcher, "/fleet/trucks");

    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(response.status).toBe(200);
  });
});
