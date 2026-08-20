/**
 * The streaming screen (design document sections 35, 94).
 *
 *     authentication  250/s  ████████████████████░  99.2%     18,024
 *     alert         8/min    ████████████████████░  100%         180
 *     → syslog://siem:514                                     18,204
 *
 * What this page adds over `cacophony stream` is the one thing a terminal
 * cannot do: steer. A rate is a slider you move while the stream runs, and the
 * attainment bar beside it says whether the machine could actually deliver
 * what you just asked for.
 *
 * Attainment is the headline number, deliberately. A workload generator that
 * reports "18,204 delivered" while quietly running at sixty per cent of the
 * requested rate is measuring the wrong thing, so achieved-over-requested is
 * the largest figure on the screen and turns amber below 95%.
 *
 * The records table is a *window*, not a log. A stream has no end, so the
 * server keeps a bounded number of recent records and this shows those; the
 * rest were delivered and forgotten, which is what makes a six-hour stream
 * cost what a six-second one does.
 */

import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  useSchema,
  useStreamControls,
  useStreamFeed,
  useStreamRecords,
  useStreams,
} from "../api/hooks";
import type { CreateStreamBody, StreamView } from "../api/types";
import { PageHead } from "../components/Layout";
import {
  Empty,
  ErrorNotice,
  Notice,
  Panel,
  ProgressBar,
  Spinner,
  Stat,
  formatBytes,
  formatDuration,
  formatNumber,
  renderCell,
} from "../components/ui";
import { useStudio } from "../state/store";

/** Rates people actually ask for, offered so nobody has to learn the grammar. */
const RATE_PRESETS = ["1/s", "10/s", "50/s", "250/s", "1000/s", "10/minute", "1/hour"];

const RUNNING = new Set(["queued", "running", "paused"]);

export function StreamPage(): ReactNode {
  const projectId = useStudio((state) => state.projectId);
  const schema = useSchema(projectId);
  const streams = useStreams(projectId ?? undefined);
  const controls = useStreamControls(projectId);

  const [selected, setSelected] = useState<string | null>(null);

  // Follow whatever is running, so arriving at this page after starting a
  // stream elsewhere shows the stream rather than an empty form.
  useEffect(() => {
    if (selected || !streams.data) return;
    const live = streams.data.find((stream) => RUNNING.has(stream.state));
    if (live) setSelected(live.id);
  }, [streams.data, selected]);

  if (projectId === null) {
    return (
      <Empty title="No project selected">
        <p>
          Choose one on the <Link to="/projects">Projects</Link> page.
        </p>
      </Empty>
    );
  }

  if (schema.isLoading) return <Spinner label="Compiling the schema" />;
  if (schema.isError) return <ErrorNotice error={schema.error} />;
  if (!schema.data) return null;

  const entities = schema.data.entity_order;

  return (
    <>
      <PageHead
        title="Stream"
        subtitle={`${schema.data.name} · a rate per entity, and no end`}
      />

      {selected ? (
        <StreamDashboard
          streamId={selected}
          onLeave={() => setSelected(null)}
          controls={controls}
        />
      ) : (
        <StartForm
          entities={entities}
          controls={controls}
          onStarted={(stream) => setSelected(stream.id)}
        />
      )}

      <StreamList
        streams={streams.data ?? []}
        selected={selected}
        onSelect={setSelected}
        onForget={(id) => controls.forget.mutate(id)}
      />
    </>
  );
}

/* ------------------------------------------------------------------ */
/* Starting one                                                        */
/* ------------------------------------------------------------------ */

type Controls = ReturnType<typeof useStreamControls>;

function StartForm({
  entities,
  controls,
  onStarted,
}: {
  entities: string[];
  controls: Controls;
  onStarted: (stream: StreamView) => void;
}): ReactNode {
  // The last entity is almost always the event table - the one people
  // actually want to stream - so it is what the form offers first.
  const [rates, setRates] = useState<Record<string, string>>(() =>
    entities.length > 0 ? { [entities[entities.length - 1] as string]: "50/s" } : {},
  );
  const [destination, setDestination] = useState("");
  const [keepRecords, setKeepRecords] = useState(200);
  const [followShape, setFollowShape] = useState(false);
  const [historical, setHistorical] = useState(false);
  const [maxRecords, setMaxRecords] = useState("");

  const chosen = Object.keys(rates);

  const toggle = (entity: string) =>
    setRates((current) => {
      const next = { ...current };
      if (entity in next) delete next[entity];
      else next[entity] = "50/s";
      return next;
    });

  const submit = () => {
    const body: CreateStreamBody = {
      rates,
      destinations: destination.trim() ? [destination.trim()] : [],
      keep_records: keepRecords,
      follow_shape: followShape,
      live_time: !historical,
      max_records: maxRecords.trim() === "" ? null : Number(maxRecords),
    };
    controls.start.mutate(body, { onSuccess: onStarted });
  };

  return (
    <Panel title="Start a stream">
      <ErrorNotice error={controls.start.error} />

      <div className="field-row">
        <label>Entities and rates</label>
        <div className="hint">
          Written the way people say them: <code>250/s</code>,{" "}
          <code>8 per minute</code>, <code>1200/hour</code>.
        </div>
        {entities.map((entity) => (
          <div key={entity} className="row" style={{ gap: 10, marginTop: 6 }}>
            <label className="row" style={{ gap: 6, minWidth: 200 }}>
              <input
                type="checkbox"
                checked={entity in rates}
                onChange={() => toggle(entity)}
              />
              <span>{entity}</span>
            </label>
            <input
              style={{ width: 120 }}
              disabled={!(entity in rates)}
              value={rates[entity] ?? ""}
              onChange={(event) =>
                setRates((current) => ({ ...current, [entity]: event.target.value }))
              }
            />
            <div className="row" style={{ gap: 4 }}>
              {RATE_PRESETS.slice(0, 5).map((preset) => (
                <button
                  key={preset}
                  type="button"
                  className="chip"
                  disabled={!(entity in rates)}
                  onClick={() =>
                    setRates((current) => ({ ...current, [entity]: preset }))
                  }
                >
                  {preset}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className="row" style={{ gap: 10 }}>
        <div className="field-row" style={{ flex: 2 }}>
          <label htmlFor="destination">Destination</label>
          <input
            id="destination"
            placeholder="syslog://host:514, https://host/ingest, file:///tmp/events.jsonl"
            value={destination}
            onChange={(event) => setDestination(event.target.value)}
          />
          <div className="hint">
            Leave empty to generate without sending anywhere; the sample below
            still shows what came out.
          </div>
        </div>
        <div className="field-row" style={{ flex: 1 }}>
          <label htmlFor="keep">Sample window</label>
          <input
            id="keep"
            type="number"
            min={0}
            max={5000}
            value={keepRecords}
            onChange={(event) => setKeepRecords(Number(event.target.value))}
          />
          <div className="hint">Records kept for this page. 0 keeps none.</div>
        </div>
        <div className="field-row" style={{ flex: 1 }}>
          <label htmlFor="max-records">Stop after</label>
          <input
            id="max-records"
            type="number"
            min={1}
            placeholder="never"
            value={maxRecords}
            onChange={(event) => setMaxRecords(event.target.value)}
          />
        </div>
      </div>

      <div className="row" style={{ gap: 16 }}>
        <label className="row" style={{ gap: 6 }}>
          <input
            type="checkbox"
            checked={followShape}
            onChange={(event) => setFollowShape(event.target.checked)}
          />
          <span>Follow the timeline&rsquo;s shape (quiet at night)</span>
        </label>
        <label className="row" style={{ gap: 6 }}>
          <input
            type="checkbox"
            checked={historical}
            onChange={(event) => setHistorical(event.target.checked)}
          />
          <span>Keep generated timestamps instead of the wall clock</span>
        </label>
      </div>

      <div className="row spread" style={{ marginTop: 14 }}>
        <div className="faint">
          {chosen.length === 0
            ? "Choose at least one entity."
            : `${chosen.length} entit${chosen.length === 1 ? "y" : "ies"}, no end.`}
        </div>
        <button
          type="button"
          className="primary"
          disabled={chosen.length === 0 || controls.start.isPending}
          onClick={submit}
        >
          {controls.start.isPending ? "Starting" : "Start streaming"}
        </button>
      </div>
    </Panel>
  );
}

/* ------------------------------------------------------------------ */
/* Watching one                                                        */
/* ------------------------------------------------------------------ */

function StreamDashboard({
  streamId,
  onLeave,
  controls,
}: {
  streamId: string;
  onLeave: () => void;
  controls: Controls;
}): ReactNode {
  const feed = useStreamFeed(streamId);
  const [entityFilter, setEntityFilter] = useState<string | null>(null);
  const records = useStreamRecords(
    streamId,
    { limit: 50, entity: entityFilter },
    !feed.finished,
  );

  const view = feed.view;
  if (!view) {
    return (
      <Panel title="Stream">
        {feed.status === "unavailable" ? (
          <Notice tone="warn">
            This stream is not running in the server this page is talking to.
          </Notice>
        ) : (
          <Spinner label="Connecting to the stream" />
        )}
      </Panel>
    );
  }

  const running = RUNNING.has(view.state);
  const attainment = view.stats.attainment;

  return (
    <>
      <div className="grid grid-4">
        <Stat
          label="Attainment"
          value={`${(attainment * 100).toFixed(1)}%`}
          note={`${formatNumber(view.stats.records_per_second)}/s of ${formatNumber(
            view.stats.target_records_per_second,
          )}/s requested`}
          tone={attainment >= 0.95 ? "green" : "amber"}
        />
        <Stat
          label="Generated"
          value={formatNumber(view.stats.generated)}
          note={`${formatNumber(view.stats.delivered)} delivered`}
          tone="violet"
        />
        <Stat
          label="Elapsed"
          value={formatDuration(view.stats.elapsed_seconds)}
          note={view.config.duration_seconds ? `of ${view.config.duration_seconds}s` : "no end"}
        />
        <Stat
          label="State"
          value={view.state}
          note={view.error ?? (feed.status === "open" ? "live" : feed.status)}
          tone={view.state === "failed" ? "magenta" : running ? "cyan" : undefined}
        />
      </div>

      {attainment < 0.95 && running && view.stats.generated > 0 && (
        <Notice tone="warn">
          Running at {(attainment * 100).toFixed(0)}% of the requested rate.
          Either generation or a destination cannot keep up &mdash; lower the
          rate, or accept the rate the machine can actually deliver.
        </Notice>
      )}

      {view.error && <Notice tone="error">{view.error}</Notice>}

      <Panel
        title="Rates"
        actions={
          <>
            {running && view.state !== "paused" && (
              <button type="button" onClick={() => controls.pause.mutate(streamId)}>
                Pause
              </button>
            )}
            {view.state === "paused" && (
              <button type="button" onClick={() => controls.resume.mutate(streamId)}>
                Resume
              </button>
            )}
            {running && (
              <button type="button" onClick={() => controls.stop.mutate(streamId)}>
                Stop
              </button>
            )}
            <button type="button" onClick={onLeave}>
              Back
            </button>
          </>
        }
      >
        <ErrorNotice error={controls.retarget.error} />
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Entity</th>
                <th>Rate</th>
                <th style={{ width: "30%" }}>Share of target</th>
                <th className="numeric">Produced</th>
                <th className="numeric">Next index</th>
                <th>Retarget</th>
              </tr>
            </thead>
            <tbody>
              {view.entities.map((entity) => (
                <RateRow
                  key={entity.entity}
                  entity={entity}
                  target={view.stats.target_records_per_second}
                  disabled={!running}
                  onRetarget={(rate) =>
                    controls.retarget.mutate({ id: streamId, entity: entity.entity, rate })
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <div className="grid grid-2">
        <Panel title="Destinations">
          {view.sinks.length === 0 ? (
            <Empty title="Nowhere" />
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Sink</th>
                    <th className="numeric">Delivered</th>
                    <th className="numeric">Failed</th>
                    <th className="numeric">Sent</th>
                    <th>Last error</th>
                  </tr>
                </thead>
                <tbody>
                  {view.sinks.map((sink, index) => (
                    <tr key={`${sink.sink}-${index}`}>
                      <td>
                        {sink.sink}
                        {sink.sink === "memory" && (
                          <span className="faint"> (this page)</span>
                        )}
                      </td>
                      <td className="numeric">{formatNumber(sink.delivered)}</td>
                      <td className="numeric">
                        {sink.failed > 0 ? (
                          <span style={{ color: "var(--magenta)" }}>
                            {formatNumber(sink.failed)}
                          </span>
                        ) : (
                          "0"
                        )}
                      </td>
                      <td className="numeric">{formatBytes(sink.bytes_sent)}</td>
                      <td className="faint">{sink.last_error ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {view.destinations.length > 0 && (
            <div className="hint" style={{ marginTop: 8 }}>
              {view.destinations.join(", ")}
            </div>
          )}
        </Panel>

        <Panel title="Configuration">
          <dl className="detail-list">
            <dt>Batch size</dt>
            <dd>{formatNumber(view.config.batch_size)}</dd>
            <dt>Flush after</dt>
            <dd>{view.config.flush_seconds}s</dd>
            <dt>Timestamps</dt>
            <dd>{view.config.live_time ? "wall clock" : "generated"}</dd>
            <dt>Timeline shape</dt>
            <dd>{view.config.follow_shape ? "followed" : "ignored"}</dd>
            <dt>Scenario cycle</dt>
            <dd>{formatDuration(view.config.scenario_cycle_seconds)}</dd>
            <dt>Stop after</dt>
            <dd>{view.config.max_records ? formatNumber(view.config.max_records) : "never"}</dd>
            <dt>In flight ceiling</dt>
            <dd>{formatNumber(view.config.max_in_flight)} per tick</dd>
          </dl>
        </Panel>
      </div>

      <RecordWindow
        records={records.data}
        loading={records.isLoading}
        entities={view.entities.map((entity) => entity.entity)}
        filter={entityFilter}
        onFilter={setEntityFilter}
      />
    </>
  );
}

function RateRow({
  entity,
  target,
  disabled,
  onRetarget,
}: {
  entity: StreamView["entities"][number];
  target: number;
  disabled: boolean;
  onRetarget: (rate: string) => void;
}): ReactNode {
  const [draft, setDraft] = useState(entity.rate);

  // Follow the server when somebody else changes the rate, but never fight a
  // field that is being typed into.
  useEffect(() => setDraft(entity.rate), [entity.rate]);

  const share = target > 0 ? entity.per_second / target : 0;

  return (
    <tr>
      <td>{entity.entity}</td>
      <td className="mono">{entity.rate}</td>
      <td>
        <ProgressBar value={share} />
      </td>
      <td className="numeric">{formatNumber(entity.produced)}</td>
      <td className="numeric">{formatNumber(entity.index)}</td>
      <td>
        <div className="row" style={{ gap: 6 }}>
          <input
            style={{ width: 90 }}
            value={draft}
            disabled={disabled}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && draft !== entity.rate) onRetarget(draft);
            }}
          />
          <button
            type="button"
            disabled={disabled || draft === entity.rate}
            onClick={() => onRetarget(draft)}
          >
            Set
          </button>
        </div>
      </td>
    </tr>
  );
}

function RecordWindow({
  records,
  loading,
  entities,
  filter,
  onFilter,
}: {
  records: ReturnType<typeof useStreamRecords>["data"];
  loading: boolean;
  entities: string[];
  filter: string | null;
  onFilter: (entity: string | null) => void;
}): ReactNode {
  const columns = useMemo(() => {
    const rows = records?.records ?? [];
    const seen: string[] = [];
    for (const row of rows.slice(0, 10)) {
      for (const key of Object.keys(row.record)) {
        if (!key.startsWith("_") && !seen.includes(key)) seen.push(key);
      }
    }
    return seen.slice(0, 8);
  }, [records]);

  return (
    <Panel
      title="Going past"
      actions={
        entities.length > 1 && (
          <select
            value={filter ?? ""}
            onChange={(event) => onFilter(event.target.value || null)}
          >
            <option value="">every entity</option>
            {entities.map((entity) => (
              <option key={entity} value={entity}>
                {entity}
              </option>
            ))}
          </select>
        )
      }
    >
      {loading && !records ? (
        <Spinner label="Waiting for records" />
      ) : !records?.sampled ? (
        <Empty title="Not sampled">
          <p>
            This stream keeps no window, so there is nothing to show here. It is
            still delivering to its destinations.
          </p>
        </Empty>
      ) : records.records.length === 0 ? (
        <Empty title="Nothing yet">
          <p>The stream has not delivered a batch since this page opened.</p>
        </Empty>
      ) : (
        <>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Entity</th>
                  {columns.map((column) => (
                    <th key={column}>{column}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {records.records.map((row) => (
                  <tr key={row.seq}>
                    <td className="faint numeric">{row.seq}</td>
                    <td>{row.entity}</td>
                    {columns.map((column) => (
                      <td key={column} className="mono">
                        {renderCell(row.record[column])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="hint" style={{ marginTop: 8 }}>
            The last {records.keep} records, newest first. A stream has no end,
            so everything older has been delivered and forgotten.
          </div>
        </>
      )}
    </Panel>
  );
}

/* ------------------------------------------------------------------ */
/* Everything running                                                  */
/* ------------------------------------------------------------------ */

function StreamList({
  streams,
  selected,
  onSelect,
  onForget,
}: {
  streams: StreamView[];
  selected: string | null;
  onSelect: (id: string) => void;
  onForget: (id: string) => void;
}): ReactNode {
  if (streams.length === 0) return null;

  return (
    <Panel title={`Streams (${streams.length})`}>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Stream</th>
              <th>State</th>
              <th>Rates</th>
              <th className="numeric">Generated</th>
              <th className="numeric">Attainment</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {streams.map((stream) => (
              <tr key={stream.id} className={stream.id === selected ? "selected" : undefined}>
                <td className="mono">{stream.id.slice(0, 8)}</td>
                <td>{stream.state}</td>
                <td className="faint">
                  {Object.entries(stream.config.rates)
                    .map(([entity, rate]) => `${entity} ${rate}`)
                    .join(", ")}
                </td>
                <td className="numeric">{formatNumber(stream.stats.generated)}</td>
                <td className="numeric">{(stream.stats.attainment * 100).toFixed(0)}%</td>
                <td>
                  <div className="row" style={{ gap: 6 }}>
                    <button type="button" onClick={() => onSelect(stream.id)}>
                      Watch
                    </button>
                    {!RUNNING.has(stream.state) && (
                      <button type="button" onClick={() => onForget(stream.id)}>
                        Forget
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
