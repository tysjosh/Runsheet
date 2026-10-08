/** UI-3 for TankCard (R7.1–R7.3): level text + meter, stale, no forecast. */
import { render, screen } from "@testing-library/react";
import { tank } from "../__fixtures__/portal";
import TankCard, {
  levelText,
  NO_FORECAST_TEXT,
  STALE_READING_TEXT,
} from "../TankCard";

describe("TankCard", () => {
  it("shows the level as text and as a labelled meter", () => {
    render(<TankCard tank={tank()} />);
    expect(screen.getByText("1,240 of 2,000 gal (62%)")).toBeInTheDocument();
    const meter = screen.getByLabelText("Fuel level for North yard");
    expect(meter.tagName).toBe("METER");
    expect(meter).toHaveAttribute("max", "2000");
    expect(meter).toHaveAttribute("value", "1240");
    expect(screen.getByRole("link", { name: "North yard" })).toHaveAttribute(
      "href",
      "/portal/tanks/QA-TANK-1",
    );
    expect(screen.getByText(/About 11 days until empty/)).toBeInTheDocument();
    expect(screen.queryByText(STALE_READING_TEXT)).toBeNull();
  });

  it("calls out a stale reading in words", () => {
    render(<TankCard tank={tank({ reading_stale: true })} />);
    expect(screen.getByText(STALE_READING_TEXT)).toBeInTheDocument();
  });

  it("says when no forecast exists yet", () => {
    render(<TankCard tank={tank({ forecast: null })} />);
    expect(screen.getByText(NO_FORECAST_TEXT)).toBeInTheDocument();
  });

  it("shows the next delivery label and window", () => {
    render(
      <TankCard
        tank={tank({
          next_delivery: {
            order_id: "o1",
            status_label: "Confirmed",
            window_start: null,
            window_end: null,
          },
        })}
      />,
    );
    expect(screen.getByText(/Confirmed, Not scheduled/)).toBeInTheDocument();
  });

  it("uses the tenant's volume unit", () => {
    expect(levelText(tank(), "L")).toBe("1,240 of 2,000 L (62%)");
  });
});
