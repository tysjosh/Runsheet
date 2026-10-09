/** Customer-facing tank titles (D29, PE2, AC 12). */
import { tank } from "../__fixtures__/portal";
import {
  isFallbackLabel,
  orderTankTitle,
  tankTitle,
  tankTitles,
} from "../tankTitle";

describe("tankTitle", () => {
  it("keeps a real label", () => {
    expect(tankTitle(tank({ label: "North yard diesel" }))).toBe(
      "North yard diesel",
    );
  });

  it("replaces the id-fragment fallback with the product name", () => {
    const t = tank({ label: "Tank …TANK-2", product_code: "HEATING_OIL" });
    expect(isFallbackLabel(t.label)).toBe(true);
    expect(tankTitle(t)).toBe("Heating oil (No. 2) tank");
    expect(tankTitle(t)).not.toMatch(/^Tank/);
  });

  it("prefers the staff-set display name (PE2)", () => {
    expect(
      tankTitle(tank({ label: "Tank …abc123", display_name: "Farm off-road" })),
    ).toBe("Farm off-road");
  });

  it("numbers fallback tanks of the same product by id order", () => {
    const titles = tankTitles([
      tank({
        customer_tank_id: "b",
        label: "Tank …00000b",
        product_code: "DIESEL_2",
      }),
      tank({
        customer_tank_id: "a",
        label: "Tank …00000a",
        product_code: "DIESEL_2",
      }),
      tank({ customer_tank_id: "c", label: "Yard", product_code: "DIESEL_2" }),
    ]);
    expect(titles.get("a")).toBe("Diesel #2 (on-road) tank");
    expect(titles.get("b")).toBe("Diesel #2 (on-road) tank 2");
    expect(titles.get("c")).toBe("Yard");
  });

  it("titles an order's tank from the list, else from its label and product", () => {
    const titles = new Map([["t1", "Farm off-road"]]);
    expect(
      orderTankTitle(
        { customer_tank_id: "t1", label: "Tank …t1" },
        "DIESEL_2",
        titles,
      ),
    ).toBe("Farm off-road");
    expect(
      orderTankTitle({ customer_tank_id: "t2", label: "Tank …t2" }, "KEROSENE"),
    ).toBe("Kerosene (K-1) tank");
  });
});
