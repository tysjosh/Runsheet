/**
 * FormDialog (task 1.6, R6.2–R6.3): focus trap and return, Esc/× close, the
 * dirty-form prompt, Enter submits (not in a textarea), envelope field errors
 * in both shapes, the form-level banner, the saving spinner, the success
 * toast, steps and sections.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import { ApiError } from "../../services/api";
import { Field, INPUT_CLASS } from "./Field";
import { envelopeFieldErrors, FormDialog, FormSection } from "./FormDialog";
import { GlobalToaster, resetToasts } from "./toast/notify";

type V = { name: string; notes: string; gallons: number | null };
const INITIAL: V = { name: "Station 1", notes: "", gallons: 100 };

function Harness({
  onSubmit = jest.fn().mockResolvedValue({ id: "s1" }),
  onSaved = jest.fn(),
  validate,
  steps,
  sections,
}: {
  onSubmit?: (v: V) => Promise<unknown>;
  onSaved?: (r: unknown) => void;
  validate?: (v: V) => Record<string, string | undefined>;
  steps?: {
    id: string;
    title: string;
    validate?: (v: V) => Record<string, string | undefined>;
  }[];
  sections?: { id: string; title: string }[];
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Edit station
      </button>
      <FormDialog<V>
        open={open}
        title="Edit fuel station"
        help="Changes apply to new orders."
        initialValues={INITIAL}
        validate={validate}
        onSubmit={onSubmit}
        onSaved={onSaved}
        onClose={() => setOpen(false)}
        successMessage="Station saved"
        steps={steps}
        sections={sections}
      >
        {({ values, set, errors, step }) => (
          <>
            {(!step || step === "one") && (
              <Field label="Name" error={errors.name} required>
                <input
                  className={INPUT_CLASS}
                  value={values.name}
                  onChange={(e) => set("name", e.target.value)}
                />
              </Field>
            )}
            {(!step || step === "two") && (
              <Field label="Notes" error={errors.notes}>
                <textarea
                  value={values.notes}
                  onChange={(e) => set("notes", e.target.value)}
                />
              </Field>
            )}
            {sections && (
              <FormSection id="extra" title="Extra">
                <p>extra section</p>
              </FormSection>
            )}
          </>
        )}
      </FormDialog>
      <GlobalToaster />
    </>
  );
}

const open = () =>
  fireEvent.click(screen.getByRole("button", { name: "Edit station" }));
const nameInput = () => screen.getByRole("textbox", { name: /Name/ });

beforeEach(() => {
  resetToasts();
  // FormDialog focuses the first invalid field in a frame callback.
  jest
    .spyOn(window, "requestAnimationFrame")
    .mockImplementation((cb: FrameRequestCallback) => {
      cb(0);
      return 0;
    });
});
afterEach(() => jest.restoreAllMocks());

describe("FormDialog", () => {
  it("is a labelled, described modal dialog that starts in the first field", () => {
    render(<Harness />);
    open();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleName("Edit fuel station");
    expect(dialog).toHaveAccessibleDescription("Changes apply to new orders.");
    expect(nameInput()).toHaveFocus();
  });

  it("traps Tab inside the dialog and returns focus to the trigger on close", () => {
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Edit station" });
    trigger.focus();
    open();
    const save = screen.getByRole("button", { name: "Save changes" });
    save.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    // Wrapped to the first focusable (the × button).
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(save).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it("closes on Escape and on × when the form is clean", () => {
    render(<Harness />);
    open();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    open();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("asks before discarding a dirty form (Esc, ×, Cancel)", () => {
    render(<Harness />);
    open();
    fireEvent.change(nameInput(), { target: { value: "Renamed" } });

    fireEvent.keyDown(document, { key: "Escape" });
    const prompt = screen.getByRole("alertdialog", {
      name: "Discard changes?",
    });
    expect(prompt).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Keep editing" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(nameInput()).toHaveValue("Renamed");

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    // Escape inside the prompt dismisses the prompt, not the dialog.
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("submits on Enter in a field but not in a textarea", async () => {
    const onSubmit = jest.fn().mockResolvedValue({});
    render(<Harness onSubmit={onSubmit} />);
    open();
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Notes" }), {
      key: "Enter",
    });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(nameInput(), { key: "Enter" });
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(INITIAL));
  });

  it("validates before submitting and links the error to the field", async () => {
    const onSubmit = jest.fn();
    render(
      <Harness
        onSubmit={onSubmit}
        validate={(v) => ({ name: v.name ? undefined : "Name is required" })}
      />,
    );
    open();
    fireEvent.change(nameInput(), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onSubmit).not.toHaveBeenCalled();
    const input = nameInput();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("Name is required");
    expect(input).toHaveFocus();
    // Editing the field clears its error.
    fireEvent.change(input, { target: { value: "x" } });
    expect(input).not.toHaveAttribute("aria-invalid");
  });

  it("maps details.fields from the error envelope to inline errors", async () => {
    const onSubmit = jest.fn().mockRejectedValue(
      new ApiError("Validation failed", 422, "VALIDATION_ERROR", {
        fields: { name: "Name already used" },
      }),
    );
    render(<Harness onSubmit={onSubmit} />);
    open();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Name already used")).toBeInTheDocument();
    expect(nameInput()).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("maps FastAPI-style details.errors ([{loc,msg}]) to inline errors", async () => {
    const onSubmit = jest.fn().mockRejectedValue(
      new ApiError("Invalid", 422, "VALIDATION_ERROR", {
        errors: [{ loc: ["body", "notes"], msg: "Too long" }],
      }),
    );
    render(<Harness onSubmit={onSubmit} />);
    open();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Too long")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Notes" })).toHaveAttribute(
      "aria-invalid",
      "true",
    );
  });

  it("shows the envelope message in a banner when no field is named", async () => {
    const onSubmit = jest
      .fn()
      .mockRejectedValue(new ApiError("Station is locked by a plan", 409));
    render(<Harness onSubmit={onSubmit} />);
    open();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Station is locked by a plan",
    );
  });

  it("disables the primary with a spinner while saving, then toasts and closes", async () => {
    let resolve: (v: unknown) => void = () => {};
    const onSubmit = jest.fn(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const onSaved = jest.fn();
    render(<Harness onSubmit={onSubmit} onSaved={onSaved} />);
    open();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    const save = screen.getByRole("button", { name: "Save changes" });
    expect(save).toBeDisabled();
    expect(save).toHaveAttribute("aria-busy", "true");
    expect(save.querySelector("svg.animate-spin")).not.toBeNull();
    await act(async () => resolve({ id: "s1" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onSaved).toHaveBeenCalledWith({ id: "s1" });
    expect(screen.getByText("Station saved")).toBeInTheDocument();
  });

  it("walks steps: Next validates the step and Submit only shows on the last", async () => {
    const onSubmit = jest.fn().mockResolvedValue({});
    render(
      <Harness
        onSubmit={onSubmit}
        steps={[
          {
            id: "one",
            title: "Basics",
            validate: (v) => ({ name: v.name ? undefined : "Required" }),
          },
          { id: "two", title: "Notes" },
        ]}
      />,
    );
    open();
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
    expect(screen.getByRole("list", { name: "Steps" })).toBeInTheDocument();
    fireEvent.change(nameInput(), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("Required")).toBeInTheDocument();
    fireEvent.change(nameInput(), { target: { value: "ok" } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("textbox", { name: "Notes" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(nameInput()).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
  });

  it("renders section jump links for sectioned dialogs", () => {
    render(
      <Harness
        sections={[
          { id: "basics", title: "Basics" },
          { id: "extra", title: "Extra" },
        ]}
      />,
    );
    open();
    expect(
      screen.getByRole("navigation", { name: "Sections" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Extra" })).toBeInTheDocument();
  });
});

describe("envelopeFieldErrors", () => {
  it("returns null without field details", () => {
    expect(envelopeFieldErrors(new ApiError("x", 500))).toBeNull();
    expect(envelopeFieldErrors(null)).toBeNull();
  });
  it("reads both shapes", () => {
    expect(
      envelopeFieldErrors({
        details: { fields: { a: "bad", b: ["first", "second"] } },
      }),
    ).toEqual({ a: "bad", b: "first" });
    expect(
      envelopeFieldErrors({
        details: { errors: [{ loc: ["body", "c"], msg: "no" }] },
      }),
    ).toEqual({ c: "no" });
  });
});
