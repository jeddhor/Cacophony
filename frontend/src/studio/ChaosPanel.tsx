/**
 * The Chaos Panel (design document section 78).
 *
 * Section 78 asks for this by name: a set of rates, and presets that set them
 * all at once. The rates and the presets have existed in the engine since the
 * simulation phase — what did not exist was anywhere to see them, so a project
 * either had the `chaos:` block somebody typed or had none.
 *
 * Everything here writes YAML. The panel is a view of the schema, not a second
 * place settings live: choosing a preset sends the preset's name, moving a
 * slider sends that one rate, and both are visible in the file afterwards.
 */

import { type ReactNode, useEffect, useState } from "react";

import type { SchemaOperation } from "../api/types";
import { Notice, Panel } from "../components/ui";

/** Section 78's controls, in the order the section lists them. */
const CONTROLS: { key: string; label: string; hint: string }[] = [
  { key: "outliers", label: "Outliers", hint: "Values far outside the declared range" },
  { key: "missing_data", label: "Missing data", hint: "A field that should be there and is not" },
  { key: "duplicates", label: "Duplicates", hint: "The same record twice, as a retry would" },
  { key: "malformed_text", label: "Malformed text", hint: "Truncation, doubled spaces, stray quotes" },
  { key: "unexpected_unicode", label: "Unexpected Unicode", hint: "Emoji, right-to-left marks, zero-width joins" },
  { key: "temporal_anomalies", label: "Temporal anomalies", hint: "A timestamp that cannot have happened" },
  { key: "referential_anomalies", label: "Referential anomalies", hint: "A foreign key pointing at nothing" },
];

/** What each preset is for, in one line. The rates themselves are the engine's. */
const PRESETS: { name: string; blurb: string }[] = [
  { name: "pristine", blurb: "Nothing damaged. The default." },
  { name: "realistic", blurb: "What ordinary production data looks like." },
  { name: "messy", blurb: "A system with known data-quality problems." },
  { name: "hostile_qa", blurb: "Deliberately awkward, for testing the pipeline." },
  { name: "absolute", blurb: "Absolute cacophony. Very little survives intact." },
];

interface ChaosBlock {
  preset?: string | null;
  [rate: string]: unknown;
}

export function ChaosPanel({
  chaos,
  editable,
  onPatch,
  pending,
}: {
  chaos: ChaosBlock;
  editable: boolean;
  onPatch: (operations: SchemaOperation[]) => void;
  pending: boolean;
}): ReactNode {
  const preset = typeof chaos.preset === "string" ? chaos.preset : "";
  const rateOf = (key: string): number => {
    const value = chaos[key];
    return typeof value === "number" ? value : 0;
  };

  const set = (key: string, value: unknown) => onPatch([{ op: "set_chaos", key, value }]);

  const damaged = CONTROLS.reduce((total, control) => total + rateOf(control.key), 0);

  return (
    <Panel title="Chaos">
      <p className="hint" style={{ marginTop: 0 }}>
        Deliberate damage, as a fraction of records. Every defect is recorded in the
        record's provenance, so a pipeline test can tell this from its own bugs.
      </p>

      {!editable && (
        <Notice tone="warn">
          This project has no file to write to, so the rates are read-only.
        </Notice>
      )}

      <div className="field-row">
        <label>Preset</label>
        <div className="row" style={{ gap: 6 }}>
          {PRESETS.map((entry) => (
            <button
              key={entry.name}
              type="button"
              className={`button-sm ${preset === entry.name ? "button-primary" : ""}`}
              title={entry.blurb}
              disabled={!editable || pending}
              aria-pressed={preset === entry.name}
              onClick={() => set("preset", preset === entry.name ? null : entry.name)}
            >
              {entry.name.replace("_", " ")}
            </button>
          ))}
        </div>
        <div className="hint">
          {PRESETS.find((entry) => entry.name === preset)?.blurb ??
            "No preset. Each rate below is whatever the schema says."}
        </div>
      </div>

      {CONTROLS.map((control) => (
        <Rate
          key={control.key}
          label={control.label}
          hint={control.hint}
          value={rateOf(control.key)}
          editable={editable && !pending}
          onCommit={(value) => set(control.key, value > 0 ? value : null)}
        />
      ))}

      <p className="hint" style={{ marginBottom: 0 }}>
        {damaged > 0
          ? `About ${(Math.min(1, damaged) * 100).toFixed(1)}% of records will carry something deliberate.`
          : "Nothing is damaged: every record will be exactly what the schema describes."}
      </p>
    </Panel>
  );
}

/**
 * One rate, as a slider and a number.
 *
 * The slider is for finding a value and the number is for saying one — 0.5% is
 * not a thing a slider can be dropped on, and it is a rate people actually
 * want. Both commit on release rather than on every movement, because each
 * commit is a file write and a recompile.
 */
function Rate({
  label,
  hint,
  value,
  editable,
  onCommit,
}: {
  label: string;
  hint: string;
  value: number;
  editable: boolean;
  onCommit: (value: number) => void;
}): ReactNode {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  const percent = (draft * 100).toFixed(draft > 0 && draft < 0.01 ? 2 : 1);

  return (
    <div className="field-row">
      <label htmlFor={`chaos-${label}`}>
        {label}
        <span className="faint nums" style={{ float: "right" }}>
          {percent}%
        </span>
      </label>
      <input
        id={`chaos-${label}`}
        type="range"
        min={0}
        max={0.5}
        step={0.005}
        value={draft}
        disabled={!editable}
        aria-label={`${label} rate`}
        onChange={(event) => setDraft(Number(event.target.value))}
        onMouseUp={() => draft !== value && onCommit(draft)}
        onKeyUp={() => draft !== value && onCommit(draft)}
        onTouchEnd={() => draft !== value && onCommit(draft)}
      />
      <div className="hint">{hint}</div>
    </div>
  );
}
