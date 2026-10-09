/**
 * Task 3.11 shared additions, all opt-in: FormDialog `mobile`,
 * `submitDisabled`, `keepOpenOnSuccess`; Modal `mobile="sheet"`; DataTable
 * `rowHeight="touch"` and `stickyTop`. Each prop is tested set and unset, so
 * existing callers (props absent) keep today's markup and behaviour.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FormDialog } from "./FormDialog";
import { Modal } from "./Modal";
import { EmptyState } from "./EmptyState";
import { FilterChips } from "./FilterChips";
import { InlineBanner } from "./InlineBanner";
import { Menu } from "./Menu";
import { DataTable } from "./Table";

type V = { name: string };

function Dialog(props: {
  onSubmit?: (v: V) => Promise<unknown>;
  onClose?: () => void;
  onSaved?: (r: unknown) => void;
  mobile?: "sheet";
  submitDisabled?: boolean;
  keepOpenOnSuccess?: boolean;
}) {
  return (
    <FormDialog<V>
      open
      title="Request delivery"
      initialValues={{ name: "a" }}
      onSubmit={props.onSubmit ?? jest.fn().mockResolvedValue("ok")}
      onClose={props.onClose ?? jest.fn()}
      onSaved={props.onSaved}
      submitLabel="Send"
      mobile={props.mobile}
      submitDisabled={props.submitDisabled}
      keepOpenOnSuccess={props.keepOpenOnSuccess}
    >
      {() => <p>body</p>}
    </FormDialog>
  );
}

describe("Modal mobile", () => {
  it("is the centred dialog by default (no-change)", () => {
    render(
      <Modal isOpen onClose={jest.fn()} title="t">
        <p>b</p>
      </Modal>,
    );
    const dialog = screen.getByRole("dialog");
    const backdrop = dialog.parentElement as HTMLElement;
    expect(backdrop.className).toContain("items-center");
    expect(backdrop.className).not.toContain("items-end");
    expect(backdrop).not.toHaveAttribute("data-mobile");
    expect(dialog.className).toContain("mx-4");
    expect(dialog.className).toContain("rounded-xl");
    expect(dialog.className).not.toContain("rounded-t-xl");
  });
  it("is a bottom sheet below sm with mobile=sheet", () => {
    render(
      <Modal isOpen onClose={jest.fn()} title="t" mobile="sheet">
        <p>b</p>
      </Modal>,
    );
    const dialog = screen.getByRole("dialog");
    const backdrop = dialog.parentElement as HTMLElement;
    expect(backdrop).toHaveAttribute("data-mobile", "sheet");
    expect(backdrop.className).toContain("items-end");
    expect(backdrop.className).toContain("sm:items-center");
    expect(dialog.className).toContain("rounded-t-xl");
    expect(dialog.className).toContain("sm:rounded-xl");
    expect(dialog.className).toContain("mx-0");
  });
});

describe("FormDialog additions", () => {
  it("passes mobile through to the Modal", () => {
    render(<Dialog mobile="sheet" />);
    expect(screen.getByRole("dialog").parentElement).toHaveAttribute(
      "data-mobile",
      "sheet",
    );
  });
  it("submits and closes by default (no-change)", async () => {
    const onSubmit = jest.fn().mockResolvedValue("ok");
    const onClose = jest.fn();
    render(<Dialog onSubmit={onSubmit} onClose={onClose} />);
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).not.toHaveAttribute("aria-disabled");
    fireEvent.click(send);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(onSubmit).toHaveBeenCalledWith({ name: "a" });
  });
  it("submitDisabled keeps the button focusable but does nothing", () => {
    const onSubmit = jest.fn().mockResolvedValue("ok");
    render(<Dialog onSubmit={onSubmit} submitDisabled />);
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toHaveAttribute("aria-disabled", "true");
    expect(send).not.toBeDisabled();
    send.focus();
    expect(send).toHaveFocus();
    fireEvent.click(send);
    expect(onSubmit).not.toHaveBeenCalled();
  });
  it("keepOpenOnSuccess stays open and still reports the result", async () => {
    const onClose = jest.fn();
    const onSaved = jest.fn();
    render(<Dialog onClose={onClose} onSaved={onSaved} keepOpenOnSuccess />);
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith("ok"));
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

describe("DataTable additions", () => {
  const cols = [{ key: "name", header: "Name" }];
  const data = [{ id: "1", name: "A" }];
  it("keeps comfortable rows and the scroll wrapper by default (no-change)", () => {
    const { container } = render(
      <DataTable columns={cols} data={data} keyExtractor={(r) => r.id} />,
    );
    const table = container.querySelector("table") as HTMLElement;
    expect((table.parentElement as HTMLElement).className).toContain(
      "overflow-x-auto",
    );
    expect(container.querySelector("tbody tr")?.className).toContain("h-10");
    expect(container.querySelector("thead")?.getAttribute("style")).toBeNull();
  });
  it("renders 48 px rows with rowHeight=touch", () => {
    const { container } = render(
      <DataTable
        columns={cols}
        data={data}
        keyExtractor={(r) => r.id}
        rowHeight="touch"
      />,
    );
    expect(container.querySelector("tbody tr")?.className).toContain("h-12");
  });
  it("sticks the header at stickyTop without the overflow wrapper", () => {
    const { container } = render(
      <DataTable
        columns={cols}
        data={data}
        keyExtractor={(r) => r.id}
        stickyHeader={false}
        stickyTop={56}
      />,
    );
    const table = container.querySelector("table") as HTMLElement;
    expect((table.parentElement as HTMLElement).className).not.toContain(
      "overflow-x-auto",
    );
    const thead = container.querySelector("thead") as HTMLElement;
    expect(thead.className).toContain("sticky");
    expect(thead.style.top).toBe("56px");
  });
});

describe("44 px touch variants (R14.19)", () => {
  const chips = [
    { id: "all", label: "All", count: 3 },
    { id: "open", label: "Open", count: 1 },
  ];
  it("FilterChips: 28 px by default, 44 px with size=touch", () => {
    const { rerender } = render(
      <FilterChips
        options={chips}
        value="all"
        onChange={jest.fn()}
        label="Status"
      />,
    );
    expect(screen.getByRole("button", { name: /All/ }).className).toContain(
      "h-7",
    );
    rerender(
      <FilterChips
        options={chips}
        value="all"
        onChange={jest.fn()}
        label="Status"
        size="touch"
      />,
    );
    const all = screen.getByRole("button", { name: /All/ });
    expect(all.className).toContain("h-11");
    expect(all.className).not.toContain("h-7");
  });
  it("Menu: 32 px items by default, 44 px with size=touch", () => {
    const trigger = (p: object) => (
      <button type="button" {...p}>
        Open
      </button>
    );
    const items = [{ id: "a", label: "Account" }];
    const { unmount } = render(<Menu trigger={trigger} items={items} />);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("menuitem").className).toContain("h-8");
    unmount();
    render(<Menu trigger={trigger} items={items} size="touch" />);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("menuitem").className).toContain("h-11");
  });
  it("EmptyState: the action reaches 44 px only with size=touch", () => {
    const action = { label: "Request delivery", onClick: jest.fn() };
    const { rerender } = render(
      <EmptyState title="No orders" action={action} />,
    );
    expect(
      screen.getByRole("button", { name: "Request delivery" }).className,
    ).not.toContain("min-h-11");
    rerender(<EmptyState title="No orders" action={action} size="touch" />);
    expect(
      screen.getByRole("button", { name: "Request delivery" }).className,
    ).toContain("min-h-11");
  });
  it("InlineBanner: 32 px strip by default, 44 px with size=touch", () => {
    const { rerender } = render(<InlineBanner>Offline</InlineBanner>);
    expect(screen.getByRole("status").className).toContain("min-h-8");
    expect(screen.getByRole("status").className).toContain("text-xs");
    rerender(<InlineBanner size="touch">Offline</InlineBanner>);
    expect(screen.getByRole("status").className).toContain("min-h-11");
    expect(screen.getByRole("status").className).toContain("text-sm");
  });
});
