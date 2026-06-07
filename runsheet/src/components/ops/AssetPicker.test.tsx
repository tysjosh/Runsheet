/**
 * Tests for AssetPicker.
 *
 * Pins the behaviours the scheduling flow relies on: loading the live fleet
 * roster filtered by the job's required asset type (replacing the old
 * hardcoded TRK-001 list), surfacing readiness hints, reporting loaded ids,
 * and selecting an asset by value.
 */
import "@testing-library/jest-dom";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

jest.mock("../../services/api", () => ({
  apiService: { getAssets: jest.fn() },
}));

import { apiService } from "../../services/api";
import AssetPicker from "./AssetPicker";

const mockGetAssets = apiService.getAssets as jest.MockedFunction<
  typeof apiService.getAssets
>;

const assetFixture = {
  data: [
    {
      id: "AST-100",
      name: "Volvo FH16",
      assetType: "vehicle",
      assetSubtype: "fuel_truck",
      status: "active",
    },
    {
      id: "AST-200",
      name: "Kenworth T680",
      assetType: "vehicle",
      assetSubtype: "truck",
      status: "active",
    },
  ],
} as unknown as Awaited<ReturnType<typeof apiService.getAssets>>;

beforeEach(() => {
  mockGetAssets.mockReset();
});

it("loads assets filtered by the required asset type", async () => {
  mockGetAssets.mockResolvedValue(assetFixture);
  render(<AssetPicker assetType="vehicle" value={null} onChange={jest.fn()} />);

  await waitFor(() =>
    expect(mockGetAssets).toHaveBeenCalledWith({ asset_type: "vehicle" }),
  );

  fireEvent.click(await screen.findByLabelText("Asset"));
  expect(await screen.findByText("Volvo FH16")).toBeInTheDocument();
  expect(screen.getByText("Kenworth T680")).toBeInTheDocument();
});

it("reports the loaded asset ids via onAssetsLoaded", async () => {
  mockGetAssets.mockResolvedValue(assetFixture);
  const onAssetsLoaded = jest.fn();
  render(
    <AssetPicker
      assetType="vehicle"
      value={null}
      onChange={jest.fn()}
      onAssetsLoaded={onAssetsLoaded}
    />,
  );

  await waitFor(() =>
    expect(onAssetsLoaded).toHaveBeenCalledWith(["AST-100", "AST-200"]),
  );
});

it("calls onChange with the selected asset id", async () => {
  mockGetAssets.mockResolvedValue(assetFixture);
  const onChange = jest.fn();
  render(<AssetPicker assetType="vehicle" value={null} onChange={onChange} />);

  fireEvent.click(await screen.findByLabelText("Asset"));
  fireEvent.click(await screen.findByText("Kenworth T680"));

  expect(onChange).toHaveBeenCalledWith("AST-200");
});

it("annotates options with a readiness hint when provided", async () => {
  mockGetAssets.mockResolvedValue(assetFixture);
  render(
    <AssetPicker
      assetType="vehicle"
      value={null}
      onChange={jest.fn()}
      readinessByAsset={{ "AST-100": "critical" }}
    />,
  );

  fireEvent.click(await screen.findByLabelText("Asset"));
  expect(await screen.findByText(/critical shortage/i)).toBeInTheDocument();
});

it("shows an error message when the roster fails to load", async () => {
  mockGetAssets.mockRejectedValue(new Error("boom"));
  await act(async () => {
    render(
      <AssetPicker assetType="vessel" value={null} onChange={jest.fn()} />,
    );
  });

  fireEvent.click(await screen.findByLabelText("Asset"));
  expect(await screen.findByText(/couldn't load assets/i)).toBeInTheDocument();
});
