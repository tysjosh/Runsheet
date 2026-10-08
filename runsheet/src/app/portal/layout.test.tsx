/**
 * UI-2 / T-UI-SHELL: the portal layout imports and renders no staff shell
 * module and constructs no WebSocket (PD20, R10.2, design §10.2).
 *
 * Every staff shell module is mocked to throw on import, so a single
 * transitive import from the portal layout fails this suite. `WebSocket` is
 * replaced with a spy that must never be constructed.
 */
import { render, screen, waitFor } from "@testing-library/react";
import type React from "react";

jest.mock("../../components/Sidebar", () => {
  throw new Error("staff shell module imported by the portal: Sidebar");
});
jest.mock("../../components/Header", () => {
  throw new Error("staff shell module imported by the portal: Header");
});
jest.mock("../../components/AIChat", () => {
  throw new Error("staff shell module imported by the portal: AIChat");
});
jest.mock("../../components/GlobalSearch", () => {
  throw new Error("staff shell module imported by the portal: GlobalSearch");
});
jest.mock("../../components/NotificationBell", () => {
  throw new Error(
    "staff shell module imported by the portal: NotificationBell",
  );
});
jest.mock("../../components/WebSocketStatus", () => {
  throw new Error("staff shell module imported by the portal: WebSocketStatus");
});
jest.mock("../dashboard/shell-context", () => {
  throw new Error("staff shell module imported by the portal: shell-context");
});
jest.mock("../dashboard/layout", () => {
  throw new Error(
    "staff shell module imported by the portal: dashboard layout",
  );
});
jest.mock("../../hooks", () => {
  throw new Error("staff shell module imported by the portal: hooks");
});
jest.mock("../../hooks/useWebSocket", () => {
  throw new Error("staff shell module imported by the portal: useWebSocket");
});
jest.mock("../../hooks/useAgentWebSocket", () => {
  throw new Error(
    "staff shell module imported by the portal: useAgentWebSocket",
  );
});
jest.mock("../../hooks/useFleetWebSocket", () => {
  throw new Error(
    "staff shell module imported by the portal: useFleetWebSocket",
  );
});
jest.mock("../../hooks/useFuelPlanningWebSocket", () => {
  throw new Error(
    "staff shell module imported by the portal: useFuelPlanningWebSocket",
  );
});
jest.mock("../../hooks/useInventoryWebSocket", () => {
  throw new Error(
    "staff shell module imported by the portal: useInventoryWebSocket",
  );
});
jest.mock("../../hooks/useNotificationWebSocket", () => {
  throw new Error(
    "staff shell module imported by the portal: useNotificationWebSocket",
  );
});
jest.mock("../../hooks/useOpsWebSocket", () => {
  throw new Error("staff shell module imported by the portal: useOpsWebSocket");
});
jest.mock("../../hooks/useOrdersWebSocket", () => {
  throw new Error(
    "staff shell module imported by the portal: useOrdersWebSocket",
  );
});
jest.mock("../../hooks/useSchedulingWebSocket", () => {
  throw new Error(
    "staff shell module imported by the portal: useSchedulingWebSocket",
  );
});
jest.mock("../../hooks/usePlanExecutionSocket", () => {
  throw new Error(
    "staff shell module imported by the portal: usePlanExecutionSocket",
  );
});

const mockReplace = jest.fn();
const mockRouter = { replace: mockReplace, push: jest.fn() };
jest.mock("next/navigation", () => ({
  __esModule: true,
  useRouter: () => mockRouter,
  usePathname: () => "/portal/orders",
}));

jest.mock("supertokens-auth-react/recipe/session", () => ({
  __esModule: true,
  default: {
    doesSessionExist: jest.fn(),
    signOut: jest.fn(),
    attemptRefreshingSession: jest.fn(),
  },
}));

jest.mock("../../utils/auth", () => ({
  __esModule: true,
  getCurrentUserRoles: jest.fn(),
  signOut: jest.fn(),
}));

import Session from "supertokens-auth-react/recipe/session";
import { installFetch, ME } from "../../components/portal/__fixtures__/portal";
import { getCurrentUserRoles } from "../../utils/auth";

const existsMock = Session.doesSessionExist as unknown as jest.Mock;
const rolesMock = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;

let webSocketSpy: jest.Mock;
const realWebSocket = global.WebSocket;

beforeEach(() => {
  mockReplace.mockReset();
  existsMock.mockReset();
  rolesMock.mockReset();
  webSocketSpy = jest.fn(() => {
    throw new Error("WebSocket constructed by the portal");
  });
  global.WebSocket = webSocketSpy as unknown as typeof WebSocket;
});

afterAll(() => {
  global.WebSocket = realWebSocket;
});

function loadLayout() {
  // Imported lazily so a throwing staff-module mock surfaces as a test
  // failure with the module's name rather than a suite-level crash.
  return require("./layout").default as React.ComponentType<{
    children: React.ReactNode;
  }>;
}

describe("portal layout (T-UI-SHELL)", () => {
  it("imports without pulling in any staff shell module", () => {
    expect(() => loadLayout()).not.toThrow();
  });

  it("renders the portal shell for a customer and opens no WebSocket", async () => {
    existsMock.mockResolvedValue(true);
    rolesMock.mockResolvedValue(["customer"]);
    const fetchMock = installFetch({ body: { data: ME, request_id: "r1" } });
    const PortalLayout = loadLayout();

    render(
      <PortalLayout>
        <p>portal page</p>
      </PortalLayout>,
    );

    expect(await screen.findByText("portal page")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Skip to content" }),
    ).toHaveAttribute("href", "#main");
    expect(screen.getByRole("banner")).toHaveTextContent(ME.supplier_name);
    expect(screen.getByRole("banner")).toHaveTextContent(
      ME.customer_display_name,
    );
    expect(
      screen.getByRole("button", { name: "Sign out" }),
    ).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Portal" });
    expect(nav).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Orders" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const main = screen.getByRole("main");
    expect(main).toHaveAttribute("id", "main");
    expect(main).toHaveAttribute("tabindex", "-1");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/portal\/me$/);
    expect(webSocketSpy).not.toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it("hides the Invoices nav item when invoicing is off", async () => {
    existsMock.mockResolvedValue(true);
    rolesMock.mockResolvedValue(["customer"]);
    installFetch({
      body: { data: { ...ME, invoices_available: false }, request_id: "r1" },
    });
    const PortalLayout = loadLayout();
    render(
      <PortalLayout>
        <p>portal page</p>
      </PortalLayout>,
    );
    await screen.findByText("portal page");
    expect(screen.queryByRole("link", { name: "Invoices" })).toBeNull();
  });

  it("sends a signed-out visitor to /signin without calling the API", async () => {
    existsMock.mockResolvedValue(false);
    const fetchMock = installFetch({ body: {} });
    const PortalLayout = loadLayout();
    render(
      <PortalLayout>
        <p>portal page</p>
      </PortalLayout>,
    );
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/signin"));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByText("portal page")).toBeNull();
  });

  it("sends a staff session to /dashboard without calling the API", async () => {
    existsMock.mockResolvedValue(true);
    rolesMock.mockResolvedValue(["dispatcher"]);
    const fetchMock = installFetch({ body: {} });
    const PortalLayout = loadLayout();
    render(
      <PortalLayout>
        <p>portal page</p>
      </PortalLayout>,
    );
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/dashboard"));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(webSocketSpy).not.toHaveBeenCalled();
  });

  it("explains a suspended account instead of rendering the shell", async () => {
    existsMock.mockResolvedValue(true);
    rolesMock.mockResolvedValue(["customer"]);
    (
      Session.attemptRefreshingSession as unknown as jest.Mock
    ).mockResolvedValue(true);
    installFetch({
      status: 403,
      body: {
        error_code: "PORTAL_ACCESS_SUSPENDED",
        message: "suspended",
        details: {},
        request_id: "r",
      },
    });
    const PortalLayout = loadLayout();
    render(
      <PortalLayout>
        <p>portal page</p>
      </PortalLayout>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Your portal access is suspended",
    );
    expect(screen.queryByText("portal page")).toBeNull();
  });
});
