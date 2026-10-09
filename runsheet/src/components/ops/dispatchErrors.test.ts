/**
 * OI-16: a dispatch refused for `placed` orders names them in the toast.
 */
import { ApiError } from "../../services/api";
import { approveErrorMessage } from "./dispatchErrors";

describe("approveErrorMessage", () => {
  it("names the orders to confirm", () => {
    const err = new ApiError("server text", 409, "ORDERS_NOT_CONFIRMED", {
      order_ids: ["ORD-1", "ORD-2"],
    });
    expect(approveErrorMessage(err)).toBe(
      "Confirm these orders before dispatching: ORD-1, ORD-2",
    );
  });

  it("shows five ids and counts the rest", () => {
    const ids = ["A", "B", "C", "D", "E", "F", "G"];
    const err = new ApiError("x", 409, "ORDERS_NOT_CONFIRMED", {
      order_ids: ids,
    });
    expect(approveErrorMessage(err)).toBe(
      "Confirm these orders before dispatching: A, B, C, D, E and 2 more",
    );
  });

  it("falls back to the server message without ids", () => {
    const err = new ApiError(
      "Confirm these orders before dispatching: ORD-9",
      409,
      "ORDERS_NOT_CONFIRMED",
    );
    expect(approveErrorMessage(err)).toBe(
      "Confirm these orders before dispatching: ORD-9",
    );
  });

  it("keeps other errors' messages", () => {
    expect(
      approveErrorMessage(new ApiError("Plan busy", 409, "PLAN_BUSY")),
    ).toBe("Plan busy");
    expect(approveErrorMessage("nope")).toBe("Failed to approve plan");
  });
});
