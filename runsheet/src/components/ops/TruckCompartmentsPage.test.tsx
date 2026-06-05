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
    listCompartmentTrucks: jest.fn(),
    recordCleaningEvent: jest.fn(),
    checkCompartmentLoadEligibility: jest.fn(),
  };
});

jest.mock("../../services/driverApi", () => ({
  presignPodUpload: jest.fn(),
  putPresignedFile: jest.fn(),
}));

import { presignPodUpload, putPresignedFile } from "../../services/driverApi";
import type {
  CleaningEvent,
  LoadEligibilityResponse,
  TruckCompartmentListResponse,
  TruckCompartmentState,
} from "../../services/fuelApi";
import {
  checkCompartmentLoadEligibility,
  listCompartmentTrucks,
  listTruckCompartments,
  litersToGallons,
  recordCleaningEvent,
} from "../../services/fuelApi";
import TruckCompartmentsPage, {
  CompartmentStateBadge,
  ELIGIBILITY_DECISION_CONFIG,
  formatCapacity,
  STATE_BADGE_CONFIG,
  validateCleaningForm,
} from "./TruckCompartmentsPage";

const mockList = listTruckCompartments as jest.MockedFunction<
  typeof listTruckCompartments
>;
const mockListTrucks = listCompartmentTrucks as jest.MockedFunction<
  typeof listCompartmentTrucks
>;
const mockRecord = recordCleaningEvent as jest.MockedFunction<
  typeof recordCleaningEvent
>;
const mockCheckEligibility =
  checkCompartmentLoadEligibility as jest.MockedFunction<
    typeof checkCompartmentLoadEligibility
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

function eligibilityFixture(
  overrides: Partial<LoadEligibilityResponse> = {},
): LoadEligibilityResponse {
  const base: LoadEligibilityResponse = {
    compartment_id: "TRUCK-1_c1",
    proposed_product: "DIESEL_2",
    previous_product: "DIESEL_2",
    decision: "allowed",
    reason: null,
    governing_rule: "allowed",
    compartment_state: {
      compartment_id: "TRUCK-1_c1",
      truck_id: "TRUCK-1",
      state: "clean",
      last_loaded_product: "DIESEL_2",
      last_loaded_at: null,
      last_cleaned_at: null,
    },
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

  it("formats capacity as gallons", () => {
    expect(formatCapacity(compartmentFixture())).toMatch(/gal/);
    expect(formatCapacity(compartmentFixture())).not.toMatch(/\bL\b/);
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
    expect(badge.className).toMatch(/bg-error-light/);
  });

  it("renders the clean label with green styling", () => {
    render(<CompartmentStateBadge state="clean" />);
    const badge = screen.getByTestId("compartment-state-badge-clean");
    expect(badge).toHaveTextContent(/clean/i);
    expect(badge.className).toMatch(/bg-success-light/);
  });

  it("renders the loaded label with blue styling", () => {
    render(<CompartmentStateBadge state="loaded" />);
    const badge = screen.getByTestId("compartment-state-badge-loaded");
    expect(badge).toHaveTextContent(/loaded/i);
    expect(badge.className).toMatch(/bg-info-light/);
  });
});

// ─── Page component ─────────────────────────────────────────────────────────

describe("TruckCompartmentsPage", () => {
  beforeEach(() => {
    mockList.mockReset();
    mockListTrucks.mockReset();
    // Default: no trucks have compartments, so the mount effect does not
    // auto-select a truck and tests start from the empty state. Tests that
    // need the picker populated override this.
    mockListTrucks.mockResolvedValue({ items: [], total: 0 });
    mockRecord.mockReset();
    mockCheckEligibility.mockReset();
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

  it("renders an empty hint before a lookup is submitted", async () => {
    render(<TruckCompartmentsPage />);
    // After the truck-list mount effect resolves to empty, the page shows
    // the "no trucks configured" hint rather than auto-selecting one.
    expect(
      await screen.findByText(/No trucks have compartments configured yet/i),
    ).toBeInTheDocument();
  });

  it("auto-selects the first tanker and loads its compartments on mount", async () => {
    mockListTrucks.mockResolvedValue({
      items: [
        { truck_id: "TNK-001", compartment_count: 4 },
        { truck_id: "TNK-002", compartment_count: 5 },
      ],
      total: 2,
    });
    mockList.mockResolvedValue(
      listResponseFixture(
        [compartmentFixture({ truck_id: "TNK-001" })],
        "TNK-001",
      ),
    );

    render(<TruckCompartmentsPage />);

    // The mount effect picks the first truck and fetches its compartments
    // without any user interaction.
    await waitFor(() => {
      expect(mockList).toHaveBeenCalledWith("TNK-001");
    });
    // Both tankers appear as quick-pick chips.
    expect(await screen.findByText("TNK-002")).toBeInTheDocument();
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

  it("renders a Check eligibility button on every compartment row", async () => {
    mockList.mockResolvedValue(
      listResponseFixture([
        compartmentFixture({ compartment_id: "TRUCK-1_c1" }),
        compartmentFixture({
          compartment_id: "TRUCK-1_c2",
          position_index: 1,
        }),
      ]),
    );
    render(<TruckCompartmentsPage />);
    await lookupTruck();

    await waitFor(() => {
      expect(
        screen.getByTestId("check-eligibility-TRUCK-1_c1"),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("check-eligibility-TRUCK-1_c2"),
    ).toBeInTheDocument();
  });

  it("submits a product code and renders the decision, governing rule, and reason", async () => {
    mockList.mockResolvedValue(
      listResponseFixture([
        compartmentFixture({
          compartment_id: "TRUCK-1_c1",
          last_loaded_product: "HEATING_OIL",
          state: "loaded",
        }),
      ]),
    );
    mockCheckEligibility.mockResolvedValue(
      eligibilityFixture({
        proposed_product: "DIESEL_2",
        previous_product: "HEATING_OIL",
        decision: "blocked",
        governing_rule: "blocked",
        reason: "cross_contamination_blocked",
      }),
    );

    render(<TruckCompartmentsPage />);
    await lookupTruck();

    await waitFor(() => {
      expect(
        screen.getByTestId("check-eligibility-TRUCK-1_c1"),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("check-eligibility-TRUCK-1_c1"));

    fireEvent.change(screen.getByLabelText(/Proposed product code/i), {
      target: { value: "diesel_2" },
    });
    await act(async () => {
      fireEvent.submit(screen.getByTestId("load-eligibility-form"));
    });

    await waitFor(() => {
      expect(mockCheckEligibility).toHaveBeenCalledWith(
        "TRUCK-1_c1",
        "DIESEL_2",
      );
    });
    await waitFor(() => {
      expect(
        screen.getByTestId("load-eligibility-decision-blocked"),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("load-eligibility-governing-blocked"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("load-eligibility-reason")).toHaveTextContent(
      /cross_contamination_blocked/,
    );
  });

  it("dismisses the eligibility modal when the close button is clicked", async () => {
    mockList.mockResolvedValue(listResponseFixture([compartmentFixture()]));
    render(<TruckCompartmentsPage />);
    await lookupTruck();

    await waitFor(() => {
      expect(
        screen.getByTestId("check-eligibility-TRUCK-1_c1"),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("check-eligibility-TRUCK-1_c1"));
    expect(screen.getByTestId("load-eligibility-form")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: /Close load eligibility form/i }),
    );
    expect(
      screen.queryByTestId("load-eligibility-form"),
    ).not.toBeInTheDocument();
  });
});

describe("ELIGIBILITY_DECISION_CONFIG", () => {
  it("covers every eligibility decision", () => {
    expect(Object.keys(ELIGIBILITY_DECISION_CONFIG).sort()).toEqual(
      ["allowed", "blocked", "requires_cleaning"].sort(),
    );
  });
});
