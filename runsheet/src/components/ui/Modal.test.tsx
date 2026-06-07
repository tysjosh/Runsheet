/**
 * Tests for the shared Modal primitive's accessibility behavior (via
 * useDialogA11y): dialog semantics, initial focus, Escape-to-close, and
 * focus restoration on close.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { Modal } from "./Modal";

it("exposes dialog semantics and labels itself with the title", () => {
  render(
    <Modal isOpen onClose={jest.fn()} title="Delete thing">
      <p>body</p>
    </Modal>,
  );
  const dialog = screen.getByRole("dialog");
  expect(dialog).toHaveAttribute("aria-modal", "true");
  // aria-labelledby points at the rendered title.
  expect(dialog).toHaveAccessibleName("Delete thing");
});

it("moves focus into the dialog on open", () => {
  render(
    <Modal isOpen onClose={jest.fn()} title="Focus me">
      <button type="button">Inside</button>
    </Modal>,
  );
  // First focusable is the header close button.
  expect(screen.getByLabelText("Close modal")).toHaveFocus();
});

it("closes on Escape", () => {
  const onClose = jest.fn();
  render(
    <Modal isOpen onClose={onClose} title="Escape closes">
      <p>body</p>
    </Modal>,
  );
  fireEvent.keyDown(document, { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);
});

it("restores focus to the trigger when closed", () => {
  function Harness() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          Open
        </button>
        <Modal isOpen={open} onClose={() => setOpen(false)} title="Restore">
          <p>body</p>
        </Modal>
      </>
    );
  }
  render(<Harness />);
  const trigger = screen.getByRole("button", { name: "Open" });
  trigger.focus();
  fireEvent.click(trigger);
  // Dialog open, focus moved inside.
  expect(screen.getByLabelText("Close modal")).toHaveFocus();
  // Close via Escape; focus returns to the trigger.
  fireEvent.keyDown(document, { key: "Escape" });
  expect(trigger).toHaveFocus();
});
