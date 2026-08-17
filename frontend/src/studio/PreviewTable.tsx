/**
 * Data preview (design document section 51).
 *
 *     employee_id   name          title               biography
 *     RULE          FAKER         LLM                 LLM
 *
 * Section 51 puts a generation-source row under the header and says clicking a
 * cell shows its provenance. That is exactly what this does: the badge row is
 * the first row of the table, and a cell's title carries the generator, its
 * configuration and the fields it read.
 */

import { type ReactNode, useState } from "react";

import type { EntityView, PreviewResult } from "../api/types";
import { GeneratorBadge, renderCell } from "../components/ui";
import { RecordEditor } from "./RecordEditor";

export function PreviewTable({
  preview,
  entity,
}: {
  preview: PreviewResult;
  entity: EntityView | undefined;
}): ReactNode {
  const columns = preview.columns;
  // Which row the record editor is open on. Section 104's editing, which writes
  // a patch rule rather than saving a row - see `RecordEditor`.
  const [editing, setEditing] = useState<number | null>(null);

  return (
    <>
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
          {/* Section 51's source row. */}
          <tr>
            {columns.map((column) => {
              const generator = preview.sources[column] ?? "?";
              const field = entity?.fields[column];
              return (
                <th key={column} style={{ borderBottom: "1px solid var(--border)" }}>
                  <GeneratorBadge
                    generator={generator}
                    title={field?.generator_describe ?? generator}
                  />
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {preview.records.map((record, index) => (
            <tr
              key={index}
              onDoubleClick={() => setEditing(index)}
              title="Double-click to build a patch rule from this record (section 104)"
              className={editing === index ? "selected" : undefined}
            >
              {columns.map((column) => {
                const value = record[column];
                const field = entity?.fields[column];
                return (
                  <td
                    key={column}
                    className={value === null || value === undefined ? "faint" : undefined}
                    title={provenanceOf(column, field, value)}
                  >
                    <span className="truncate">{renderCell(value)}</span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
    {editing !== null && preview.records[editing] && (
      <RecordEditor
        entity={entity}
        record={preview.records[editing] as Record<string, unknown>}
        onClose={() => setEditing(null)}
      />
    )}
    </>
  );
}

/** What a cell says about itself when you hover it (section 51). */
function provenanceOf(
  column: string,
  field: EntityView["fields"][string] | undefined,
  value: unknown,
): string {
  if (!field) return `${column} = ${renderCell(value)}`;
  const lines = [
    `${column} = ${renderCell(value)}`,
    `generator: ${field.generator_describe}`,
    `type: ${field.type}`,
  ];
  if (field.dependencies.length > 0) {
    lines.push(`reads: ${field.dependencies.join(", ")}`);
  }
  if (field.inferred) {
    lines.push("generator inferred by the recommendation engine");
  }
  if (field.requires_provider) {
    lines.push(`provider: ${field.requires_provider}`);
  }
  return lines.join("\n");
}
