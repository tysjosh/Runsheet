/**
 * Settings → Feature flags (task 3.8): table row with a state badge and the
 * Change state FormDialog.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../services/adminApi", () => ({
  getOrderIntakePipelineState: jest.fn(),
  setOrderIntakePipelineState: jest.fn(),
}));

import {
  getOrderIntakePipelineState,
  setOrderIntakePipelineState,
} from "../../services/adminApi";
import FeatureFlagsAdmin from "./FeatureFlagsAdmin";

beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
  (getOrderIntakePipelineState as jest.Mock).mockResolvedValue({
    data: { state: "shadow" },
  });
});
afterEach(() => jest.useRealTimers());

async function renderLoaded() {
  render(<FeatureFlagsAdmin />);
  await act(async () => {
    jest.advanceTimersByTime(450);
  });
  jest.useRealTimers();
  return screen.findByText("Shadow mode");
}

it("shows the flag's current state for the tenant", async () => {
  await renderLoaded();
  expect(getOrderIntakePipelineState).toHaveBeenCalledWith("demo-tenant");
  const table = screen.getByRole("table", { name: "Feature flags" });
  expect(within(table).getByText("Order intake pipeline")).toBeInTheDocument();
});

it("changes state through the FormDialog and refuses the current state", async () => {
  (setOrderIntakePipelineState as jest.Mock).mockResolvedValue({
    data: { new_state: "active_gated", ws_broadcast: true },
  });
  await renderLoaded();
  fireEvent.click(screen.getByText("Order intake pipeline"));
  const dialog = screen.getByRole("dialog", { name: "Order intake pipeline" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Update flag" }));
  expect(
    await within(dialog).findByText("Pick a state other than the current one."),
  ).toBeInTheDocument();
  fireEvent.click(
    within(dialog).getByRole("radio", { name: /Active \(gated\)/ }),
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "Update flag" }));
  await waitFor(() =>
    expect(setOrderIntakePipelineState).toHaveBeenCalledWith(
      "demo-tenant",
      "active_gated",
    ),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(screen.getByText("Active (gated)")).toBeInTheDocument();
});
