/**
 * TankRow (R7.1–R7.3, R14.8, D23, D29): level text and meter, status badge,
 * stale reading, no forecast, next delivery, per-tank Request delivery.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { tank } from "../__fixtures__/portal";
import TankRow from "../TankRow";
import { NO_FORECAST_TEXT, STALE_READING_TEXT } from "../tankLevel";

describe("TankRow", () => {
  it("shows the level as text and as a named meter with a spoken value", () => {
    render(<TankRow tank={tank()} title="North yard" />);
    expect(
      screen.getByText(/1,240 of 2,000 gal · 62 % · empty in about 11 days/),
    ).toBeInTheDocument();
    const meter = screen.getByRole("meter", { name: "North yard" });
    expect(meter).toHaveAttribute("aria-valuemin", "0");
    expect(meter).toHaveAttribute("aria-valuemax", "2000");
    expect(meter).toHaveAttribute("aria-valuenow", "1240");
    expect(meter).toHaveAttribute("aria-valuetext", "1,240 of 2,000 gal, 62 %");
    expect(screen.getByRole("link", { name: "North yard" })).toHaveAttribute(
      "href",
      "/portal/tanks/QA-TANK-1",
    );
    expect(screen.getByText("OK").closest("[data-status]")).toHaveAttribute(
      "data-status",
      "ok",
    );
    expect(screen.queryByText(new RegExp(STALE_READING_TEXT))).toBeNull();
  });

  it("colours a 12 % tank as Order now in the badge and the bar", () => {
    render(
      <TankRow
        tank={tank({
          capacity_gallons: 1500,
          current_level_gallons: 180,
          percent_full: 12,
          forecast: null,
        })}
        title="Farm off-road"
      />,
    );
    expect(
      screen.getByText("Order now").closest("[data-status]"),
    ).toHaveAttribute("data-status", "critical");
    const meter = screen.getByRole("meter", { name: "Farm off-road" });
    expect(meter).toHaveAttribute("data-level", "critical");
    expect(meter).toHaveAttribute("aria-valuetext", "180 of 1,500 gal, 12 %");
  });

  it("calls out a stale reading in words", () => {
    render(<TankRow tank={tank({ reading_stale: true })} title="North yard" />);
    expect(
      screen.getByText(new RegExp(STALE_READING_TEXT)),
    ).toBeInTheDocument();
  });

  it("says when no forecast exists yet", () => {
    render(<TankRow tank={tank({ forecast: null })} title="North yard" />);
    expect(screen.getByText(new RegExp(NO_FORECAST_TEXT))).toBeInTheDocument();
  });

  it("pluralises one day", () => {
    render(
      <TankRow
        tank={tank({
          forecast: {
            runout_at: "2026-10-09T12:00:00Z",
            days_to_runout: 1,
            generated_at: "2026-10-08T12:00:00Z",
          },
        })}
        title="North yard"
      />,
    );
    expect(screen.getByText(/empty in about 1 day$/)).toBeInTheDocument();
  });

  it("shows the next delivery as a badge and its window", () => {
    render(
      <TankRow
        tank={tank({
          next_delivery: {
            order_id: "o1",
            status_code: "confirmed",
            status_label: "Confirmed",
            window_start: null,
            window_end: null,
          },
        })}
        title="North yard"
      />,
    );
    expect(
      screen.getByText("Confirmed").closest("[data-status]"),
    ).toHaveAttribute("data-status", "planned");
    expect(screen.getByText("Not scheduled")).toBeInTheDocument();
  });

  it("on phones shows the day only and drops the row button when a delivery is on the way (P3P-R11)", () => {
    render(
      <TankRow
        tank={tank({
          next_delivery: {
            order_id: "o1",
            status_code: "confirmed",
            status_label: "Confirmed",
            window_start: "2026-10-09T14:00:00Z",
            window_end: "2026-10-09T18:00:00Z",
          },
        })}
        title="North yard"
        onRequest={jest.fn()}
      />,
    );
    const full = screen.getByText(/–/);
    expect(full).toHaveClass("max-md:hidden");
    const short = full.nextElementSibling;
    expect(short).toHaveClass("md:hidden");
    expect(short?.textContent).not.toMatch(/–/);
    expect(
      screen.getByRole("button", { name: "Request delivery for North yard" }),
    ).toHaveClass("max-md:hidden");
  });

  it("offers Request delivery for this tank", () => {
    const onRequest = jest.fn();
    render(<TankRow tank={tank()} title="North yard" onRequest={onRequest} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Request delivery for North yard" }),
    );
    expect(onRequest).toHaveBeenCalledWith("QA-TANK-1");
  });

  it("uses the tenant's volume unit", () => {
    render(<TankRow tank={tank()} title="North yard" unit="L" />);
    expect(screen.getByText(/1,240 of 2,000 L/)).toBeInTheDocument();
  });

  it("shows the product as a cap and name, never the code", () => {
    render(
      <TankRow
        tank={tank({ product_code: "DIESEL_2" })}
        title="North yard"
        showProduct
      />,
    );
    expect(
      screen.getByRole("img", { name: "Diesel #2 (on-road)" }),
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\b[A-Z0-9]+_[A-Z0-9_]+\b/);
  });
});
