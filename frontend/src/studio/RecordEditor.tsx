/**
 * Editing a generated record (design document section 104).
 *
 *     For small datasets, allow manual editing.
 *
 *     For enormous datasets, editing individual rows is inappropriate. Instead
 *     support: regeneration, transformations, filtering, patch rules.
 *
 * So this does not save a row. It **writes a rule**, and shows you the rule
 * before you take it.
 *
 * The reason is the property the whole platform rests on: a Cacophony dataset is
 * a pure function of its schema and its seed. Editing a value in a preview and
 * writing it somewhere would produce a row that corresponds to nothing — the
 * next `generate` overwrites it, no other machine reproduces it, and the dataset
 * has quietly stopped being reproducible. A `patches:` rule in the project has
 * neither problem: it travels with the schema, applies on every run, and
 * `cacophony regenerate` produces the edited value a year later.
 *
 * What you get, therefore, is a diff and a block of YAML — never a "saved"
 * message about a file that does not exist.
 */

import { type ReactNode, useMemo, useState } from "react";

import type { EntityView } from "../api/types";
import { Notice } from "../components/ui";
import { renderCell } from "../components/ui";

/** The operations `patches:` understands, as `cacophony transform` lists them. */
const OPERATIONS = [
  "mask:4",
  "hash:16",
  "nullify",
  "lowercase",
  "uppercase",
  "truncate:40",
  "normalize",
  "round:2",
  "add_noise:5",
  "format_date:%Y-%m",
  "encode:base64",
];

export function RecordEditor({
  entity,
  record,
  onClose,
}: {
  entity: EntityView | undefined;
  record: Record<string, unknown>;
  onClose: () => void;
}): ReactNode {
  const columns = useMemo(
    () => Object.keys(record).filter((name) => !name.startsWith("_")),
    [record],
  );

  const [field, setField] = useState<string>(columns[0] ?? "");
  const [operation, setOperation] = useState<string>(OPERATIONS[0] as string);
  const [condition, setCondition] = useState<string>("");
  const [name, setName] = useState<string>("");

  const before = record[field];
  const spec = entity?.fields[field];
  const ruleName = name.trim() || `edit_${field || "field"}`;

  const yaml = useMemo(
    () =>
      [
        "patches:",
        `  ${ruleName}:`,
        ...(entity ? [`    entity: ${entity.name}`] : []),
        ...(condition.trim() ? [`    where: "${condition.trim().replace(/"/g, '\\"')}"`] : []),
        "    set:",
        `      ${field}: ${operation}`,
      ].join("\n"),
    [ruleName, entity, condition, field, operation],
  );

  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <div className="row spread" style={{ marginBottom: 10 }}>
        <div className="panel-title" style={{ margin: 0 }}>
          Edit this record
        </div>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </div>

      <Notice>
        Cacophony does not save edited rows. A dataset is a function of its schema
        and its seed, and a row edited outside the schema corresponds to nothing —
        the next run overwrites it. This builds a <strong>patch rule</strong>
        instead, which applies on every run and survives a regeneration.
      </Notice>

      <div className="row" style={{ gap: 10, marginTop: 12 }}>
        <div className="field-row" style={{ flex: 1 }}>
          <label htmlFor="patch-field">Field</label>
          <select
            id="patch-field"
            value={field}
            onChange={(event) => setField(event.target.value)}
          >
            {columns.map((column) => (
              <option key={column} value={column}>
                {column}
              </option>
            ))}
          </select>
          {spec && <div className="hint">{spec.generator_describe}</div>}
        </div>

        <div className="field-row" style={{ flex: 1 }}>
          <label htmlFor="patch-operation">Change it to</label>
          <input
            id="patch-operation"
            list="patch-operations"
            value={operation}
            onChange={(event) => setOperation(event.target.value)}
          />
          <datalist id="patch-operations">
            {OPERATIONS.map((item) => (
              <option key={item} value={item} />
            ))}
          </datalist>
          <div className="hint">
            An operation like <code>mask:4</code>, several joined by{" "}
            <code>|</code>, or an expression over the record.
          </div>
        </div>
      </div>

      <div className="field-row">
        <label htmlFor="patch-where">Only records where</label>
        <input
          id="patch-where"
          placeholder={`e.g. ${columns[1] ?? "department"} == "Finance"`}
          value={condition}
          onChange={(event) => setCondition(event.target.value)}
        />
        <div className="hint">Leave empty to apply to every record.</div>
      </div>

      <div className="field-row">
        <label htmlFor="patch-name">Rule name</label>
        <input
          id="patch-name"
          placeholder={ruleName}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </div>

      <div className="panel-title" style={{ marginTop: 14 }}>
        This record, before
      </div>
      <div className="mono" style={{ fontSize: "0.82rem" }}>
        {field} = {renderCell(before)}
      </div>

      <div className="panel-title" style={{ marginTop: 14 }}>
        Add this to the project
      </div>
      <pre
        className="mono"
        style={{
          background: "var(--surface-2)",
          padding: 10,
          borderRadius: 4,
          overflowX: "auto",
          fontSize: "0.8rem",
        }}
      >
        {yaml}
      </pre>
      <div className="row" style={{ gap: 8 }}>
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard?.writeText(yaml);
          }}
        >
          Copy
        </button>
        <span className="faint" style={{ fontSize: "0.78rem" }}>
          Paste it into the project&rsquo;s <code>patches:</code> block, or apply it to an
          existing file with <code>cacophony transform --project</code>.
        </span>
      </div>
    </div>
  );
}
