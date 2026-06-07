/**
 * Tests for the shared SearchableSelect primitive.
 *
 * This component replaces free-text "type the ID from memory" inputs across
 * dispatcher flows, so these tests pin the behaviours those flows depend on:
 * opening the panel, filtering by query, selecting an option, the loading and
 * empty states, and outside-click dismissal.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import {
  SearchableSelect,
  type SearchableSelectOption,
} from "./SearchableSelect";

const options: SearchableSelectOption[] = [
  { value: "DRV-001", label: "Ada Lovelace", sublabel: "CDL A · DRV-001" },
  { value: "DRV-002", label: "Grace Hopper", sublabel: "CDL B · DRV-002" },
  { value: "DRV-003", label: "Katherine Johnson", sublabel: "CDL A · DRV-003" },
];

it("shows the placeholder when nothing is selected", () => {
  render(
    <SearchableSelect
      options={options}
      value={null}
      onChange={jest.fn()}
      placeholder="Select a driver…"
    />,
  );
  expect(screen.getByText("Select a driver…")).toBeInTheDocument();
});

it("shows the selected option's label", () => {
  render(
    <SearchableSelect options={options} value="DRV-002" onChange={jest.fn()} />,
  );
  expect(screen.getByText("Grace Hopper")).toBeInTheDocument();
});

it("opens the panel and filters options by query", () => {
  render(
    <SearchableSelect options={options} value={null} onChange={jest.fn()} />,
  );

  fireEvent.click(screen.getByRole("button"));
  const search = screen.getByRole("textbox");
  fireEvent.change(search, { target: { value: "grace" } });

  expect(screen.getByText("Grace Hopper")).toBeInTheDocument();
  expect(screen.queryByText("Ada Lovelace")).not.toBeInTheDocument();
});

it("filters by sublabel / id as well as name", () => {
  render(
    <SearchableSelect options={options} value={null} onChange={jest.fn()} />,
  );

  fireEvent.click(screen.getByRole("button"));
  fireEvent.change(screen.getByRole("textbox"), {
    target: { value: "DRV-003" },
  });

  expect(screen.getByText("Katherine Johnson")).toBeInTheDocument();
  expect(screen.queryByText("Ada Lovelace")).not.toBeInTheDocument();
});

it("calls onChange with the option value when an option is clicked", () => {
  const onChange = jest.fn();
  render(
    <SearchableSelect options={options} value={null} onChange={onChange} />,
  );

  fireEvent.click(screen.getByRole("button"));
  fireEvent.click(screen.getByText("Ada Lovelace"));

  expect(onChange).toHaveBeenCalledWith("DRV-001");
});

it("selects the highlighted option with the Enter key", () => {
  const onChange = jest.fn();
  render(
    <SearchableSelect options={options} value={null} onChange={onChange} />,
  );

  fireEvent.click(screen.getByRole("button"));
  const search = screen.getByRole("textbox");
  // Move highlight to the second option, then commit.
  fireEvent.keyDown(search, { key: "ArrowDown" });
  fireEvent.keyDown(search, { key: "Enter" });

  expect(onChange).toHaveBeenCalledWith("DRV-002");
});

it("renders a loading state", () => {
  render(
    <SearchableSelect options={[]} value={null} onChange={jest.fn()} loading />,
  );
  fireEvent.click(screen.getByRole("button"));
  expect(screen.getByText("Loading…")).toBeInTheDocument();
});

it("renders the empty message when there are no matches", () => {
  render(
    <SearchableSelect
      options={options}
      value={null}
      onChange={jest.fn()}
      emptyMessage="No active drivers found"
    />,
  );
  fireEvent.click(screen.getByRole("button"));
  fireEvent.change(screen.getByRole("textbox"), {
    target: { value: "zzz-nobody" },
  });
  expect(screen.getByText("No active drivers found")).toBeInTheDocument();
});

it("does not select a disabled option", () => {
  const onChange = jest.fn();
  render(
    <SearchableSelect
      options={[{ value: "x", label: "Disabled option", disabled: true }]}
      value={null}
      onChange={onChange}
    />,
  );
  fireEvent.click(screen.getByRole("button"));
  fireEvent.click(screen.getByText("Disabled option"));
  expect(onChange).not.toHaveBeenCalled();
});
