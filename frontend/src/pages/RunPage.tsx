/**
 * Live run visualisation and the run inspector (design document sections 55, 56).
 *
 * Section 55: "Generation should look satisfying." Per-entity counters,
 * throughput, records per second, tokens per second — and section 56's
 * after-the-fact view: duration, records, errors, retries, validation
 * failures, output size.
 *
 * They are one page rather than two, because they are one thing seen at
 * different moments, and a run that finishes while you are watching it should
 * not throw the page away and give you a different one.
 */

import { type ReactNode, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, ApiError } from "../api/client";
import { liveOrStored, useRun, useRunControl, useRunStream } from "../api/hooks";
import type { JobView, RunView, StoredEvent } from "../api/types";
import { PageHead } from "../components/Layout";
import {
  Empty,
  ErrorNotice,
  Notice,
  Panel,
  ProgressBar,
  Spinner,
  StateChip,
  Stat,
  formatBytes,
  formatDuration,
  formatNumber,
  formatWhen,
} from "../components/ui";
import { useQuery } from "@tanstack/react-query";

export function RunPage(): ReactNode {
  const { runId } = useParams<{ runId: string }>();
  const run = useRun(runId ?? null);
  const live = useRunStream(runId ?? null, run.data ? isActive(run.data) : true);
  const control = useRunControl(runId ?? "");

  if (run.isLoading) return <Spinner label="Loading run" />;
  if (run.isError) {
    const error = run.error;
    if (error instanceof ApiError && error.status === 404) {
      return (
        <Empty title="No such run">
          <p>
            It may have been pruned. <Link to="/runs">Back to runs</Link>.
          </p>
        </Empty>
      );
    }
    return <ErrorNotice error={error} />;
  }
  if (!run.data || !runId) return null;

  const snapshot = liveOrStored(run.data, live);
  const active = isActive(run.data);
  const entities = snapshot?.entities ?? {};

  return (
    <>
      <PageHead
        title={
          <>
            Run <span className="mono">{runId.slice(0, 8)}</span>
          </> as unknown as string
        }
        subtitle={
          <span className="row" style={{ gap: 12 }}>
            <StateChip state={run.data.state} />
            <span>seed {run.data.seed}</span>
            <span>
              {run.data.output_format} → {run.data.output_dir}
            </span>
            {live.status === "open" && <span style={{ color: "var(--cyan)" }}>live</span>}
          </span>
        }
        actions={
          active ? (
            <>
              {run.data.paused ? (
                <button
                  type="button"
                  onClick={() => control.resume.mutate()}
                  disabled={control.resume.isPending}
                >
                  Resume
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => control.pause.mutate()}
                  disabled={control.pause.isPending}
                >
                  Pause
                </button>
              )}
              <button
                type="button"
                className="button-danger"
                onClick={() => control.cancel.mutate()}
                disabled={control.cancel.isPending}
              >
                Cancel
              </button>
            </>
          ) : run.data.state !== "completed" ? (
            <button
              type="button"
              className="button-primary"
              onClick={() => control.resume.mutate()}
              disabled={control.resume.isPending}
            >
              {control.resume.isPending ? "Resuming…" : "Resume from checkpoint"}
            </button>
          ) : undefined
        }
      />

      {run.data.error && <Notice tone="error">{run.data.error}</Notice>}
      {active && live.status === "unavailable" && (
        <Notice tone="warn">
          This run is executing in another process, so there is no live feed.
          Progress below is polled from its checkpoints.
        </Notice>
      )}
      <ErrorNotice error={control.pause.error ?? control.resume.error ?? control.cancel.error} />

      {/* Section 55's headline figures. */}
      <div className="grid grid-4">
        <Stat
          label="Records"
          value={formatNumber(snapshot?.records_written ?? run.data.records_written)}
          note={`of ${formatNumber(run.data.records_requested)}`}
          tone="violet"
        />
        <Stat
          label="Records / sec"
          value={formatNumber(Math.round(snapshot?.records_per_second ?? 0))}
          note={
            snapshot?.mean_records_per_second
              ? `${formatNumber(Math.round(snapshot.mean_records_per_second))} mean`
              : undefined
          }
          tone="cyan"
        />
        <Stat
          label="Elapsed"
          value={formatDuration(snapshot?.elapsed_seconds ?? run.data.duration_seconds)}
          note={
            active && snapshot?.eta_seconds
              ? `~${formatDuration(snapshot.eta_seconds)} remaining`
              : undefined
          }
        />
        <Stat
          label={snapshot && snapshot.provider_calls > 0 ? "Model calls" : "Validation failures"}
          value={formatNumber(
            snapshot && snapshot.provider_calls > 0
              ? snapshot.provider_calls
              : (snapshot?.validation_failures ?? 0),
          )}
          note={
            snapshot && snapshot.provider_calls > 0
              ? `${formatNumber(Math.round(snapshot.tokens_per_second))} tokens/sec`
              : undefined
          }
          tone={
            snapshot && snapshot.provider_calls > 0
              ? "magenta"
              : (snapshot?.validation_failures ?? 0) > 0
                ? "amber"
                : "green"
          }
        />
      </div>

      <div style={{ height: 16 }} />

      <Panel title="Progress">
        <div style={{ marginBottom: 14 }}>
          <div className="row spread" style={{ marginBottom: 5 }}>
            <strong>Overall</strong>
            <span className="nums faint">
              {((snapshot?.progress ?? run.data.progress) * 100).toFixed(1)}%
            </span>
          </div>
          <ProgressBar value={snapshot?.progress ?? run.data.progress} state={run.data.state} />
        </div>

        {/* Section 55's per-entity counters. */}
        {Object.values(entities).map((entity) => (
          <div key={entity.entity} style={{ marginBottom: 10 }}>
            <div className="row spread" style={{ marginBottom: 4, fontSize: "0.82rem" }}>
              <span>{entity.entity}</span>
              <span className="nums faint">
                {formatNumber(entity.written)} / {formatNumber(entity.requested)}
                {entity.records_per_second > 0 && (
                  <> · {formatNumber(Math.round(entity.records_per_second))}/s</>
                )}
              </span>
            </div>
            <ProgressBar value={entity.progress} />
          </div>
        ))}

        {Object.keys(entities).length === 0 && (
          <JobProgress jobs={run.data.jobs ?? []} />
        )}
      </Panel>

      <div style={{ height: 16 }} />

      <div className="grid grid-2">
        <JobsPanel run={run.data} />
        <InspectorPanel run={run.data} snapshot={snapshot} />
      </div>

      <div style={{ height: 16 }} />

      <EventLog runId={runId} live={live.events} active={active} />
    </>
  );
}

function isActive(run: RunView): boolean {
  return run.state === "running" || run.state === "paused" || run.state === "queued";
}

function JobProgress({ jobs }: { jobs: JobView[] }): ReactNode {
  return (
    <>
      {jobs.map((job) => (
        <div key={job.id} style={{ marginBottom: 10 }}>
          <div className="row spread" style={{ marginBottom: 4, fontSize: "0.82rem" }}>
            <span>{job.entity ?? job.type}</span>
            <span className="nums faint">
              {formatNumber(job.completed)} / {formatNumber(job.requested)}
            </span>
          </div>
          <ProgressBar value={job.progress} state={job.state} />
        </div>
      ))}
    </>
  );
}

/** Section 29's jobs, with the checkpoint that makes them resumable. */
function JobsPanel({ run }: { run: RunView }): ReactNode {
  return (
    <Panel title="Jobs">
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Entity</th>
              <th>State</th>
              <th style={{ textAlign: "right" }}>Done</th>
              <th style={{ textAlign: "right" }}>Requested</th>
              <th style={{ textAlign: "right" }}>Part</th>
            </tr>
          </thead>
          <tbody>
            {(run.jobs ?? []).map((job) => (
              <tr key={job.id}>
                <td>{job.entity ?? job.type}</td>
                <td>
                  <StateChip state={job.state} />
                </td>
                <td className="nums" style={{ textAlign: "right" }}>
                  {formatNumber(job.completed)}
                </td>
                <td className="nums" style={{ textAlign: "right" }}>
                  {formatNumber(job.requested)}
                </td>
                <td className="nums faint" style={{ textAlign: "right" }}>
                  {job.part}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(run.jobs ?? []).some((job) => job.error) && (
        <div style={{ marginTop: 10 }}>
          {(run.jobs ?? [])
            .filter((job) => job.error)
            .map((job) => (
              <Notice tone="error" key={job.id}>
                {job.entity}: {job.error}
              </Notice>
            ))}
        </div>
      )}
    </Panel>
  );
}

/** Section 56's inspector, plus section 58's quality scores. */
function InspectorPanel({
  run,
  snapshot,
}: {
  run: RunView;
  snapshot: ReturnType<typeof liveOrStored>;
}): ReactNode {
  const quality = run.statistics?.filter((stat) => stat.scope === "quality") ?? [];
  const files = (run.summary as { files?: string[] })?.files ?? [];

  return (
    <Panel title="Inspector">
      <table>
        <tbody>
          <Row label="Started" value={formatWhen(run.started_at)} />
          <Row label="Finished" value={formatWhen(run.finished_at)} />
          <Row label="Duration" value={formatDuration(run.duration_seconds)} />
          <Row label="Schema revision" value={run.revision_id ?? "—"} />
          <Row label="Output size" value={formatBytes(snapshot?.bytes_written)} />
          <Row label="Validation failures" value={formatNumber(snapshot?.validation_failures ?? 0)} />
          <Row label="Retries" value={formatNumber(snapshot?.retries ?? 0)} />
          <Row label="Provider errors" value={formatNumber(snapshot?.provider_errors ?? 0)} />
          {snapshot && (snapshot.cache_hits > 0 || snapshot.cache_misses > 0) && (
            <Row
              label="Cache"
              value={`${formatNumber(snapshot.cache_hits)} hits / ${formatNumber(snapshot.cache_misses)} misses`}
            />
          )}
        </tbody>
      </table>

      {quality.length > 0 && (
        <>
          <div className="panel-title" style={{ marginTop: 16 }}>
            Quality
          </div>
          {quality.map((stat) => (
            <div className="dist-row" key={stat.name}>
              <span className="dist-label">{stat.name.replace(/_/g, " ")}</span>
              <span className="dist-track">
                <span
                  className="dist-fill"
                  style={{
                    width: `${Math.min(1, stat.value ?? 0) * 100}%`,
                    background:
                      (stat.value ?? 0) >= 0.99
                        ? "var(--green)"
                        : "linear-gradient(90deg, var(--amber), var(--violet))",
                  }}
                />
              </span>
              <span className="dist-value">{((stat.value ?? 0) * 100).toFixed(2)}%</span>
            </div>
          ))}
        </>
      )}

      {files.length > 0 && (
        <>
          <div className="panel-title" style={{ marginTop: 16 }}>
            Output
          </div>
          {files.map((file) => (
            <div key={file} className="faint mono" style={{ fontSize: "0.74rem" }}>
              {file}
            </div>
          ))}
        </>
      )}
    </Panel>
  );
}

function Row({ label, value }: { label: string; value: ReactNode }): ReactNode {
  return (
    <tr>
      <td className="faint">{label}</td>
      <td style={{ textAlign: "right" }}>{value}</td>
    </tr>
  );
}

/**
 * The event log (design document section 86).
 *
 * Live events come from the socket; a finished run's come from the store. Both
 * are the same events, so the display does not care which it is showing.
 */
function EventLog({
  runId,
  live,
  active,
}: {
  runId: string;
  live: { kind: string; message: string; level: string; entity: string | null }[];
  active: boolean;
}): ReactNode {
  const [expanded, setExpanded] = useState(false);
  const stored = useQuery({
    queryKey: ["run-events", runId],
    queryFn: () => api.runEvents(runId, 0, 200),
    enabled: !active,
  });

  const lines = active
    ? live.map((event) => ({
        kind: event.kind,
        message: event.message,
        level: event.level,
        entity: event.entity,
      }))
    : ((stored.data ?? []) as StoredEvent[]).map((event) => ({
        kind: event.event,
        message: event.message,
        level: event.level,
        entity: event.entity,
      }));

  const shown = expanded ? lines : lines.slice(-12);

  return (
    <Panel
      title={`Events${active ? " · live" : ""}`}
      actions={
        lines.length > 12 ? (
          <button type="button" className="button-sm" onClick={() => setExpanded(!expanded)}>
            {expanded ? "Show recent" : `Show all ${lines.length}`}
          </button>
        ) : undefined
      }
    >
      {lines.length === 0 && <p className="faint">No events yet.</p>}
      {shown.map((line, index) => (
        <div className={`log-line level-${line.level}`} key={`${line.kind}-${index}`}>
          <span className="kind">{line.kind}</span>
          <span>{line.message}</span>
        </div>
      ))}
    </Panel>
  );
}
