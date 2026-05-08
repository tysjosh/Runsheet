/**
 * Tests for :file:`TruckCompartmentsPage.tsx` (Task 11.9).
 *
 * Coverage targets the behaviours the spec calls out:
 *
 *  1. **``validateCleaningForm`` pure helper** — required-field check
 *     on ``actor_id`` and method validation against the
 *     ``{flush, purge, sanitize}`` enum.
 *  2. **``CompartmentStateBadge``** — renders the correct label +
 *     colour combination for each of the three lifecycle states
 *     (``clean`` / ``loaded`` / ``needs_cleaning``).
 *  3. **Page render** — loading a truck surfaces each compartment row
 *     with its state badge and the "Record cleaning" action.
 *  4. **Cleaning-event submission** — submitting the modal form
 *     invokes ``recordCleaningEvent`` with the tenant-stamped payload
 *     and refreshes the list on success.
 *  5. **Evidence upload integration** — selecting a file triggers
 *     {@link presignPodUpload} + {@link putPresignedFile}, and the
 *     returned ``file_ref`` is forwarded to ``recordCleaningEvent`` on
 *     submit.
 *
 * ``fuelApi`` and ``driverApi`` are mocked so these tests never touch
 * the network.
 *
 * Validates: Requirement 7.1.4.
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

jest.mock("../../services/fuelApi", () => {
  const actual = jest.requireActual("../../services/fuelApi");
  return {
    ...actual,
    listTruckCompartments: jest.fn(),
    recordCleaningEvent: jest.fn(),
  };
});

jest.mock("../../services/driverApi", () => ({
  presignPodUpload: jest.fn(),
  putPresignedFile: jest.fn(),
}));

import { presignPodUpload, putPresignedFile } from "../../services/driverApi";
import type {
  CleaningEvent,
  TruckCompartmentListResponse,
  TruckCompartmentState,
} from "../../services/fuelApi";
import {
  listTruckCompartments,
  recordCleaningEvent,
} from "../../services/fuelApi";
import TruckCompartmentsPage, {
  CompartmentStateBadge,
  formatCapacity,
  litersToGallons,
  STATE_BADGE_CONFIG,
  validateCleaningForm,
} from "./TruckCompartmentsPage";

const mockList = listTruckCompartments as jest.MockedFunction<
  typeof listTruckCompartments
>;
const mockRecord = recordCleaningEvent as jest.MockedFunction<
  typeof recordCleaningEvent
>;
const mockPresign = presignPodUpload as jest.MockedFunction<
  typeof presignPodUpload
>;
const mockPut = putPresignedFile as jest.MockedFunction<
  typeof putPresignedFile
>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function compartmentFixture(
  overrides: Partial<TruckCompartmentState> = {},
): TruckCompartmentState {
  const base: TruckCompartmentState = {
    compartment_id: "TRUCK-1_c1",
    truck_id: "TRUCK-1",
    capacity_liters: 5000,
    allowed_grades: ["DIESEL_2", "GASOLINE_REG"],
    position_index: 0,
    state: "clean",
    last_loaded_product: null,
    last_loaded_at: null,
    last_cleaned_at: null,
  };
  return { ...base, ...overrides };
}

function listResponseFixture(
  items: TruckCompartmentState[],
  truckId = "TRUCK-1",
): TruckCompartmentListResponse {
  return { truck_id: truckId, items, total: items.length };
}

function cleaningEventFixture(
  overrides: Partial<CleaningEvent> = {},
): CleaningEvent {
  const now = new Date().toISOString();
  const base: CleaningEvent = {
    cleaning_event_id: "ce_test",
    tenant_id: "tenant-a",
    compartment_id: "TRUCK-1_c1",
    truck_id: "TRUCK-1",
    method: "flush",
    actor_id: "driver-042",
    notes: null,
    evidence_refs: [],
    cleaned_at: now,
    created_at: now,
    updated_at: now,
  };
  return { ...base, ...overrides };
}

// ─── Pure helpers ────────────────────────────────────────────────────────────

describe("validateCleaningForm", () => {
  it("returns no errors for a well-formed form", () => {
    expect(
      validateCleaningForm({
        method: "flush",
        actor_id: "driver-042",
        notes: "",
      }),
    ).toEqual({});
  });

  it("flags a blank actor_id", () => {
    const errors = validateCleaningForm({
      method: "purge",
      actor_id: "   ",
      notes: "",
    });
    expect(errors.actor_id).toMatch(/actor/i);
  });

  it("rejects an out-of-enum method", () => {
    const errors = validateCleaningForm({
      // @ts-expect-error testing invalid enum value
      method: "rinse",
      actor_id: "driver-042",
      notes: "",
    });
    expect(errors.method).toMatch(/flush/i);
  });
});

describe("litersToGallons / formatCapacity", () => {
  it("converts liters to gallons using the canonical factor", () => {
    expect(litersToGallons(3785.411784)).toBeCloseTo(1000, 5);
  });

  it("formats capacity as liters + gallons", () => {
    expect(formatCapacity(5000)).toMatch(/5000\s*L/);
    expect(formatCapacity(5000)).toMatch(/gal/);
  });

  it("returns em-dash for null capacity", () => {
    expect(formatCapacity(null)).toBe("—");
  });
});

describe("STATE_BADGE_CONFIG", () => {
  it("covers every lifecycle state", () => {
    expect(Object.keys(STATE_BADGE_CONFIG).sort()).toEqual(
      ["clean", "loaded", "needs_cleaning"].sort(),
    );
  });
});

// ─── Badge component ─────────────────────────────────────────────────────────

describe("CompartmentStateBadge", () => {
  it("renders the needs_cleaning label with red styling", () => {
    render(<CompartmentStateBadge state="needs_cleaning" />);
    const badge = screen.getByTestId("compartment-state-badge-needs_cleaning");
    expect(badge).toHaveTextContent(/needs cleaning/i);
    expect(badge.className).toMatch(/bg-red-100/);
  });

  it("renders the clean label with green styling", () => {
    render(<CompartmentStateBadge state="clean" />);
    const badge = screen.getByTestId("compartment-state-badge-clean");
    expect(badge).toHaveTextContent(/clean/i);
    expect(badge.className).toMatch(/bg-green-100/);
  });

  it("renders the loaded label with blue styling", () => {
    render(<CompartmentStateBadge state="loaded" />);
    const badge = screen.getByTestId("compartment-state-badge-loaded");
    expect(badge).toHaveTextContent(/loaded/i);
    expect(badge.className).toMatch(/bg-blue-100/);
  });
});

// ─── Page component ─────────────────────────────────────────────────────────

describe("TruckCompartmentsPage", () => {
  beforeEach(() => {
    mockList.mockReset();
    mockRecord.mockReset();
    mockPresign.mockReset();
    mockPut.mockReset();
  });

  async function lookupTruck(truckId = "TRUCK-1") {
    fireEvent.change(screen.getByLabelText(/Truck ID/i), {
      target: { value: truckId },
    });
    await act(async () => {
      fireEvent.submit(screen.getByTestId("truck-lookup-form"));
    });
  }

  it("renders an empty hint before a lookup is submitted", () => {
    render(<TruckCompartmentsPage />);
    expect(
      screen.getByText(/Enter a truck ID above to see its compartments/i),
    ).toBeInTheDocument();
  });

  it("renders a row per compartment with its state badge and record action", async () => {
    mockList.mockResolvedValue(
      listResponseFixture([
        compartmentFixture({
          compartment_id: "TRUCK-1_c1",
          state: "clean",
        }),
        compartmentFixture({
          compartment_id: "TRUCK-1_c2",
          position_index: 1,
          state: "needs_cleaning",
          last_loaded_product: "HEATING_OIL",
          last_loaded_at: "2024-06-01T10:00:00Z",
        }),
      ]),
    );

    render(<TruckCompartmentsPage />);
    await lookupTruck();

    await waitFor(() => {
      expect(
        screen.getByTestId("compartment-row-TRUCK-1_c1"),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("compartment-row-TRUCK-1_c2"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("compartment-state-badge-clean"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("compartment-state-badge-needs_cleaning"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("record-cleaning-TRUCK-1_c1"),
    ).toBeInTheDocument();
  });

  it("shows the 'no compartments' empty state when the truck has none configured", async () => {
    mockList.mockResolvedValue(listResponseFixture([], "TRUCK-99"));
    render(<TruckCompartmentsPage />);
    await lookupTruck("TRUCK-99");

    await waitFor(() => {
      expect(
        screen.getByText(/No compartments configured/i),
      ).toBeInTheDocument();
    });
  });

  it("surfaces the API error when the list fetch fails", async () => {
    mockList.mockRejectedValue(new Error("boom"));
    render(<TruckCompartmentsPage />);
    await lookupTruck();

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/boom/);
    });
  });

  it("submits a cleaning event and refreshes the list on success", async () => {
    const initial = listResponseFixture([
      compartmentFixture({ state: "needs_cleaning" }),
    ]);
    const refreshed = listResponseFixture([
      compartmentFixture({
        state: "clean",
        last_cleaned_at: "2024-06-02T12:00:00Z",
      }),
    ]);
    mockList.mockResolvedValueOnce(initial).mockResolvedValueOnce(refreshed);
    mockRecord.mockResolvedValue(cleaningEventFixture());

    render(<TruckCompartmentsPage />);
    await lookupTruck();

    await waitFor(() => {
      expect(
        screen.getByTestId("record-cleaning-TRUCK-1_c1"),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("record-cleaning-TRUCK-1_c1"));
    fireEvent.change(screen.getByLabelText(/Actor ID/i), {
      target: { value: "driver-042" },
    });
    await act(async () => {
      fireEvent.submit(screen.getByTestId("cleaning-event-form"));
    });

    await waitFor(() => {
      expect(mockRecord).toHaveBeenCalledTimes(1);
    });
    const [compartmentId, payload] = mockRecord.mock.calls[0];
    expect(compartmentId).toBe("TRUCK-1_c1");
    expect(payload).toEqual(
      expect.objectContaining({
        method: "flush",
        actor_id: "driver-042",
      }),
    );
    // Evidence refs omitted when no uploads were queued.
    expect(payload.evidence_refs).toEqual([]);
    // The list is re-fetched after the successful submission.
    await waitFor(() => {
      expect(mockList).toHaveBeenCalledTimes(2);
    });
  });

  it("blocks submit on missing actor_id and shows the field error", async () => {
    mockList.mockResolvedValue(
      listResponseFixture([compartmentFixture({ state: "needs_cleaning" })]),
    );
    render(<TruckCompartmentsPage />);
    await lookupTruck();

    await waitFor(() => {
      expect(
        screen.getByTestId("record-cleaning-TRUCK-1_c1"),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("record-cleaning-TRUCK-1_c1"));
    // Leave actor_id blank.
    await act(async () => {
      fireEvent.submit(screen.getByTestId("cleaning-event-form"));
    });

    expect(mockRecord).not.toHaveBeenCalled();
    expect(screen.getByText(/Actor ID is required/i)).toBeInTheDocument();
  });

  it("uploads evidence photos via presigned PUT and forwards the file_ref on submit", async () => {
    mockList.mockResolvedValue(
      listResponseFixture([compartmentFixture({ state: "needs_cleaning" })]),
    );
    mockPresign.mockResolvedValue({
      file_ref: "tenants/tenant-a/photo/2024/06/01/abc.jpg",
      upload_url: "https://s3.example/upload",
      expires_at: new Date().toISOString(),
      content_type: "image/jpeg",
      max_file_bytes: 10_000_000,
    });
    mockPut.mockResolvedValue();
    mockRecord.mockResolvedValue(cleaningEventFixture());

    render(<TruckCompartmentsPage />);
    await lookupTruck();

    await waitFor(() => {
      expect(
        screen.getByTestId("record-cleaning-TRUCK-1_c1"),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("record-cleaning-TRUCK-1_c1"));

    const fileInput = screen.getByTestId(
      "cleaning-event-evidence-input",
    ) as HTMLInputElement;
    const file = new File(["hello"], "evidence.jpg", { type: "image/jpeg" });
    await act(async () => {
      fireEvent.change(fileInput, { target: { files: [file] } });
    });

    await waitFor(() => {
      expect(mockPresign).toHaveBeenCalledWith("photo", "image/jpeg");
    });
    await waitFor(() => {
      expect(mockPut).toHaveBeenCalledTimes(1);
    });
    // Wait for the uploaded status badge so the form knows the file is ready.
    await waitFor(() => {
      expect(screen.getByText(/Uploaded/i)).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText(/Actor ID/i), {
      target: { value: "driver-042" },
    });
    await act(async () => {
      fireEvent.submit(screen.getByTestId("cleaning-event-form"));
    });

    await waitFor(() => {
      expect(mockRecord).toHaveBeenCalledTimes(1);
    });
    const [, payload] = mockRecord.mock.calls[0];
    expect(payload.evidence_refs).toEqual([
      "tenants/tenant-a/photo/2024/06/01/abc.jpg",
    ]);
  });
});
