/**
 * Shared presentation pieces (design document sections 45, 51, 52, 55).
 *
 * The one that carries real meaning is `GeneratorBadge`. Section 51 says the
 * preview should identify each column's generation source, and section 45 asks
 * the interface to say who is speaking. So a generator's family has one colour
 * and one label everywhere it appears - the preview table, the studio, the
 * plan, the graph - and the label is a word, never only a colour.
 */

import type { ReactNode } from "react";

import type { JobState, LintIssue, RunState } from "../api/types";

/* ------------------------------------------------------------------ */
/* Generator identity                                                  */
/* ------------------------------------------------------------------ */

type Family = "rule" | "faker" | "llm" | "media" | "derived" | "reference";

const FAMILY: Record<string, Family> = {
  faker: "faker",
  llm: "llm",
  image: "media",
  tts: "media",
  expression: "derived",
  template: "derived",
  transform: "derived",
  composite: "derived",
  reference: "reference",
};

const LABEL: Record<string, string> = {
  faker: "FAKER",
  llm: "LLM",
  image: "IMAGE",
  tts: "TTS",
  expression: "EXPR",
  template: "TMPL",
  sequence: "SEQ",
  constant: "CONST",
  weighted: "WEIGHT",
  distribution: "DIST",
  lookup: "LOOKUP",
  pattern: "PATTERN",
  datetime: "TIME",
  uuid: "UUID",
  reference: "REF",
  composite: "COMP",
  transform: "XFORM",
  boolean: "BOOL",
  random: "RANDOM",
  phone: "PHONE",
  government_id: "GOV ID",
  ip: "IP",
  mac: "MAC",
  null: "NULL",
};

export const generatorFamily = (name: string): Family => FAMILY[name] ?? "rule";
export const generatorLabel = (name: string): string =>
  LABEL[name] ?? name.replace(/_/g, " ").toUpperCase().slice(0, 8);

export function GeneratorBadge({
  generator,
  title,
}: {
  generator: string;
  title?: string;
}): ReactNode {
  return (
    <span
      className={`badge badge-${generatorFamily(generator)}`}
      title={title ?? generator}
    >
      {generatorLabel(generator)}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* States                                                              */
/* ------------------------------------------------------------------ */

export function StateChip({ state }: { state: RunState | JobState | string }): ReactNode {
  return (
    <span className={`state state-${state}`}>
      {state}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Structure                                                           */
/* ------------------------------------------------------------------ */

export function Panel({
  title,
  actions,
  children,
  className = "",
}: {
  title?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}): ReactNode {
  return (
    <section className={`panel ${className}`}>
      {(title || actions) && (
        <div className="row spread" style={{ marginBottom: 12 }}>
          {title && <div className="panel-title" style={{ margin: 0 }}>{title}</div>}
          {actions && <div className="row">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  tone?: "violet" | "cyan" | "magenta" | "green" | "amber";
}): ReactNode {
  return (
    <div className="panel">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={tone ? { color: `var(--${tone})` } : undefined}>
        {value}
      </div>
      {note && <div className="stat-note">{note}</div>}
    </div>
  );
}

export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}): ReactNode {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }): ReactNode {
  return (
    <span className="row" role="status">
      <span className="spinner" aria-hidden="true" />
      <span className="faint">{label}</span>
    </span>
  );
}

export function Notice({
  tone = "info",
  children,
}: {
  tone?: "info" | "warn" | "error";
  children: ReactNode;
}): ReactNode {
  return (
    <div className={`notice ${tone === "info" ? "" : `notice-${tone}`}`} role={tone === "error" ? "alert" : undefined}>
      {children}
    </div>
  );
}

export function ErrorNotice({ error }: { error: unknown }): ReactNode {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  return <Notice tone="error">{message}</Notice>;
}

/* ------------------------------------------------------------------ */
/* Bars (sections 52, 55)                                              */
/* ------------------------------------------------------------------ */

export function ProgressBar({
  value,
  state,
}: {
  value: number;
  state?: RunState | JobState | string;
}): ReactNode {
  const percent = Math.max(0, Math.min(1, value)) * 100;
  const modifier = state === "completed" ? "done" : state === "failed" ? "failed" : "";
  return (
    <div
      className="bar"
      role="progressbar"
      aria-valuenow={Math.round(percent)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div className={`bar-fill ${modifier}`} style={{ width: `${percent}%` }} />
    </div>
  );
}

/**
 * A weighted-choice distribution (design document section 52).
 *
 * Section 52 draws these as bars with percentages, so that is what this is:
 * the shape carries the comparison and the number carries the precision.
 */
export function DistributionBars({
  distribution,
  limit = 12,
}: {
  distribution: Record<string, number>;
  limit?: number;
}): ReactNode {
  const entries = Object.entries(distribution).sort((a, b) => b[1] - a[1]);
  const shown = entries.slice(0, limit);
  const hidden = entries.length - shown.length;
  const largest = shown.length > 0 ? (shown[0]?.[1] ?? 1) : 1;

  return (
    <div>
      {shown.map(([name, share]) => (
        <div className="dist-row" key={name}>
          <span className="dist-label" title={name}>
            {name}
          </span>
          <span className="dist-track">
            <span
              className="dist-fill"
              style={{ width: `${largest > 0 ? (share / largest) * 100 : 0}%` }}
            />
          </span>
          <span className="dist-value">{(share * 100).toFixed(1)}%</span>
        </div>
      ))}
      {hidden > 0 && <div className="faint" style={{ fontSize: "0.75rem" }}>+{hidden} more</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Lint (section 102)                                                  */
/* ------------------------------------------------------------------ */

export function LintList({ issues }: { issues: LintIssue[] }): ReactNode {
  if (issues.length === 0) {
    return <p className="faint">No issues found.</p>;
  }
  return (
    <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
      {issues.map((issue, index) => (
        <li key={`${issue.code}-${issue.location}-${index}`} style={{ marginBottom: 10 }}>
          <div className="row" style={{ gap: 8 }}>
            <span
              className="badge"
              style={{
                color:
                  issue.severity === "error"
                    ? "var(--red)"
                    : issue.severity === "warning"
                      ? "var(--amber)"
                      : "var(--cyan)",
              }}
            >
              {issue.severity}
            </span>
            <code className="faint">{issue.location}</code>
          </div>
          <div style={{ marginTop: 2 }}>{issue.message}</div>
          {issue.hint && <div className="faint" style={{ fontSize: "0.76rem" }}>{issue.hint}</div>}
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------------ */
/* Formatting                                                          */
/* ------------------------------------------------------------------ */

export const formatNumber = (value: number | null | undefined): string =>
  value === null || value === undefined ? "-" : value.toLocaleString();

export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 1) return `${(seconds * 1000).toFixed(0)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${rest}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "-";
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? "-" : when.toLocaleString();
}

/** Render any generated value compactly for a table cell. */
export function renderCell(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
