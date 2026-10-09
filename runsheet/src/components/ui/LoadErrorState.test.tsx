import { fireEvent, render, screen } from "@testing-library/react";
import type { LoadFailure } from "../../services/apiErrors";
import { LoadErrorState } from "./LoadErrorState";

function renderState(
  failure: LoadFailure,
  extra: { onRetry?: () => void; staffOnly?: boolean; embedded?: boolean } = {},
) {
  const onBack = jest.fn();
  return {
    onBack,
    ...render(
      <LoadErrorState
        failure={failure}
        entityLabel="Terminal"
        entityId="T-1"
        onBack={onBack}
        backLabel="Back to Terminals"
        homeHref="/dashboard/compliance"
        homeLabel="Go to Compliance"
        {...extra}
      />,
    ),
  };
}

function expectCommonActions(onBack: jest.Mock) {
  fireEvent.click(screen.getByRole("button", { name: /Back to Terminals/ }));
  expect(onBack).toHaveBeenCalledTimes(1);
  expect(
    screen.getByRole("link", { name: "Go to Compliance" }),
  ).toHaveAttribute("href", "/dashboard/compliance");
  expect(screen.queryByText(/\[object Object\]/)).toBeNull();
}

describe("LoadErrorState", () => {
  it("renders not_found with the entity id", () => {
    const { onBack } = renderState({
      kind: "not_found",
      message: "x",
      status: 404,
    });
    expect(
      screen.getByRole("heading", { name: "Terminal not found" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/terminal "T-1"/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expectCommonActions(onBack);
  });

  it("renders module_disabled with the module name", () => {
    const { onBack } = renderState({
      kind: "module_disabled",
      message: "x",
      status: 404,
      code: "CUSTOMERS_DISABLED",
      moduleName: "Customers",
    });
    expect(
      screen.getByRole("heading", {
        name: "Customers isn't enabled for your account",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expectCommonActions(onBack);
  });

  it("renders forbidden with the API's message by default", () => {
    const { onBack } = renderState({
      kind: "forbidden",
      message: "This action requires one of the roles: admin, dispatcher",
      status: 403,
    });
    expect(
      screen.getByRole("heading", {
        name: "You don't have access to this terminal",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "This action requires one of the roles: admin, dispatcher",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Runsheet staff/)).toBeNull();
    expectCommonActions(onBack);
  });

  it("renders forbidden as staff access required when staffOnly", () => {
    const { onBack } = renderState(
      { kind: "forbidden", message: "x", status: 403 },
      { staffOnly: true },
    );
    expect(
      screen.getByRole("heading", { name: "Runsheet staff access required" }),
    ).toBeInTheDocument();
    expectCommonActions(onBack);
  });

  it("pads itself by default and drops the padding when embedded", () => {
    const failure: LoadFailure = { kind: "error", message: "boom" };
    const { container, unmount } = renderState(failure);
    expect(container.firstElementChild).toHaveClass("p-6");
    unmount();
    const embedded = renderState(failure, { embedded: true });
    expect(embedded.container.firstElementChild).not.toHaveClass("p-6");
  });

  it("renders the error banner with retry", () => {
    const onRetry = jest.fn();
    const { onBack } = renderState(
      { kind: "error", message: "boom", status: 500 },
      { onRetry },
    );
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expectCommonActions(onBack);
  });

  it("hides retry for non-error kinds and when no handler is given", () => {
    renderState({ kind: "not_found", message: "x" }, { onRetry: jest.fn() });
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("omits the home link when no homeHref is given", () => {
    render(
      <LoadErrorState
        failure={{ kind: "not_found", message: "x" }}
        entityLabel="Invoice"
        onBack={jest.fn()}
      />,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByRole("button", { name: /Go back/ })).toBeInTheDocument();
    expect(screen.getByText(/We couldn't find invoice\./)).toBeInTheDocument();
  });
  it("replaces a raw fetch failure with the R11 network copy and retry", () => {
    const onRetry = jest.fn();
    renderState({ kind: "network", message: "Failed to fetch" }, { onRetry });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Can't reach Runsheet. Check your connection and retry.",
    );
    expect(screen.queryByText("Failed to fetch")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
  it("shows a role hint on 403 when the envelope names the roles", () => {
    renderState({
      kind: "forbidden",
      message: "Your role can't view terminals.",
      status: 403,
      details: { required_roles: ["admin", "dispatcher"] },
    });
    expect(
      screen.getByRole("heading", {
        name: "You don't have access to this terminal",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Ask an administrator for the admin or dispatcher role\./,
      ),
    ).toBeInTheDocument();
  });
  it("uses the staff copy only when the API says platform_admin is required (OI-48)", () => {
    const { unmount } = renderState(
      {
        kind: "forbidden",
        message: "Admins only",
        status: 403,
        details: { reason: "role_missing" },
      },
      { staffOnly: true },
    );
    expect(
      screen.queryByRole("heading", { name: "Runsheet staff access required" }),
    ).toBeNull();
    unmount();
    renderState({
      kind: "forbidden",
      message: "x",
      status: 403,
      details: { reason: "platform_admin_required" },
    });
    expect(
      screen.getByRole("heading", { name: "Runsheet staff access required" }),
    ).toBeInTheDocument();
  });
});
