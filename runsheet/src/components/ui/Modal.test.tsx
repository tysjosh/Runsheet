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
it("still closes on Escape from an expanded disclosure toggle", () => {
  const onClose = jest.fn();
  render(
    <Modal isOpen onClose={onClose} title="Disclosure">
      <button type="button" aria-expanded="true" aria-controls="more">
        More options
      </button>
      <div id="more">extra</div>
    </Modal>,
  );
  const toggle = screen.getByRole("button", { name: "More options" });
  toggle.focus();
  fireEvent.keyDown(toggle, { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);
});
it("leaves Escape to an open combobox or menu button first", () => {
  const onClose = jest.fn();
  render(
    <Modal isOpen onClose={onClose} title="Popups">
      <button
        type="button"
        role="combobox"
        aria-label="Product"
        aria-expanded="true"
        aria-controls="lb"
      >
        Product
      </button>
      <div id="lb" role="listbox" tabIndex={-1}>
        <div role="option" aria-selected="false" tabIndex={-1}>
          Diesel
        </div>
      </div>
      <button type="button" aria-haspopup="menu" aria-expanded="true">
        Actions
      </button>
    </Modal>,
  );
  const combo = screen.getByRole("combobox", { name: "Product" });
  fireEvent.keyDown(combo, { key: "Escape" });
  fireEvent.keyDown(screen.getByRole("button", { name: "Actions" }), {
    key: "Escape",
  });
  expect(onClose).not.toHaveBeenCalled();
});

describe("stacked modals (task 3.4)", () => {
  it("Escape closes only the topmost modal", () => {
    const closeOuter = jest.fn();
    const closeInner = jest.fn();
    render(
      <>
        <Modal isOpen onClose={closeOuter} title="Outer">
          <button type="button">outer</button>
        </Modal>
        <Modal isOpen onClose={closeInner} title="Inner">
          <button type="button">inner</button>
        </Modal>
      </>,
    );
    fireEvent.keyDown(document.activeElement ?? document.body, {
      key: "Escape",
    });
    expect(closeInner).toHaveBeenCalledTimes(1);
    expect(closeOuter).not.toHaveBeenCalled();
  });
});
