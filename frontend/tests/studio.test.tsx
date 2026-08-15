/**
 * The Studio (design document sections 48, 49, 51, 52).
 *
 * These test the things the design document actually asks for: that the
 * preview names each column's generation source, that a distribution is
 * readable as a distribution, that an inferred generator is visibly inferred,
 * and that editing a field sends a *targeted* patch rather than rewriting the
 * document.
 */

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  DistributionBars,
  GeneratorBadge,
  formatBytes,
  formatDuration,
  formatNumber,
  generatorFamily,
  generatorLabel,
  renderCell,
} from "../src/components/ui";
import { FieldEditor } from "../src/studio/FieldEditor";
import { PreviewTable } from "../src/studio/PreviewTable";
import {
  employeeEntity,
  employeeFields,
  previewFixture,
  renderWithProviders,
} from "./fixtures";

describe("generator identity (section 51)", () => {
  it("gives each generator family its own colour class", () => {
    expect(generatorFamily("faker")).toBe("faker");
    expect(generatorFamily("llm")).toBe("llm");
    expect(generatorFamily("image")).toBe("media");
    expect(generatorFamily("expression")).toBe("derived");
    expect(generatorFamily("sequence")).toBe("rule");
  });

  it("labels a generator in words, never only in colour", () => {
    expect(generatorLabel("llm")).toBe("LLM");
    expect(generatorLabel("sequence")).toBe("SEQ");
    // An unknown generator still gets a readable label rather than nothing.
    expect(generatorLabel("something_new")).toBe("SOMETHIN");
  });

  it("renders a badge carrying the generator's description", () => {
    renderWithProviders(<GeneratorBadge generator="llm" title="llm(per_record)" />);
    const badge = screen.getByTitle("llm(per_record)");
    expect(badge).toHaveTextContent("LLM");
    expect(badge).toHaveClass("badge-llm");
  });
});

describe("preview table (section 51)", () => {
  it("puts a generation-source row under the header", () => {
    renderWithProviders(
      <PreviewTable preview={previewFixture} entity={employeeEntity} />,
    );
    // The header row names the columns...
    expect(screen.getByRole("columnheader", { name: "employee_id" })).toBeInTheDocument();
    // ...and the row beneath names what produced them.
    expect(screen.getByText("SEQ")).toBeInTheDocument();
    expect(screen.getByText("FAKER")).toBeInTheDocument();
    expect(screen.getByText("LLM")).toBeInTheDocument();
    expect(screen.getByText("TMPL")).toBeInTheDocument();
  });

  it("shows every record", () => {
    renderWithProviders(
      <PreviewTable preview={previewFixture} entity={employeeEntity} />,
    );
    expect(screen.getByText("EMP-000001")).toBeInTheDocument();
    expect(screen.getByText("EMP-000002")).toBeInTheDocument();
  });

  it("shows a cell's provenance on hover", () => {
    renderWithProviders(
      <PreviewTable preview={previewFixture} entity={employeeEntity} />,
    );
    const cell = screen.getByText("tyrone@example.com").closest("td");
    expect(cell?.title).toContain("template(");
    expect(cell?.title).toContain("reads: first_name");
  });

  it("marks a null distinctly rather than leaving the cell blank", () => {
    renderWithProviders(
      <PreviewTable preview={previewFixture} entity={employeeEntity} />,
    );
    const nulls = screen.getAllByText("null");
    expect(nulls.length).toBeGreaterThan(0);
    expect(nulls[0]?.closest("td")).toHaveClass("faint");
  });

  it("copes with a column the schema no longer describes", () => {
    renderWithProviders(<PreviewTable preview={previewFixture} entity={undefined} />);
    expect(screen.getByText("EMP-000001")).toBeInTheDocument();
  });
});

describe("distribution preview (section 52)", () => {
  it("draws a bar and a percentage for each value", () => {
    renderWithProviders(
      <DistributionBars distribution={{ Windows: 0.67, macOS: 0.18, Linux: 0.13 }} />,
    );
    expect(screen.getByText("Windows")).toBeInTheDocument();
    expect(screen.getByText("67.0%")).toBeInTheDocument();
    expect(screen.getByText("13.0%")).toBeInTheDocument();
  });

  it("orders by share so the shape is readable at a glance", () => {
    const { container } = renderWithProviders(
      <DistributionBars distribution={{ rare: 0.1, common: 0.9 }} />,
    );
    const labels = [...container.querySelectorAll(".dist-label")].map(
      (node) => node.textContent,
    );
    expect(labels).toEqual(["common", "rare"]);
  });

  it("summarises a long tail rather than printing all of it", () => {
    const many = Object.fromEntries(
      Array.from({ length: 20 }, (_, index) => [`v${index}`, 0.05]),
    );
    renderWithProviders(<DistributionBars distribution={many} limit={5} />);
    expect(screen.getByText("+15 more")).toBeInTheDocument();
  });
});

describe("field editor (section 49)", () => {
  const editorProps = {
    entity: employeeEntity,
    editable: true,
    onPreview: vi.fn(),
    pending: false,
  };

  it("shows section 49's controls", () => {
    renderWithProviders(
      <FieldEditor {...editorProps} field={employeeFields.biography!} onPatch={vi.fn()} />,
    );
    expect(screen.getByLabelText("Name")).toHaveValue("biography");
    expect(screen.getByLabelText("Type")).toBeInTheDocument();
    expect(screen.getByLabelText("Meaning")).toHaveValue("A short professional biography.");
    expect(screen.getByLabelText("Generation")).toBeInTheDocument();
    expect(screen.getByLabelText("Null probability")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /generate samples/i })).toBeInTheDocument();
  });

  it("shows the fields this one reads, which is why the order is what it is", () => {
    renderWithProviders(
      <FieldEditor {...editorProps} field={employeeFields.email!} onPatch={vi.fn()} />,
    );
    expect(screen.getByText("Context")).toBeInTheDocument();
    expect(screen.getByText("first_name")).toBeInTheDocument();
  });

  it("explains an inferred generator rather than presenting it as chosen", () => {
    renderWithProviders(
      <FieldEditor {...editorProps} field={employeeFields.first_name!} onPatch={vi.fn()} />,
    );
    expect(
      screen.getByText(/Inferred by the recommendation engine/i),
    ).toBeInTheDocument();
  });

  it("sends one targeted operation when a field is edited", async () => {
    const onPatch = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(
      <FieldEditor {...editorProps} field={employeeFields.biography!} onPatch={onPatch} />,
    );

    const meaning = screen.getByLabelText("Meaning");
    await user.clear(meaning);
    await user.type(meaning, "A terse professional biography.");
    await user.tab();

    await waitFor(() => expect(onPatch).toHaveBeenCalledTimes(1));
    expect(onPatch).toHaveBeenCalledWith([
      {
        op: "set_field",
        entity: "employee",
        field: "biography",
        key: "semantic",
        value: "A terse professional biography.",
      },
    ]);
  });

  it("does not send a patch when nothing changed", async () => {
    const onPatch = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(
      <FieldEditor {...editorProps} field={employeeFields.biography!} onPatch={onPatch} />,
    );
    await user.click(screen.getByLabelText("Meaning"));
    await user.tab();
    expect(onPatch).not.toHaveBeenCalled();
  });

  it("renames through a rename operation, not a delete and an add", async () => {
    const onPatch = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(
      <FieldEditor {...editorProps} field={employeeFields.email!} onPatch={onPatch} />,
    );

    const name = screen.getByLabelText("Name");
    await user.clear(name);
    await user.type(name, "work_email");
    await user.tab();

    await waitFor(() => expect(onPatch).toHaveBeenCalled());
    expect(onPatch).toHaveBeenCalledWith([
      { op: "rename_field", entity: "employee", field: "email", name: "work_email" },
    ]);
  });

  it("shows a categorical field's distribution (section 52)", () => {
    renderWithProviders(
      <FieldEditor {...editorProps} field={employeeFields.department!} onPatch={vi.fn()} />,
    );
    expect(screen.getByText("Distribution")).toBeInTheDocument();
    expect(screen.getByText("Engineering")).toBeInTheDocument();
    expect(screen.getByText("50.0%")).toBeInTheDocument();
  });

  it("offers tone only where a model will read it", () => {
    const { unmount } = renderWithProviders(
      <FieldEditor {...editorProps} field={employeeFields.biography!} onPatch={vi.fn()} />,
    );
    expect(screen.getByLabelText("Tone")).toBeInTheDocument();
    unmount();

    renderWithProviders(
      <FieldEditor {...editorProps} field={employeeFields.employee_id!} onPatch={vi.fn()} />,
    );
    expect(screen.queryByLabelText("Tone")).not.toBeInTheDocument();
  });

  it("refuses to edit a project with no file to save to", () => {
    renderWithProviders(
      <FieldEditor
        {...editorProps}
        editable={false}
        field={employeeFields.biography!}
        onPatch={vi.fn()}
      />,
    );
    expect(
      screen.getByText(/no file to write to, so the schema is read-only/i),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveAttribute("readonly");
    expect(screen.getByLabelText("Type")).toBeDisabled();
  });

  it("shows the options belonging to this generator", () => {
    renderWithProviders(
      <FieldEditor {...editorProps} field={employeeFields.employee_id!} onPatch={vi.fn()} />,
    );
    expect(screen.getByLabelText("format")).toHaveValue("EMP-{000000}");
  });
});

describe("formatting", () => {
  it("formats numbers, bytes and durations for reading", () => {
    expect(formatNumber(1234567)).toBe("1,234,567");
    expect(formatNumber(null)).toBe("-");
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(5_368_709_120)).toBe("5.0 GB");
    expect(formatDuration(0.25)).toBe("250ms");
    expect(formatDuration(45.5)).toBe("45.5s");
    expect(formatDuration(3725)).toBe("1h 2m");
  });

  it("renders any generated value compactly", () => {
    expect(renderCell(null)).toBe("null");
    expect(renderCell(42)).toBe("42");
    expect(renderCell(["a", "b"])).toBe('["a","b"]');
  });
});
