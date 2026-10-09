/**
 * Settings → Company → Depots (task 3.8): list template, the depot FormDialog
 * (create, patch-only edit, inline validation), delete confirm and the
 * setup banner.
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

const mockPush = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: jest.fn(), back: jest.fn() }),
}));
jest.mock("../../services/fuelApi", () => ({
  listDepots: jest.fn(),
  createDepot: jest.fn(),
  updateDepot: jest.fn(),
  deleteDepot: jest.fn(),
}));

import {
  createDepot,
  deleteDepot,
  listDepots,
  updateDepot,
} from "../../services/fuelApi";
import DepotsPage from "./DepotsPage";

const depot = {
  depot_id: "DEP-1",
  name: "Chicago Yard",
  location_lat: 41.8781234,
  location_lon: -87.6298,
  address: "1000 N Halsted St",
  timezone: "America/Chicago",
  fuel_types_supported: ["DIESEL_2", "GASOLINE_REG"],
  status: "active",
  is_default: true,
};

beforeEach(() => {
  jest.clearAllMocks();
  (listDepots as jest.Mock).mockResolvedValue({
    items: [depot],
    total: 1,
    has_next: false,
  });
});

it("lists depots with products by name and the default marker", async () => {
  render(<DepotsPage />);
  const table = await screen.findByRole("table", { name: "Depot list" });
  await within(table).findByText("Chicago Yard");
  expect(within(table).getByText("Default")).toBeInTheDocument();
  expect(table).not.toHaveTextContent("DIESEL_2");
  expect(screen.queryByTestId("depot-required-banner")).toBeNull();
  fireEvent.click(within(table).getByText("Chicago Yard"));
  expect(mockPush).toHaveBeenCalledWith("/dashboard/settings/depots/DEP-1");
});

it("shows the setup banner with no depots and creates one in the dialog", async () => {
  (listDepots as jest.Mock).mockResolvedValue({
    items: [],
    total: 0,
    has_next: false,
  });
  (createDepot as jest.Mock).mockResolvedValue({ ...depot, depot_id: "DEP-9" });
  render(<DepotsPage />);
  const banner = await screen.findByTestId("depot-required-banner");
  fireEvent.click(
    within(banner).getByRole("button", { name: "Create first depot" }),
  );
  const dialog = screen.getByRole("dialog", { name: "Add depot" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Create depot" }));
  expect(
    await within(dialog).findByText("Depot name is required."),
  ).toBeInTheDocument();
  expect(
    within(dialog).getByText("Select at least one supported fuel product."),
  ).toBeInTheDocument();
  fireEvent.change(within(dialog).getByLabelText(/^Name/), {
    target: { value: "Joliet" },
  });
  fireEvent.change(within(dialog).getByLabelText(/^Address/), {
    target: { value: "1 Main St" },
  });
  fireEvent.change(within(dialog).getByLabelText(/^Latitude/), {
    target: { value: "41.5" },
  });
  fireEvent.change(within(dialog).getByLabelText(/^Longitude/), {
    target: { value: "-88.1" },
  });
  fireEvent.click(within(dialog).getByLabelText(/Diesel #2 \(on-road\)/));
  fireEvent.click(within(dialog).getByRole("button", { name: "Create depot" }));
  await waitFor(() =>
    expect(createDepot).toHaveBeenCalledWith({
      name: "Joliet",
      location_lat: 41.5,
      location_lon: -88.1,
      address: "1 Main St",
      timezone: "America/Chicago",
      fuel_types_supported: ["DIESEL_2"],
      status: "active",
    }),
  );
});

it("edits send only the changed fields", async () => {
  (updateDepot as jest.Mock).mockResolvedValue({ ...depot, name: "Main" });
  render(<DepotsPage />);
  await screen.findByText("Chicago Yard");
  fireEvent.click(
    screen.getByRole("button", { name: /Actions for Depot Chicago Yard/ }),
  );
  fireEvent.click(await screen.findByRole("menuitem", { name: "Edit" }));
  const dialog = screen.getByRole("dialog", { name: "Edit depot" });
  expect(within(dialog).getByLabelText(/^Latitude/)).toHaveValue("41.87812");
  fireEvent.change(within(dialog).getByLabelText(/^Name/), {
    target: { value: "Main" },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
  await waitFor(() =>
    expect(updateDepot).toHaveBeenCalledWith("DEP-1", { name: "Main" }),
  );
});

it("deletes after confirmation", async () => {
  (deleteDepot as jest.Mock).mockResolvedValue(undefined);
  render(<DepotsPage />);
  await screen.findByText("Chicago Yard");
  fireEvent.click(
    screen.getByRole("button", { name: /Actions for Depot Chicago Yard/ }),
  );
  fireEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
  const dialog = screen.getByRole("dialog", { name: "Delete depot?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
  await waitFor(() => expect(deleteDepot).toHaveBeenCalledWith("DEP-1"));
});
