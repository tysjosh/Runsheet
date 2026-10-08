/**
 * UI-1: AudienceGuard routing (customer portal design §10.1).
 *
 * - customers are sent from every staff prefix to /portal, staff from /portal
 *   to /dashboard; unknown and signed-out sessions never redirect
 * - the audience resolves once per session: pathname changes never re-read
 *   roles, and one session event triggers exactly one re-resolution
 * - a staff navigation inside /dashboard never remounts the child
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";

const replaceMock = jest.fn();
const router = { replace: replaceMock, push: jest.fn() };
let mockPathname = "/";
jest.mock("next/navigation", () => ({
  __esModule: true,
  useRouter: () => router,
  usePathname: () => mockPathname,
}));

jest.mock("supertokens-auth-react/recipe/session", () => ({
  __esModule: true,
  default: { doesSessionExist: jest.fn() },
}));

jest.mock("../utils/auth", () => ({
  __esModule: true,
  getCurrentUserRoles: jest.fn(),
}));

import Session from "supertokens-auth-react/recipe/session";
import {
  handleSessionEvent,
  SESSION_CHANGED_EVENT,
} from "../config/supertokens";
import { getCurrentUserRoles } from "../utils/auth";
import AudienceGuard, { redirectTarget, STAFF_PREFIXES } from "./AudienceGuard";

const existsMock = Session.doesSessionExist as unknown as jest.Mock;
const rolesMock = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;

let mounts = 0;
function Child() {
  useEffect(() => {
    mounts += 1;
  }, []);
  return <p>child content</p>;
}

function renderGuard() {
  return render(
    <AudienceGuard>
      <Child />
    </AudienceGuard>,
  );
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

beforeEach(() => {
  mounts = 0;
  mockPathname = "/";
  replaceMock.mockReset();
  existsMock.mockReset();
  rolesMock.mockReset();
  existsMock.mockResolvedValue(true);
});

describe("redirects", () => {
  it.each(STAFF_PREFIXES.flatMap((p) => [p, `${p}/nested/path`]))(
    "sends a customer on %s to /portal",
    async (path) => {
      mockPathname = path;
      rolesMock.mockResolvedValue(["customer"]);
      renderGuard();
      await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/portal"));
      expect(replaceMock).toHaveBeenCalledTimes(1);
      expect(screen.queryByText("child content")).not.toBeInTheDocument();
    },
  );

  it.each(["/portal", "/portal/invoices/inv_1"])(
    "sends staff on %s to /dashboard",
    async (path) => {
      mockPathname = path;
      rolesMock.mockResolvedValue(["dispatcher"]);
      renderGuard();
      await waitFor(() =>
        expect(replaceMock).toHaveBeenCalledWith("/dashboard"),
      );
      expect(screen.queryByText("child content")).not.toBeInTheDocument();
    },
  );

  it("lets a customer stay on the portal and staff stay on the dashboard", async () => {
    mockPathname = "/portal/orders";
    rolesMock.mockResolvedValue(["customer"]);
    const first = renderGuard();
    expect(await screen.findByText("child content")).toBeInTheDocument();
    first.unmount();

    mockPathname = "/dashboard/orders";
    rolesMock.mockResolvedValue(["admin"]);
    renderGuard();
    expect(await screen.findByText("child content")).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("renders children again once its own redirect lands", async () => {
    mockPathname = "/dashboard";
    rolesMock.mockResolvedValue(["customer"]);
    const view = renderGuard();
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/portal"));
    mockPathname = "/portal";
    view.rerender(
      <AudienceGuard>
        <Child />
      </AudienceGuard>,
    );
    expect(await screen.findByText("child content")).toBeInTheDocument();
    expect(replaceMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["signed out", false, [] as string[]],
    ["unknown (no roles)", true, [] as string[]],
  ])("never redirects a %s session", async (_label, exists, roles) => {
    existsMock.mockResolvedValue(exists);
    rolesMock.mockResolvedValue(roles);
    for (const path of ["/dashboard", "/portal"]) {
      mockPathname = path;
      const view = renderGuard();
      expect(await screen.findByText("child content")).toBeInTheDocument();
      view.unmount();
    }
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("treats a failed role read as unknown and does not redirect", async () => {
    mockPathname = "/portal";
    existsMock.mockRejectedValue(new Error("sdk not ready"));
    renderGuard();
    expect(await screen.findByText("child content")).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("does not treat a lookalike path as the portal", () => {
    expect(redirectTarget("staff", "/portalx")).toBeNull();
    expect(redirectTarget("customer", "/ordersx")).toBeNull();
    expect(redirectTarget("customer", "/")).toBeNull();
    expect(redirectTarget("customer", "/signin")).toBeNull();
  });
});

describe("rendering while pending", () => {
  it("renders nothing on staff and portal paths until the audience resolves", async () => {
    const roles = deferred<string[]>();
    rolesMock.mockReturnValue(roles.promise);
    mockPathname = "/dashboard";
    renderGuard();
    expect(screen.queryByText("child content")).not.toBeInTheDocument();
    await act(async () => roles.resolve(["admin"]));
    expect(await screen.findByText("child content")).toBeInTheDocument();
  });

  it("renders public pages immediately", () => {
    rolesMock.mockReturnValue(new Promise(() => {}));
    for (const path of ["/", "/signin", "/privacy"]) {
      mockPathname = path;
      const view = renderGuard();
      expect(screen.getByText("child content")).toBeInTheDocument();
      view.unmount();
    }
  });
});

describe("resolve once per session", () => {
  it("never remounts the child or re-reads roles on staff navigation", async () => {
    mockPathname = "/dashboard";
    rolesMock.mockResolvedValue(["dispatcher"]);
    const view = renderGuard();
    expect(await screen.findByText("child content")).toBeInTheDocument();

    for (const path of [
      "/dashboard/orders",
      "/dashboard/fleet",
      "/dashboard/orders/ord_1",
    ]) {
      mockPathname = path;
      view.rerender(
        <AudienceGuard>
          <Child />
        </AudienceGuard>,
      );
      expect(screen.getByText("child content")).toBeInTheDocument();
    }

    expect(mounts).toBe(1);
    expect(rolesMock).toHaveBeenCalledTimes(1);
    expect(existsMock).toHaveBeenCalledTimes(1);
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("re-resolves exactly once per session event and keeps the old audience meanwhile", async () => {
    mockPathname = "/dashboard";
    rolesMock.mockResolvedValueOnce(["admin"]);
    renderGuard();
    expect(await screen.findByText("child content")).toBeInTheDocument();
    expect(rolesMock).toHaveBeenCalledTimes(1);

    const next = deferred<string[]>();
    rolesMock.mockReturnValueOnce(next.promise);
    act(() => {
      window.dispatchEvent(
        new CustomEvent(SESSION_CHANGED_EVENT, {
          detail: { action: "SESSION_CREATED" },
        }),
      );
    });
    await waitFor(() => expect(rolesMock).toHaveBeenCalledTimes(2));
    // The previous answer stays visible: no flash back to pending.
    expect(screen.getByText("child content")).toBeInTheDocument();
    expect(mounts).toBe(1);

    await act(async () => next.resolve(["customer"]));
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/portal"));
    expect(rolesMock).toHaveBeenCalledTimes(2);
  });

  it("ignores a stale resolution that lands after a newer one", async () => {
    mockPathname = "/dashboard";
    const first = deferred<string[]>();
    const second = deferred<string[]>();
    rolesMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    renderGuard();
    await waitFor(() => expect(rolesMock).toHaveBeenCalledTimes(1));
    act(() => {
      window.dispatchEvent(new CustomEvent(SESSION_CHANGED_EVENT));
    });
    await waitFor(() => expect(rolesMock).toHaveBeenCalledTimes(2));
    await act(async () => second.resolve(["admin"]));
    await act(async () => first.resolve(["customer"]));
    expect(screen.getByText("child content")).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });
});

describe("Session.init onHandleEvent", () => {
  it("re-broadcasts the four audience-changing actions only", () => {
    const seen: string[] = [];
    const listener = (e: Event) =>
      seen.push((e as CustomEvent<{ action: string }>).detail.action);
    window.addEventListener(SESSION_CHANGED_EVENT, listener);
    for (const action of [
      "SESSION_CREATED",
      "SIGN_OUT",
      "ACCESS_TOKEN_PAYLOAD_UPDATED",
      "UNAUTHORISED",
      "REFRESH_SESSION",
      "API_INVALID_CLAIM",
    ]) {
      handleSessionEvent({ action });
    }
    window.removeEventListener(SESSION_CHANGED_EVENT, listener);
    expect(seen).toEqual([
      "SESSION_CREATED",
      "SIGN_OUT",
      "ACCESS_TOKEN_PAYLOAD_UPDATED",
      "UNAUTHORISED",
    ]);
  });
});
