/**
 * Tests for ProductPicker.
 *
 * Pins the behaviours the Orders filter relies on: loading the fuel product
 * catalog and selecting a product by its canonical code.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("../../services/fuelApi", () => ({
  listFuelProducts: jest.fn(),
}));

import { listFuelProducts } from "../../services/fuelApi";
import ProductPicker from "./ProductPicker";

const mockListFuelProducts = listFuelProducts as jest.MockedFunction<
  typeof listFuelProducts
>;

const catalogFixture = {
  region: "us",
  total: 2,
  items: [
    { product_code: "DIESEL_2", display_name: "Diesel #2", category: "diesel" },
    { product_code: "PROPANE", display_name: "Propane", category: "lpg" },
  ],
} as unknown as Awaited<ReturnType<typeof listFuelProducts>>;

beforeEach(() => {
  mockListFuelProducts.mockReset();
});

it("loads and lists products by display name", async () => {
  mockListFuelProducts.mockResolvedValue(catalogFixture);
  render(<ProductPicker value={null} onChange={jest.fn()} />);

  await waitFor(() => expect(mockListFuelProducts).toHaveBeenCalled());
  fireEvent.click(await screen.findByLabelText("Product"));
  expect(await screen.findByText("Diesel #2")).toBeInTheDocument();
  expect(screen.getByText("Propane")).toBeInTheDocument();
});

it("calls onChange with the selected product code", async () => {
  mockListFuelProducts.mockResolvedValue(catalogFixture);
  const onChange = jest.fn();
  render(<ProductPicker value={null} onChange={onChange} />);

  fireEvent.click(await screen.findByLabelText("Product"));
  fireEvent.click(await screen.findByText("Propane"));
  expect(onChange).toHaveBeenCalledWith("PROPANE");
});
