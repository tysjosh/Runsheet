/**
 * Settings → Notifications → Templates (task 3.8): the template list as a
 * DataTable and the lg template FormDialog (validation, placeholders, live
 * preview, save).
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../services/notificationApi", () => ({
  getNotificationRules: jest.fn().mockResolvedValue({ items: [] }),
  getNotificationPreferences: jest.fn().mockResolvedValue({ items: [] }),
  getNotificationTemplates: jest.fn(),
  updateNotificationRule: jest.fn(),
  updateNotificationTemplate: jest.fn(),
  upsertNotificationPreference: jest.fn(),
}));

import {
  getNotificationTemplates,
  updateNotificationTemplate,
} from "../services/notificationApi";
import NotificationSettingsTab from "./NotificationSettingsTab";

const template = {
  template_id: "tpl-1",
  event_type: "delay_alert",
  channel: "sms",
  subject_template: "",
  body_template: "Order {order_id} is running late",
  placeholders: ["order_id", "eta"],
};

beforeEach(() => {
  jest.clearAllMocks();
  (getNotificationTemplates as jest.Mock).mockResolvedValue({
    items: [template],
  });
});

async function openTemplate() {
  render(<NotificationSettingsTab />);
  fireEvent.click(screen.getByRole("tab", { name: "Templates" }));
  const table = await screen.findByRole("table", {
    name: "Notification templates",
  });
  fireEvent.click(await within(table).findByText(/running late/));
  return screen.getByRole("dialog", { name: "Edit template" });
}

it("edits a template in the FormDialog with a live preview", async () => {
  (updateNotificationTemplate as jest.Mock).mockResolvedValue({
    ...template,
    body_template: "Order {order_id} ETA {eta}",
  });
  const dialog = await openTemplate();
  const body = within(dialog).getByLabelText(/^Message/);
  fireEvent.change(body, { target: { value: "Order {order_id} ETA " } });
  fireEvent.click(
    within(dialog).getByRole("button", { name: "Insert placeholder eta" }),
  );
  expect(body).toHaveValue("Order {order_id} ETA {eta}");
  const preview = within(dialog).getByRole("region", { name: "Live preview" });
  expect(preview).not.toHaveTextContent("{order_id}");
  fireEvent.click(within(dialog).getByRole("button", { name: "Save template" }));
  await waitFor(() =>
    expect(updateNotificationTemplate).toHaveBeenCalledWith("tpl-1", {
      subject_template: undefined,
      body_template: "Order {order_id} ETA {eta}",
    }),
  );
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

it("requires a message body and keeps the dialog open", async () => {
  const dialog = await openTemplate();
  fireEvent.change(within(dialog).getByLabelText(/^Message/), {
    target: { value: "  " },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save template" }));
  expect(
    await within(dialog).findByText("Enter the message body."),
  ).toBeInTheDocument();
  expect(updateNotificationTemplate).not.toHaveBeenCalled();
});
