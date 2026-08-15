/**
 * Run history (design document section 56).
 *
 * Every run Cacophony has recorded, newest first. A resumable run says so and
 * offers the button, because the whole point of checkpointing is that stopping
 * is recoverable rather than final.
 */

import { type ReactNode, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useRuns } from "../api/hooks";
import { PageHead } from "../components/Layout";
import {
  Empty,
  ErrorNotice,
  Panel,
  ProgressBar,
  Spinner,
  StateChip,
  formatDuration,
  formatNumber,
  formatWhen,
} from "../components/ui";
import { useStudio } from "../state/store";

const STATES = ["", "running", "paused", "completed", "failed", "cancelled"];

export function RunsPage(): ReactNode {
  const navigate = useNavigate();
  const projectId = useStudio((state) => state.projectId);
  const [state, setState] = useState("");
  const [allProjects, setAllProjects] = useState(false);

  const runs = useRuns({
    ...(allProjects || projectId === null ? {} : { project_id: projectId }),
    ...(state ? { state } : {}),
    limit: 100,
  });

  return (
    <>
      <PageHead
        title="Runs"
        subtitle="Every run, its checkpoints and what it produced."
        actions={
          <>
            <select
              value={state}
              onChange={(event) => setState(event.target.value)}
              aria-label="Filter by state"
              style={{ width: 150 }}
            >
              {STATES.map((option) => (
                <option key={option} value={option}>
                  {option || "all states"}
                </option>
              ))}
            </select>
            {projectId !== null && (
              <label className="checkbox" style={{ marginBottom: 0 }}>
                <input
                  type="checkbox"
                  checked={allProjects}
                  onChange={(event) => setAllProjects(event.target.checked)}
                />
                All projects
              </label>
            )}
          </>
        }
      />

      {runs.isLoading && <Spinner label="Loading runs" />}
      <ErrorNotice error={runs.error} />

      {runs.data && runs.data.length === 0 && (
        <Empty title="No runs yet">
          <p>Generate something, and it will appear here.</p>
        </Empty>
      )}

      {runs.data && runs.data.length > 0 && (
        <Panel>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>State</th>
                  <th style={{ width: 160 }}>Progress</th>
                  <th style={{ textAlign: "right" }}>Records</th>
                  <th style={{ textAlign: "right" }}>Duration</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.map((run) => (
                  <tr
                    key={run.id}
                    className="field-row-button"
                    onClick={() => navigate(`/runs/${run.id}`)}
                  >
                    <td className="mono">{run.id.slice(0, 8)}</td>
                    <td>
                      <StateChip state={run.state} />
                    </td>
                    <td>
                      <ProgressBar value={run.progress} state={run.state} />
                    </td>
                    <td className="nums" style={{ textAlign: "right" }}>
                      {formatNumber(run.records_written)}
                      <span className="faint"> / {formatNumber(run.records_requested)}</span>
                    </td>
                    <td className="nums faint" style={{ textAlign: "right" }}>
                      {formatDuration(run.duration_seconds)}
                    </td>
                    <td className="faint">{formatWhen(run.started_at ?? run.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </>
  );
}
