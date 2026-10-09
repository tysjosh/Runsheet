/**
 * Customers → Communications (UI revamp task 3.3): one toolbar with status
 * chips carrying the summary counts, a DataTable with status badges and
 * formatted dates, detail in a drawer with retry for failed sends.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("../hooks/useNotificationWebSocket", () => ({
  useNotificationWebSocket: () => ({
    lastNotificationCreated: null,
    lastStatusChanged: null,
  }),
}));
jest.mock("../services/notificationApi", () => ({
  getNotifications: jest.fn(),
  getNotificationSummary: jest.fn(),
  retryNotification: jest.fn(),
}));

import {
  getNotificationSummary,
  getNotifications,
  retryNotification,
} from "../services/notificationApi";
import NotificationHistoryTab from "./NotificationHistoryTab";

const mockList = getNotifications as jest.Mock;
const mockSummary = getNotificationSummary as jest.Mock;
const mockRetry = retryNotification as jest.Mock;

const row = (over: Record<string, unknown> = {}) => ({
  notification_id: "QA-N-1",
  notification_type: "delay_alert",
  channel: "sms",
  recipient_name: "QA Customer",
  recipient_reference: "+15550100",
  subject: "Your delivery is running late",
  message_body: "Running about 20 minutes late.",
  delivery_status: "failed",
  failure_reason: "Carrier rejected",
  related_entity_id: "QA-ORD-1",
  related_entity_type: "order",
  retry_count: 1,
  created_at: "2026-10-08T13:30:00Z",
  updated_at: "2026-10-08T13:31:00Z",
  sent_at: null,
  delivered_at: null,
  failed_at: "2026-10-08T13:31:00Z",
  ...over,
});

beforeEach(() => {
  jest.clearAllMocks();
  mockList.mockResolvedValue({
    data: [row()],
    pagination: { page: 1, size: 20, total: 1, total_pages: 1 },
  });
  mockSummary.mockResolvedValue({
    total: 12,
    by_status: { pending: 1, sent: 3, delivered: 7, failed: 1 },
    by_type: {},
    by_channel: {},
  });
});

describe("NotificationHistoryTab", () => {
  it("renders no page heading of its own (the hub owns the h1)", async () => {
    render(<NotificationHistoryTab />);
    await screen.findByText("QA Customer");
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
  });

  it("shows status chips with the summary counts and filters on click", async () => {
    render(<NotificationHistoryTab />);
    expect(
      await screen.findByRole("button", { name: /All\s*12/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Delivered\s*7/ }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Failed\s*1/ }));
    await waitFor(() =>
      expect(mockList).toHaveBeenLastCalledWith(
        expect.objectContaining({ delivery_status: "failed", page: 1 }),
      ),
    );
  });

  it("renders readable labels, a status badge and a formatted date", async () => {
    render(<NotificationHistoryTab />);
    const name = await screen.findByText("QA Customer");
    const tr = name.closest("tr") as HTMLElement;
    expect(tr).toHaveTextContent("Delay alert");
    expect(tr).toHaveTextContent("SMS");
    expect(tr).toHaveTextContent("Failed");
    expect(tr.querySelector("[data-status='exception']")).not.toBeNull();
    expect(tr).not.toHaveTextContent("2026-10-08T");
  });

  it("opens the detail drawer and retries a failed send", async () => {
    mockRetry.mockResolvedValue(row({ delivery_status: "sent" }));
    render(<NotificationHistoryTab />);
    fireEvent.click(await screen.findByText("QA Customer"));
    const drawer = await screen.findByRole("dialog", {
      name: "Notification details",
    });
    expect(drawer).toHaveTextContent("Carrier rejected");
    fireEvent.click(screen.getByRole("button", { name: /Retry notification/ }));
    await waitFor(() => expect(mockRetry).toHaveBeenCalledWith("QA-N-1"));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Retry notification/ }),
      ).toBeNull(),
    );
  });
});
