/**
 * CargoManifestEditor (3.10 review finding 7): Edit opens an lg FormDialog
 * with one row per item; weights are NumberFields (not type=number), an
 * untouched weight goes back exactly, errors keep the dialog open.
 */
import "@testing-library/jest-dom";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../services/schedulingApi", () => ({
  updateCargo: jest.fn(),
  updateCargoItemStatus: jest.fn(),
}));
jest.mock("./CargoManifestView", () => ({
  __esModule: true,
  default: ({ items }: { items: { item_id: string }[] }) => (
    <ul aria-label="Manifest">
      {items.map((i) => (
        <li key={i.item_id}>{i.item_id}</li>
      ))}
    </ul>
  ),
}));

import { updateCargo } from "../../services/schedulingApi";
import type { SchedulingCargoItem } from "../../types/api";
import CargoManifestEditor, { validateManifest } from "./CargoManifestEditor";

const ITEMS = [
  {
    item_id: "C-1",
    description: "Pallets",
    weight_kg: 1234.5678,
    container_number: "CONT-9",
    seal_number: "S-1",
    item_status: "pending",
  },
  {
    item_id: "C-2",
    description: "Drums",
    weight_kg: 200,
    item_status: "loaded",
  },
] as unknown as SchedulingCargoItem[];

function open() {
  fireEvent.click(screen.getByRole("button", { name: "Edit cargo manifest" }));
  return screen.getByRole("dialog", { name: "Edit cargo manifest" });
}

beforeEach(() => jest.clearAllMocks());

it("edits in an lg FormDialog with NumberField weights and saves", async () => {
  const onItemsChange = jest.fn();
  (updateCargo as jest.Mock).mockImplementation(async (_id, items) => ({
    data: items,
  }));
  render(
    <CargoManifestEditor
      jobId="JOB-7"
      items={ITEMS}
      onItemsChange={onItemsChange}
    />,
  );
  const dialog = open();
  const weights = within(dialog).getAllByLabelText(/Weight/);
  expect(weights[0]).not.toHaveAttribute("type", "number");
  expect(weights[0]).toHaveValue("1,234.57");
  fireEvent.change(weights[1], { target: { value: "1,250" } });
  fireEvent.blur(weights[1]);
  await act(async () => {
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Save manifest" }),
    );
  });
  expect(updateCargo).toHaveBeenCalledTimes(1);
  const [jobId, sent] = (updateCargo as jest.Mock).mock.calls[0];
  expect(jobId).toBe("JOB-7");
  // Untouched weight is sent back exactly; the edited one parsed.
  expect(sent[0].weight_kg).toBe(1234.5678);
  expect(sent[1].weight_kg).toBe(1250);
  await waitFor(() => expect(onItemsChange).toHaveBeenCalledWith(sent));
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("keeps the dialog open with the API error and the edits", async () => {
  (updateCargo as jest.Mock).mockRejectedValue(new Error("Job is locked"));
  render(<CargoManifestEditor jobId="JOB-7" items={ITEMS} />);
  const dialog = open();
  const desc = within(dialog).getAllByLabelText(/Description/)[0];
  fireEvent.change(desc, { target: { value: "Pallets (shrink-wrapped)" } });
  await act(async () => {
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Save manifest" }),
    );
  });
  expect(within(dialog).getByText("Job is locked")).toBeInTheDocument();
  expect(desc).toHaveValue("Pallets (shrink-wrapped)");
});

it("validates weights and descriptions", () => {
  const errors = validateManifest({
    items: [
      { ...ITEMS[0], weight_kg: Number.NaN },
      { ...ITEMS[1], description: " ", weight_kg: -1 },
    ],
  });
  expect(errors.weight_C1).toBeUndefined();
  expect(errors["weight_C-1"]).toBe("Enter a weight of 0 kg or more.");
  expect(errors["weight_C-2"]).toBe("Enter a weight of 0 kg or more.");
  expect(errors["description_C-2"]).toBe("Enter a description.");
  expect(validateManifest({ items: ITEMS })).toEqual({});
});

it("disables Edit when there are no items", () => {
  render(<CargoManifestEditor jobId="JOB-7" items={[]} />);
  expect(
    screen.getByRole("button", { name: "Edit cargo manifest" }),
  ).toBeDisabled();
});
