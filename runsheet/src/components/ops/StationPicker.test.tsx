/**
 * Tests for StationPicker.
 *
 * Pins the behaviours the replan flow relies on: loading the live station
 * roster, selecting a station by value, and surfacing a load error.
 */
import "@testing-library/jest-dom";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

jest.mock("../../services/fuelApi", () => ({
  getStations: jest.fn(),
}));

import { getStations } from "../../services/fuelApi";
import StationPicker from "./StationPicker";

const mockGetStations = getStations as jest.MockedFunction<typeof getStations>;

const stationFixture = {
  data: [
    {
      station_id: "STN-001",
      name: "Houston Terminal",
      fuel_type: "diesel",
      location_name: "Houston, TX",
    },
    {
      station_id: "STN-002",
      name: "Dallas Depot",
      fuel_type: "gasoline",
      location_name: "Dallas, TX",
    },
  ],
} as unknown as Awaited<ReturnType<typeof getStations>>;

beforeEach(() => {
  mockGetStations.mockReset();
});

it("loads and lists stations", async () => {
  mockGetStations.mockResolvedValue(stationFixture);
  render(<StationPicker value={null} onChange={jest.fn()} />);

  await waitFor(() => expect(mockGetStations).toHaveBeenCalled());

  fireEvent.click(await screen.findByLabelText("Station"));
  expect(await screen.findByText("Houston Terminal")).toBeInTheDocument();
  expect(screen.getByText("Dallas Depot")).toBeInTheDocument();
});

it("calls onChange with the selected station id", async () => {
  mockGetStations.mockResolvedValue(stationFixture);
  const onChange = jest.fn();
  render(<StationPicker value={null} onChange={onChange} />);

  fireEvent.click(await screen.findByLabelText("Station"));
  fireEvent.click(await screen.findByText("Dallas Depot"));

  expect(onChange).toHaveBeenCalledWith("STN-002");
});

it("shows an error message when the roster fails to load", async () => {
  mockGetStations.mockRejectedValue(new Error("boom"));
  await act(async () => {
    render(<StationPicker value={null} onChange={jest.fn()} />);
  });

  fireEvent.click(await screen.findByLabelText("Station"));
  expect(
    await screen.findByText(/couldn't load stations/i),
  ).toBeInTheDocument();
});
