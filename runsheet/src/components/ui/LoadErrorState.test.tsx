import { fireEvent, render, screen } from "@testing-library/react";
import type { LoadFailure } from "../../services/apiErrors";
import { LoadErrorState } from "./LoadErrorState";

function renderState(
  failure: LoadFailure,
  extra: { onRetry?: () => void } = {},
) {
  const onBack = jest.fn();
  render(
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
  );
  return { onBack };
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

  it("renders forbidden as staff access required", () => {
    const { onBack } = renderState({
      kind: "forbidden",
      message: "x",
      status: 403,
    });
    expect(
      screen.getByRole("heading", { name: "Runsheet staff access required" }),
    ).toBeInTheDocument();
    expectCommonActions(onBack);
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
});
