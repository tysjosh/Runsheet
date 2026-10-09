import { formatStatus } from "./formatStatus";

describe("formatStatus", () => {
  it("title-cases a snake_case status", () => {
    expect(formatStatus("in_progress")).toBe("In Progress");
    expect(formatStatus("scheduled")).toBe("Scheduled");
  });

  it("returns an em dash for a missing status instead of throwing (R-1)", () => {
    expect(formatStatus(undefined)).toBe("—");
    expect(formatStatus(null)).toBe("—");
    expect(formatStatus("")).toBe("—");
  });
});
