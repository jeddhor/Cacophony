/**
 * Settings (design document section 46).
 *
 * Cacophony's settings live in the project schema and on the command line, not
 * in browser storage, so this page reports the environment the Studio is
 * talking to rather than pretending to own configuration it does not.
 */

import type { ReactNode } from "react";

import { useSchemaTypes, useSystem } from "../api/hooks";
import { PageHead } from "../components/Layout";
import { ErrorNotice, Panel, Spinner, formatNumber } from "../components/ui";
import { useStudio } from "../state/store";

export function SettingsPage(): ReactNode {
  const system = useSystem();
  const types = useSchemaTypes();
  const resetGenerate = useStudio((state) => state.resetGenerate);
  const selectProject = useStudio((state) => state.selectProject);

  return (
    <>
      <PageHead title="Settings" subtitle="What this Studio is connected to." />

      {system.isLoading && <Spinner />}
      <ErrorNotice error={system.error} />

      <div className="grid grid-2">
        {system.data && (
          <Panel title="Backend">
            <table>
              <tbody>
                <tr>
                  <td className="faint">Version</td>
                  <td style={{ textAlign: "right" }}>{system.data.version}</td>
                </tr>
                <tr>
                  <td className="faint">Store</td>
                  <td style={{ textAlign: "right" }} className="mono truncate">
                    {system.data.store.path}
                  </td>
                </tr>
                <tr>
                  <td className="faint">Store schema</td>
                  <td style={{ textAlign: "right" }}>v{system.data.store.schema_version}</td>
                </tr>
                <tr>
                  <td className="faint">Projects</td>
                  <td style={{ textAlign: "right" }}>{formatNumber(system.data.projects)}</td>
                </tr>
                <tr>
                  <td className="faint">Schema revisions</td>
                  <td style={{ textAlign: "right" }}>{formatNumber(system.data.revisions)}</td>
                </tr>
                <tr>
                  <td className="faint">Runs</td>
                  <td style={{ textAlign: "right" }}>{formatNumber(system.data.runs)}</td>
                </tr>
                <tr>
                  <td className="faint">Events</td>
                  <td style={{ textAlign: "right" }}>{formatNumber(system.data.events)}</td>
                </tr>
                <tr>
                  <td className="faint">Active runs</td>
                  <td style={{ textAlign: "right" }}>{system.data.active_runs.length}</td>
                </tr>
              </tbody>
            </table>
          </Panel>
        )}

        <Panel title="This browser">
          <p className="faint" style={{ marginTop: 0, fontSize: "0.8rem" }}>
            The Studio remembers only which project you were working on.
            Generation options are deliberately not remembered: a record count
            carried over from last week is a way to overwrite a dataset by
            accident.
          </p>
          <div className="row">
            <button type="button" className="button-sm" onClick={() => resetGenerate()}>
              Reset generate form
            </button>
            <button type="button" className="button-sm" onClick={() => selectProject(null)}>
              Clear selected project
            </button>
          </div>
        </Panel>
      </div>

      {types.data && (
        <>
          <div style={{ height: 16 }} />
          <Panel title={`Generators (${types.data.generators.length})`}>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Needs</th>
                    <th>Deterministic</th>
                    <th>Summary</th>
                  </tr>
                </thead>
                <tbody>
                  {types.data.generators.map((generator) => (
                    <tr key={generator.name}>
                      <td className="mono">{generator.name}</td>
                      <td className="faint">{generator.requires_provider ?? "—"}</td>
                      <td className="faint">{generator.deterministic ? "yes" : "no"}</td>
                      <td className="faint">{generator.summary}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        </>
      )}
    </>
  );
}
