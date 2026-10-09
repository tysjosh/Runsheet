/**
 * Settings → Integrations → Intake channels (task 3.8): list, Register
 * channel FormDialog, the one-time secret and the row actions.
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../services/intakeChannelsApi", () => ({
  listIntakeChannels: jest.fn(),
  createIntakeChannel: jest.fn(),
  rotateIntakeChannelSecret: jest.fn(),
  updateIntakeChannel: jest.fn(),
  deleteIntakeChannel: jest.fn(),
}));

import {
  createIntakeChannel,
  listIntakeChannels,
  rotateIntakeChannelSecret,
  updateIntakeChannel,
} from "../../services/intakeChannelsApi";
import IntakeChannelsAdminPanel from "./IntakeChannelsAdminPanel";

const channel = {
  channel_id: "voice-1",
  channel_type: "voice",
  display_name: "Voice AI",
  supported_schema_versions: ["1.0"],
  enabled: true,
};

beforeEach(() => {
  jest.clearAllMocks();
  (listIntakeChannels as jest.Mock).mockResolvedValue({ items: [channel] });
});

it("registers a channel in the FormDialog and shows the secret once", async () => {
  (createIntakeChannel as jest.Mock).mockResolvedValue({
    ...channel,
    channel_id: "edi-2",
    hmac_secret: "s3cr3t",
  });
  render(<IntakeChannelsAdminPanel />);
  await screen.findByText("Voice AI");
  fireEvent.click(screen.getByRole("button", { name: "Register Channel" }));
  const dialog = screen.getByRole("dialog", { name: "Register channel" });
  fireEvent.change(within(dialog).getByLabelText(/^Channel ID/), {
    target: { value: "bad id" },
  });
  fireEvent.click(
    within(dialog).getByRole("button", { name: "Register channel" }),
  );
  expect(
    await within(dialog).findByText("No spaces in the channel ID."),
  ).toBeInTheDocument();
  expect(within(dialog).getByText("Enter a display name.")).toBeInTheDocument();
  fireEvent.change(within(dialog).getByLabelText(/^Channel ID/), {
    target: { value: "edi-2" },
  });
  fireEvent.change(within(dialog).getByLabelText(/^Display name/), {
    target: { value: "EDI feed" },
  });
  fireEvent.change(within(dialog).getByLabelText(/^Type/), {
    target: { value: "edi" },
  });
  fireEvent.click(
    within(dialog).getByRole("button", { name: "Register channel" }),
  );
  await waitFor(() =>
    expect(createIntakeChannel).toHaveBeenCalledWith(
      expect.objectContaining({
        channel_id: "edi-2",
        display_name: "EDI feed",
        channel_type: "edi",
      }),
    ),
  );
  const secret = await screen.findByRole("dialog", { name: "Channel created" });
  expect(within(secret).getByTestId("secret-value")).toHaveTextContent(
    "s3cr3t",
  );
});

it("rotates and disables from the row menu", async () => {
  (rotateIntakeChannelSecret as jest.Mock).mockResolvedValue({
    ...channel,
    hmac_secret: "n3w",
  });
  (updateIntakeChannel as jest.Mock).mockResolvedValue({});
  render(<IntakeChannelsAdminPanel />);
  await screen.findByText("Voice AI");
  fireEvent.click(screen.getByRole("button", { name: /Actions for Voice AI/ }));
  fireEvent.click(
    await screen.findByRole("menuitem", { name: "Rotate secret" }),
  );
  expect(
    await screen.findByRole("dialog", { name: "Secret rotated" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  fireEvent.click(screen.getByRole("button", { name: /Actions for Voice AI/ }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "Disable" }));
  await waitFor(() =>
    expect(updateIntakeChannel).toHaveBeenCalledWith("voice-1", {
      enabled: false,
    }),
  );
});
