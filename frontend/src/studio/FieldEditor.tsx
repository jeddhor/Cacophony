/**
 * The field editor (design document section 49).
 *
 * Section 49's panel, in its order: name, type, meaning, generation, context,
 * length, tone, null probability, and a button that generates samples. What is
 * shown adapts to the field, because a length constraint on a boolean and a
 * temperature on a sequence are noise.
 *
 * Edits are sent as targeted schema operations, so the YAML file keeps its
 * comments and its ordering. Each control saves on blur rather than on every
 * keystroke: a patch is a file write plus a recompile, and doing that per
 * character would be both slow and unreadable in a Git history.
 */

import { type ReactNode, useEffect, useId, useState } from "react";

import { useSchemaTypes } from "../api/hooks";
import type { EntityView, FieldView, SchemaOperation } from "../api/types";
import {
  DistributionBars,
  GeneratorBadge,
  Notice,
  Panel,
} from "../components/ui";

interface Props {
  entity: EntityView;
  field: FieldView;
  editable: boolean;
  onPatch: (operations: SchemaOperation[]) => void;
  onPreview: () => void;
  pending: boolean;
}

/** Options that mean something for a given generator. */
const GENERATOR_OPTIONS: Record<string, string[]> = {
  sequence: ["format", "start", "step", "pad"],
  pattern: ["pattern"],
  template: ["template"],
  expression: ["expression"],
  weighted: ["choices"],
  lookup: ["values", "path", "column", "mode"],
  distribution: ["distribution", "mean", "stddev", "min", "max", "lam", "alpha", "beta"],
  random: ["min", "max", "length", "charset", "precision"],
  boolean: ["probability"],
  datetime: ["start", "end", "business_hours", "weekdays_only"],
  faker: ["provider", "locale", "safe"],
  ip: ["network", "version", "safe"],
  mac: ["oui", "separator", "safe"],
  phone: ["format", "area_code", "safe"],
  uuid: ["version"],
  constant: ["value"],
  llm: ["provider", "mode", "max_tokens", "temperature", "on_unavailable"],
  image: ["provider", "width", "height", "workflow", "steps", "on_unavailable"],
  tts: ["provider", "voice", "source", "on_unavailable"],
  reference: ["entity", "field", "on_unavailable"],
};

export function FieldEditor({
  entity,
  field,
  editable,
  onPatch,
  onPreview,
  pending,
}: Props): ReactNode {
  const types = useSchemaTypes();

  const set = (key: string, value: unknown) =>
    onPatch([{ op: "set_field", entity: entity.name, field: field.name, key, value }]);

  const isNumeric = ["integer", "float", "decimal"].includes(field.type);
  const isTextual = ["string", "text", "email", "hostname", "uri", "phone", "enum"].includes(
    field.type,
  );

  return (
    <Panel
      title="Field"
      actions={
        <button type="button" className="button-sm" onClick={onPreview} disabled={pending}>
          Generate samples
        </button>
      }
    >
      {!editable && (
        <Notice tone="warn">
          This project has no file to write to, so the schema is read-only.
        </Notice>
      )}

      <TextRow
        label="Name"
        value={field.name}
        readOnly={!editable}
        onCommit={(value) =>
          value !== field.name &&
          onPatch([
            { op: "rename_field", entity: entity.name, field: field.name, name: value },
          ])
        }
      />

      <div className="field-row">
        <label htmlFor="field-type">Type</label>
        <select
          id="field-type"
          value={field.type}
          disabled={!editable}
          onChange={(event) => set("type", event.target.value)}
        >
          {(types.data?.types ?? [{ value: field.type }]).map((entry) => (
            <option key={entry.value} value={entry.value}>
              {entry.value}
            </option>
          ))}
        </select>
      </div>

      {/* Section 9's semantic annotation: the field that drives everything. */}
      <div className="field-row">
        <label htmlFor="field-semantic">Meaning</label>
        <textarea
          id="field-semantic"
          rows={3}
          defaultValue={field.semantic ?? ""}
          readOnly={!editable}
          key={`${entity.name}.${field.name}.semantic`}
          onBlur={(event) => {
            const value = event.target.value.trim();
            if (value !== (field.semantic ?? "")) set("semantic", value || null);
          }}
        />
        <div className="hint">
          What the field <em>means</em>. The prompt compiler and the
          recommendation engine both read this.
        </div>
      </div>

      <div className="field-row">
        <label htmlFor="field-generator">Generation</label>
        <div className="row" style={{ gap: 8 }}>
          <select
            id="field-generator"
            value={field.generator}
            disabled={!editable}
            onChange={(event) => set("generator", event.target.value)}
          >
            {(types.data?.generators ?? []).map((generator) => (
              <option key={generator.name} value={generator.name}>
                {generator.name}
              </option>
            ))}
          </select>
          <GeneratorBadge generator={field.generator} title={field.generator_describe} />
        </div>
        {field.inferred && (
          <div className="hint">
            Inferred by the recommendation engine (section 68) because the field
            names no generator. Choosing one here makes it explicit.
          </div>
        )}
        {field.recipe && (
          <div className="hint">
            Came from the <strong>{field.recipe}</strong> recipe (section 80).
            Editing it here overrides the recipe for this project without
            forking it; the other fields the recipe contributed are unaffected.
          </div>
        )}
      </div>

      <GeneratorOptions
        entity={entity}
        field={field}
        editable={editable}
        onPatch={onPatch}
      />

      {field.dependencies.length > 0 && (
        <div className="field-row">
          <span className="visually-hidden" id="context-label">
            Fields this one reads
          </span>
          <label aria-hidden="true">Context</label>
          <div className="row" style={{ gap: 6 }} aria-labelledby="context-label">
            {field.dependencies.map((name) => (
              <span key={name} className="badge badge-derived">
                {name}
              </span>
            ))}
          </div>
          <div className="hint">
            Generated before this field, and visible to it. The compiler derives
            the order from these.
          </div>
        </div>
      )}

      {isTextual && (
        <div className="row" style={{ gap: 10 }}>
          <NumberRow
            label="Min length"
            value={field.constraints.min_length}
            readOnly={!editable}
            onCommit={(value) => set("constraints", { ...field.constraints, min_length: value })}
          />
          <NumberRow
            label="Max length"
            value={field.constraints.max_length}
            readOnly={!editable}
            onCommit={(value) => set("constraints", { ...field.constraints, max_length: value })}
          />
        </div>
      )}

      {isNumeric && (
        <div className="row" style={{ gap: 10 }}>
          <NumberRow
            label="Minimum"
            value={field.constraints.min as number | undefined}
            readOnly={!editable}
            onCommit={(value) => set("constraints", { ...field.constraints, min: value })}
          />
          <NumberRow
            label="Maximum"
            value={field.constraints.max as number | undefined}
            readOnly={!editable}
            onCommit={(value) => set("constraints", { ...field.constraints, max: value })}
          />
        </div>
      )}

      {field.requires_provider === "language_model" && (
        <TextRow
          label="Tone"
          value={field.tone ?? ""}
          readOnly={!editable}
          onCommit={(value) => set("tone", value || null)}
        />
      )}

      <NumberRow
        label="Null probability"
        value={field.null_probability}
        step={0.01}
        readOnly={!editable}
        onCommit={(value) => set("null_probability", value)}
      />

      <label className="checkbox">
        <input
          type="checkbox"
          checked={field.unique}
          disabled={!editable}
          onChange={(event) => set("unique", event.target.checked || null)}
        />
        Unique
      </label>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={field.primary_key}
          disabled={!editable}
          onChange={(event) => set("primary_key", event.target.checked || null)}
        />
        Primary key
      </label>

      {/* Section 52: show the distribution, and let it be read at a glance. */}
      {field.distribution && (
        <div style={{ marginTop: 16 }}>
          <div className="panel-title">Distribution</div>
          <DistributionBars distribution={field.distribution} />
        </div>
      )}

      {editable && (
        <div style={{ marginTop: 16 }}>
          <button
            type="button"
            className="button-sm button-danger"
            onClick={() => {
              if (window.confirm(`Remove ${entity.name}.${field.name}?`)) {
                onPatch([{ op: "remove_field", entity: entity.name, name: field.name }]);
              }
            }}
          >
            Remove field
          </button>
        </div>
      )}
    </Panel>
  );
}

/** The options that belong to whichever generator this field uses. */
function GeneratorOptions({
  entity,
  field,
  editable,
  onPatch,
}: {
  entity: EntityView;
  field: FieldView;
  editable: boolean;
  onPatch: (operations: SchemaOperation[]) => void;
}): ReactNode {
  const known = GENERATOR_OPTIONS[field.generator] ?? [];
  const options = field.generator_options ?? {};
  // Show the generator's own options plus anything already set on the field,
  // so an option this build does not know about is still visible and editable.
  const names = [...new Set([...known, ...Object.keys(options)])].filter(
    (name) => name !== "locale",
  );

  if (names.length === 0) return null;

  return (
    <div className="field-row">
      <label aria-hidden="true">Generator options</label>
      {names.map((name) => {
        const value = options[name];
        const complex = value !== null && typeof value === "object";
        const inputId = `option-${entity.name}-${field.name}-${name}`;
        return (
          <div key={name} style={{ marginBottom: 6 }}>
            <label htmlFor={inputId} className="faint" style={{ fontSize: "0.72rem" }}>
              {name}
            </label>
            {complex ? (
              <code className="faint" style={{ fontSize: "0.72rem" }}>
                {JSON.stringify(value)}
              </code>
            ) : (
              <input
                id={inputId}
                defaultValue={value === undefined || value === null ? "" : String(value)}
                readOnly={!editable}
                key={`${entity.name}.${field.name}.${name}`}
                onBlur={(event) => {
                  const raw = event.target.value.trim();
                  const current = value === undefined || value === null ? "" : String(value);
                  if (raw === current) return;
                  onPatch([
                    {
                      op: "set_field",
                      entity: entity.name,
                      field: field.name,
                      key: name,
                      value: raw === "" ? null : coerce(raw),
                    },
                  ]);
                }}
              />
            )}
          </div>
        );
      })}
      <div className="hint">
        Complex options - choices, bins, lists - are shown read-only here; edit
        them in the source view.
      </div>
    </div>
  );
}

/** Turn a typed string back into the value a schema would hold. */
function coerce(raw: string): unknown {
  if (raw === "true") return true;
  if (raw === "false") return false;
  if (/^-?\d+$/.test(raw)) return Number.parseInt(raw, 10);
  if (/^-?\d*\.\d+$/.test(raw)) return Number.parseFloat(raw);
  return raw;
}

function TextRow({
  label,
  value,
  readOnly,
  onCommit,
}: {
  label: string;
  value: string;
  readOnly: boolean;
  onCommit: (value: string) => void;
}): ReactNode {
  const id = useId();
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  return (
    <div className="field-row">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        value={draft}
        readOnly={readOnly}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => draft !== value && onCommit(draft.trim())}
        onKeyDown={(event) => {
          if (event.key === "Enter") event.currentTarget.blur();
          if (event.key === "Escape") setDraft(value);
        }}
      />
    </div>
  );
}

function NumberRow({
  label,
  value,
  step,
  readOnly,
  onCommit,
}: {
  label: string;
  value: number | undefined;
  step?: number;
  readOnly: boolean;
  onCommit: (value: number | null) => void;
}): ReactNode {
  const id = useId();
  const shown = value === undefined || value === null ? "" : String(value);
  return (
    <div className="field-row" style={{ flex: 1 }}>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type="number"
        step={step}
        defaultValue={shown}
        readOnly={readOnly}
        key={`${label}-${shown}`}
        onBlur={(event) => {
          const raw = event.target.value.trim();
          if (raw === shown) return;
          onCommit(raw === "" ? null : Number(raw));
        }}
      />
    </div>
  );
}
