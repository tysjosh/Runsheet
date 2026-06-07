/**
 * Tests for CustomerPicker.
 *
 * Pins the behaviours the Orders filter relies on: loading the live customer
 * roster, selecting by value, and the clearable (filter) affordance.
 */
import "@testing-library/jest-dom";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

jest.mock("../../services/commerceApi", () => ({
  getCustomers: jest.fn(),
}));

import { getCustomers } from "../../services/commerceApi";
import CustomerPicker from "./CustomerPicker";

const mockGetCustomers = getCustomers as jest.MockedFunction<
  typeof getCustomers
>;

const customerFixture = {
  data: [
    { customer_id: "CUST-1", display_name: "Acme Fuel Co", status: "active" },
    { customer_id: "CUST-2", display_name: "Beta Corp", status: "active" },
  ],
  cursor: null,
} as unknown as Awaited<ReturnType<typeof getCustomers>>;

beforeEach(() => {
  mockGetCustomers.mockReset();
});

it("loads and lists customers", async () => {
  mockGetCustomers.mockResolvedValue(customerFixture);
  render(<CustomerPicker value={null} onChange={jest.fn()} />);

  await waitFor(() => expect(mockGetCustomers).toHaveBeenCalled());
  fireEvent.click(await screen.findByLabelText("Customer"));
  expect(await screen.findByText("Acme Fuel Co")).toBeInTheDocument();
  expect(screen.getByText("Beta Corp")).toBeInTheDocument();
});

it("calls onChange with the selected customer id", async () => {
  mockGetCustomers.mockResolvedValue(customerFixture);
  const onChange = jest.fn();
  render(<CustomerPicker value={null} onChange={onChange} />);

  fireEvent.click(await screen.findByLabelText("Customer"));
  fireEvent.click(await screen.findByText("Beta Corp"));
  expect(onChange).toHaveBeenCalledWith("CUST-2");
});

it("clears the value when the clear control is used", async () => {
  mockGetCustomers.mockResolvedValue(customerFixture);
  const onChange = jest.fn();
  await act(async () => {
    render(<CustomerPicker value="CUST-1" onChange={onChange} allowClear />);
  });

  fireEvent.click(screen.getByLabelText("Clear selection"));
  expect(onChange).toHaveBeenCalledWith("");
});
