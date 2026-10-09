/**
 * Copilot inline confirmation for medium-risk actions (task 3.6, R3.5),
 * ported from the retired /ops/command console. Approve and Reject go
 * through the approvals API, so Live → Approvals stays the single inbox.
 */

import { TextDecoder, TextEncoder } from "node:util";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("react-markdown", () => ({
  __esModule: true,
  default: ({ children }: { children: string }) => <p>{children}</p>,
}));
jest.mock("remark-gfm", () => ({ __esModule: true, default: () => {} }));
jest.mock("../services/agentApi", () => ({
  approveAction: jest.fn(),
  rejectAction: jest.fn(),
}));

import { approveAction, rejectAction } from "../services/agentApi";
import AIChat from "./AIChat";

Object.assign(globalThis, { TextEncoder, TextDecoder });

function sseResponse(events: unknown[]) {
  const body = events.map((e) => `data: ${JSON.stringify(e)}\n`).join("");
  const bytes = new TextEncoder().encode(body);
  let sent = false;
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => {
          if (sent) return { done: true, value: undefined };
          sent = true;
          return { done: false, value: bytes };
        },
      }),
    },
  };
}

const CONFIRMATION = {
  type: "confirmation",
  action: {
    action_id: "act-42",
    tool_name: "reassign_job",
    risk_level: "medium",
    summary: "Move JOB-7 from Truck 3 to Truck 5",
  },
};

async function ask() {
  render(<AIChat isOpen onClose={jest.fn()} />);
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "Reassign JOB-7" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  });
  return screen.findByTestId("copilot-confirmation");
}

beforeEach(() => {
  jest.clearAllMocks();
  (globalThis as { fetch: unknown }).fetch = jest
    .fn()
    .mockResolvedValue(sseResponse([CONFIRMATION, { type: "done" }]));
  Element.prototype.scrollIntoView = jest.fn();
});

it("shows a confirmation card for a medium-risk action", async () => {
  const card = await ask();
  expect(card).toHaveAccessibleName("Confirm Reassign job");
  expect(within(card).getByText("Medium risk")).toBeInTheDocument();
  expect(
    within(card).getByText("Move JOB-7 from Truck 3 to Truck 5"),
  ).toBeInTheDocument();
  expect(
    within(card).getByRole("link", { name: "Open in Approvals" }),
  ).toHaveAttribute("href", "/dashboard/control?tab=approvals&id=act-42");
});

it("approves through the approvals API and records the decision", async () => {
  (approveAction as jest.Mock).mockResolvedValue({});
  const card = await ask();
  await act(async () => {
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
  });
  expect(approveAction).toHaveBeenCalledWith("act-42");
  await waitFor(() =>
    expect(within(card).getByText("Approved")).toBeInTheDocument(),
  );
  expect(within(card).queryByRole("button", { name: "Approve" })).toBeNull();
  expect(
    screen.getByText("Action approved. Executing now..."),
  ).toBeInTheDocument();
});

it("rejects through the approvals API", async () => {
  (rejectAction as jest.Mock).mockResolvedValue({});
  const card = await ask();
  await act(async () => {
    fireEvent.click(within(card).getByRole("button", { name: "Reject" }));
  });
  expect(rejectAction).toHaveBeenCalledWith("act-42", "Rejected from Copilot");
  await waitFor(() =>
    expect(within(card).getByText("Rejected")).toBeInTheDocument(),
  );
  expect(
    screen.getByText("Action rejected. The operation has been cancelled."),
  ).toBeInTheDocument();
});

it("keeps the card pending with the error when the decision fails", async () => {
  (approveAction as jest.Mock).mockRejectedValue(new Error("Already decided"));
  const card = await ask();
  await act(async () => {
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
  });
  expect(within(card).getByRole("alert")).toHaveTextContent("Already decided");
  expect(within(card).getByRole("button", { name: "Approve" })).toBeEnabled();
});
